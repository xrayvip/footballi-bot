import os
import json
import time
import re
import subprocess
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
import requests
from bs4 import BeautifulSoup
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ['https://www.googleapis.com/auth/youtube']
HISTORY_FILE = 'uploaded_matches.json'
DEFAULT_PRIVACY = 'public'
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '').strip()
APIFY_TOKEN = os.environ.get('APIFY_TOKEN', '').strip()

# Duration rules
MIN_LONG_VIDEO_SECONDS = 300
MIN_SUMMARY_VIDEO_SECONDS = 60
MAX_SHORT_VIDEO_SECONDS = 180

# Daily Quota & Slot Reservation Settings
DAILY_CHANNEL_LIMIT = 10
RESERVED_SUMMARY_SLOTS = 3
MAX_NON_SUMMARY_PER_24H = DAILY_CHANNEL_LIMIT - RESERVED_SUMMARY_SLOTS  # 7 non-summaries max; 3 slots reserved for Match Summaries!
MAX_REELS_PER_RUN = 3

# Apify Cost Saver: Only call Apify once every 8 hours (stays 100% inside the free $5 credit)
APIFY_COOLDOWN_HOURS = 8

PLAYLIST_CACHE = {}
WORKING_MODELS = []

def get_youtube_service():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if os.environ.get('CI'):
                raise RuntimeError("Invalid or expired token in cloud runner. Please refresh token.json.")
            flow = InstalledAppFlow.from_client_secrets_file('client_secrets.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return build('youtube', 'v3', credentials=creds)

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
    return []

def save_history(history):
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def should_run_apify(history):
    now_ts = int(time.time())
    last_apify_ts = 0
    for entry in history:
        if isinstance(entry, str) and entry.startswith("apify_ts_"):
            try:
                ts = int(entry.replace("apify_ts_", ""))
                if ts > last_apify_ts:
                    last_apify_ts = ts
            except Exception:
                pass

    elapsed_hours = (now_ts - last_apify_ts) / 3600.0
    if elapsed_hours < APIFY_COOLDOWN_HOURS:
        remaining = round(APIFY_COOLDOWN_HOURS - elapsed_hours, 1)
        print(f"[APIFY COST SAVER] Last Instagram check was {round(elapsed_hours, 1)}h ago. Next check in {remaining}h (keeps monthly cost under $1.80).")
        return False
    return True

def record_apify_run(history):
    history[:] = [x for x in history if not (isinstance(x, str) and x.startswith("apify_ts_"))]
    history.append(f"apify_ts_{int(time.time())}")
    save_history(history)

def normalize_persian(text):
    if not text:
        return ""
    text = text.replace('ی', 'ی').replace('ي', 'ی').replace('ک', 'ک').replace('ك', 'ک')
    text = text.replace('\u200c', ' ')
    text = re.sub(r'#\S+', '', text)
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip().lower()
    return text

def is_duplicate_title(candidate_title, existing_titles):
    norm_cand = normalize_persian(candidate_title)
    if not norm_cand or len(norm_cand) < 8:
        return False

    for existing in existing_titles:
        norm_ex = normalize_persian(existing)
        if not norm_ex:
            continue
        if norm_cand == norm_ex:
            return True
        similarity = SequenceMatcher(None, norm_cand, norm_ex).ratio()
        if similarity >= 0.88:
            return True
    return False

def get_channel_status_and_24h_count(youtube):
    titles = []
    uploads_last_24h = 0
    now_utc = datetime.now(timezone.utc)
    cutoff_24h = now_utc - timedelta(hours=24)

    try:
        ch_res = youtube.channels().list(part='contentDetails', mine=True).execute()
        items = ch_res.get('items', [])
        if not items:
            return titles, 0
        uploads_playlist_id = items[0]['contentDetails']['relatedPlaylists']['uploads']

        next_page = None
        for _ in range(2):
            pl_res = youtube.playlistItems().list(
                part='snippet',
                playlistId=uploads_playlist_id,
                maxResults=50,
                pageToken=next_page
            ).execute()
            for item in pl_res.get('items', []):
                snippet = item['snippet']
                t = snippet['title']
                titles.append(t)

                pub_str = snippet.get('publishedAt')
                if pub_str:
                    try:
                        pub_dt = datetime.fromisoformat(pub_str.replace('Z', '+00:00'))
                        if pub_dt >= cutoff_24h:
                            uploads_last_24h += 1
                    except Exception:
                        pass

            next_page = pl_res.get('nextPageToken')
            if not next_page:
                break

        print(f"Channel Status: {len(titles)} recent videos | {uploads_last_24h}/{DAILY_CHANNEL_LIMIT} uploaded in last 24h (3 slots reserved for Match Summaries).")
    except Exception as e:
        print("Note: Could not fetch existing YouTube videos list:", str(e))
    return titles, uploads_last_24h

def extract_duration_seconds(raw_text):
    match = re.search(r'(\d+):(\d+)', raw_text)
    if match:
        minutes = int(match.group(1))
        seconds = int(match.group(2))
        return minutes * 60 + seconds
    return None

def inspect_video_file(file_path):
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,duration:format=duration',
            '-of', 'json',
            file_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        data = json.loads(res.stdout)
        streams = data.get('streams', [])
        width = int(streams[0].get('width', 0)) if streams else 0
        height = int(streams[0].get('height', 0)) if streams else 0
        
        duration = 0.0
        if streams and 'duration' in streams[0]:
            duration = float(streams[0]['duration'])
        elif 'format' in data and 'duration' in data['format']:
            duration = float(data['format']['duration'])

        return width, height, duration
    except Exception as e:
        print("Note: Could not run ffprobe inspection:", str(e))
        return 0, 0, 0.0

def clean_title(raw_text):
    cleaned = re.sub(r'play_arrow\d*:\d*', '', raw_text)
    cleaned = re.sub(r'visibility\s*[\d,]*', '', cleaned)
    cleaned = re.sub(r'comment\s*[\d,]*', '', cleaned)
    return cleaned.strip()

def discover_working_models():
    global WORKING_MODELS
    if WORKING_MODELS:
        return WORKING_MODELS

    candidates = [
        "models/gemini-3.5-flash-lite",
        "models/gemini-3.6-flash",
        "models/gemini-3.8-flash",
        "models/gemini-3.5-flash"
    ]

    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            for m in res.json().get('models', []):
                name = m.get('name', '')
                methods = m.get('supportedGenerationMethods', [])
                if 'generateContent' in methods and 'gemini' in name:
                    if not any(old in name for old in ['1.5', '2.0', '2.5', 'vision', 'image', 'tts', 'embedding']):
                        if name not in candidates:
                            candidates.append(name)
    except Exception:
        pass

    WORKING_MODELS = candidates
    return WORKING_MODELS

def ai_batch_evaluate(items_to_audit, existing_yt_titles):
    if not GEMINI_API_KEY or not items_to_audit:
        return {}

    models = discover_working_models()
    recent_channel_titles = existing_yt_titles[:25]
    all_results_map = {}

    chunk_size = 15
    for i in range(0, len(items_to_audit), chunk_size):
        chunk = items_to_audit[i:i + chunk_size]
        items_payload = []
        for it in chunk:
            items_payload.append({
                "id": it['id'],
                "source": it['source'],
                "title": it['eval_title'],
                "context": it['context_text'][:250]
            })

        prompt = f"""
You are the Chief YouTube Copyright & Content Auditor for Footballi (فوتبالی).
Audit this batch of {len(items_payload)} candidate videos from our Website and Instagram Reels.

RECENTLY UPLOADED VIDEOS ON OUR CHANNEL:
{json.dumps(recent_channel_titles, ensure_ascii=False)}

CANDIDATE VIDEOS TO AUDIT:
{json.dumps(items_payload, ensure_ascii=False, indent=2)}

STRICT AUDIT RULES:

1. ALWAYS APPROVE IRANIAN DOMESTIC FOOTBALL & ORIGINAL REELS (is_safe = true):
   - ALWAYS APPROVE match summaries ("خلاصه بازی"), goals, interviews, press conferences, referee analysis, sidelines, and vertical Instagram Reels covering Iranian football:
     * Persian Gulf Pro League (لیگ برتر ایران / خلیج فارس) - e.g., پرسپولیس، استقلال، سپاهان، تراکتور، گل‌گهر، ملوان، فولاد، ذوب‌آهن، شمس‌آذر، آلومینیوم، چادرملو، خیبر، مس رفسنجان، نساجی و غیره.
     * Azadegan League (لیگ آزادگان) & Hazfi Cup (جام حذفی ایران).
     * Iranian Women's Football/Futsal & Iran National Team interviews/analysis/sidelines.
     * Studio shows, podcasts ("ویدیوکست", "شب‌های فوتبالی"), referee reviews ("کارشناسی داوری"), and Footballi original social edits/interviews.

2. REJECT ONLY FOREIGN / AFC BROADCASTS (is_safe = false):
   - Raw European club match broadcasts/goals (UEFA Champions League, Premier League, La Liga, Serie A, Bundesliga, Ligue 1).
   - Raw AFC Champions League Elite / ACL Two match broadcasts.
   - Exact duplicate of a video already in RECENTLY UPLOADED VIDEOS.

Output ONLY a raw JSON array of objects, one for each input "id":
[
  {{
    "id": "exact input id",
    "is_safe": true or false,
    "reason": "Short reason in English",
    "category": "خلاصه بازی‌ها" or "مصاحبه‌ها و نشست‌های خبری" or "برنامه‌ها و ویدیوکست‌ها" or "حواشی و تمرینات تیم‌ها" or "گل‌های فوتبال ایران" or "ویدیوهای اختصاصی فوتبالی",
    "clean_title": "Clean, engaging Persian title (max 75 chars, no emojis or hashtags)"
  }}
]
"""

        chunk_success = False
        for model_name in models:
            if chunk_success:
                break
            api_url = f"https://generativelanguage.googleapis.com/v1beta/{model_name}:generateContent?key={GEMINI_API_KEY}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"}
            }

            for attempt in range(2):
                try:
                    print(f"Sending batch of {len(items_payload)} videos to AI ({model_name})...")
                    res = requests.post(api_url, json=payload, timeout=25)
                    if res.status_code == 200:
                        raw_text = res.json()['candidates'][0]['content']['parts'][0]['text']
                        results_list = json.loads(raw_text)
                        for r in results_list:
                            if isinstance(r, dict) and 'id' in r:
                                all_results_map[r['id']] = r
                        print(f"Successfully audited {len(results_list)} videos!")
                        chunk_success = True
                        break
                    elif res.status_code in [429, 503]:
                        print(f"Model {model_name} busy (HTTP {res.status_code}), retrying in 4s...")
                        time.sleep(4)
                    else:
                        break
                except Exception as e:
                    print(f"Batch call exception on {model_name}: {e}")
                    time.sleep(2)

        if not chunk_success:
            print("AI models busy on this chunk. Will retry remaining items on next run.")
            break

    return all_results_map if all_results_map else None

