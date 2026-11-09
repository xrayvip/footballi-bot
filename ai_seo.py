# ai_seo.py
import os
import re
import json
import requests
from config import PROTO

GOOGLE_API_BASE = PROTO + "generativelanguage.googleapis" + ".com/v1beta"


def sanitize_url(u):
    if not u:
        return ""
    s = str(u).strip()
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", s)
    s = s.replace("[", "").replace("]", "").strip()
    s = re.sub(r"^(https?://)+", "https" + "://", s, flags=re.IGNORECASE)
    return s


def ai_batch_evaluate(candidates):
    """
    100% Free SEO & Copyright Evaluation Engine using Google AI Studio free tier.
    Zero external dependencies, zero API billing ($0 Cost).
    """
    if not candidates:
        return {}

    google_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not google_key:
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

TASK 2 - OFFICIAL NEWS AGENCY SEO TITLE, DESCRIPTION & TAGS (in Persian, for safe videos):
- "seo_title": Write an authoritative, professional, journalistic, and SEO-optimized Persian title (max 85 characters).
  * TONE: Official sports news agency (like Sky Sports or Varzesh3). No clickbait exaggeration, no messy emojis.
  * STRUCTURE:
    - Match Summaries: "خلاصه بازی [تیم میزبان] [نتیجه] [تیم میهمان] | هفته [X] لیگ برتر خلیج فارس"
    - Interviews/Press Conferences: "[نام شخص]: [مهم‌ترین جمله خبری به صورت رسمی] | حواشی [نام تیم/رقابت]"
    - News/Behind-the-scenes: "[موضوع دقیق و رسمی ویدیو] | [نام تیم یا رقابت مربوطه]"
  * If "is_short" is true, append " #shorts" at the end of the title.
- "seo_description": Write a clean, professional 3-paragraph journalistic Persian description:
  * Paragraph 1: 2 formal, informative sentences summarizing the match/event with natural high-search keywords.
  * Paragraph 2: Official channel footer: "رسانه رسمی فوتبالی؛ پوشش زنده و لحظه‌به‌لحظه مسابقات، خلاصه بازی‌ها، نشست‌های خبری و ویدیوهای اختصاصی فوتبال ایران. برای دنبال کردن آخرین ویدیوها، کانال رسمی فوتبالی را سابسکرایب کنید."
  * Paragraph 3: 6 to 8 relevant official hashtags (e.g. #فوتبالی #فوتبال_ایران #لیگ_برتر #خلاصه_بازی #استقلال #پرسپولیس #سپاهان #تراکتور).
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
    "seo_description": "...",
    "seo_tags": ["...", "..."]
  }}
]

Candidates:
{json.dumps(items_payload, ensure_ascii=False)}
"""

    models_to_try = [
        "models/gemini-flash-lite-latest",
        "models/gemini-3.5-flash-lite",
        "models/gemini-2.0-flash",
        "models/gemini-flash-latest"
    ]

    for model_name in models_to_try:
        try:
            url = f"{GOOGLE_API_BASE}/{model_name}:generateContent"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}
            }
            resp = requests.post(url, params={"key": google_key}, json=payload, timeout=45)
            if resp.status_code == 200:
                raw_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                if raw_text.startswith("```"):
                    raw_text = re.sub(r"^```(?:json)?|```$", "", raw_text, flags=re.MULTILINE).strip()
                parsed = json.loads(raw_text)
                result_map = {item["index"]: item for item in parsed if "index" in item}
                print(f"[FREE-SEO] Model ({model_name}) evaluated and SEO-optimized {len(result_map)} videos for $0.")
                return result_map
        except Exception:
            continue

    return {}
