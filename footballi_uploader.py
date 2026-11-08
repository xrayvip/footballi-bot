import os
import re
import sys
import json
import time
import shutil
import subprocess
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# ==========================================
# Core Configuration & Daily Quota Settings
# ==========================================
PROTO = "https" + "://"
BASE_URL = PROTO + "footballi.net"
VIDEO_LIST_URLS = [
    BASE_URL + "/video",
    BASE_URL,
    BASE_URL + "/competition/14",  # Persian Gulf Pro League
    BASE_URL + "/competition/1",   # Hazfi Cup
    BASE_URL + "/competition/90",  # Azadegan League
    BASE_URL + "/competition/91",  # Women's League
]

HISTORY_FILE = "uploaded_matches.json"
TOKEN_FILE = "token.json"

# Daily quota resets at 11:30 AM Iran Time (08:00 UTC) matching YouTube
MAX_DAILY_UPLOADS = 30
RESERVED_FOR_SUMMARIES = 3
MAX_UPLOADS_PER_RUN = 4
APIFY_COOLDOWN_HOURS = 8
MAX_REELS_PER_RUN = 2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
}

FORBIDDEN_KEYWORDS = [
    "لیگ قهرمانان اروپا", "لیگ نخبگان", "لیگ قهرمانان آسیا", "سطح ۲ آسیا", "سطح دو آسیا",
    "لالیگا", "لیگ برتر انگلیس", "سری آ", "بوندسلیگا", "لیگ یک فرانسه",
    "رئال مادرید", "بارسلونا", "منچسترسیتی", "منچستر سیتی", "منچستریونایتد", "لیورپول",
    "آرسنال", "چلسی", "بایرن مونیخ", "دورتموند", "اینتر میلان", "یوونتوس", "آث میلان",
    "پاری سن ژرمن", "الهلال", "النصر", "الاهلی عربستان", "الاتحاد", "السد", "العین",
    "شباب الاهلی", "پاختاکور", "الشرطه", "الغرافه", "الوصل", "الریان",
    "champions league", "premier league", "laliga", "serie a", "bundesliga", "uefa", "afc"
]

SUMMARY_KEYWORDS = [
    "خلاصه بازی", "خلاصه مسابقه", "خلاصه دیدار", "گل های بازی", "گل‌های بازی"
]


# ==========================================
# History File Management
# ==========================================
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {
                        "uploaded_urls": data,
                        "uploaded_titles": [],
                        "last_apify_run": 0
                    }
                if isinstance(data, dict):
                    data.setdefault("uploaded_urls", [])
                    data.setdefault("uploaded_titles", [])
                    data.setdefault("last_apify_run", 0)
                    return data
        except Exception as e:
            print(f"[WARN] Could not read history file: {e}")
    return {"uploaded_urls": [], "uploaded_titles": [], "last_apify_run": 0}


def save_history(history):
    try:
        # Avoid duplicate entries in history list
        history["uploaded_urls"] = list(dict.fromkeys(history.get("uploaded_urls", [])))[-1500:]
        history["uploaded_titles"] = list(dict.fromkeys(history.get("uploaded_titles", [])))[-1000:]
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] Could not save history file: {e}")


# ==========================================
# Text & URL Sanitization
# ==========================================
def sanitize_url(u):
    """Strip any markdown link brackets/parentheses or spaces injected during copy-paste"""
    if not u:
        return ""
    u = str(u).strip()
    # If markdown format [text](url) is present, extract the actual URL
    md_match = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\2", u)
    md_match = md_match.replace("[", "").replace("]", "")
    m = re.search(r"https?://[^\s\"'<>)]+", md_match)
    return m.group(0) if m else md_match


