# ai_seo.py
import os
import re
import json
import base64
import requests
from config import PROTO

# اتصال مستقیم به AI Gateway شما
GATEWAY_BASE = PROTO + "ai.najafivip" + ".ir/v1"


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
    Uses Flagship Claude-Opus-4.7 via https://ai.najafivip.ir/v1 to:
    1. Strictly audit copyright compliance.
    2. Write authoritative News Agency Persian SEO Title, Description & Tags.
    3. Write a bespoke 8K photorealistic English prompt ('image_prompt') for Gemini-3-Pro-Image-Preview.
    """
    if not candidates:
        return {}

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("[WARN] API Key (GEMINI_API_KEY) is not set.")
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

Analyze each candidate video below and perform THREE tasks:

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
- "seo_description": Write a clean, professional 3-paragraph journalistic Persian description with keywords, official subscribe CTA, and 6-8 hashtags.
- "seo_tags": A list of 10 high-search Persian keywords specific to this video.
- "category": Choose the exact playlist name:
  * "خلاصه بازی‌ها" (ONLY for match highlights/summaries)
  * "ویدیوهای کوتاه (Shorts)" (ONLY when is_short is true)
  * "مصاحبه و حواشی فوتبال ایران" (for interviews, news, press conferences, reactions)

TASK 3 - CUSTOM AI THUMBNAIL PROMPT ("image_prompt", in English, for safe non-short videos):
- Write a highly detailed, photorealistic English prompt for Gemini-3-Pro-Image-Preview to create a stunning 16:9 YouTube thumbnail specific to this video's subject:
  * Mention exact team jersey colors accurately (e.g., Esteghlal = royal blue kit, Persepolis = crimson red kit, Sepahan = golden yellow kit, Tractor = red kit, Malavan = white/navy kit, Gol Gohar = dark blue, Foulad = red/yellow).
  * For Match Summaries: Describe an intense, dramatic action shot on the pitch between players in those specific jersey colors under bright stadium floodlights at night, flying grass particles, shallow depth of field, 8k sports photography.
  * For Coach/Player Interviews or Press Conferences: Describe a dramatic cinematic portrait of a football head coach or player at a modern press conference microphone or stadium touchline with blurred stadium lights in background, serious authoritative expression, Canon EOS R5 85mm lens.
  * CRITICAL RULE: Always end the image_prompt with: "photorealistic 8k sports journalism photography, 16:9 widescreen aspect ratio, strictly NO text, NO letters, NO numbers, NO watermarks."

Return ONLY a valid JSON array with this exact schema:
[
  {{
    "index": 0,
    "safe": true,
    "reason": "Domestic Persian Gulf Pro League content",
    "category": "مصاحبه و حواشی فوتبال ایران",
    "seo_title": "...",
    "seo_description": "...",
    "seo_tags": ["...", "..."],
    "image_prompt": "..."
  }}
]

Candidates:
{json.dumps(items_payload, ensure_ascii=False)}
"""

    # اولویت با قوی‌ترین مدل‌های پرچم‌دار لیست شما
    text_models = [
        "GPT-4o-mini",
        "Claude-Opus-4.6",
        "Claude-Sonnet-4.6",
        "Gemini-3.1-Pro-Preview"
    ]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    chat_url = sanitize_url(f"{GATEWAY_BASE}/chat/completions")

    for model_name in text_models:
        try:
            payload = {
                "model": model_name,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a precise JSON-only sports journalism and SEO engine. Output valid JSON array only."
                    },
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.2
            }
            resp = requests.post(chat_url, headers=headers, json=payload, timeout=75)
            if resp.status_code == 200:
                data = resp.json()
                raw_text = data["choices"][0]["message"]["content"].strip()
                if "```" in raw_text:
                    m = re.search(r"\[.*\]", raw_text, flags=re.DOTALL)
                    if m:
                        raw_text = m.group(0)
                parsed = json.loads(raw_text)
                result_map = {}
                for entry in parsed:
                    idx = entry.get("index")
                    if idx is not None:
                        result_map[idx] = entry
                print(f"[AI] Flagship Model ({model_name}) audited and SEO-optimized {len(result_map)} videos.")
                return result_map
            else:
                print(f"[WARN] Text Model {model_name} returned HTTP {resp.status_code}: {resp.text[:120]}")
        except Exception as e:
            print(f"[WARN] Error calling {model_name}: {e}")

    return {}


