import os
import json
import time
import re
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

PLAYLIST_CACHE = {}
ACTIVE_GEMINI_MODEL = None

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
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_history(history):
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def clean_title(raw_text):
    cleaned = re.sub(r'play_arrow\d*:\d*', '', raw_text)
    cleaned = re.sub(r'visibility\s*[\d,]*', '', cleaned)
    cleaned = re.sub(r'comment\s*[\d,]*', '', cleaned)
    return cleaned.strip()

def get_active_gemini_model():
    """
    Queries the API for all available models and live-tests them from newest to oldest
    until it finds the active working model for this project.
    """
    global ACTIVE_GEMINI_MODEL
    if ACTIVE_GEMINI_MODEL:
        return ACTIVE_GEMINI_MODEL

    if not GEMINI_API_KEY:
        return None

    candidate_models = [
        "models/gemini-3.8-flash",
        "models/gemini-3.6-flash",
        "models/gemini-3.5-flash-lite",
        "models/gemini-3.5-flash",
        "models/gemini-3.0-flash"
    ]

    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            models = res.json().get('models', [])
            api_candidates = []
            for m in models:
                name = m.get('name', '')
                methods = m.get('supportedGenerationMethods', [])
                if 'generateContent' in methods and 'gemini' in name:
                    # Skip deprecated 1.5, 2.0, 2.5 and specialized image/tts models
                    if not any(old in name for old in ['1.5', '2.0', '2.5', 'vision', 'image', 'tts', 'embedding']):
                        api_candidates.append(name)
            # Sort newest versions first
            api_candidates.sort(reverse=True)
            for c in api_candidates:
                if c not in candidate_models:
                    candidate_models.append(c)
    except Exception as e:
        print(f"Could not fetch model list: {e}")

    # Probe candidates to find the first model that returns HTTP 200
    for model in candidate_models:
        test_url = f"https://generativelanguage.googleapis.com/v1beta/{model}:generateContent?key={GEMINI_API_KEY}"
        test_payload = {"contents": [{"parts": [{"text": "ping"}]}]}
        try:
            r = requests.post(test_url, json=test_payload, timeout=8)
            if r.status_code == 200:
                ACTIVE_GEMINI_MODEL = model
                print(f"Verified working AI model: {ACTIVE_GEMINI_MODEL}")
                return ACTIVE_GEMINI_MODEL
        except Exception:
            continue

    return None