def clean_scraped_title(raw_title):
    if not raw_title:
        return ""
    t = re.sub(r"play_arrow\s*\d+:\d+", " ", raw_title, flags=re.IGNORECASE)
    t = re.sub(r"(?:comment|visibility|remove_red_eye)\s*[\d,]*", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"^\s*\d+:\d+\s*", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def normalize_persian(text):
    if not text:
        return ""
    t = text.replace("ي", "ی").replace("ك", "ک").replace("ة", "ه").replace("‌", " ")
    t = re.sub(r"#\S+", " ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def is_duplicate_title(candidate_title, existing_titles_set):
    norm_cand = normalize_persian(candidate_title)
    if not norm_cand or len(norm_cand) < 5:
        return False
    cand_words = [w for w in norm_cand.split() if len(w) > 2]
    for ex in existing_titles_set:
        norm_ex = normalize_persian(ex)
        if not norm_ex:
            continue
        if norm_cand == norm_ex or norm_cand in norm_ex or norm_ex in norm_cand:
            return True
        if len(cand_words) >= 4:
            ex_words = set(norm_ex.split())
            overlap = sum(1 for w in cand_words if w in ex_words)
            if overlap / len(cand_words) >= 0.8:
                return True
    return False


def is_match_summary(title):
    t = normalize_persian(title)
    return any(normalize_persian(k) in t for k in SUMMARY_KEYWORDS)


def is_safe_by_keywords(title, tags_text=""):
    combined = normalize_persian(f"{title} {tags_text}")
    for kw in FORBIDDEN_KEYWORDS:
        if normalize_persian(kw) in combined:
            print(f"[SKIP] Forbidden keyword detected ('{kw}'): {title[:55]}")
            return False
    return True


# ==========================================
# YouTube Authentication & Quota Check
# ==========================================
def get_authenticated_service():
    yt_scope = PROTO + "www.googleapis.com/auth/youtube"
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
            raise RuntimeError("[ERROR] Valid YouTube token not found!")

    return build("youtube", "v3", credentials=creds)


def get_channel_status_and_24h_count(youtube):
    """Count uploads since 08:00 UTC (11:30 AM Iran) and collect titles + RefTitles"""
    existing_titles = set()
    uploads_since_reset = 0

    try:
        now_utc = datetime.now(timezone.utc)
        reset_time = now_utc.replace(hour=8, minute=0, second=0, microsecond=0)
        if now_utc < reset_time:
            reset_time -= timedelta(days=1)

        ch_res = youtube.channels().list(part="contentDetails", mine=True).execute()
        items = ch_res.get("items", [])
        if not items:
            return existing_titles, 0

        uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

        pl_req = youtube.playlistItems().list(
            part="snippet",
            playlistId=uploads_playlist_id,
            maxResults=50
        )
        pl_res = pl_req.execute()

        for item in pl_res.get("items", []):
            snippet = item.get("snippet", {})
            title = snippet.get("title", "").strip()
            desc = snippet.get("description", "")
            pub_str = snippet.get("publishedAt", "")

            if title:
                existing_titles.add(title)

            for line in desc.splitlines():
                if line.strip().startswith("RefTitle:"):
                    orig_ref = line.split("RefTitle:", 1)[1].strip()
                    if orig_ref:
                        existing_titles.add(orig_ref)

            if pub_str:
                pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                if pub_dt >= reset_time:
                    uploads_since_reset += 1

        print(
            f"[STATUS] Channel indexed titles: {len(existing_titles)} | "
            f"Uploads since 11:30 AM reset: {uploads_since_reset}/{MAX_DAILY_UPLOADS} "
            f"(Reserved for Match Summaries: {RESERVED_FOR_SUMMARIES})"
        )
        return existing_titles, uploads_since_reset

    except Exception as e:
        print(f"[WARN] Failed to read channel upload history: {e}")
        return existing_titles, 0


# ==========================================
# Gemini AI: Copyright Audit + SEO Title/Description/Tags
# ==========================================
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
You are an elite YouTube SEO Strategist and Copyright Auditor for the Iranian football channel 'Footballi' (فوتبالی).

Analyze each candidate video below and perform TWO tasks:

TASK 1 - STRICT COPYRIGHT AUDIT:
- APPROVE ("safe": true): Iranian domestic football ONLY (Persian Gulf Pro League / لیگ برتر خلیج فارس, Hazfi Cup / جام حذفی, Azadegan League, Women's League, Iran National Team press conferences/interviews, domestic player/coach/manager interviews, refereeing analysis, studio shows, fan reactions).
- REJECT ("safe": false):
  1. ANY European football match highlights or broadcast footage (Champions League, LaLiga, Premier League, Serie A, Bundesliga, Real Madrid, Barcelona, Man City, etc.).
  2. ANY AFC Champions League Elite / ACL Two / Asian Champions League match footage or highlights (even if Iranian teams like Esteghlal, Persepolis, Tractor, or Sepahan are playing!).

TASK 2 - HIGH-CTR YOUTUBE SEO TITLE, DESCRIPTION & TAGS (in Persian, for safe videos):
- "seo_title": Write a captivating, high-CTR, SEO-optimized Persian YouTube title (max 85 characters).
  * Keep exact team names, match scores, and person names accurate.
  * Make it intriguing and clickable for Iranian football fans using 1 or 2 fitting emojis (🔥, ⚽, 🚨, 💬).
  * If it is a match summary, start with "خلاصه بازی" and include the score + "لیگ برتر ایران".
  * If "is_short" is true, append " #shorts" at the end of the title.
- "seo_description": Write a structured, keyword-rich Persian description (4-5 lines):
  * Paragraph 1: Engaging 2-sentence explanation of the video containing high-search Persian keywords (names of teams, coaches, players, stadium, league week).
  * Paragraph 2: Call to action: "برای تماشای سریع‌ترین خلاصه بازی‌ها، حواشی داغ و مصاحبه‌های اختصاصی فوتبال ایران، کانال فوتبالی را سابسکرایب کنید، ویدیو را لایک کنید و نظرتان را در کامنت‌ها بنویسید! 🔔👍⚽"
  * Paragraph 3: 6 to 8 relevant hashtags (e.g. #فوتبال_ایران #لیگ_برتر #فوتبالی #استقلال #پرسپولیس #تراکتور #سپاهان #خلاصه_بازی).
- "seo_tags": A list of 10 high-traffic Persian search keywords specific to this video.
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

    gemini_base = PROTO + "generativelanguage.googleapis.com/v1beta"
    models_to_try = [
        "models/gemini-flash-lite-latest",
        "models/gemini-3.5-flash-lite",
        "models/gemini-flash-latest",
        "models/gemini-2.0-flash"
    ]

    try:
        r_m = requests.get(
            sanitize_url(f"{gemini_base}/models?key={api_key}"),
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
            url = sanitize_url(f"{gemini_base}/{model_name}:generateContent?key={api_key}")
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.25,
                    "responseMimeType": "application/json"
                }
            }
            resp = requests.post(url, json=payload, timeout=50)
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


# ==========================================
# Website & Instagram Reel Scraping (High-Res)
# ==========================================
def scrape_website_candidates(history, existing_titles):
    candidates = []
    seen_urls = set(history.get("uploaded_urls", []))

    for raw_page_url in VIDEO_LIST_URLS:
        page_url = sanitize_url(raw_page_url)
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=20)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/video/" not in href:
                    continue
                full_url = sanitize_url(urljoin(BASE_URL, href).split("?")[0].rstrip("/"))
                if full_url in seen_urls:
                    continue

                title_attr = a.get("title") or a.get_text(" ", strip=True)
                clean_t = clean_scraped_title(title_attr)
                if not clean_t or len(clean_t) < 8:
                    continue

                if is_duplicate_title(clean_t, existing_titles):
                    seen_urls.add(full_url)
                    history["uploaded_urls"].append(full_url)
                    continue

                if not is_safe_by_keywords(clean_t):
                    seen_urls.add(full_url)
                    history["uploaded_urls"].append(full_url)
                    continue

                seen_urls.add(full_url)
                candidates.append({
                    "url": full_url,
                    "title": clean_t,
                    "source": "web",
                    "is_short": False,
                    "is_summary": is_match_summary(clean_t)
                })
        except Exception as e:
            print(f"[WARN] Failed scraping {page_url}: {e}")

    save_history(history)
    return candidates


def extract_video_details_from_page(page_url):
    clean_page_url = sanitize_url(page_url)
    try:
        resp = requests.get(clean_page_url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return None
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        h1 = soup.find("h1")
        title = clean_scraped_title(h1.get_text(" ", strip=True)) if h1 else ""

        tags_list = [t.get_text(strip=True) for t in soup.select("a[href*='/tag/']")]
        tags_text = " ".join(tags_list)

        if not is_safe_by_keywords(title, tags_text):
            return None

        mp4_matches = re.findall(r'https?://[^\s"\'<>]+\.mp4', html)
        mp4_url = None
        if mp4_matches:
            for q in ["1080", "720", "high", "hd"]:
                for m in mp4_matches:
                    if q in m.lower():
                        mp4_url = m
                        break
                if mp4_url:
                    break
            if not mp4_url:
                mp4_url = mp4_matches[0]

        if not mp4_url:
            vid_tag = soup.find("video")
            if vid_tag and vid_tag.get("src"):
                mp4_url = vid_tag["src"]

        thumb_url = None
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            thumb_url = sanitize_url(og_img["content"])

        if not mp4_url:
            return None

        return {
            "mp4_url": sanitize_url(mp4_url),
            "title": title,
            "tags_text": tags_text,
            "thumb_url": thumb_url
        }
    except Exception as e:
        print(f"[WARN] Error extracting details from {clean_page_url}: {e}")
        return None


def pick_best_instagram_video_url(item):
    """Select the highest resolution/bitrate stream available from Apify output"""
    versions = item.get("videoVersions") or item.get("video_versions") or []
    if isinstance(versions, list) and versions:
        best = max(
            versions,
            key=lambda v: int(v.get("width", 0)) * int(v.get("height", 0))
        )
        if best.get("url"):
            return sanitize_url(best["url"])
    return sanitize_url(item.get("videoUrl") or item.get("video_url") or "")


def fetch_instagram_reels(history, existing_titles):
    apify_token = os.environ.get("APIFY_TOKEN", "").strip()
    if not apify_token:
        return []

    last_run = history.get("last_apify_run", 0)
    hours_passed = (time.time() - last_run) / 3600.0
    if hours_passed < APIFY_COOLDOWN_HOURS:
        rem = APIFY_COOLDOWN_HOURS - hours_passed
        print(f"[APIFY] Cooldown active ({rem:.1f}h remaining before next Instagram check).")
        return []

    print("[APIFY] Fetching latest Instagram Reels from @footballiapp...")
    apify_endpoint = (
        PROTO
        + "[api.apify.com/v2/acts/apify~instagram-reel-scraper/run-sync-get-dataset-items?token=](https://api.apify.com/v2/acts/apify~instagram-reel-scraper/run-sync-get-dataset-items?token=)"
        + apify_token
    )
    api_url = sanitize_url(apify_endpoint)
    payload = {
        "username": ["footballiapp"],
        "resultsLimit": 6
    }

    try:
        resp = requests.post(api_url, json=payload, timeout=90)
        history["last_apify_run"] = time.time()
        save_history(history)

        if resp.status_code not in (200, 201):
            print(f"[WARN] Apify returned HTTP {resp.status_code}")
            return []

        items = resp.json()
        reels = []
        seen_urls = set(history.get("uploaded_urls", []))

        for item in items:
            video_url = pick_best_instagram_video_url(item)
            short_code = item.get("shortCode") or item.get("id") or video_url
            caption = item.get("caption") or ""
            first_line = clean_scraped_title(caption.splitlines()[0]) if caption else "ویدیوی کوتاه فوتبالی"

            unique_id = f"ig_{short_code}"
            if not video_url or unique_id in seen_urls:
                continue

            if is_duplicate_title(first_line, existing_titles):
                seen_urls.add(unique_id)
                history["uploaded_urls"].append(unique_id)
                continue

            if not is_safe_by_keywords(first_line, caption):
                seen_urls.add(unique_id)
                history["uploaded_urls"].append(unique_id)
                continue

            reels.append({
                "url": unique_id,
                "mp4_url": video_url,
                "title": first_line[:90],
                "tags": caption[:200],
                "source": "instagram",
                "is_short": True,
                "is_summary": False
            })
            if len(reels) >= MAX_REELS_PER_RUN:
                break

        save_history(history)
        print(f"[APIFY] Found {len(reels)} new candidate Reels.")
        return reels
    except Exception as e:
        print(f"[WARN] Failed fetching Instagram Reels: {e}")
        return []


# ==========================================
# Video Inspection & Full HD Enhancement for Shorts/Reels
# ==========================================
def inspect_video_file(filepath):
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration",
            "-show_entries", "format=duration",
            "-of", "json",
            filepath
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        data = json.loads(res.stdout)
        streams = data.get("streams", [{}])
        width = int(streams[0].get("width", 1920))
        height = int(streams[0].get("height", 1080))
        dur_str = streams[0].get("duration") or data.get("format", {}).get("duration") or "0"
        duration = float(dur_str)
        return width, height, duration
    except Exception as e:
        print(f"[WARN] ffprobe error: {e}")
        return 1920, 1080, 120.0


def enhance_short_to_1080p(input_path, width, height):
    """Upscale and sharpen low-res vertical Shorts/Reels to crisp 1080x1920 Full HD"""
    if width >= 1080 or height >= 1920:
        print(f"[QUALITY] Short is already Full HD ({width}x{height}).")
        return input_path

    enhanced_path = input_path.replace(".mp4", "_1080p.mp4")
    print(f"[QUALITY] Enhancing Short from {width}x{height} to 1080x1920 Full HD (Lanczos + Unsharp)...")
    vf_filter = (
        "scale=1080:1920:flags=lanczos:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
        "unsharp=3:3:0.6:3:3:0.0"
    )
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", input_path,
        "-vf", vf_filter,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        "-b:v", "4500k",
        "-maxrate", "5500k",
        "-bufsize", "9000k",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-movflags", "+faststart",
        enhanced_path
    ]
    try:
        res = subprocess.run(cmd, timeout=180)
        if res.returncode == 0 and os.path.exists(enhanced_path) and os.path.getsize(enhanced_path) > 50_000:
            os.replace(enhanced_path, input_path)
            print("[QUALITY] Successfully upgraded Short to 1080x1920 Full HD.")
    except Exception as e:
        print(f"[WARN] Enhancement skipped ({e}), using original file.")
        if os.path.exists(enhanced_path):
            os.remove(enhanced_path)
    return input_path


def download_file(url, dest_path):
    clean_u = sanitize_url(url)
    if not clean_u:
        return False
    try:
        with requests.get(clean_u, headers=HEADERS, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
        return os.path.exists(dest_path) and os.path.getsize(dest_path) > 50_000
    except Exception as e:
        print(f"[WARN] Download failed for {clean_u}: {e}")
        return False


# ==========================================
# Playlist & YouTube Upload Execution
# ==========================================
def get_or_create_playlist(youtube, playlist_title, cache):
    if playlist_title in cache:
        return cache[playlist_title]

    try:
        req = youtube.playlists().list(part="snippet", mine=True, maxResults=50)
        res = req.execute()
        for item in res.get("items", []):
            title = item["snippet"]["title"]
            cache[title] = item["id"]
            if title == playlist_title:
                return item["id"]

        body = {
            "snippet": {
                "title": playlist_title,
                "description": f"مجموعه {playlist_title} در کانال رسمی فوتبالی (Footballi)"
            },
            "status": {"privacyStatus": "public"}
        }
        created = youtube.playlists().insert(part="snippet,status", body=body).execute()
        pl_id = created["id"]
        cache[playlist_title] = pl_id
        return pl_id
    except Exception as e:
        print(f"[WARN] Could not get/create playlist '{playlist_title}': {e}")
        return None


def add_video_to_playlist(youtube, video_id, playlist_id):
    if not playlist_id:
        return
    try:
        body = {
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {
                    "kind": "youtube#video",
                    "videoId": video_id
                }
            }
        }
        youtube.playlistItems().insert(part="snippet", body=body).execute()
        print("[PLAYLIST] Video added to target playlist.")
    except Exception as e:
        print(f"[WARN] Failed adding video to playlist: {e}")


def upload_to_youtube(youtube, video_path, thumb_path, title, description, tags):
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": tags[:25],
            "categoryId": "17",  # Sports
            "defaultLanguage": "fa",
            "defaultAudioLanguage": "fa"
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False
        }
    }

    media = MediaFileUpload(video_path, chunksize=1024 * 1024 * 4, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    last_printed_pct = -10
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            if pct - last_printed_pct >= 20:
                print(f"[UPLOAD] Progress: {pct}%")
                last_printed_pct = pct

    video_id = response["id"]
    print(f"[SUCCESS] Uploaded video ID: {video_id}")

    if thumb_path and os.path.exists(thumb_path):
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumb_path)
            ).execute()
            print("[THUMBNAIL] Custom thumbnail applied.")
        except Exception as e:
            print(f"[INFO] Custom thumbnail skipped: {e}")

    return video_id


