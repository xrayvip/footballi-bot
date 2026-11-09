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
from PIL import Image, ImageDraw, ImageFont

GATEWAY_BASE = "https://ai.najafivip.ir/v1"
TOKEN_FILE = "token.json"


def extract_video_id(input_str):
    """Extracts YouTube 11-char Video ID from any URL format or bare ID"""
    if not input_str:
        return ""
    s = input_str.strip()
    # Match standard youtu.be or watch?v= or embed/ or bare 11 chars
    m = re.search(r"(?:v=|\/|be\/|embed\/)([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)
    if len(s) == 11:
        return s
    return s


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


def add_official_footballi_watermark(image_path):
    """Adds official Footballi green/white broadcast watermark badge in the top-right corner"""
    try:
        img = Image.open(image_path).convert("RGBA")
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Draw sleek emerald-dark pill badge in top-right
        x1, y1, x2, y2 = 980, 35, 1245, 95
        draw.rounded_rectangle([(x1, y1), (x2, y2)], radius=15, fill=(6, 26, 18, 235), outline=(0, 195, 110, 255), width=3)

        # Draw green accent circle
        draw.ellipse([(x1 + 18, y1 + 20), (x1 + 38, y1 + 40)], fill=(0, 215, 120, 255))

        # Brand Text
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26)
        except Exception:
            font = ImageFont.load_default()

        draw.text((x1 + 52, y1 + 17), "FOOTBALLI", font=font, fill=(255, 255, 255, 255))

        final_img = Image.alpha_composite(img, overlay).convert("RGB")
        final_img.save(image_path, "JPEG", quality=95)
        print("[WATERMARK] Official Footballi brand watermark embedded.")
    except Exception as e:
        print(f"[WARN] Watermark note: {e}")


def generate_face_preserving_prompt(title, desc, arvan_key):
    headers = {"Authorization": f"Bearer {arvan_key}", "Content-Type": "application/json"}
    prompt = f"""
You are an elite YouTube Thumbnail Art Director for Iranian sports media 'Footballi'.
Video Title: {title}
Video Details: {desc[:350]}

Write a photorealistic, viral, high-CTR YouTube thumbnail prompt tailored to this specific video:
- If this is an interview with an Iranian coach/player (e.g. Hamid Derakhshan): Describe a realistic, mature Iranian football expert with true facial resemblance, sitting at an official press microphone, intense expressive look, studio broadcast lighting, 16:9 widescreen.
- If it's a match: Realistic Iranian league players in true kit colors (Esteghlal blue, Persepolis red, Sepahan yellow, Tractor red).
- Leave space in the top-right corner for branding.
- End with: "photorealistic 8k sports photography, dramatic lighting, 16:9 widescreen, strictly NO text, NO watermarks."

Output ONLY the prompt text in English.
"""
    try:
        payload = {
            "model": "GPT-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2
        }
        r = requests.post(f"{GATEWAY_BASE}/chat/completions", headers=headers, json=payload, timeout=30)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[WARN] Prompt error: {e}")
    return f"Photorealistic 8k sports photography of {title}, dramatic broadcast studio lighting, 16:9 widescreen, strictly no text."


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
                    add_official_footballi_watermark(output_jpg)
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
                        add_official_footballi_watermark(output_jpg)
                        return True
        except Exception as e:
            print(f"[WARN] {m} error: {e}")

    return False


def main():
    arvan_key = os.environ.get("ARVAN_API_KEY", "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
    if not arvan_key:
        print("❌ ARVAN_API_KEY is not set!")
        return

    raw_target = os.environ.get("TARGET_VIDEO", "").strip()
    vid_id = extract_video_id(raw_target)
    if not vid_id:
        print(f"❌ Invalid Video URL or ID: '{raw_target}'")
        return

    youtube = get_youtube_service()

    # Fetch targeted video info from YouTube
    print(f"🔍 Fetching details for Video ID: {vid_id} ...")
    resp = youtube.videos().list(part="snippet", id=vid_id).execute()
    items = resp.get("items", [])
    if not items:
        print(f"❌ Video not found on your channel with ID: {vid_id}")
        return

    sn = items[0]["snippet"]
    title = sn.get("title", "")
    desc = sn.get("description", "")
    print(f"🎯 Target Title: {title}")

    # 1. Generate Prompt
    print("🧠 Crafting bespoke creative prompt...")
    ai_prompt = generate_face_preserving_prompt(title, desc, arvan_key)
    print(f"🎬 Directed Prompt: {ai_prompt}")

    # 2. Generate Image & Watermark
    tmp_img = f"thumb_{vid_id}.jpg"
    if create_ai_image(ai_prompt, tmp_img, arvan_key):
        try:
            youtube.thumbnails().set(videoId=vid_id, media_body=MediaFileUpload(tmp_img)).execute()
            print(f"✅ Successfully updated thumbnail for video: https://youtu.be/{vid_id}")
        except Exception as e:
            print(f"❌ YouTube API error: {e}")
        finally:
            if os.path.exists(tmp_img):
                os.remove(tmp_img)
    else:
        print(f"❌ Thumbnail generation failed for {vid_id}.")


if __name__ == "__main__":
    main()