def ai_evaluate_video(title, page_url, page_tags_text=""):
    """
    Evaluates copyright compliance strictly using YouTube Content ID rules.
    Operates on a strict ZERO-TRUST / WHITELIST-ONLY basis.
    """
    model_name = get_active_gemini_model()
    if not model_name or not GEMINI_API_KEY:
        print("AI service unavailable -> Rejecting by default for channel safety.")
        return False, "No working AI model found", "ویدیوهای اختصاصی فوتبالی", title, False

    prompt = f"""
You are the Chief YouTube Copyright Compliance Auditor for Footballi (فوتبالی).
Your primary mission: PROTECT THE YOUTUBE CHANNEL FROM COPYRIGHT STRIKES AND GLOBAL CONTENT ID BLOCKS.

Video Details to Audit:
- Title: "{title}"
- Page URL: "{page_url}"
- Context/Tags from webpage: "{page_tags_text}"

YOUTUBE CONTENT ID COMPLIANCE POLICIES:

1. ABSOLUTE REJECTION (is_safe = false) - MUST BLOCK:
   - ANY match broadcast footage, goals, highlights, or 3D animations from European competitions or leagues (UEFA Champions League, Europa League, Premier League, La Liga, Serie A, Bundesliga, Ligue 1, etc.).
   - ANY match broadcast footage from AFC international competitions (AFC Champions League Elite, AFC Champions League 2, Asian Cup) - EVEN IF AN IRANIAN CLUB (Esteghlal, Persepolis, Tractor, Sepahan, Gol Gohar) IS PLAYING! The AFC owns international broadcast rights and blocks them globally.
   - ANY foreign club matches, foreign goals, foreign training sessions, or foreign league clips (Real Madrid, Man City, Arsenal, Leipzig, Brugge, PSV, Dortmund, Betis, Como, Lens, etc.).
   - Any video using foreign satellite commentary audio (beIN, Sky, DAZN, etc.).

2. STRICT APPROVAL (is_safe = true) - ONLY ALLOW:
   - Domestic Iranian Football: Persian Gulf Pro League (لیگ برتر خلیج فارس / لیگ برتر ایران), Hazfi Cup (جام حذفی ایران), League 1 (لیگ آزادگان), Iranian Women's Football/Futsal League (لیگ فوتبال و فوتسال بانوان).
   - Original Non-Broadcast Media: In-person player/coach interviews (مصاحبه اختصاصی، صحبت‌های علی سامره و غیره), press room conferences (نشست خبری), studio shows and tactical talks (برنامه ویدیوکست، تحلیل، کارشناسی داوری), field-side training of Iranian teams (تمرینات تیم‌های ایرانی), fan reactions and atmosphere (حواشی و هواداران).

If there is ANY doubt or foreign tournament involvement, you MUST default to REJECT (is_safe = false).

Format Decision:
- If duration is short (clip, goal, reaction, interview under 3 mins), set "is_short": true.
- If it's a full domestic match summary or full studio video cast, set "is_short": false.

Output ONLY a raw, valid JSON object (no markdown formatting):
{{
  "is_safe": true or false,
  "reason": "Detailed reason in English",
  "category": "خلاصه بازی‌ها" or "مصاحبه‌ها و نشست‌های خبری" or "برنامه‌ها و ویدیوکست‌ها" or "حواشی و تمرینات تیم‌ها" or "گل‌های فوتبال ایران" or "ویدیوهای اختصاصی فوتبالی",
  "clean_title": "Professional Persian title without noise",
  "is_short": true or false
}}
"""
    api_url = f"https://generativelanguage.googleapis.com/v1beta/{model_name}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"}
    }

    try:
        res = requests.post(api_url, json=payload, timeout=12)
        if res.status_code == 200:
            result = res.json()
            raw_text = result['candidates'][0]['content']['parts'][0]['text']
            data = json.loads(raw_text)
            return (
                data.get('is_safe', False),
                data.get('reason', 'Rejected by AI'),
                data.get('category', 'ویدیوهای اختصاصی فوتبالی'),
                data.get('clean_title', title),
                data.get('is_short', True)
            )
        else:
            print(f"Gemini API returned status {res.status_code} ({res.text[:100]}) -> Defaulting to REJECT")
    except Exception as e:
        print(f"AI evaluation exception: {e} -> Defaulting to REJECT")

    return False, "AI evaluation failed; rejected for safety", "ویدیوهای اختصاصی فوتبالی", title, False

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
    url = "https://footballi.net/video"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code != 200:
            return []
    except Exception as e:
        print(f"Could not load footballi.net/video: {e}")
        return []

    soup = BeautifulSoup(res.text, 'html.parser')
    videos = []

    for a in soup.find_all('a', href=True):
        href = a['href']
        if '/video/' in href:
            parts = [p for p in href.split('/video/')[1].split('/') if p]
            if not parts or not parts[0].isdigit():
                continue
            
            video_id = parts[0]
            title = clean_title(a.text)
            if len(title) < 5:
                continue

            full_url = href if href.startswith('http') else 'https://footballi.net' + href

            if not any(v['id'] == f"web_{video_id}" for v in videos):
                videos.append({
                    'id': f"web_{video_id}",
                    'raw_id': video_id,
                    'title': title,
                    'page_url': full_url,
                    'source': 'website'
                })
    return videos

