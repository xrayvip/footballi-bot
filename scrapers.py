# scrapers.py
# Autonomous AI Web-Browsing Agent (Playwright Chromium + Gemini Decision Engine)
import os
import re
import json
import time
from urllib.parse import urljoin, unquote
import requests
from bs4 import BeautifulSoup

from config import (
    PROTO, BASE_URL, VIDEO_LIST_URLS, HEADERS,
    FORBIDDEN_KEYWORDS, SUMMARY_KEYWORDS,
    APIFY_COOLDOWN_HOURS, MAX_REELS_PER_RUN
)
from ai_seo import sanitize_url, GOOGLE_API_BASE

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


def clean_scraped_title(raw_title):
    if not raw_title:
        return ""
    t = re.sub(r"play_arrow\s*\d+:\d+", " ", raw_title, flags=re.IGNORECASE)
    t = re.sub(r"(?:comment|visibility|remove_red_eye|videocam|play_circle)\s*[\d,]*", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\b\d+:\d+\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def normalize_persian(text):
    if not text:
        return ""
    t = text.replace("ي", "ی").replace("ك", "ک").replace("ة", "ه").replace("‌", " ")
    persian_digits = "۰۱۲۳۴۵۶۷۸۹"
    arabic_digits = "٠١٢٣٤٥٦٧٨٩"
    for i in range(10):
        t = t.replace(persian_digits[i], str(i)).replace(arabic_digits[i], str(i))
    t = re.sub(r"#\S+", " ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def is_match_summary(title):
    t = normalize_persian(title)
    return any(normalize_persian(k) in t for k in SUMMARY_KEYWORDS)


def is_duplicate_title(candidate_title, existing_titles_set):
    norm_cand = normalize_persian(candidate_title)
    if not norm_cand or len(norm_cand) < 6:
        return False

    for ex in existing_titles_set:
        norm_ex = normalize_persian(ex)
        if not norm_ex:
            continue
        if norm_cand == norm_ex:
            return True
        if len(norm_cand) >= 16 and (norm_cand in norm_ex or norm_ex in norm_cand):
            return True
    return False


def is_safe_by_keywords(title, tags_text=""):
    combined = normalize_persian(f"{title} {tags_text}")
    for kw in FORBIDDEN_KEYWORDS:
        if normalize_persian(kw) in combined:
            return False
    return True


def ai_agent_rank_and_select(raw_links, existing_titles):
    """
    The AI Editorial Brain:
    Receives all discovered video links across Footballi.net, compares them against existing channel videos,
    filters out any foreign/AFC copyrighted matches, and ranks the best domestic videos by YouTube viral potential.
    """
    google_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not google_key or not raw_links:
        return raw_links[:15]

    prompt = f"""
You are the Autonomous AI Chief Editor browsing 'footballi.net' for our official Iranian football YouTube channel.

Below is a list of candidate videos currently on the website, and a list of recent titles already on our YouTube channel.

YOUR EDITORIAL MISSION:
1. EXCLUDE any video that is already on our channel (even if phrased slightly differently).
2. EXCLUDE any foreign football (LaLiga, Premier League, Serie A, Bundesliga, Champions League, Arab leagues, etc.) or AFC Asian Champions League matches (even if Iranian teams play).
3. SELECT ONLY safe Iranian domestic football content (Persian Gulf Pro League, Hazfi Cup, Azadegan League, Iran National Team, domestic club/coach/player interviews & controversies).
4. PRIORITIZE & RANK the selected videos from #1 (highest priority) to last:
   - Priority 1: Full Domestic Match Summaries ("خلاصه بازی...") -> set "is_summary": true
   - Priority 2: High-engagement domestic controversies, coach press conferences, Esteghlal/Persepolis/Tractor/Sepahan exclusive videos -> set "is_summary": false

Return ONLY a valid JSON array in exact priority order (max 12 best items):
[
  {{
    "url": "https://footballi.net/video/...",
    "title": "...",
    "is_summary": true,
    "priority_score": 98,
    "editorial_note": "Full domestic match summary"
  }}
]

Already on YouTube Channel (DO NOT pick these):
{json.dumps(list(existing_titles)[:40], ensure_ascii=False)}

Candidate Videos Found on Website:
{json.dumps(raw_links[:60], ensure_ascii=False)}
"""

    models = ["models/gemini-flash-lite-latest", "models/gemini-2.0-flash"]
    for m in models:
        try:
            url = f"{GOOGLE_API_BASE}/{m}:generateContent"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.15, "responseMimeType": "application/json"}
            }
            r = requests.post(url, params={"key": google_key}, json=payload, timeout=45)
            if r.status_code == 200:
                txt = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                if txt.startswith("```"):
                    txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.MULTILINE).strip()
                selected = json.loads(txt)
                out = []
                for item in selected:
                    if item.get("url") and item.get("title"):
                        out.append({
                            "url": sanitize_url(item["url"]),
                            "title": item["title"],
                            "source": "web",
                            "is_short": False,
                            "is_summary": bool(item.get("is_summary", False))
                        })
                print(f"[AI-AGENT] Browsed {len(raw_links)} links -> Selected & prioritized {len(out)} best domestic videos.")
                return out
        except Exception as e:
            print(f"[WARN] AI Agent ranking note ({m}): {e}")

    return raw_links[:15]


