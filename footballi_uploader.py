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

FALLBACK_BLOCKED = [
    'لیگ نخبگان', 'لیگ قهرمانان آسیا', 'لیگ قهرمانان اروپا', 'چمپیونزلیگ',
    'لیگ برتر انگلیس', 'لالیگا', 'سری آ', 'بوندسلیگا', 'لیگ ۱ فرانسه',
    'رئال مادرید', 'بارسلونا', 'بایرن مونیخ', 'پاری سن ژرمن', 'منچسترسیتی',
    'منچستریونایتد', 'یونایتد', 'لیورپول', 'آرسنال', 'چلسی', 'تاتنهام', 'اتلتیکو مادرید',
    'میلان', 'اینتر', 'یوونتوس', 'رم', 'ناپولی', 'پورتو', 'دورتموند', 'لورکوزن',
    'الهلال', 'النصر', 'الاتحاد', 'الاهلی', 'شباب الاهلی', 'الغرافه', 'السد', 'العین',
    'فاینورد', 'ویارئال', 'استون ویلا', 'شاختار', 'اسپورتینگ', 'نیوکاسل', 'اورتون',
    'گلیمت', 'لیل', 'آاک آتن', 'کونیا', 'لایمر', 'اسپانیا'
]

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
    global ACTIVE_GEMINI_MODEL
    if ACTIVE_GEMINI_MODEL:
        return ACTIVE_GEMINI_MODEL

    if not GEMINI_API_KEY:
        return None

    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            models = res.json().get('models', [])
            for preferred in ['models/gemini-2.0-flash', 'models/gemini-2.5-flash', 'models/gemini-flash-latest']:
                for m in models:
                    if m.get('name') == preferred and 'generateContent' in m.get('supportedGenerationMethods', []):
                        ACTIVE_GEMINI_MODEL = m['name']
                        print(f"Connected to AI model: {ACTIVE_GEMINI_MODEL}")
                        return ACTIVE_GEMINI_MODEL
            for m in models:
                if 'flash' in m.get('name', '') and 'generateContent' in m.get('supportedGenerationMethods', []):
                    ACTIVE_GEMINI_MODEL = m['name']
                    print(f"Connected to AI model: {ACTIVE_GEMINI_MODEL}")
                    return ACTIVE_GEMINI_MODEL
    except Exception as e:
        print(f"Could not list Gemini models ({e})")

    ACTIVE_GEMINI_MODEL = "models/gemini-2.0-flash"
    return ACTIVE_GEMINI_MODEL

def fallback_evaluate(title):
    t_lower = title.lower()
    for b in FALLBACK_BLOCKED:
        if b in t_lower:
            return False, f"Contains blocked foreign keyword '{b}'", "ویدیوهای اختصاصی فوتبالی", title, False

    is_short = True
    if 'خلاصه بازی' in title:
        cat = 'خلاصه بازی‌ها'
        is_short = False
    elif any(w in title for w in ['مصاحبه', 'صحبت', 'نشست', 'کنفرانس', 'گفتگو']):
        cat = 'مصاحبه‌ها و نشست‌های خبری'
    elif any(w in title for w in ['ویدیو کست', 'کارشناسی', 'تحلیل', 'برنامه', 'چطور', 'بررسی']):
        cat = 'برنامه‌ها و ویدیوکست‌ها'
        is_short = False
    elif any(w in title for w in ['تمرینات', 'حواشی', 'چالش', 'هواداران', 'درگیری']):
        cat = 'حواشی و تمرینات تیم‌ها'
    elif 'گل' in title:
        cat = 'گل‌های فوتبال ایران'
    else:
        cat = 'ویدیوهای اختصاصی فوتبالی'

    return True, "Approved by local domestic rules", cat, title, is_short

def ai_evaluate_video(title, page_url):
    """Uses Google Gemini AI to evaluate copyright safety, category, and Shorts classification."""
    model_name = get_active_gemini_model()
    if not model_name:
        return fallback_evaluate(title)

    prompt = f"""
You are a YouTube Content ID compliance officer for Footballi (فوتبالی), an Iranian sports network.
Analyze this video:
Title: "{title}"
URL: "{page_url}"

STRICT RULES:
1. REJECT (is_safe = false):
   - Any European club match, goal, training, or animation (e.g., Real Madrid, Barcelona, Man United, Roma, Napoli, Porto, Aston Villa, Bayern, Atletico, etc.) or UEFA Champions League / foreign league content.
   - Any AFC Champions League Elite / foreign Asian club match.

2. APPROVE (is_safe = true):
   - Domestic Iranian football/futsal (لیگ برتر ایران, لیگ آزادگان, جام حذفی, تیم ملی ایران, فوتسال ایران, تراکتور, پرسپولیس, استقلال, سپاهان, گل گهر).
   - Tactical analysis of Iranian teams, player interviews, press conferences, fan reactions, training clips.

3. SHORTS CLASSIFICATION:
   - If the video is a short clip, goal, reaction, interview snippet, or training moment under 3 minutes, set "is_short": true.
   - If it is a full match summary ("خلاصه بازی") or full studio podcast ("ویدیوکست"), set "is_short": false.

Return ONLY valid JSON:
{{
  "is_safe": true or false,
  "reason": "Short explanation in English",
  "category": "خلاصه بازی‌ها" or "مصاحبه‌ها و نشست‌های خبری" or "برنامه‌ها و ویدیوکست‌ها" or "حواشی و تمرینات تیم‌ها" or "گل‌های فوتبال ایران" or "ویدیوهای اختصاصی فوتبالی",
  "clean_title": "Clean Persian title",
  "is_short": true or false
}}
"""
    api_url = f"https://generativelanguage.googleapis.com/v1beta/{model_name}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
    }

    try:
        res = requests.post(api_url, json=payload, timeout=12)
        if res.status_code == 200:
            result = res.json()
            raw_text = result['candidates'][0]['content']['parts'][0]['text']
            data = json.loads(raw_text)
            return (
                data.get('is_safe', False),
                data.get('reason', ''),
                data.get('category', 'ویدیوهای اختصاصی فوتبالی'),
                data.get('clean_title', title),
                data.get('is_short', True)
            )
        else:
            return fallback_evaluate(title)
    except Exception:
        return fallback_evaluate(title)

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
                print(f"Note: Could not add to playlist ({e})")

