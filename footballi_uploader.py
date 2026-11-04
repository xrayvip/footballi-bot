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
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')

PLAYLIST_CACHE = {}

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

def ai_evaluate_video(title, page_url):
    """Uses Google Gemini AI to evaluate copyright safety, playlist category, and title."""
    if not GEMINI_API_KEY:
        print("Warning: GEMINI_API_KEY not set. Falling back to default.")
        return False, "No AI key provided", "ویدیوهای اختصاصی فوتبالی", title

    prompt = f"""
You are an expert YouTube Copyright & Content ID compliance officer for Footballi (فوتبالی), Iran's premier sports media network.
Analyze this video item from footballi.net:
Title: "{title}"
URL: "{page_url}"

Evaluate whether uploading this video to YouTube will trigger automated YouTube Content ID blocks from satellite broadcasters.

STRICT COPYRIGHT RULES:
1. REJECT (is_safe = false):
   - Match summaries, goals, or broadcast footage from European competitions (UEFA Champions League, Europa League, Conference League).
   - Match footage from foreign leagues (Premier League, La Liga, Serie A, Bundesliga, Ligue 1, etc.).
   - Match footage from Asian international tournaments (AFC Champions League Elite, AFC Champions League 2).
   - Any foreign club match broadcast footage (e.g., Bodø/Glimt, Lille, Al Hilal, Sporting, Shakhtar, etc.).

2. APPROVE (is_safe = true):
   - Domestic Iranian Football: Persian Gulf Pro League (لیگ برتر ایران), League 1 (لیگ آزادگان), Hazfi Cup (جام حذفی), Women's Football League (لیگ فوتبال بانوان).
   - Original non-match media: Player/coach interviews (مصاحبه، صحبت ها), press conferences (نشست خبری), studio analysis / shows / video casts (ویدیوکست، برنامه، کارشناسی داوری), training sessions (تمرینات), and fan reactions/events (حواشی، درگیری).

Output ONLY a raw JSON object with no markdown formatting:
{{
  "is_safe": true or false,
  "reason": "Short explanation in English",
  "category": "خلاصه بازی‌ها" or "مصاحبه‌ها و نشست‌های خبری" or "برنامه‌ها و ویدیوکست‌ها" or "حواشی و تمرینات تیم‌ها" or "گل‌های فوتبال ایران" or "ویدیوهای اختصاصی فوتبالی",
  "clean_title": "Clean, engaging Persian title for YouTube"
}}
"""
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
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
            return data.get('is_safe', False), data.get('reason', ''), data.get('category', 'ویدیوهای اختصاصی فوتبالی'), data.get('clean_title', title)
        else:
            print(f"Gemini API returned status {res.status_code}")
    except Exception as e:
        print(f"AI evaluation error ({e})")

    return False, "AI check failed", "ویدیوهای اختصاصی فوتبالی", title

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

def scrape_all_videos():
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

            if not any(v['id'] == video_id for v in videos):
                videos.append({
                    'id': video_id,
                    'title': title,
                    'page_url': full_url
                })
    return videos

def extract_video_details(video_page_url):
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
            'tags': ['فوتبالی', 'فوتبال ایران', 'استقلال', 'پرسپولیس', 'لیگ برتر'],
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
    videos = scrape_all_videos()

    print(f"Found {len(videos)} videos on footballi.net/video")

    for v in videos:
        if v['id'] in history:
            continue

        print(f"\nConsulting AI on: {v['title']}")
        is_safe, reason, category, clean_title_text = ai_evaluate_video(v['title'], v['page_url'])

        if not is_safe:
            print(f"AI REJECTED: {reason}")
            history.append(v['id'])
            save_history(history)
            continue

        print(f"AI APPROVED: {reason}")
        print(f"Category: '{category}' | Clean Title: '{clean_title_text}'")

        mp4_url, thumb_url, _ = extract_video_details(v['page_url'])
        if not mp4_url:
            print(f"Could not locate video stream, skipping.")
            continue

        video_filename = f"temp_{v['id']}.mp4"
        thumb_filename = f"thumb_{v['id']}.jpg"

        print("Downloading video...")
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
            f"#فوتبالی #فوتبال_ایران #{category.replace(' ', '_')}"
        )

        video_id = upload_to_youtube(
            youtube, 
            video_filename, 
            clean_title_text, 
            description, 
            thumb_filename if os.path.exists(thumb_filename) else None
        )

        playlist_id = get_or_create_playlist(youtube, category)
        add_to_playlist(youtube, video_id, playlist_id)

        if os.path.exists(video_filename):
            os.remove(video_filename)
        if os.path.exists(thumb_filename):
            os.remove(thumb_filename)

        history.append(v['id'])
        save_history(history)
        print("Done. Pausing 15 seconds before next video...")
        time.sleep(15)

if __name__ == '__main__':
    process_pipeline()