def get_or_create_playlist(youtube, playlist_title):
    if playlist_title in PLAYLIST_CACHE:
        return PLAYLIST_CACHE[playlist_title]

    request = youtube.playlists().list(part='snippet', mine=True, maxResults=50)
    response = request.execute()

    for item in response.get('items', []):
        if item['snippet']['title'].strip() == playlist_title.strip():
            playlist_id = item['id']
            PLAYLIST_CACHE[playlist_title] = playlist_id
            return playlist_id

    create_request = youtube.playlists().insert(
        part='snippet,status',
        body={
            'snippet': {
                'title': playlist_title,
                'description': f'ویدیوهای رسمی {playlist_title} از رسانه فوتبالی'
            },
            'status': {'privacyStatus': 'public'}
        }
    )
    res = create_request.execute()
    playlist_id = res['id']
    PLAYLIST_CACHE[playlist_title] = playlist_id
    print(f"Created new playlist: '{playlist_title}'")
    time.sleep(2)
    return playlist_id

def add_to_playlist(youtube, video_id, playlist_id):
    for attempt in range(2):
        try:
            youtube.playlistItems().insert(
                part='snippet',
                body={
                    'snippet': {
                        'playlistId': playlist_id,
                        'resourceId': {
                            'kind': 'youtube#video',
                            'videoId': video_id
                        }
                    }
                }
            ).execute()
            print("Added to playlist successfully.")
            return
        except Exception as e:
            if attempt == 0:
                time.sleep(3)
            else:
                print("Note: Could not add to playlist:", str(e))

