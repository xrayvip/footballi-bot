# ai_seo.py
import os
import re
import json
import requests
from config import PROTO, FORBIDDEN_KEYWORDS

GOOGLE_API_BASE = PROTO + "generativelanguage.googleapis" + ".com/v1beta"
ARVAN_GATEWAY = PROTO + "ai.najafivip" + ".ir/v1"

DOMESTIC_SAFE_INDICATORS = [
    "استقلال", "پرسپولیس", "سپاهان", "تراکتور", "گل گهر", "گل‌گهر", "ذوب آهن", "ذوب‌آهن",
    "فولاد", "ملوان", "نساجی", "چادرملو", "مس رفسنجان", "خیبر", "آلومینیوم", "شمس آذر",
    "هوادار", "پیکان", "صنعت نفت", "سایپا", "فجر", "لیگ برتر ایران", "خلیج فارس", "جام حذفی",
    "لیگ آزادگان", "تیم ملی", "قلعه نویی", "قلعه‌نویی", "میثاقی", "فوتبال برتر", "داوری",
    "کمیته انضباطی", "فدراسیون", "درخشان", "تارتار", "رحمتی", "ربیعی", "کارترون", "مطهری",
    "آسانی", "حردانی", "رامین رضاییان", "بیرانوند", "حسینی", "شمسایی", "فوتسال"
]

FOREIGN_STARS_BLACKLIST = [
    "رونالدینیو", "دل پیرو", "دل‌پیرو", "مارادونا", "مسی", "رونالدو", "امباپه", "هالند",
    "عمر مومنی", "مومنی", "زیدان", "پپ", "گواردیولا", "آنتونی", "وینسیوس", "بلینگهام",
    "نیمار", "سوارز", "لواندوفسکی", "کلوپ", "مورینیو", "آنچلوتی"
]


def sanitize_url(u):
    if not u:
        return ""
    s = str(u).strip()
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", s)
    s = s.replace("[", "").replace("]", "").strip()
    s = re.sub(r"^(https?://)+", "https" + "://", s, flags=re.IGNORECASE)
    return s


def get_working_google_free_models(google_key):
    """Dynamically fetches the exact free Gemini models enabled on your Google AI Studio key"""
    default_models = [
        "models/gemini-flash-lite-latest",
        "models/gemini-2.5-flash-lite",
        "models/gemini-2.5-flash",
        "models/gemini-2.0-flash"
    ]
    try:
        r = requests.get(f"{GOOGLE_API_BASE}/models", params={"key": google_key}, timeout=12)
        if r.status_code == 200:
            avail = [
                m["name"] for m in r.json().get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])
                and "gemini" in m["name"]
                and not any(x in m["name"] for x in ["tts", "image", "vision", "embedding", "pro"])
            ]
            if avail:
                avail.sort(key=lambda x: (0 if "lite" in x else (1 if "flash" in x else 2)))
                return avail[:4]
    except Exception:
        pass
    return default_models