# ==========================================
# Main Pipeline
# ==========================================
def main():
    print("[START] Footballi AI Bot starting (SEO + Full HD Shorts Engine)...")
    history = load_history()
    youtube = get_authenticated_service()

    existing_titles, uploads_today = get_channel_status_and_24h_count(youtube)
    for t in history.get("uploaded_titles", []):
        existing_titles.add(t)

    if uploads_today >= MAX_DAILY_UPLOADS:
        print("[STOP] Daily quota reached. Waiting until 11:30 AM tomorrow.")
        return

    web_candidates = scrape_website_candidates(history, existing_titles)
    reel_candidates = fetch_instagram_reels(history, existing_titles)

    summaries = [c for c in web_candidates if c.get("is_summary")]
    others = [c for c in web_candidates if not c.get("is_summary")]

    non_summary_cap = MAX_DAILY_UPLOADS - RESERVED_FOR_SUMMARIES
    if uploads_today >= non_summary_cap:
        print("[QUOTA] Reserved summary zone active! Processing Match Summaries only.")
        pool = summaries[:8]
    else:
        pool = (summaries + reel_candidates + others)[:12]

    if not pool:
        print("[IDLE] No new candidate videos found.")
        return

    enriched_pool = []
    for item in pool:
        if item["source"] == "web":
            details = extract_video_details_from_page(item["url"])
            if not details:
                history["uploaded_urls"].append(item["url"])
                continue
            item["mp4_url"] = details["mp4_url"]
            item["title"] = details["title"] or item["title"]
            item["tags"] = details["tags_text"]
            item["thumb_url"] = details["thumb_url"]
            item["is_summary"] = is_match_summary(item["title"])
        enriched_pool.append(item)

    if not enriched_pool:
        save_history(history)
        print("[IDLE] No valid videos remained after page inspection.")
        return

    ai_results = ai_batch_evaluate(enriched_pool)

    playlist_cache = {}
    uploaded_in_this_run = 0

    for idx, item in enumerate(enriched_pool):
        if uploaded_in_this_run >= MAX_UPLOADS_PER_RUN:
            break
        if uploads_today >= MAX_DAILY_UPLOADS:
            break

        is_sum = item.get("is_summary", False)
        if not is_sum and uploads_today >= (MAX_DAILY_UPLOADS - RESERVED_FOR_SUMMARIES):
            print(f"[RESERVE] Skipping non-summary '{item['title'][:40]}' (saving last 3 slots for Match Summaries).")
            continue

        ai_info = ai_results.get(idx)
        if ai_info is not None and not ai_info.get("safe", False):
            print(f"[AI REJECT] ({ai_info.get('reason')}): {item['title']}")
            history["uploaded_urls"].append(item["url"])
            save_history(history)
            continue

        video_file = f"temp_video_{idx}.mp4"
        thumb_file = f"temp_thumb_{idx}.jpg"

        if not download_file(item["mp4_url"], video_file):
            continue

        width, height, duration = inspect_video_file(video_file)
        is_vertical = height > width
        is_short_video = is_vertical or duration <= 180

        if not is_vertical and not is_sum and duration < 180:
            print(f"[FAST SKIP] Horizontal video shorter than 3 mins ({int(duration)}s): {item['title']}")
            os.remove(video_file)
            history["uploaded_urls"].append(item["url"])
            save_history(history)
            continue

        if is_vertical:
            enhance_short_to_1080p(video_file, width, height)

        original_title = item["title"]

        if ai_info:
            seo_title = ai_info.get("seo_title") or original_title
            seo_desc = ai_info.get("seo_description") or original_title
            seo_tags = ai_info.get("seo_tags") or ["فوتبالی", "فوتبال ایران", "لیگ برتر ایران"]
        else:
            seo_title = original_title
            seo_desc = (
                f"{original_title}\n\n"
                "برای تماشای سریع‌ترین خلاصه بازی‌ها و حواشی فوتبال ایران، کانال فوتبالی را سابسکرایب کنید! 🔔⚽\n"
                "#فوتبال_ایران #لیگ_برتر #فوتبالی"
            )
            seo_tags = ["فوتبالی", "فوتبال ایران", "لیگ برتر ایران", "خلاصه بازی", "ورزش"]

        if is_short_video:
            target_playlist = "ویدیوهای کوتاه (Shorts)"
            if "#shorts" not in seo_title.lower():
                seo_title = f"{seo_title[:88]} #shorts"
            thumb_to_use = None
        else:
            seo_title = re.sub(r"#shorts", "", seo_title, flags=re.IGNORECASE).strip()
            if is_sum:
                target_playlist = "خلاصه بازی‌ها"
            else:
                target_playlist = "مصاحبه و حواشی فوتبال ایران"

            thumb_to_use = None
            if item.get("thumb_url") and download_file(item["thumb_url"], thumb_file):
                thumb_to_use = thumb_file

        final_description = (
            f"{seo_desc}\n\n"
            f"🌐 رسانه رسمی فوتبالی | Footballi\n"
            f"RefTitle: {original_title}"
        )

        try:
            print(f"[UPLOAD] Publishing with SEO Title: {seo_title}")
            vid_id = upload_to_youtube(
                youtube,
                video_file,
                thumb_to_use,
                seo_title,
                final_description,
                seo_tags
            )

            pl_id = get_or_create_playlist(youtube, target_playlist, playlist_cache)
            add_video_to_playlist(youtube, vid_id, pl_id)

            history["uploaded_urls"].append(item["url"])
            history["uploaded_titles"].append(original_title)
            history["uploaded_titles"].append(seo_title)
            save_history(history)

            existing_titles.add(original_title)
            existing_titles.add(seo_title)
            uploads_today += 1
            uploaded_in_this_run += 1

        except HttpError as e:
            err_str = str(e)
            print("[ERROR] YouTube API Upload error:", err_str, "- skipping.")
            if "uploadLimitExceeded" in err_str:
                print("[STOP] YouTube daily upload limit reached for this account.")
                break
        except Exception as e:
            print("[ERROR] Unexpected upload error:", str(e), "- skipping.")
        finally:
            if os.path.exists(video_file):
                os.remove(video_file)
            if os.path.exists(thumb_file):
                os.remove(thumb_file)

    print(f"[DONE] Run completed. Total videos uploaded in this run: {uploaded_in_this_run}")


if __name__ == "__main__":
    main()