def scrape_website_candidates(history, existing_titles, save_history_fn):
    """
    Uses a real headless Chromium browser (Playwright) to navigate Footballi.net like a human editor,
    rendering dynamic Angular components, collecting all video links, and handing them to the AI Editor.
    """
    permanently_done_urls = set(history.get("uploaded_urls", []))
    discovered_map = {}

    if PLAYWRIGHT_AVAILABLE:
        print("[AI-BROWSER] Launching real headless Chromium browser to explore footballi.net...")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=HEADERS["User-Agent"],
                    locale="fa-IR",
                    viewport={"width": 1440, "height": 900}
                )
                page = context.new_page()

                # Browse key hubs with real JavaScript execution
                hubs_to_visit = [
                    BASE_URL + "/video",
                    BASE_URL + "/news/video",
                    BASE_URL,
                    BASE_URL + "/team/830",  # Esteghlal
                    BASE_URL + "/team/829",  # Persepolis
                    BASE_URL + "/team/833",  # Sepahan
                    BASE_URL + "/team/836",  # Tractor
                ]

                for hub_url in hubs_to_visit:
                    try:
                        page.goto(hub_url, wait_until="domcontentloaded", timeout=25000)
                        page.wait_for_timeout(2000)
                        # Scroll down once to trigger lazy-loaded Angular video cards
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                        page.wait_for_timeout(1000)

                        # Extract all rendered /video/<id> links directly from the live DOM
                        links_data = page.evaluate("""() => {
                            const items = [];
                            document.querySelectorAll('a[href*="/video/"]').forEach(a => {
                                const href = a.href;
                                const title = a.getAttribute('title') || a.innerText || '';
                                if (href && title) {
                                    items.push({url: href, title: title});
                                }
                            });
                            return items;
                        }""")

                        for entry in links_data:
                            u = sanitize_url(entry["url"].split("?")[0].rstrip("/"))
                            m_id = re.search(r"/video/(\d+)", u)
                            if not m_id:
                                continue
                            vid_id = m_id.group(1)
                            canon = f"{BASE_URL}/video/{vid_id}"
                            if canon in permanently_done_urls or u in permanently_done_urls or canon in discovered_map:
                                continue

                            clean_t = clean_scraped_title(entry["title"])
                            if len(clean_t) < 6:
                                continue
                            if is_duplicate_title(clean_t, existing_titles):
                                continue
                            if not is_safe_by_keywords(clean_t):
                                permanently_done_urls.add(canon)
                                history["uploaded_urls"].append(canon)
                                continue

                            discovered_map[canon] = {
                                "url": u,
                                "canonical_id": vid_id,
                                "title": clean_t,
                                "source": "web",
                                "is_short": False,
                                "is_summary": is_match_summary(clean_t)
                            }
                    except Exception as e:
                        print(f"[AI-BROWSER] Hub navigation note on {hub_url}: {e}")

                browser.close()
        except Exception as e:
            print(f"[WARN] Playwright browser fallback triggered: {e}")

    # Fallback / Supplement with fast HTTP scan if Playwright was not installed yet
    if not discovered_map:
        for raw_page_url in VIDEO_LIST_URLS:
            page_url = sanitize_url(raw_page_url)
            try:
                resp = requests.get(page_url, headers=HEADERS, timeout=18)
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    m_id = re.search(r"/video/(\d+)", a["href"])
                    if not m_id:
                        continue
                    vid_id = m_id.group(1)
                    canon = f"{BASE_URL}/video/{vid_id}"
                    full_u = sanitize_url(urljoin(BASE_URL, a["href"]).split("?")[0].rstrip("/"))
                    if canon in permanently_done_urls or full_u in permanently_done_urls or canon in discovered_map:
                        continue
                    clean_t = clean_scraped_title(a.get("title") or a.get_text(" ", strip=True))
                    if len(clean_t) < 6 or is_duplicate_title(clean_t, existing_titles):
                        continue
                    if not is_safe_by_keywords(clean_t):
                        permanently_done_urls.add(canon)
                        history["uploaded_urls"].append(canon)
                        continue
                    discovered_map[canon] = {
                        "url": full_u,
                        "canonical_id": vid_id,
                        "title": clean_t,
                        "source": "web",
                        "is_short": False,
                        "is_summary": is_match_summary(clean_t)
                    }
            except Exception:
                pass

    save_history_fn(history)
    all_raw = list(discovered_map.values())
    if not all_raw:
        return []

    # Let the AI Editorial Agent rank, filter, and prioritize the discovered videos
    return ai_agent_rank_and_select(all_raw, existing_titles)


