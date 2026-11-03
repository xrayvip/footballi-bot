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

# YouTube Full Access Scope (Upload Videos & Manage Playlists)
SCOPES = ['https://www.googleapis.com/auth/youtube']
HISTORY_FILE = 'uploaded_matches.json'
DEFAULT_PRIVACY = 'public'

# Foreign clubs, players, and international leagues that trigger YouTube Content ID blocks
BLOCKED_KEYWORDS = [
    'لیگ نخبگان', 'لیگ قهرمانان آسیا', 'لیگ قهرمانان اروپا', 'چمپیونزلیگ',
    'لیگ برتر انگلیس', 'لالیگا', 'سری آ', 'بوندسلیگا', 'لیگ ۱ فرانسه',
    'جام حذفی اسپانیا', 'جام حذفی انگلیس', 'جام اتحادیه',
    'رئال مادرید', 'بارسلونا', 'بایرن مونیخ', 'پاری سن ژرمن', 'منچسترسیتی',
    'منچستریونایتد', 'یونایتد', 'لیورپول', 'آرسنال', 'چلسی', 'تاتنهام', 'اتلتیکو مادرید',
    'میلان', 'اینتر', 'یوونتوس', 'رم', 'ناپولی', 'دورتموند', 'لورکوزن',
    'الهلال', 'النصر', 'الاتحاد', 'الاهلی', 'شباب الاهلی', 'الغرافه', 'السد', 'العین',
    'فاینورد', 'ویارئال', 'استون ویلا', 'شاختار', 'اسپورتینگ', 'نیوکاسل', 'اورتون',
    'برونو', 'رافینیا', 'امباپه', 'هالند', 'صلاح', 'وینسیوس', 'بلینگهام'
]

PLAYLIST_CACHE = {}

def get_youtube_service():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
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

def categorize_video(title):
    if 'خلاصه بازی' in title:
        return 'خلاصه بازی‌ها'
    elif any(w in title for w in ['مصاحبه', 'صحبت های', 'صحبت‌های', 'نشست خبری', 'کنفرانس', 'گفتگو', 'خطیر', 'میثاقی']):
        return 'مصاحبه‌ها و نشست‌های خبری'
    elif any(w in title for w in ['ویدیو کست', 'کارشناسی', 'تحلیل', 'برنامه', 'گلایه', 'بررسی']):
        return 'برنامه‌ها و ویدیوکست‌ها'
    elif any(w in title for w in ['تمرینات', 'حواشی', 'چالش', 'هواداران', 'جالب', 'اتفاقات', 'درگیری']):
        return 'حواشی و تمرینات تیم‌ها'
    elif 'گل' in title:
        return 'گل‌های فوتبال ایران'
    else:
        return 'ویدیوهای اختصاصی فوتبالی'

def is_safe_content(title):
    """Checks strictly the video title to avoid site-wide footer links."""
    title_lower = title.lower()
    for blocked in BLOCKED_KEYWORDS:
        if blocked in title_lower:
            return False, blocked
    return True, None

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
    return playlist_id

def add_to_playlist(youtube, video_id, playlist_id):
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
    except Exception as e:
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
            category = categorize_video(title)

            if not any(v['id'] == video_id for v in videos):
                videos.append({
                    'id': video_id,
                    'title': title,
                    'page_url': full_url,
                    'category': category
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

        print(f"\nEvaluating: {v['title']}")
        mp4_url, thumb_url, clean_page_title = extract_video_details(v['page_url'])
        final_title = clean_page_title if clean_page_title else v['title']

        # Strict Title-Only Broadcast Rights Filter
        is_safe, blocked_keyword = is_safe_content(final_title)
        if not is_safe:
            print(f"SKIP: Contains '{blocked_keyword}' (International broadcast rights).")
            history.append(v['id'])
            save_history(history)
            continue

        if not mp4_url:
            print(f"Could not locate video stream for {final_title}, skipping.")
            continue

        print(f"SAFE: Approved for playlist '{v['category']}'.")
        video_filename = f"temp_{v['id']}.mp4"
        thumb_filename = f"thumb_{v['id']}.jpg"

        print("Downloading video...")
        try:
            with requests.get(mp4_url, stream=True, headers={"User-Agent": "Mozilla/5.0"}, timeout=30) as r:
                with open(video_filename, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024*1024):
                        f.write(chunk)
        except Exception as e:
            print(f"Download error on {final_title} ({e}), skipping.")
            continue

        if thumb_url:
            try:
                with requests.get(thumb_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15) as r:
                    with open(thumb_filename, 'wb') as f:
                        f.write(r.content)
            except Exception:
                pass

        description = (
            f"{final_title}\n\n"
            f"برای مشاهده نتایج زنده، اخبار و ویدیوهای بیشتر به وبسایت فوتبالی مراجعه کنید:\n"
            f"https://footballi.net/\n\n"
            f"دانلود اپلیکیشن فوتبالی:\n"
            f"https://footballi.net/download\n\n"
            f"#فوتبالی #فوتبال_ایران #{v['category'].replace(' ', '_')}"
        )

        video_id = upload_to_youtube(
            youtube, 
            video_filename, 
            final_title, 
            description, 
            thumb_filename if os.path.exists(thumb_filename) else None
        )

        playlist_id = get_or_create_playlist(youtube, v['category'])
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