def scrape_website_videos():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    videos = []

    urls_to_check = [
        "https://footballi.net/video",
        "https://footballi.net/",
        "https://footballi.net/competition/14",  # لیگ برتر خلیج فارس
        "https://footballi.net/competition/1",
        "https://footballi.net/competition/90",  # لیگ آزادگان
        "https://footballi.net/competition/91"   # جام حذفی ایران
    ]

    for url in urls_to_check:
        try:
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code != 200:
                continue
        except Exception as e:
            print(f"Could not load {url}: {e}")
            continue

        soup = BeautifulSoup(res.text, 'html.parser')

        for a in soup.find_all('a', href=True):
            href = a['href']
            if '/video/' in href:
                parts = [p for p in href.split('/video/')[1].split('/') if p]
                if not parts or not parts[0].isdigit():
                    continue
                
                video_id = parts[0]
                raw_text = a.text
                duration_hint = extract_duration_seconds(raw_text)
                title = clean_title(raw_text)
                if len(title) < 5:
                    continue

                full_url = href if href.startswith('http') else 'https://footballi.net' + href

                if not any(v['id'] == f"web_{video_id}" for v in videos):
                    videos.append({
                        'id': f"web_{video_id}",
                        'raw_id': video_id,
                        'title': title,
                        'duration_hint': duration_hint,
                        'page_url': full_url,
                        'source': 'website'
                    })
    return videos

