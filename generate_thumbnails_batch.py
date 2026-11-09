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
        print("[WATERMARK] Official Footballi brand watermark embedded.")
    except Exception as e:
        print(f"[WARN] Watermark note: {e}")


def generate_pure_english_prompt(title, desc, arvan_key):
    """Guarantees a clean, 100% English prompt tailored to the match or interview"""
    headers = {"Authorization": f"Bearer {arvan_key}", "Content-Type": "application/json"}
    
    # Simple, bulletproof system prompt
    system_instruction = "You are an AI Image Prompt Generator. Translate the football match or sports event into an ultra-realistic English prompt for an image generator. Output English text ONLY."
    user_query = f"Create a 16:9 football YouTube thumbnail prompt for this match/video: Title='{title}'. Mention team kits accurately (Esteghlal Khuzestan vs Esteghlal: blue vs blue/white duel on grass), intense action, stadium floodlights at night, 8k sports photography, strictly no text."

    models = ["GPT-4o-mini", "Gemini-3.1-Flash-Lite-Preview", "Claude-Sonnet-4.6"]
    for m in models:
        try:
            payload = {
                "model": m,
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_query}
                ],
                "temperature": 0.2
            }
            r = requests.post(f"{GATEWAY_BASE}/chat/completions", headers=headers, json=payload, timeout=30)
            if r.status_code == 200:
                out = r.json()["choices"][0]["message"]["content"].strip()
                # Ensure no Persian characters exist in the output prompt
                if not re.search(r"[\u0600-\u06FF]", out):
                    print(f"[AI-PROMPT] Prompt successfully translated by {m}.")
                    return out
        except Exception as e:
            print(f"[INFO] Prompt model {m}: {e}")

    # Solid pure English fallback if API fails
    return "Epic soccer match collision on grass between Esteghlal players in blue kit vs opponent in white kit, ball in mid-air, intense facial expressions, packed stadium floodlights at night, dramatic cinematic sports photography, 16:9 widescreen, strictly no text."


def create_ai_image(image_prompt, output_jpg, arvan_key):
    headers = {"Authorization": f"Bearer {arvan_key}", "Content-Type": "application/json"}
    models = ["Gemini-3-Pro-Image-Preview", "Gemini-3.1-Flash-Image-Preview"]

    for m in models:
        # Route A: /chat/completions
        try:
            print(f"[AI-IMAGE] Requesting image via chat endpoint ({m})...")
            payload = {
                "model": m,
                "messages": [
                    {"role": "user", "content": f"Create this photorealistic 16:9 image: {image_prompt}"}
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
            else:
                print(f"[DEBUG] Chat response HTTP {r.status_code}: {r.text[:140]}")
        except Exception as e:
            print(f"[DEBUG] Chat error: {e}")

        # Route B: /images/generations
        try:
            print(f"[AI-IMAGE] Requesting image via images endpoint ({m})...")
            payload = {"model": m, "prompt": image_prompt, "n": 1, "size": "1024x1024"}
            r = requests.post(f"{GATEWAY_BASE}/images/generations", headers=headers, json=payload, timeout=60)
            if r.status_code == 200:
                data_list = r.json().get("data", [])
                if data_list:
                    b64 = data_list[0].get("b64_json")
                    u = data_list[0].get("url")
                    if b64:
                        with open(output_jpg, "wb") as f:
                            f.write(base64.b64decode(b64))
                        add_official_footballi_watermark(output_jpg)
                        return True
                    elif u:
                        dl = requests.get(u, timeout=30)
                        if dl.status_code == 200:
                            with open(output_jpg, "wb") as f:
                                f.write(dl.content)
                            add_official_footballi_watermark(output_jpg)
                            return True
            else:
                print(f"[DEBUG] Images response HTTP {r.status_code}: {r.text[:140]}")
        except Exception as e:
            print(f"[DEBUG] Images error: {e}")

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

    # 1. Pure English Prompt Translation
    print("🧠 Crafting 100% English Photography Prompt...")
    ai_prompt = generate_pure_english_prompt(title, desc, arvan_key)
    print(f"🎬 Directed English Prompt:\n{ai_prompt}\n")

    # 2. Render Image & Apply Watermark
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