def generate_ai_thumbnail(image_prompt, output_path):
    """
    Calls Flagship Gemini-3-Pro-Image-Preview (with fallback to Gemini-3.1-Flash-Image-Preview)
    via [https://ai.najafivip.ir/v1](https://ai.najafivip.ir/v1) to generate a bespoke 16:9 photorealistic sports thumbnail.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or not image_prompt:
        return False

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    image_models = [
        "Gemini-3-Pro-Image-Preview",
        "Gemini-3.1-Flash-Image-Preview"
    ]

    for model_name in image_models:
        # Method A: Standard /v1/images/generations endpoint
        try:
            img_gen_url = sanitize_url(f"{GATEWAY_BASE}/images/generations")
            payload = {
                "model": model_name,
                "prompt": image_prompt,
                "n": 1,
                "size": "1280x720"
            }
            resp = requests.post(img_gen_url, headers=headers, json=payload, timeout=80)
            if resp.status_code == 200:
                res_json = resp.json()
                data_list = res_json.get("data", [])
                if data_list:
                    b64_data = data_list[0].get("b64_json")
                    img_url = data_list[0].get("url")
                    if b64_data:
                        with open(output_path, "wb") as f:
                            f.write(base64.b64decode(b64_data))
                        print(f"[AI-IMAGE] Generated bespoke 16:9 thumbnail via {model_name} (b64).")
                        return True
                    elif img_url:
                        r_dl = requests.get(sanitize_url(img_url), timeout=30)
                        if r_dl.status_code == 200 and len(r_dl.content) > 10_000:
                            with open(output_path, "wb") as f:
                                f.write(r_dl.content)
                            print(f"[AI-IMAGE] Generated bespoke 16:9 thumbnail via {model_name} (url).")
                            return True
        except Exception as e:
            print(f"[INFO] /images/generations check on {model_name}: {e}")

        # Method B: OpenAI-compatible /v1/chat/completions endpoint for multimodal Gemini-Image models
        try:
            chat_url = sanitize_url(f"{GATEWAY_BASE}/chat/completions")
            payload = {
                "model": model_name,
                "messages": [
                    {"role": "user", "content": f"Generate a 16:9 widescreen image: {image_prompt}"}
                ]
            }
            resp = requests.post(chat_url, headers=headers, json=payload, timeout=80)
            if resp.status_code == 200:
                res_json = resp.json()
                msg = res_json.get("choices", [{}])[0].get("message", {})
                content = msg.get("content", "")

                b64_match = re.search(r"data:image/[a-zA-Z]+;base64,([A-Za-z0-9+/=]+)", str(content))
                if b64_match:
                    with open(output_path, "wb") as f:
                        f.write(base64.b64decode(b64_match.group(1)))
                    print(f"[AI-IMAGE] Generated bespoke 16:9 thumbnail via {model_name} (chat base64).")
                    return True

                url_match = re.search(r"https?://[^\s\)\"'>]+\.(?:png|jpg|jpeg|webp)[^\s\)\"'>]*", str(content), re.IGNORECASE)
                if not url_match:
                    url_match = re.search(r"!\[.*?\]\((https?://[^\s\)]+)\)", str(content))
                    img_u = url_match.group(1) if url_match else None
                else:
                    img_u = url_match.group(0)

                if img_u:
                    r_dl = requests.get(sanitize_url(img_u), timeout=30)
                    if r_dl.status_code == 200 and len(r_dl.content) > 10_000:
                        with open(output_path, "wb") as f:
                            f.write(r_dl.content)
                        print(f"[AI-IMAGE] Generated bespoke 16:9 thumbnail via {model_name} (chat URL).")
                        return True
        except Exception as e:
            print(f"[WARN] Chat image generation failed on {model_name}: {e}")

    return False
