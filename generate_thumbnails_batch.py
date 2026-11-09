# generate_thumbnails_batch.py
import os
import re
import json
import base64
import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

GATEWAY_BASE = "https://ai.najafivip.ir/v1"
TOKEN_FILE = "token.json"


def get_youtube_service():
    yt_scope = "https://www.googleapis.com/auth/youtube"
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, [yt_scope])
    elif os.environ.get("YOUTUBE_TOKEN"):
        token_data = json.loads(os.environ["YOUTUBE_TOKEN"])
        creds = Credentials.from_authorized_user_info(token_data, [yt_scope])

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError("YouTube Token invalid!")
    return build("youtube", "v3", credentials=creds)


def find_original_footballi_poster(ref_title):
    """Finds original media poster from footballi.net using lightweight regex (no external library)"""
    try:
        search_url = "https://footballi.net/news/video"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36"}
        r = requests.get(search_url, headers=headers, timeout=12)
        if r.status_code == 200:
            html = r.text
            links = re.findall(r'href=["\'](/video/[^"\']+)["\']', html)
            for l in links[:15]:
                full_page = f"https://footballi.net{l}"
                rp = requests.get(full_page, headers=headers, timeout=10)
                if rp.status_code == 200:
                    page_html = rp.text
                    posters = re.findall(r'<meta[^>]+(?:og:image|twitter:image)[^>]+content=["\']([^"\']+)["\']', page_html)
                    if not posters:
                        posters = re.findall(r'poster=["\']([^"\']+)["\']', page_html)
                    if posters:
                        return posters[0]
    except Exception:
        pass
    return None


def generate_smart_prompt_budget(title, desc, ref_poster_url, arvan_key):
    """
    Uses the ultra-cheap, high-IQ GPT-4o-mini / Gemini-3.1-Flash-Lite (Cost: $0.0001)
    to craft an unrestricted, highly clickable, viral YouTube Thumbnail prompt.
    """
    headers = {"Authorization": f"Bearer {arvan_key}", "Content-Type": "application/json"}
    models = ["GPT-4o-mini", "Gemini-3.1-Flash-Lite-Preview", "GLM-5.3-Flash"]

    prompt = f"""
You are a World-Class YouTube Thumbnail Designer for Iran's top sports media 'Footballi'.
Video Title: {title}
Video Details: {desc[:350]}
Original Media Poster Reference URL: {ref_poster_url or "None"}

YOUR GOAL: Design a modern, viral, high-CTR YouTube thumbnail prompt tailored EXACTLY to this specific video.
DO NOT use repetitive formulas (like generic stadium duels or cartoonish characters).
Make it feel like a real Iranian sports broadcast cover:
1. If it's a match: Feature real players from both teams, true kits, intense facial expressions, scoreboard graphics or match tension.
2. If it's an interview or press conference: Realistic studio/press room setting with real microphone, dramatic facial expression of the speaker, engaging news headline text.
3. Typography & Branding: Include bold, eye-catching text overlay in Persian or English relevant to the topic (e.g. key quote, score, or breaking news badge), and subtle official 'FOOTBALLI' logo watermark in the corner.
4. Style: Ultra-realistic 4K broadcast journalism, sharp focus, natural human anatomy, dynamic depth of field, 16:9 widescreen format.

Output ONLY the final image generation prompt in English.
"""

    for m in models:
        try:
            payload = {
                "model": m,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4
            }
            r = requests.post(f"{GATEWAY_BASE}/chat/completions", headers=headers, json=payload, timeout=35)
            if r.status_code == 200:
                out = r.json()["choices"][0]["message"]["content"].strip()
                print(f"[AI-DIRECTOR] Prompt crafted by {m} at near-zero cost.")
                return out
        except Exception as e:
            print(f"[WARN] Prompt model {m} failed: {e}")

    return f"A modern high-CTR YouTube thumbnail for Iranian football media 'Footballi': {title}, photorealistic 4K broadcast style, dramatic lighting, official sports graphic, 16:9 aspect ratio."


