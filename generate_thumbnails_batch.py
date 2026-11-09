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


def generate_prompt_with_claude(title, desc, arvan_key):
    headers = {"Authorization": f"Bearer {arvan_key}", "Content-Type": "application/json"}
    prompt = f"""
You are an award-winning Sports Photography Director for a major football network.
Video Title: {title}
Video Description: {desc[:300]}

Write a photorealistic, ultra-detailed English prompt for an AI image generator to create a stunning 16:9 widescreen YouTube thumbnail:
- Accurately mention team jersey colors (e.g. Esteghlal = royal blue kit, Persepolis = crimson red kit, Sepahan = golden yellow, Tractor = red).
- Dramatic stadium lighting at night, volumetric floodlights, flying grass particles, dynamic angle.
- For interviews/press: dramatic coach/player portrait at modern press mic with blurred stadium background.
- ALWAYS END WITH: "photorealistic 8k sports photography, dramatic cinematic lighting, 16:9 widescreen aspect ratio, strictly NO text, NO letters, NO watermarks, NO badges."

Output ONLY the prompt text, nothing else.
"""
    try:
        payload = {
            "model": "Claude-Opus-4.7",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3
        }
        r = requests.post(f"{GATEWAY_BASE}/chat/completions", headers=headers, json=payload, timeout=50)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[WARN] Claude prompt failed: {e}")
    return f"Epic dramatic football match scene on pitch, bright stadium floodlights at night, photorealistic 8k sports photography, 16:9 widescreen, no text, no watermarks."


def create_ai_image(image_prompt, output_jpg, arvan_key):
    headers = {"Authorization": f"Bearer {arvan_key}", "Content-Type": "application/json"}
    models = ["Gemini-3-Pro-Image-Preview", "Gemini-3.1-Flash-Image-Preview"]

    for m in models:
        # Route 1: OpenAI Images Endpoint
        try:
            print(f"[AI-IMAGE] Trying /images/generations with {m}...")
            payload = {"model": m, "prompt": image_prompt, "n": 1, "size": "1024x1024"}
            r = requests.post(f"{GATEWAY_BASE}/images/generations", headers=headers, json=payload, timeout=60)
            if r.status_code == 200:
                res_json = r.json()
                data_list = res_json.get("data", [])
                if data_list:
                    b64 = data_list[0].get("b64_json")
                    u = data_list[0].get("url")
                    if b64:
                        with open(output_jpg, "wb") as f:
                            f.write(base64.b64decode(b64))
                        return True
                    elif u:
                        dl = requests.get(u, timeout=30)
                        if dl.status_code == 200:
                            with open(output_jpg, "wb") as f:
                                f.write(dl.content)
                            return True
            else:
                print(f"[DEBUG] /images/generations ({m}) returned {r.status_code}: {r.text[:120]}")
        except Exception as e:
            print(f"[DEBUG] /images/generations error: {e}")

        # Route 2: Chat Completions Endpoint (Multimodal Image Gen)
        try:
            print(f"[AI-IMAGE] Trying /chat/completions with {m}...")
            payload = {
                "model": m,
                "messages": [
                    {"role": "user", "content": f"Generate a photorealistic 16:9 image: {image_prompt}"}
                ]
            }
            r = requests.post(f"{GATEWAY_BASE}/chat/completions", headers=headers, json=payload, timeout=80)
            if r.status_code == 200:
                data = r.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

                # Check base64
                b64_match = re.search(r"data:image/[a-zA-Z]+;base64,([A-Za-z0-9+/=]+)", str(content))
                if b64_match:
                    with open(output_jpg, "wb") as f:
                        f.write(base64.b64decode(b64_match.group(1)))
                    return True

                # Check URL
                url_match = re.search(r"https?://[^\s\)\"'>]+\.(?:png|jpg|jpeg|webp)", str(content), re.IGNORECASE)
                if not url_match:
                    url_match = re.search(r"!\[.*?\]\((https?://[^\s\)]+)\)", str(content))
                    img_u = url_match.group(1) if url_match else None
                else:
                    img_u = url_match.group(0)

                if img_u:
                    dl = requests.get(img_u, timeout=30)
                    if dl.status_code == 200:
                        with open(output_jpg, "wb") as f:
                            f.write(dl.content)
                        return True
            else:
                print(f"[DEBUG] /chat/completions ({m}) returned {r.status_code}: {r.text[:120]}")
        except Exception as e:
            print(f"[DEBUG] /chat/completions error: {e}")

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

    print(f"🎯 Found {len(long_videos)} recent long-form videos to upgrade.")

    for idx, v in enumerate(long_videos, 1):
        vid_id = v["id"]
        title = v["title"]
        print(f"\n[{idx}/3] Processing: {title[:50]} (ID: {vid_id})")

        print("🧠 Claude-Opus-4.7 is directing the photography prompt...")
        ai_prompt = generate_prompt_with_claude(title, v["desc"], arvan_key)
        print(f"🎬 Prompt: {ai_prompt[:120]}...")

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
            print(f"❌ Image generation failed for video {vid_id}.")


if __name__ == "__main__":
    main()
