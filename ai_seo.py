# ai_seo.py
import os
import re
import json
import requests
from config import PROTO


def sanitize_url(u):
    if not u:
        return ""
    s = str(u).strip()
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", s)
    s = s.replace("[", "").replace("]", "").strip()
    s = re.sub(r"^(https?://)+", "https" + "://", s, flags=re.IGNORECASE)
    return s


def ai_batch_evaluate(candidates):
    if not candidates:
        return {}

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("[WARN] GEMINI_API_KEY is not set.")
        return {}

    items_payload = []
    for idx, c in enumerate(candidates):
        items_payload.append({
            "index": idx,
            "title": c.get("title", ""),
            "tags": c.get("tags", ""),
            "source": c.get("source", "web"),
            "is_short": c.get("is_short", False)
        })

    prompt = f"""
You are the Chief Editor and YouTube SEO Strategist for the official Iranian sports news agency 'Footballi' (رسانه رسمی فوتبالی).

Analyze each candidate video below and perform TWO tasks:

TASK 1 - STRICT COPYRIGHT AUDIT:
- APPROVE ("safe": true): Iranian domestic football ONLY (Persian Gulf Pro League / لیگ برتر خلیج فارس, Hazfi Cup / جام حذفی, Azadegan League, Women's League, Iran National Team press conferences/interviews, domestic player/coach/manager interviews, refereeing analysis, studio shows, official club media).
- REJECT ("safe": false):
  1. ANY European football match highlights or broadcast footage (Champions League, LaLiga, Premier League, Serie A, Bundesliga, Real Madrid, Barcelona, Man City, etc.).
  2. ANY AFC Champions League Elite / ACL Two / Asian Champions League match footage or highlights (even if Iranian teams like Esteghlal, Persepolis, Tractor, or Sepahan are playing!).

TASK 2 - OFFICIAL NEWS AGENCY SEO & THUMBNAIL HEADLINE (in Persian, for safe videos):
- "seo_title": Write an authoritative, professional, journalistic, and SEO-optimized Persian title (max 85 characters).
  * TONE: Official sports news agency (like Sky Sports or Varzesh3).
  * STRUCTURE:
    - For Match Summaries: "خلاصه بازی [تیم میزبان] [نتیجه] [تیم میهمان] | هفته [X] لیگ برتر خلیج فارس"
    - For Interviews/News: "[نام شخص یا تیم]: [مهم‌ترین و جذاب‌ترین جمله خبری به صورت رسمی] | [نام رقابت]"
  * If "is_short" is true, append " #shorts" at the end of the title.
- "thumb_text": Write a VERY SHORT, punchy, high-impact Persian headline (strictly 3 to 6 words, max 32 characters) to be printed in large bold typography on the video Thumbnail image!
  * Examples for Match Summary: "سپاهان ۲ - ۰ ملوان" or "توقف استقلال در اهواز!"
  * Examples for Interview/News: "واکنش تند کارترون به داوری" or "پشت صحنه اختصاصی سپاهان"
- "seo_description": Write a clean, professional 3-paragraph journalistic Persian description with keywords, official subscribe CTA, and 6-8 hashtags.
- "seo_tags": A list of 10 high-search Persian keywords specific to this video.
- "category": Choose the exact playlist name:
  * "خلاصه بازی‌ها" (ONLY for match highlights/summaries)
  * "ویدیوهای کوتاه (Shorts)" (ONLY when is_short is true)
  * "مصاحبه و حواشی فوتبال ایران" (for interviews, news, press conferences, reactions)

Return ONLY a valid JSON array with this exact schema:
[
  {{
    "index": 0,
    "safe": true,
    "reason": "Domestic Persian Gulf Pro League content",
    "category": "مصاحبه و حواشی فوتبال ایران",
    "seo_title": "...",
    "thumb_text": "...",
    "seo_description": "...",
    "seo_tags": ["...", "..."]
  }}
]

Candidates:
{json.dumps(items_payload, ensure_ascii=False)}
"""

    gemini_base = PROTO + "generativelanguage.googleapis" + ".com/v1beta"
    models_to_try = [
        "models/gemini-flash-lite-latest",
        "models/gemini-3.5-flash-lite",
        "models/gemini-flash-latest",
        "models/gemini-2.0-flash"
    ]

    try:
        r_m = requests.get(
            sanitize_url(f"{gemini_base}/models"),
            params={"key": api_key},
            timeout=15
        )
        if r_m.status_code == 200:
            avail = [
                m["name"] for m in r_m.json().get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])
                and "gemini" in m["name"]
                and "tts" not in m["name"]
                and "image" not in m["name"]
                and "vision" not in m["name"]
            ]
            if avail:
                lite_first = sorted(avail, key=lambda x: (0 if "lite" in x else (1 if "flash" in x else 2)))
                models_to_try = lite_first[:4]
    except Exception:
        pass

    for model_name in models_to_try:
        try:
            url = sanitize_url(f"{gemini_base}/{model_name}:generateContent")
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "responseMimeType": "application/json"
                }
            }
            resp = requests.post(url, params={"key": api_key}, json=payload, timeout=50)
            if resp.status_code == 200:
                raw_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                if raw_text.startswith("```"):
                    raw_text = re.sub(r"^```(?:json)?|```$", "", raw_text, flags=re.MULTILINE).strip()
                parsed = json.loads(raw_text)
                result_map = {}
                for entry in parsed:
                    idx = entry.get("index")
                    if idx is not None:
                        result_map[idx] = entry
                print(f"[AI] Model ({model_name}) audited and SEO-optimized {len(result_map)} videos.")
                return result_map
            else:
                print(f"[WARN] Model {model_name} returned HTTP {resp.status_code}, trying next...")
        except Exception as e:
            print(f"[WARN] Error calling {model_name}: {e}")

    return {}