def create_ai_image(image_prompt, output_jpg, arvan_key):
    headers = {"Authorization": f"Bearer {arvan_key}", "Content-Type": "application/json"}
    models = ["Gemini-3-Pro-Image-Preview", "Gemini-3.1-Flash-Image-Preview"]

    for m in models:
        try:
            print(f"[AI-IMAGE] Generating bespoke thumbnail with {m}...")
            payload = {
                "model": m,
                "messages": [
                    {"role": "user", "content": f"Create this exact YouTube thumbnail image (16:9 aspect ratio): {image_prompt}"}
                ]
            }
            r = requests.post(f"{GATEWAY_BASE}/chat/completions", headers=headers, json=payload, timeout=85)
            if r.status_code == 200:
                data = r.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

                b64_match = re.search(r"data:image/[a-zA-Z]+;base64,([A-Za-z0-9+/=]+)", str(content))
                if b64_match:
                    with open(output_jpg, "wb") as f:
                        f.write(base64.b64decode(b64_match.group(1)))
                    return True

                url_match = re.search(r"https?://[^\s\)\"'>]+\.(?:png|jpg|jpeg|webp)", str(content), re.IGNORECASE)
                if not url_match:
                    url_match = re.search(r"!\[.*?\]\((https?://[^\s\)]+)\)", str(content))
                    img_u = url_match.group(1) if url_match else None
                else:
                    img_u = url_match.group(0)

                if img_u:
                    dl = requests.get(img_u, timeout=30)
                    if dl.status_code == 200 and len(dl.content) > 10_000:
                        with open(output_jpg, "wb") as f:
                            f.write(dl.content)
                        return True
            else:
                print(f"[DEBUG] {m} returned {r.status_code}: {r.text[:120]}")
        except Exception as e:
            print(f"[WARN] {m} error: {e}")

    return False


def main():
    arvan_key = os.environ.get("ARVAN_API_KEY", "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
    if not arvan_key:
        print("❌ ARVAN_API_KEY is not set!")
        return

    youtube = get_youtube_service()

    ch = youtube.channels().list(part="contentDetails", mine=True).execute()
    up_pl = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    pl_items = youtube.playlistItems().list(part="snippet", playlistId=up_pl, maxResults=10).execute()

    long_videos = []
    for it in pl_items.get("items", []):
        sn = it["snippet"]
        title = sn["title"]
        vid_id = sn["resourceId"]["videoId"]
        if "#shorts" not in title.lower():
            long_videos.append({"id": vid_id, "title": title, "desc": sn.get("description", "")})
        if len(long_videos) >= 3:
            break

    print(f"🎯 Upgrading last {len(long_videos)} long-form videos with Smart Creative Engine...")

    for idx, v in enumerate(long_videos, 1):
        vid_id = v["id"]
        title = v["title"]
        print(f"\n[{idx}/3] Processing: {title[:50]} (ID: {vid_id})")

        # 1. Search original web poster as inspiration reference
        ref_poster = find_original_footballi_poster(title)
        if ref_poster:
            print(f"🖼️ Found original Footballi media reference: {ref_poster[:65]}...")

        # 2. Budget AI Director (GPT-4o-mini / Gemini-Flash-Lite) crafts creative prompt
        ai_prompt = generate_smart_prompt_budget(title, v["desc"], ref_poster, arvan_key)
        print(f"🎬 Creative Prompt: {ai_prompt[:130]}...")

        # 3. Gemini-3-Pro-Image renders unrestricted thumbnail
        tmp_img = f"thumb_{vid_id}.jpg"
        if create_ai_image(ai_prompt, tmp_img, arvan_key):
            try:
                youtube.thumbnails().set(videoId=vid_id, media_body=MediaFileUpload(tmp_img)).execute()
                print(f"✅ Successfully updated thumbnail: https://youtu.be/{vid_id}")
            except Exception as e:
                print(f"❌ YouTube API error: {e}")
            finally:
                if os.path.exists(tmp_img):
                    os.remove(tmp_img)
        else:
            print(f"❌ Thumbnail generation failed for {vid_id}.")


if __name__ == "__main__":
    main()
