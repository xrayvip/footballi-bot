# ai_seo.py
import os
import re
import json
import requests
from openai import OpenAI
from config import PROTO

GOOGLE_API_BASE = PROTO + "generativelanguage.googleapis" + ".com/v1beta"

# تنظیمات اتصال به پلتفرم هوش مصنوعی آروان‌کلود
ARVAN_API_KEY = os.environ.get("ARVAN_API_KEY", "").strip()
ARVAN_BASE_URL = os.environ.get("ARVAN_BASE_URL", "https://api.arvancloud.ir/v1").strip()
ARVAN_SEO_MODEL = "claude-sonnet-4-6"  # یا مدل متنی مدنظر در پنل آروان شما


def sanitize_url(u):
    if not u:
        return ""
    s = str(u).strip()
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", s)
    s = s.replace("[", "").replace("]", "").strip()
    s = re.sub(r"^(https?://)+", "https" + "://", s, flags=re.IGNORECASE)
    return s


# --------------------------------------------------------------------------
# ۱. ممیزی رایگان کپی‌رایت با جمنای (فراخوانی تکی برای آپلودر)
# --------------------------------------------------------------------------
def ai_free_copyright_audit(video_title: str) -> bool:
    """
    ممیزی سریع کپی‌رایت بر اساس سیاست Zero-Trust از طریق اکانت رایگان گوگل استودیو.
    رد قطعی فوتبال خارجی/لیگ نخبگان آسیا و تایید فوتبال و حواشی داخلی.
    """
    google_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not google_key:
        print("[AUDIT-WARN] GEMINI_API_KEY not found. Skipping audit.")
        return False

    prompt = f"""
تو ممیز ارشد کپی‌رایت یوتیوب هستی بر اساس سیاست Zero-Trust رسانه فوتبالی.

قوانین ممیزی:
۱. مجاز (SAFE): فقط فوتبال و حواشی داخلی ایران (لیگ برتر خلیج فارس، جام حذفی، لیگ آزادگان، فوتبال بانوان، نشست‌های خبری، مصاحبه مربیان/بازیکنان داخلی، کارشناسی داوری و تولیدات باشگاه‌های داخلی).
۲. غیرمجاز (REJECT):
   - هرگونه خلاصه بازی، تصویر یا بریده مسابقات لیگ‌های اروپایی و بین‌المللی (چمپیونزلیگ، لیگ برتر انگلیس، لالیگا، بوندسلیگا و...).
   - هرگونه تصویر یا خلاصه بازی‌های لیگ نخبگان آسیا (ACL Elite / ACL Two) حتی اگر استقلال، پرسپولیس، سپاهان یا تراکتور بازی کرده باشند.

عنوان ویدیو: "{video_title}"

پاسخ را دقیقاً و فقط در یک کلمه ارسال کن: SAFE یا REJECT
"""

    models_to_try = [
        "models/gemini-flash-lite-latest",
        "models/gemini-2.0-flash",
        "models/gemini-flash-latest"
    ]

    for model_name in models_to_try:
        try:
            url = f"{GOOGLE_API_BASE}/{model_name}:generateContent"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.0}
            }
            resp = requests.post(url, params={"key": google_key}, json=payload, timeout=25)
            if resp.status_code == 200:
                verdict = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip().upper()
                is_safe = "SAFE" in verdict and "REJECT" not in verdict
                print(f"[FREE-AUDIT] Model ({model_name}) -> '{video_title}': {verdict}")
                return is_safe
        except Exception:
            continue

    return False