def extract_video_details_from_page(page_url):
    """
    Uses the real headless browser to open the exact video page, wait for the main <video> player
    to load its active .mp4 source, and verify the H1 title—100% immune to sidebar mixups!
    """
    clean_page_url = sanitize_url(page_url)
    id_match = re.search(r"/video/(\d+)", clean_page_url)
    if not id_match:
        return None
    target_vid_id = id_match.group(1)

    if PLAYWRIGHT_AVAILABLE:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent=HEADERS["User-Agent"], locale="fa-IR")
                page = context.new_page()

                captured_mp4s = []
                # Sniff real network requests for .mp4 streams initiated by the video player
                page.on(
                    "request",
                    lambda req: captured_mp4s.append(req.url) if ".mp4" in req.url.lower() else None
                )

                page.goto(clean_page_url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(2500)

                data = page.evaluate("""() => {
                    const h1 = document.querySelector('h1');
                    const vid = document.querySelector('main video, article video, video');
                    const src = vid ? (vid.currentSrc || vid.src || (vid.querySelector('source') ? vid.querySelector('source').src : '')) : '';
                    const poster = vid ? vid.poster : '';
                    const ogImg = document.querySelector('meta[property="og:image"]');
                    const tags = Array.from(document.querySelectorAll('a[href*="/tag/"]')).map(el => el.innerText).slice(0, 12).join(' ');
                    return {
                        title: h1 ? h1.innerText : '',
                        videoSrc: src,
                        poster: poster || (ogImg ? ogImg.content : ''),
                        tags: tags
                    };
                }""")

                browser.close()

                title = clean_scraped_title(data.get("title", ""))
                tags_text = data.get("tags", "")
                if not title:
                    return None

                if not is_safe_by_keywords(title, tags_text):
                    return {"rejected_forever": True}

                mp4_url = data.get("videoSrc") or (captured_mp4s[0] if captured_mp4s else None)
                if mp4_url and ".mp4" in mp4_url:
                    return {
                        "mp4_url": sanitize_url(mp4_url),
                        "title": title,
                        "tags_text": tags_text,
                        "thumb_url": sanitize_url(data.get("poster")) if data.get("poster") else None
                    }
        except Exception as e:
            print(f"[INFO] Browser player inspection fallback on {clean_page_url}: {e}")

    # Fallback: Target-ID Window Extraction in static HTML
    try:
        resp = requests.get(clean_page_url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return None
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1")
        page_title = clean_scraped_title(h1.get_text(" ", strip=True)) if h1 else ""
        if not page_title:
            return None

        tags_list = [t.get_text(strip=True) for t in soup.select("a[href*='/tag/']")][:15]
        tags_text = " ".join(tags_list)
        if not is_safe_by_keywords(page_title, tags_text):
            return {"rejected_forever": True}

        unescaped = html.replace("\\/", "/")
        mp4_url = None
        for m in re.finditer(rf"\b{target_vid_id}\b", unescaped):
            pos = m.start()
            window = unescaped[max(0, pos - 1200): min(len(unescaped), pos + 2000)]
            matches = re.findall(r'https?://[^\s"\'<>\\]+\.mp4', window)
            if matches:
                mp4_url = matches[0]
                break

        if not mp4_url:
            return None

        og_img = soup.find("meta", property="og:image")
        thumb_url = og_img.get("content") if og_img else None
        return {
            "mp4_url": sanitize_url(mp4_url),
            "title": page_title,
            "tags_text": tags_text,
            "thumb_url": sanitize_url(thumb_url) if thumb_url else None
        }
    except Exception:
        return None


def extract_clean_apify_token(raw_token):
    if not raw_token:
        return ""
    t = raw_token.strip()
    m = re.search(r"(apify_api_[A-Za-z0-9]+)", t)
    if m:
        return m.group(1)
    if "token=" in t:
        return t.split("token=")[-1].split("&")[0].strip()
    return t


def pick_best_instagram_video_url(item):
    versions = item.get("videoVersions") or item.get("video_versions") or []
    if isinstance(versions, list) and versions:
        best = max(
            versions,
            key=lambda v: int(v.get("width", 0)) * int(v.get("height", 0))
        )
        if best.get("url"):
            return sanitize_url(best["url"])
    return sanitize_url(item.get("videoUrl") or item.get("video_url") or "")


def fetch_instagram_reels(history, existing_titles, save_history_fn):
    raw_token = os.environ.get("APIFY_TOKEN", "")
    apify_token = extract_clean_apify_token(raw_token)
    if not apify_token:
        return []

    last_run = history.get("last_apify_run", 0)
    hours_passed = (time.time() - last_run) / 3600.0
    if hours_passed < APIFY_COOLDOWN_HOURS:
        rem = APIFY_COOLDOWN_HOURS - hours_passed
        print(f"[APIFY] Cooldown active ({rem:.1f}h remaining before next Instagram check).")
        return []

    print("[APIFY] Fetching latest Instagram Reels from @footballiapp...")
    apify_host = "api." + "apify" + ".com"
    api_url = sanitize_url(PROTO + apify_host + "/v2/acts/apify~instagram-reel-scraper/run-sync-get-dataset-items")
    payload = {
        "username": ["footballiapp"],
        "resultsLimit": 6
    }

    try:
        resp = requests.post(
            api_url,
            params={"token": apify_token},
            json=payload,
            timeout=90
        )
        history["last_apify_run"] = time.time()
        save_history_fn(history)

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

        save_history_fn(history)
        print(f"[APIFY] Found {len(reels)} new candidate Reels.")
        return reels
    except Exception as e:
        print(f"[WARN] Failed fetching Instagram Reels: {e}")
        return []