def scrape_instagram_reels(history):
    if not APIFY_TOKEN:
        print("Note: APIFY_TOKEN not set, skipping Instagram Reels.")
        return []

    if not should_run_apify(history):
        return []

    print("Fetching latest vertical Reels from Instagram (@footballiapp)...")
    api_url = f"https://api.apify.com/v2/acts/apify~instagram-reel-scraper/run-sync-get-dataset-items?token={APIFY_TOKEN}"
    payload = {
        "username": ["footballiapp"],
        "resultsLimit": MAX_REELS_PER_RUN
    }

    reels = []
    try:
        res = requests.post(api_url, json=payload, timeout=90)
        if res.status_code in [200, 201]:
            record_apify_run(history)
            items = res.json()
            for item in items:
                video_url = item.get('videoUrl')
                if not video_url:
                    continue

                short_code = item.get('shortCode') or item.get('id') or str(int(time.time()))
                caption = item.get('caption') or "ویدیو کوتاه اختصاصی فوتبالی"
                first_line = caption.split('\n')[0][:85].strip()
                title = first_line if len(first_line) > 5 else "ریلز اختصاصی فوتبالی"
                duration = float(item.get('videoDuration') or 45.0)

                if not any(r['id'] == f"ig_{short_code}" for r in reels):
                    reels.append({
                        'id': f"ig_{short_code}",
                        'raw_id': short_code,
                        'title': title,
                        'duration_hint': duration,
                        'page_url': item.get('url') or f"https://www.instagram.com/reel/{short_code}/",
                        'direct_mp4': video_url,
                        'caption': caption,
                        'source': 'instagram'
                    })
            print(f"Found {len(reels)} vertical Reels on Instagram (@footballiapp).")
        else:
            print(f"Apify Instagram API returned status {res.status_code}: {res.text[:120]}")
    except Exception as e:
        print("Instagram scraper notice:", str(e))

    return reels

def extract_website_video_details(video_page_url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        res = requests.get(video_page_url, headers=headers, timeout=15)
        html = res.text
    except Exception as e:
        print(f"Timeout on {video_page_url}: {e}")
        return None, None, None, ""

    soup = BeautifulSoup(html, 'html.parser')

    mp4_url = None
    mp4_matches = re.findall(r'https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*', html)
    if mp4_matches:
        for match in mp4_matches:
            if 'cdnboks' in match or 'oddrun' in match or 'footballi' in match:
                mp4_url = match
                break
        if not mp4_url:
            mp4_url = mp4_matches[0]

    img_tag = soup.find('meta', property='og:image')
    thumbnail_url = img_tag['content'] if img_tag else None

    og_title = soup.find('meta', property='og:title')
    page_title = og_title['content'] if og_title else None

    tags = []
    for tag in soup.find_all('a', href=re.compile(r'/tag/|/competition/')):
        t_text = tag.text.strip()
        if t_text and t_text not in tags:
            tags.append(t_text)
    tags_text = " - ".join(tags)

    return mp4_url, thumbnail_url, page_title, tags_text

def upload_to_youtube(youtube, file_path, title, description, thumbnail_path=None):
    body = {
        'snippet': {
            'title': title[:100],
            'description': description,
            'tags': ['فوتبالی', 'فوتبال ایران', 'Shorts', 'خلاصه بازی', 'لیگ برتر', 'استقلال', 'پرسپولیس', 'سپاهان', 'تراکتور'],
            'categoryId': '17'
        },
        'status': {
            'privacyStatus': DEFAULT_PRIVACY,
            'selfDeclaredMadeForKids': False
        }
    }

    media = MediaFileUpload(file_path, chunksize=1024*1024*4, resumable=True, mimetype='video/mp4')
    request = youtube.videos().insert(part=','.join(body.keys()), body=body, media_body=media)
    
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Uploading: {int(status.progress() * 100)}%")

    video_id = response['id']
    print(f"Uploaded successfully! Video ID: {video_id}")

    if thumbnail_path and os.path.exists(thumbnail_path):
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path, mimetype='image/jpeg')
            ).execute()
            print("Thumbnail set successfully.")
        except Exception as e:
            print("Note: Could not set thumbnail:", str(e))

    return video_id