def scrape_website_videos():
    url = "https://footballi.net/video"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code != 200:
            return []
    except Exception as e:
        print(f"Could not load footballi.net/video ({e})")
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
    """Scrapes public vertical clips and news videos from https://t.me/s/FootballiApp."""
    url = "https://t.me/s/FootballiApp"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code != 200:
            return []
    except Exception as e:
        print(f"Could not load t.me/s/FootballiApp ({e})")
        return []

    soup = BeautifulSoup(res.text, 'html.parser')
    shorts = []

    for msg in soup.find_all('div', class_='tgme_widget_message'):
        post_id = msg.get('data-post')
        if not post_id:
            continue

        # Check if message contains a video element
        video_tag = msg.find('video')
        if not video_tag or not video_tag.get('src'):
            continue

        mp4_url = video_tag['src']
        text_el = msg.find('div', class_='tgme_widget_message_text')
        caption = text_el.text.strip() if text_el else "ویدیو کوتاه فوتبالی"
        
        # Take the first line of caption as title
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
                'source': 'telegram'
            })
    return shorts

def extract_website_video_details(video_page_url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        res = requests.get(video_page_url, headers=headers, timeout=15)
        html = res.text
    except Exception as e:
        print(f"Timeout on {video_page_url} ({e})")
        return None, None, None

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

    return mp4_url, thumbnail_url, page_title

def upload_to_youtube(youtube, file_path, title, description, thumbnail_path=None):
    body = {
        'snippet': {
            'title': title[:100],
            'description': description,
            'tags': ['فوتبالی', 'فوتبال ایران', 'Shorts', 'یوتیوب شورت', 'استقلال', 'پرسپولیس'],
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
            print(f"Note: Could not set thumbnail ({e})")

    return video_id

def process_pipeline():
    youtube = get_youtube_service()
    history = load_history()

    # Collect both website videos and Telegram short clips
    website_videos = scrape_website_videos()
    telegram_shorts = scrape_telegram_shorts()

    all_items = website_videos + telegram_shorts
    print(f"Found {len(website_videos)} website videos and {len(telegram_shorts)} Telegram short clips")

    for item in all_items:
        if item['id'] in history:
            continue

        print(f"\nConsulting AI on [{item['source'].upper()}]: {item['title']}")
        is_safe, reason, category, clean_title_text, is_short = ai_evaluate_video(item['title'], item['page_url'])

        if not is_safe:
            print(f"REJECTED: {reason}")
            history.append(item['id'])
            save_history(history)
            continue

        # Format title with #Shorts if classified as a short video
        if is_short:
            if '#Shorts' not in clean_title_text and '#shorts' not in clean_title_text:
                if len(clean_title_text) < 90:
                    clean_title_text = f"{clean_title_text} #Shorts"

        print(f"APPROVED: {reason}")
        print(f"Category: '{category}' | Short: {is_short} | Title: '{clean_title_text}'")

        # Resolve media download URL
        thumb_url = None
        if item['source'] == 'telegram':
            mp4_url = item.get('direct_mp4')
        else:
            mp4_url, thumb_url, _ = extract_website_video_details(item['page_url'])

        if not mp4_url:
            print(f"Could not locate video stream, skipping.")
            continue

        video_filename = f"temp_{item['raw_id']}.mp4"
        thumb_filename = f"thumb_{item['raw_id']}.jpg"

        print(f"Downloading {item['source']} video...")
        try:
            with requests.get(mp4_url, stream=True, headers={"User-Agent": "Mozilla/5.0"}, timeout=30) as r:
                with open(video_filename, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024*1024):
                        f.write(chunk)
        except Exception as e:
            print(f"Download error ({e}), skipping.")
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

            # 1. Add to designated category playlist
            playlist_id = get_or_create_playlist(youtube, category)
            add_to_playlist(youtube, video_id, playlist_id)

            # 2. If it's a short, also add to the dedicated Shorts playlist
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
                print("Stopping run cleanly. History is saved, and new uploads will resume on the next run.")
                break
            else:
                print(f"Upload error ({e}), skipping.")
        finally:
            if os.path.exists(video_filename):
                os.remove(video_filename)
            if os.path.exists(thumb_filename):
                os.remove(thumb_filename)

if __name__ == '__main__':
    process_pipeline()
