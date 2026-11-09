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
    """Uses Claude-Opus-4.7 to generate an artistic 8K photorealistic sports photography prompt"""
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
    """Uses Gemini-3-Pro-Image-Preview to generate the image"""
    headers = {"Authorization": f"Bearer {arvan_key}", "Content-Type": "application/json"}
    models = ["Gemini-3-Pro-Image-Preview", "Gemini-3.1-Flash-Image-Preview"]

    for m in models:
        try:
            print(f"[AI-IMAGE] Generating with {m}...")
            payload = {"model": m, "prompt": image_prompt, "n": 1, "size": "1280x720"}
            r = requests.post(f"{GATEWAY_BASE}/images/generations", headers=headers, json=payload, timeout=90)
            if r.status_code == 200:
                data_list = r.json().get("data", [])
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
        except Exception as e:
            print(f"[WARN] {m} failed: {e}")
    return False


def main():
    arvan_key = os.environ.get("ARVAN_API_KEY", "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
    if not arvan_key:
        print("❌ ARVAN_API_KEY is not set!")
        return

    youtube = get_youtube_service()

    # Get uploaded playlist
    ch = youtube.channels().list(part="contentDetails", mine=True).execute()
    up_pl = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    # Fetch last 10 videos
    pl_items = youtube.playlistItems().list(part="snippet", playlistId=up_pl, maxResults=10).execute()

    # Filter out Shorts and pick the last 3 long-form videos
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

        # 1. Claude generates prompt
        print("🧠 Claude-Opus-4.7 is directing the photography prompt...")
        ai_prompt = generate_prompt_with_claude(title, v["desc"], arvan_key)
        print(f"🎬 Prompt: {ai_prompt[:120]}...")

        # 2. Gemini-3-Pro-Image generates 8K thumbnail
        tmp_img = f"thumb_{vid_id}.jpg"
        if create_ai_image(ai_prompt, tmp_img, arvan_key):
            # 3. Upload to YouTube
            try:
                youtube.thumbnails().set(videoId=vid_id, media_body=MediaFileUpload(tmp_img)).execute()
                print(f"✅ Successfully updated thumbnail for video: https://youtu.be/{vid_id}")
            except Exception as e:
                print(f"❌ YouTube API error: {e}")
            finally:
                if os.path.exists(tmp_img):
                    os.remove(tmp_img)
        else:
            print("❌ Image generation failed.")


if __name__ == "__main__":
    main()