def process_pipeline():
    youtube = get_youtube_service()
    history = load_history()
    existing_yt_titles, uploads_last_24h = get_channel_status_and_24h_count(youtube)

    website_videos = scrape_website_videos()

    if uploads_last_24h < MAX_NON_SUMMARY_PER_24H:
        instagram_reels = scrape_instagram_reels(history)
    else:
        print(f"[RESERVE GUARD] {uploads_last_24h} videos uploaded in last 24h. Skipping Instagram Reels to keep {RESERVED_SUMMARY_SLOTS} slots reserved strictly for Match Summaries!")
        instagram_reels = []

    def priority_sort_key(item):
        if 'خلاصه بازی' in item['title']:
            return 0  # VIP Priority 1: Match Summaries
        if item['source'] == 'website':
            return 1  # Priority 2: Long-form Website Shows
        return 2      # Priority 3: Instagram Reels

    all_items = website_videos + instagram_reels
    all_items.sort(key=priority_sort_key)

    candidates = []
    for it in all_items:
        if it['id'] in history or it['raw_id'] in history:
            continue
        candidates.append(it)

    print(f"Found {len(all_items)} total items ({len(candidates)} new candidates)")
    if not candidates:
        return

    new_items = []
    for item in candidates:
        is_summary_hint = ('خلاصه بازی' in item['title'])

        if not is_summary_hint and uploads_last_24h >= MAX_NON_SUMMARY_PER_24H:
            continue

        if item['source'] == 'instagram':
            item['mp4_url'] = item.get('direct_mp4')
            item['thumb_url'] = None
            item['context_text'] = item.get('caption', '')
            item['eval_title'] = item['title']
        else:
            if not is_summary_hint and item.get('duration_hint') and item['duration_hint'] < MIN_LONG_VIDEO_SECONDS:
                print(f"[FAST SKIP] Website horizontal clip under 5 mins ({item['duration_hint']}s): {item['title']}")
                history.append(item['id'])
                save_history(history)
                continue

            mp4_url, thumb_url, clean_page_title, context_text = extract_website_video_details(item['page_url'])
            item['mp4_url'] = mp4_url
            item['thumb_url'] = thumb_url
            item['context_text'] = context_text
            item['eval_title'] = clean_page_title if clean_page_title else item['title']

        if is_duplicate_title(item['eval_title'], existing_yt_titles):
            print(f"[SKIP DUPLICATE] Already on YouTube channel: {item['eval_title']}")
            history.append(item['id'])
            save_history(history)
            continue

        new_items.append(item)

    if not new_items:
        print("No new eligible videos to audit right now.")
        return

    audit_results = ai_batch_evaluate(new_items, existing_yt_titles)
    if audit_results is None:
        return

    for item in new_items:
        decision = audit_results.get(item['id'])
        if not decision:
            continue

        is_safe = decision.get('is_safe', False)
        reason = decision.get('reason', 'No reason provided')
        category = decision.get('category', 'ویدیوهای اختصاصی فوتبالی')
        clean_title_text = decision.get('clean_title', item['eval_title'])

        print(f"\n[AUDIT] [{item['source'].upper()}] {item['eval_title']}")
        if not is_safe:
            print(f"-> REJECTED BY AI: {reason}")
            history.append(item['id'])
            save_history(history)
            continue

        if is_duplicate_title(clean_title_text, existing_yt_titles):
            print(f"-> SKIP DUPLICATE: '{clean_title_text}' already uploaded in this session.")
            history.append(item['id'])
            save_history(history)
            continue

        is_match_summary = ('خلاصه بازی' in item['eval_title'] or category == 'خلاصه بازی‌ها')

        if not is_match_summary and uploads_last_24h >= MAX_NON_SUMMARY_PER_24H:
            print(f"-> [RESERVE GUARD] Holding back '{clean_title_text}' because {uploads_last_24h}/{DAILY_CHANNEL_LIMIT} daily slots are used. Last {RESERVED_SUMMARY_SLOTS} slots are strictly reserved for Match Summaries!")
            continue

        mp4_url = item.get('mp4_url')
        thumb_url = item.get('thumb_url')

        if not mp4_url:
            print("Could not locate video stream, skipping.")
            continue

        video_filename = f"temp_{item['raw_id']}.mp4"
        thumb_filename = f"thumb_{item['raw_id']}.jpg"

        print(f"Downloading {item['source']} stream to verify dimensions & duration...")
        try:
            with requests.get(mp4_url, stream=True, headers={"User-Agent": "Mozilla/5.0"}, timeout=45) as r:
                with open(video_filename, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024*1024):
                        f.write(chunk)
        except Exception as e:
            print("Download error:", str(e), "- skipping.")
            if os.path.exists(video_filename):
                os.remove(video_filename)
            continue

        width, height, duration = inspect_video_file(video_filename)
        if duration == 0 and item.get('duration_hint'):
            duration = float(item['duration_hint'])

        is_vertical_or_square = (height >= width and width > 0) or (item['source'] == 'instagram')
        is_youtube_short = is_vertical_or_square and (0 < duration <= MAX_SHORT_VIDEO_SECONDS)

        print(f"Video Specs: {width}x{height} | Duration: {int(duration)}s | Short: {is_youtube_short} | Match Summary: {is_match_summary}")

        if not is_youtube_short:
            min_required = MIN_SUMMARY_VIDEO_SECONDS if is_match_summary else MIN_LONG_VIDEO_SECONDS
            if duration < min_required:
                print(f"-> SKIP DURATION: Horizontal video is too short ({int(duration)}s < {min_required}s).")
                if os.path.exists(video_filename):
                    os.remove(video_filename)
                history.append(item['id'])
                save_history(history)
                continue

        if is_youtube_short and '#Shorts' not in clean_title_text and '#shorts' not in clean_title_text:
            if len(clean_title_text) < 90:
                clean_title_text = f"{clean_title_text} #Shorts"

        target_playlist_name = 'ویدیوهای کوتاه (Shorts)' if is_youtube_short else category
        print(f"-> APPROVED FOR UPLOAD: Playlist '{target_playlist_name}' | Short: {is_youtube_short} | Title: '{clean_title_text}'")

        if thumb_url and not is_youtube_short:
            try:
                with requests.get(thumb_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15) as r:
                    with open(thumb_filename, 'wb') as f:
                        f.write(r.content)
            except Exception:
                pass

        extra_caption = f"\n{item.get('caption', '')}\n" if item['source'] == 'instagram' else ""
        description = (
            f"{clean_title_text}\n"
            f"{extra_caption}\n"
            f"برای مشاهده نتایج زنده، اخبار و ویدیوهای بیشتر به وبسایت فوتبالی مراجعه کنید:\n"
            f"https://footballi.net/\n\n"
            f"دانلود اپلیکیشن فوتبالی:\n"
            f"https://footballi.net/download\n\n"
            f"#فوتبالی #فوتبال_ایران {'#Shorts ' if is_youtube_short else ''}#{category.replace(' ', '_')}"
        )

        try:
            video_id = upload_to_youtube(
                youtube, 
                video_filename, 
                clean_title_text, 
                description, 
                thumb_filename if (not is_youtube_short and os.path.exists(thumb_filename)) else None
            )

            # Strictly separate playlists: Shorts ONLY go to 'ویدیوهای کوتاه (Shorts)', Long videos ONLY go to their topic playlist!
            playlist_id = get_or_create_playlist(youtube, target_playlist_name)
            add_to_playlist(youtube, video_id, playlist_id)

            history.append(item['id'])
            history.append(item['raw_id'])
            save_history(history)
            existing_yt_titles.append(clean_title_text)
            existing_yt_titles.append(item['eval_title'])
            uploads_last_24h += 1

            print(f"Done. Daily Counter: {uploads_last_24h}/{DAILY_CHANNEL_LIMIT}. Pausing 15 seconds...")
            time.sleep(15)

        except Exception as e:
            err_msg = str(e)
            if 'uploadLimitExceeded' in err_msg or 'quotaExceeded' in err_msg:
                print("\n[LIMIT HIT] YouTube 24-hour upload quota reached.")
                print("Stopping run cleanly. History is saved, and new uploads will resume on next run.")
                break
            else:
                print("Upload error:", str(e), "- skipping.")
        finally:
            if os.path.exists(video_filename):
                os.remove(video_filename)
            if os.path.exists(thumb_filename):
                os.remove(thumb_filename)

if __name__ == '__main__':
    process_pipeline()
