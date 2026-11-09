# ai_seo.py
import os
import re
import json
import base64
import requests
from config import PROTO

ARVAN_GATEWAY = PROTO + "ai.najafivip" + ".ir/v1"
GOOGLE_API_BASE = PROTO + "generativelanguage.googleapis" + ".com/v1beta"


def sanitize_url(u):
    if not u:
        return ""
    s = str(u).strip()
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", s)
    s = s.replace("[", "").replace("]", "").strip()
    s = re.sub(r"^(https?://)+", "https" + "://", s, flags=re.IGNORECASE)
    return s


# =========================================================================
# مرحله ۱: ممیزی کپی‌رایت با جمینای رایگان گوگل (هزینه صفر - هر ۱۵ دقیقه)
# =========================================================================
def ai_free_copyright_audit(candidates):
    """
    100% FREE batch copyright audit using Google AI Studio free tier.
    Zero consumption on Arvan Cloud wallet.
    """
    if not candidates:
        return {}

    google_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not google_key:
        print("[WARN] Google GEMINI_API_KEY is not set.")
        return {}

    items_payload = []
    for idx, c in enumerate(candidates):
        items_payload.append({
            "index": idx,
            "title": c.get("title", ""),
            "tags": c.get("tags", ""),
            "is_short": c.get("is_short", False)
        })

    prompt = f"""
You are a strict Copyright Auditor for Iranian football channel 'Footballi'.
Audit each video below:
- "safe": true -> Iranian domestic football ONLY (Persian Gulf Pro League, Hazfi Cup, Azadegan League, Women's League, Iran National Team press conferences/interviews, domestic player/coach interviews, studio shows).
- "safe": false -> ANY European club match footage (Champions League, Premier League, LaLiga, Serie A, Bundesliga, etc.) OR AFC Champions League / Asian Champions League match broadcasts (even if Iranian clubs are playing!).

Return ONLY a valid JSON array:
[
  {{"index": 0, "safe": true, "reason": "Domestic Iranian football"}},
  {{"index": 1, "safe": false, "reason": "Foreign league match"}}
]

Candidates:
{json.dumps(items_payload, ensure_ascii=False)}
"""

    models_to_try = [
        "models/gemini-flash-lite-latest",
        "models/gemini-3.5-flash-lite",
        "models/gemini-2.0-flash"
    ]

    for model_name in models_to_try:
        try:
            url = f"{GOOGLE_API_BASE}/{model_name}:generateContent"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
            }
            resp = requests.post(url, params={"key": google_key}, json=payload, timeout=40)
            if resp.status_code == 200:
                raw_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                if raw_text.startswith("```"):
                    raw_text = re.sub(r"^```(?:json)?|```$", "", raw_text, flags=re.MULTILINE).strip()
                parsed = json.loads(raw_text)
                result_map = {item["index"]: item for item in parsed if "index" in item}
                print(f"[FREE-AUDIT] Checked {len(result_map)} candidates for $0 using Google {model_name}.")
                return result_map
        except Exception:
            continue

    return {}


# =========================================================================
# مرحله ۲: سئوی ژورنالیستی و پرامپت عکاسی فقط برای ویدیوی تاییدشده (آروان)
# =========================================================================
def arvan_generate_single_seo(video_title, video_tags="", is_short=False, is_summary=False):
    """
    Called ONLY when a valid Iranian video is downloaded and confirmed for upload!
    Uses Arvan Cloud (Gemini-3.1-Flash-Lite / GPT-4o-mini) to produce elite SEO metadata.
    """
    arvan_key = os.environ.get("ARVAN_API_KEY", "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
    if not arvan_key:
        return {}

    prompt = f"""
You are the Chief Editor and YouTube SEO Strategist for 'Footballi' (رسانه رسمی فوتبالی).
Write professional, authoritative News Agency metadata for this single video:
Title: {video_title}
Tags: {video_tags}
Is Short: {is_short}
Is Match Summary: {is_summary}

Requirements:
1. "seo_title": Authoritative, journalistic Persian title (max 85 chars, no clickbait words, max 1 clean separator |). If short, append #shorts.
2. "seo_description": 3 professional paragraphs: match/event summary with natural keywords, official subscribe CTA, and 6-8 hashtags.
3. "seo_tags": Array of 10 high-search Persian keywords.
4. "image_prompt": Detailed photorealistic English prompt for a 16:9 thumbnail (mention exact team jersey colors, floodlights, 8k sports photography, strictly NO text, NO watermarks).

Return ONLY JSON:
{{
  "seo_title": "...",
  "seo_description": "...",
  "seo_tags": ["..."],
  "image_prompt": "..."
}}
"""

    headers = {"Authorization": f"Bearer {arvan_key}", "Content-Type": "application/json"}
    chat_url = sanitize_url(f"{ARVAN_GATEWAY}/chat/completions")
    models = ["Gemini-3.1-Flash-Lite-Preview", "GPT-4o-mini", "GLM-5.3-Flash"]

    for m in models:
        try:
            payload = {
                "model": m,
                "messages": [
                    {"role": "system", "content": "You are a JSON-only sports SEO engine."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.2
            }
            resp = requests.post(chat_url, headers=headers, json=payload, timeout=35)
            if resp.status_code == 200:
                raw_text = resp.json()["choices"][0]["message"]["content"].strip()
                if "```" in raw_text:
                    match = re.search(r"\{.*\}", raw_text, flags=re.DOTALL)
                    if match:
                        raw_text = match.group(0)
                data = json.loads(raw_text)
                print(f"[PREMIUM-SEO] Generated SEO & thumbnail prompt using {m} on Arvan.")
                return data
        except Exception as e:
            print(f"[WARN] Arvan SEO error on {m}: {e}")

    return {}


# =========================================================================
# مرحله ۳: تولید تصویر تامبنیل ۱۶:۹ از آروان (فقط در زمان آپلود)
# =========================================================================
def generate_ai_thumbnail(image_prompt, output_path):
    arvan_key = os.environ.get("ARVAN_API_KEY", "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
    if not arvan_key or not image_prompt:
        return False

    headers = {"Authorization": f"Bearer {arvan_key}", "Content-Type": "application/json"}
    image_models = ["Gemini-3.1-Flash-Image-Preview", "Gemini-3-Pro-Image-Preview"]

    for model_name in image_models:
        try:
            img_gen_url = sanitize_url(f"{ARVAN_GATEWAY}/images/generations")
            payload = {"model": model_name, "prompt": image_prompt, "n": 1, "size": "1280x720"}
            resp = requests.post(img_gen_url, headers=headers, json=payload, timeout=60)
            if resp.status_code == 200:
                data_list = resp.json().get("data", [])
                if data_list:
                    b64_data = data_list[0].get("b64_json")
                    img_url = data_list[0].get("url")
                    if b64_data:
                        with open(output_path, "wb") as f:
                            f.write(base64.b64decode(b64_data))
                        print(f"[AI-IMAGE] Generated 16:9 thumbnail via {model_name}.")
                        return True
                    elif img_url:
                        r_dl = requests.get(sanitize_url(img_url), timeout=30)
                        if r_dl.status_code == 200 and len(r_dl.content) > 10_000:
                            with open(output_path, "wb") as f:
                                f.write(r_dl.content)
                            print(f"[AI-IMAGE] Generated 16:9 thumbnail via {model_name}.")
                            return True
        except Exception:
            pass

    return False