def call_ai_json(prompt):
    """
    Tier 1: Free Google AI Studio (auto-discovers active models on your key).
    Tier 2: Ultra-Cheap Lite/Nano models on Arvan Cloud (if Google is rate-limited).
    """
    google_key = os.environ.get("GEMINI_API_KEY", "").strip()
    arvan_key = os.environ.get("ARVAN_API_KEY", "").strip()

    # ---------------------------------------------------------
    # TIER 1: Free Google Gemini API ($0 Cost)
    # ---------------------------------------------------------
    if google_key and google_key.startswith("AIza"):
        google_models = get_working_google_free_models(google_key)
        for gm in google_models:
            try:
                url = f"{GOOGLE_API_BASE}/{gm}:generateContent"
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}
                }
                resp = requests.post(url, params={"key": google_key}, json=payload, timeout=40)
                if resp.status_code == 200:
                    raw_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    if "```" in raw_text:
                        m = re.search(r"\[.*\]", raw_text, flags=re.DOTALL)
                        if m:
                            raw_text = m.group(0)
                    parsed = json.loads(raw_text)
                    print(f"[AI-TIER-1] Success with FREE Google Gemini ({gm}) at $0 cost.")
                    return parsed
                else:
                    print(f"[INFO] Google Free ({gm}) HTTP {resp.status_code}")
            except Exception as e:
                print(f"[INFO] Google Free ({gm}) note: {e}")

    # ---------------------------------------------------------
    # TIER 2: Failover to Arvan Ultra-Cheap Lite/Nano Models
    # ---------------------------------------------------------
    active_arvan_key = arvan_key if arvan_key else (google_key if not google_key.startswith("AIza") else "")
    if active_arvan_key:
        ultra_cheap_arvan_models = [
            "Gemini-2.5-Flash-lite",
            "Gemini-3.1-Flash-Lite-Preview",
            "GPT-4.1-Nano",
            "GPT-4o-mini",
            "GLM-5.3-Flash"
        ]
        headers = {
            "Authorization": f"Bearer {active_arvan_key}",
            "Content-Type": "application/json"
        }
        chat_url = sanitize_url(f"{ARVAN_GATEWAY}/chat/completions")

        for am in ultra_cheap_arvan_models:
            try:
                payload = {
                    "model": am,
                    "messages": [
                        {"role": "system", "content": "You are a precise JSON-only sports journalism and SEO engine. Output valid JSON array only."},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.2
                }
                resp = requests.post(chat_url, headers=headers, json=payload, timeout=40)
                if resp.status_code == 200:
                    raw_text = resp.json()["choices"][0]["message"]["content"].strip()
                    if "```" in raw_text:
                        m = re.search(r"\[.*\]", raw_text, flags=re.DOTALL)
                        if m:
                            raw_text = m.group(0)
                    parsed = json.loads(raw_text)
                    print(f"[AI-TIER-2] Success with Arvan Ultra-Cheap Model ({am}).")
                    return parsed
            except Exception as e:
                print(f"[WARN] Arvan {am} note: {e}")

    return None


def build_local_journalistic_seo(candidate):
    title = candidate.get("title", "").strip()
    tags = candidate.get("tags", "").strip()
    is_short = candidate.get("is_short", False)
    is_sum = candidate.get("is_summary", False)
    combined = f"{title} {tags}"

    for bad in FOREIGN_STARS_BLACKLIST + FORBIDDEN_KEYWORDS:
        if bad in combined:
            return {"safe": False, "reason": f"Foreign/International keyword ('{bad}')"}

    is_iran_domestic = any(ind in combined for ind in DOMESTIC_SAFE_INDICATORS)
    if not is_iran_domestic:
        return {"safe": False, "reason": "Not verified as Iranian domestic football"}

    clean_t = re.sub(r"\s+", " ", title).strip()
    if is_sum:
        seo_title = f"{clean_t} | لیگ برتر خلیج فارس" if "لیگ برتر" not in clean_t else clean_t
        category = "خلاصه بازی‌ها"
    elif is_short:
        seo_title = f"{clean_t[:85]} #shorts"
        category = "ویدیوهای کوتاه (Shorts)"
    else:
        seo_title = f"{clean_t[:68]} | فوتبال ایران" if "|" not in clean_t else clean_t[:85]
        category = "مصاحبه و حواشی فوتبال ایران"

    seo_desc = (
        f"{clean_t}\n"
        f"پوشش ویدیویی ویژه از رقابت‌های فوتبال ایران، حواشی، کارشناسی داوری و اظهارات مربیان و بازیکنان در رسانه رسمی فوتبالی.\n\n"
        f"رسانه رسمی فوتبالی؛ پوشش زنده و لحظه‌به‌لحظه مسابقات، خلاصه بازی‌ها، نشست‌های خبری و ویدیوهای اختصاصی فوتبال ایران. "
        f"برای دنبال کردن آخرین ویدیوها، کانال رسمی فوتبالی را سابسکرایب کنید.\n\n"
        f"#فوتبالی #فوتبال_ایران #لیگ_برتر #خلاصه_بازی #استقلال #پرسپولیس #سپاهان #تراکتور"
    )

    return {
        "safe": True,
        "reason": "Verified Iranian domestic football",
        "category": category,
        "seo_title": seo_title,
        "seo_description": seo_desc,
        "seo_tags": ["فوتبالی", "فوتبال ایران", "لیگ برتر ایران", "خلاصه بازی", "استقلال", "پرسپولیس"]
    }


def ai_batch_evaluate(candidates):
    if not candidates:
        return {}

    items_payload = []
    for idx, c in enumerate(candidates):
        items_payload.append({
            "index": idx,
            "title": c.get("title", ""),
            "tags": c.get("tags", ""),
            "source": c.get("source", "web"),
            "is_short": c.get("is_short", False),
            "is_summary": c.get("is_summary", False)
        })

    prompt = f"""
You are the Chief Editor and YouTube SEO Strategist for the official Iranian sports news agency 'Footballi' (رسانه رسمی فوتبالی).

Analyze each candidate video below and perform TWO tasks:

TASK 1 - STRICT COPYRIGHT AUDIT:
- APPROVE ("safe": true): Iranian domestic football ONLY (Persian Gulf Pro League / لیگ برتر خلیج فارس, Hazfi Cup / جام حذفی, Azadegan League, Women's League, Futsal, Iran National Team, domestic player/coach/manager interviews, refereeing analysis / کارشناسی داوری, studio shows like Football Bartar / میثاقی).
- REJECT ("safe": false):
  1. ANY European/foreign football match highlights, foreign players/legends (Ronaldinho, Del Piero, Maradona, Omar Momani cartoons, etc.).
  2. ANY AFC Champions League Elite / ACL Two match footage.

TASK 2 - OFFICIAL NEWS AGENCY SEO TITLE, DESCRIPTION & TAGS (in Persian, for safe videos):
- "seo_title": Write an authoritative, professional, journalistic, and SEO-optimized Persian title (max 85 characters).
  * STRUCTURE:
    - Match Summaries: "خلاصه بازی [تیم میزبان] [نتیجه] [تیم میهمان] | لیگ برتر خلیج فارس"
    - Interviews/Refereeing/Controversies: "[موضوع دقیق و رسمی ویدیو] | حواشی لیگ برتر ایران"
  * If "is_short" is true, append " #shorts" at the end of the title.
- "seo_description": Write a clean, professional 3-paragraph journalistic Persian description with keywords, official subscribe CTA, and 6-8 hashtags.
- "seo_tags": A list of 10 high-search Persian keywords specific to this video.
- "category": Choose the exact playlist name:
  * "خلاصه بازی‌ها" (ONLY for full match summaries)
  * "ویدیوهای کوتاه (Shorts)" (ONLY when is_short is true)
  * "مصاحبه و حواشی فوتبال ایران" (for interviews, refereeing analysis, news, press conferences)

Return ONLY a valid JSON array with this exact schema:
[
  {{
    "index": 0,
    "safe": true,
    "reason": "Domestic Iranian football content",
    "category": "مصاحبه و حواشی فوتبال ایران",
    "seo_title": "...",
    "seo_description": "...",
    "seo_tags": ["...", "..."]
  }}
]

Candidates:
{json.dumps(items_payload, ensure_ascii=False)}
"""

    parsed = call_ai_json(prompt)
    if isinstance(parsed, list):
        result_map = {item["index"]: item for item in parsed if isinstance(item, dict) and "index" in item}
        if result_map:
            return result_map

    print("[AI-TIER-3] Using local journalistic auditor & SEO generator...")
    return {idx: build_local_journalistic_seo(c) for idx, c in enumerate(candidates)}