def scrape_telegram_shorts():
    url = "https://t.me/s/FootballiApp"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code != 200:
            return []
    except Exception as e:
        print(f"Could not load t.me/s/FootballiApp: {e}")
        return []

    soup = BeautifulSoup(res.text, 'html.parser')
    shorts = []

    for msg in soup.find_all('div', class_='tgme_widget_message'):
        post_id = msg.get('data-post')
        if not post_id:
            continue

        video_tag = msg.find('video')
        if not video_tag or not video_tag.get('src'):
            continue

        mp4_url = video_tag['src']
        text_el = msg.find('div', class_='tgme_widget_message_text')
        caption = text_el.text.strip() if text_el else "ویدیو کوتاه فوتبالی"
        
        first_line = caption.split('\n')[0][:90].strip()
        clean_name = clean_title(first_line)

        clean_post_id = post_id.replace('/', '_')
        if not any(s['id'] == f"tg_{clean_post_id}" for s in shorts):
            shorts.append({
                'id': f"tg_{clean_post_id}",
                'raw_id': clean_post_id,
                'title': clean_name if len(clean_name) > 5 else "ویدیو کوتاه اختصاصی فوتبالی",
                'page_url': f"https://t.me/{post_id}",
                'direct_mp4': mp4_url,
                'caption': caption,
                'source': 'telegram'
            })
    return shorts

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
            'tags': ['فوتبالی', 'فوتبال ایران', 'Shorts', 'لیگ برتر', 'استقلال', 'پرسپولیس'],
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

    website_videos = scrape_website_videos()
    telegram_shorts = scrape_telegram_shorts()

    all_items = website_videos + telegram_shorts
    print(f"Found {len(website_videos)} website videos and {len(telegram_shorts)} Telegram short clips")

    for item in all_items:
        if item['id'] in history:
            continue

        thumb_url = None
        context_text = ""
        if item['source'] == 'telegram':
            mp4_url = item.get('direct_mp4')
            context_text = item.get('caption', '')
            clean_page_title = item['title']
        else:
            mp4_url, thumb_url, clean_page_title, context_text = extract_website_video_details(item['page_url'])

        eval_title = clean_page_title if clean_page_title else item['title']

        print(f"\n[AUDIT] Consulting AI on [{item['source'].upper()}]: {eval_title}")
        is_safe, reason, category, clean_title_text, is_short = ai_evaluate_video(eval_title, item['page_url'], context_text)

        if not is_safe:
            print(f"-> REJECTED BY AI: {reason}")
            history.append(item['id'])
            save_history(history)
            continue

        print(f"-> APPROVED BY AI: {reason}")
        if is_short and '#Shorts' not in clean_title_text and '#shorts' not in clean_title_text:
            if len(clean_title_text) < 90:
                clean_title_text = f"{clean_title_text} #Shorts"

        print(f"Destination: Playlist '{category}' | Short: {is_short} | Title: '{clean_title_text}'")

        if not mp4_url:
            print("Could not locate video stream, skipping.")
            continue

        video_filename = f"temp_{item['raw_id']}.mp4"
        thumb_filename = f"thumb_{item['raw_id']}.jpg"

        print(f"Downloading {item['source']} stream...")
        try:
            with requests.get(mp4_url, stream=True, headers={"User-Agent": "Mozilla/5.0"}, timeout=30) as r:
                with open(video_filename, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024*1024):
                        f.write(chunk)
        except Exception as e:
            print("Download error:", str(e), "- skipping.")
            continue

        if thumb_url:
            try:
                with requests.get(thumb_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15) as r:
                    with open(thumb_filename, 'wb') as f:
                        f.write(r.content)
            except Exception:
                pass

        description = (
            f"{clean_title_text}\n\n"
            f"برای مشاهده نتایج زنده، اخبار و ویدیوهای بیشتر به وبسایت فوتبالی مراجعه کنید:\n"
            f"https://footballi.net/\n\n"
            f"دانلود اپلیکیشن فوتبالی:\n"
            f"https://footballi.net/download\n\n"
            f"#فوتبالی #فوتبال_ایران #Shorts #{category.replace(' ', '_')}"
        )

        try:
            video_id = upload_to_youtube(
                youtube, 
                video_filename, 
                clean_title_text, 
                description, 
                thumb_filename if os.path.exists(thumb_filename) else None
            )

            playlist_id = get_or_create_playlist(youtube, category)
            add_to_playlist(youtube, video_id, playlist_id)

            if is_short:
                shorts_playlist_id = get_or_create_playlist(youtube, 'ویدیوهای کوتاه (Shorts)')
                add_to_playlist(youtube, video_id, shorts_playlist_id)

            history.append(item['id'])
            save_history(history)
            print("Done. Pausing 15 seconds before next video...")
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