# --------------------------------------------------------------------------
# ۲. تولید سئو، تایتل ژورنالیستی و پلی‌لیست در آروان‌کلود
# --------------------------------------------------------------------------
def arvan_generate_single_seo(video_title: str, is_short: bool = False) -> dict:
    """
    تولید عنوان ژورنالیستی رسمی خبرگزاری فوتبالی، توضیحات، هشتگ‌ها و پلی‌لیست
    با استفاده از سرویس هوش مصنوعی آروان‌کلود
    """
    if not ARVAN_API_KEY:
        print("[ARVAN-SEO] ARVAN_API_KEY not configured. Falling back to default metadata.")
        short_suffix = " #shorts" if is_short else ""
        return {
            "title": f"{video_title}{short_suffix}"[:100],
            "description": f"{video_title}\n\nرسانه رسمی فوتبالی؛ پوشش زنده و لحظه‌به‌لحظه مسابقات و حواشی فوتبال ایران.\n\n#فوتبالی #فوتبال_ایران #لیگ_برتر",
            "playlist": "ویدیوهای کوتاه (Shorts)" if is_short else "مصاحبه و حواشی فوتبال ایران",
            "tags": ["فوتبالی", "فوتبال ایران", "لیگ برتر"]
        }

    system_prompt = """تو سردبیر ارشد و استراتژیست سئوی یوتیوب در رسانه رسمی «فوتبالی» هستی.
برای ویدیوی ورزشی تاییدشده، متادیتای ژورنالیستی و سئو شده به زبان فارسی تولید کن.

اصول نگارش:
۱. عنوان (title): رسمی، ژورنالیستی (مانند اسکای اسپورتس یا ورزش ۳)، حداکثر ۸۵ کاراکتر، بدون کلیک‌بیت سخیف یا اموجی‌های نامرتب.
   - خلاصه مسابقات: "خلاصه بازی [میزبان] [نتیجه] [میهمان] | هفته [X] لیگ برتر"
   - مصاحبه/نشست خبری: "[نام شخص]: [مهم‌ترین تیتر خبری] | حواشی [نام تیم]"
   - سایر حواشی: "[موضوع دقیق ویدیو] | [نام تیم یا رویداد]"
   - اگر ویدیو شورت است، در انتهای عنوان عبارت " #shorts" را درج کن.
۲. توضیحات (description) در ۳ بخش:
   - بند ۱: دو جمله خلاصه و خبری درباره رویداد همراه با کلمات کلیدی پرتکرار جستجو.
   - بند ۲: متن ثابت رسانه: "رسانه رسمی فوتبالی؛ پوشش زنده و لحظه‌به‌لحظه مسابقات، خلاصه بازی‌ها، نشست‌های خبری و ویدیوهای اختصاصی فوتبال ایران. برای دنبال کردن آخرین ویدیوها، کانال رسمی فوتبالی را سابسکرایب کنید."
   - بند ۳: ۶ تا ۸ هشتگ رسمی فارسی متناسب با ویدیو.
۳. دسته‌بندی و پلی‌لیست (playlist): دقیقاً یکی از این سه مورد باشد:
   - "خلاصه بازی‌ها" (صرفاً برای خلاصه کامل بازی)
   - "ویدیوهای کوتاه (Shorts)" (در صورت شورت بودن)
   - "مصاحبه و حواشی فوتبال ایران" (برای مصاحبه‌ها، اخبار و حواشی)
۴. تگ‌ها (tags): آرایه‌ای شامل ۸ تا ۱۰ کلیدواژه فارسی با سرچ بالا برای ویدیوی جاری.

خروجی باید الزاماً و بدون هیچ پیش‌گفتاری یک شیء JSON با ساختار زیر باشد:
{
  "title": "...",
  "description": "...",
  "playlist": "...",
  "tags": ["...", "..."]
}"""

    user_prompt = f"عنوان ورودی ویدیو: {video_title}\nویدیو عمودی/شورت: {is_short}"

    try:
        client = OpenAI(
            api_key=ARVAN_API_KEY,
            base_url=ARVAN_BASE_URL
        )

        response = client.chat.completions.create(
            model=ARVAN_SEO_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.2,
            response_format={"type": "json_object"}
        )

        content = response.choices[0].message.content.strip()
        data = json.loads(content)
        print(f"[ARVAN-SEO] Optimized: {data.get('title')}")
        return data

    except Exception as e:
        print(f"[ARVAN-SEO Error] Generation failed: {e}. Using fallback.")
        short_suffix = " #shorts" if is_short else ""
        return {
            "title": f"{video_title}{short_suffix}"[:100],
            "description": f"{video_title}\n\nرسانه رسمی فوتبالی؛ پوشش زنده مسابقات فوتبال ایران.\n\n#فوتبالی #فوتبال_ایران",
            "playlist": "ویدیوهای کوتاه (Shorts)" if is_short else "مصاحبه و حواشی فوتبال ایران",
            "tags": ["فوتبالی", "فوتبال ایران", "لیگ برتر"]
        }


# --------------------------------------------------------------------------
# ۳. ارزیابی دسته‌جمعی با جمنای (نسخه رایگان قبلی)
# --------------------------------------------------------------------------
def ai_batch_evaluate(candidates):
    """
    100% Free SEO & Copyright Evaluation Engine using Google AI Studio free tier.
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
