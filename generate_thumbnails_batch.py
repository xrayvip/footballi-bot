# generate_thumbnails_batch.py
# 100% Free - Zero Cost Edition (No API billing)
import os
import re
import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from PIL import Image, ImageDraw, ImageFont

TOKEN_FILE = "token.json"


def extract_video_id(input_str):
    if not input_str:
        return ""
    s = input_str.strip()
    m = re.search(r"(?:v=|\/|be\/|embed\/)([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)
    if len(s) == 11:
        return s
    return s.split("?")[0].split("&")[0]


def get_youtube_service():
    yt_scope = "https://www.googleapis.com/auth/youtube"
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, [yt_scope])
    elif os.environ.get("YOUTUBE_TOKEN"):
        import json
        token_data = json.loads(os.environ["YOUTUBE_TOKEN"])
        creds = Credentials.from_authorized_user_info(token_data, [yt_scope])

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError("YouTube Token invalid!")
    return build("youtube", "v3", credentials=creds)


def add_official_footballi_watermark(image_path):
    try:
        img = Image.open(image_path).convert("RGBA")
        img = img.resize((1280, 720), Image.Resampling.LANCZOS)
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        x1, y1, x2, y2 = 980, 35, 1245, 95
        draw.rounded_rectangle([(x1, y1), (x2, y2)], radius=15, fill=(6, 26, 18, 235), outline=(0, 195, 110, 255), width=3)
        draw.ellipse([(x1 + 18, y1 + 20), (x1 + 38, y1 + 40)], fill=(0, 215, 120, 255))

        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26)
        except Exception:
            font = ImageFont.load_default()

        draw.text((x1 + 52, y1 + 17), "FOOTBALLI", font=font, fill=(255, 255, 255, 255))

        final_img = Image.alpha_composite(img, overlay).convert("RGB")
        final_img.save(image_path, "JPEG", quality=95)
        print("[WATERMARK] Official Footballi brand watermark embedded cleanly.")
    except Exception as e:
        print(f"[WARN] Watermark note: {e}")


def find_original_footballi_poster(title):
    """Finds high-resolution official match poster directly from Footballi servers (Cost: $0.00)"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36"}
    search_urls = [
        "https://footballi.net/news/video",
        "https://footballi.net/video",
        "https://footballi.net/competition/14"
    ]
    keywords = [w for w in title.replace("خلاصه", "").replace("بازی", "").split() if len(w) > 2]

    for surl in search_urls:
        try:
            r = requests.get(surl, headers=headers, timeout=12)
            if r.status_code == 200:
                links = re.findall(r'href=["\'](/video/[^"\']+)["\']', r.text)
                for l in links[:20]:
                    page_u = f"https://footballi.net{l}"
                    rp = requests.get(page_u, headers=headers, timeout=10)
                    if rp.status_code == 200:
                        p_html = rp.text
                        if any(k in p_html for k in keywords[:2]):
                            # Look for high-res cdn.oddrun.ir / vod.cdnboks.com posters
                            posters = re.findall(r'<meta[^>]+(?:og:image|twitter:image)[^>]+content=["\']([^"\']+)["\']', p_html)
                            if not posters:
                                posters = re.findall(r'poster=["\']([^"\']+)["\']', p_html)
                            if posters:
                                return posters[0]
        except Exception:
            pass
    return None


def main():
    raw_target = os.environ.get("TARGET_VIDEO", "").strip()
    vid_id = extract_video_id(raw_target)
    if not vid_id:
        print(f"❌ Invalid Video URL or ID: '{raw_target}'")
        return

    youtube = get_youtube_service()

    print(f"🔍 Fetching details for Video ID: {vid_id} ...")
    resp = youtube.videos().list(part="snippet", id=vid_id).execute()
    items = resp.get("items", [])
    if not items:
        print(f"❌ Video not found on your channel with ID: {vid_id}")
        return

    sn = items[0]["snippet"]
    title = sn.get("title", "")
    print(f"🎯 Target Title: {title}")

    # 1. Fetch Official High-Res Poster ($0 Cost)
    print("🖼️ Fetching official original match poster from Footballi CDN...")
    poster_url = find_original_footballi_poster(title)

    tmp_img = f"thumb_{vid_id}.jpg"
    success = False

    if poster_url:
        print(f"✅ Found official poster: {poster_url}")
        r_img = requests.get(poster_url, timeout=25)
        if r_img.status_code == 200 and len(r_img.content) > 5_000:
            with open(tmp_img, "wb") as f:
                f.write(r_img.content)
            add_official_footballi_watermark(tmp_img)
            success = True

    if not success:
        # Fallback to direct download if poster url wasn't matched
        print("⚠️ Direct poster match not found, keeping original clean thumbnail.")
        return

    try:
        youtube.thumbnails().set(videoId=vid_id, media_body=MediaFileUpload(tmp_img)).execute()
        print(f"🎉 Successfully updated official HD thumbnail for: https://youtu.be/{vid_id}")
    except Exception as e:
        print(f"❌ YouTube API error: {e}")
    finally:
        if os.path.exists(tmp_img):
            os.remove(tmp_img)


if __name__ == "__main__":
    main()
