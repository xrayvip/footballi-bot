# scrapers.py
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
from ai_seo import sanitize_url


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
            print(f"[SKIP] Forbidden keyword ('{kw}'): {title[:55]}")
            return False
    return True


def extract_title_for_link(a_tag):
    for attr in ["title", "aria-label", "data-title"]:
        val = a_tag.get(attr)
        if val and len(clean_scraped_title(val)) >= 6:
            return clean_scraped_title(val)

    for h_tag in a_tag.find_all(["h1", "h2", "h3", "h4", "h5", "p", "span"]):
        txt = clean_scraped_title(h_tag.get_text(" ", strip=True))
        if len(txt) >= 8:
            return txt

    txt = clean_scraped_title(a_tag.get_text(" ", strip=True))
    if len(txt) >= 6:
        return txt

    return ""


def scrape_website_candidates(history, existing_titles, save_history_fn):
    candidates = []
    seen_in_this_scan = set()
    permanently_done_urls = set(history.get("uploaded_urls", []))

    for raw_page_url in VIDEO_LIST_URLS:
        page_url = sanitize_url(raw_page_url)
        decoded_page_url = unquote(page_url)

        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=20)
            if resp.status_code != 200:
                print(f"[WARN] HTTP {resp.status_code} on {decoded_page_url}")
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            page_matches = 0
            page_summaries = 0

            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                # Strictly match /video/<numeric_id> links
                vid_id_match = re.search(r"/video/(\d+)", href)
                if not vid_id_match:
                    continue

                vid_num_id = vid_id_match.group(1)
                # Normalize URL by numeric video ID so differently slugged links never duplicate
                canonical_key = f"{BASE_URL}/video/{vid_num_id}"
                full_url = sanitize_url(urljoin(BASE_URL, href).split("?")[0].rstrip("/"))

                if canonical_key in seen_in_this_scan or canonical_key in permanently_done_urls or full_url in permanently_done_urls:
                    continue

                clean_t = extract_title_for_link(a)
                if not clean_t or len(clean_t) < 6:
                    continue

                if is_duplicate_title(clean_t, existing_titles):
                    seen_in_this_scan.add(canonical_key)
                    continue

                if not is_safe_by_keywords(clean_t):
                    seen_in_this_scan.add(canonical_key)
                    permanently_done_urls.add(full_url)
                    history["uploaded_urls"].append(full_url)
                    history["uploaded_urls"].append(canonical_key)
                    continue

                is_sum = is_match_summary(clean_t)
                if is_sum:
                    page_summaries += 1

                seen_in_this_scan.add(canonical_key)
                page_matches += 1
                candidates.append({
                    "url": full_url,
                    "canonical_id": vid_num_id,
                    "title": clean_t,
                    "source": "web",
                    "is_short": False,
                    "is_summary": is_sum
                })

            if page_matches > 0:
                print(f"[CRAWLER] {decoded_page_url} -> {page_matches} videos ({page_summaries} Match Summaries)")

        except Exception as e:
            print(f"[WARN] Failed crawling {decoded_page_url}: {e}")

    save_history_fn(history)
    return candidates


def extract_video_details_from_page(page_url):
    """
    Precision extractor for Footballi's Angular SSR pages:
    Finds the exact .mp4 stream belonging to THIS video ID (ignoring global featured/sidebar videos).
    """
    clean_page_url = sanitize_url(page_url)
    id_match = re.search(r"/video/(\d+)", clean_page_url)
    if not id_match:
        return None
    target_vid_id = id_match.group(1)

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

        # Only extract tags inside the main article area (not global footer links)
        main_container = soup.find("main") or soup.find("article") or soup
        tags_list = [t.get_text(strip=True) for t in main_container.select("a[href*='/tag/']")]
        tags_text = " ".join(tags_list[:15])

        if not is_safe_by_keywords(page_title, tags_text):
            return {"rejected_forever": True}

        unescaped_html = html.replace("\\/", "/").replace("&quot;", '"')
        mp4_url = None
        thumb_url = None

        # Strategy 1: Look in the HTML/JSON window right around the target video ID (target_vid_id)
        # In Footballi's Angular TransferState, the video object contains `"id":1073641` (or `"videoId":1073641`) right next to its `.mp4` URL!
        for m in re.finditer(rf"\b{target_vid_id}\b", unescaped_html):
            pos = m.start()
            window = unescaped_html[max(0, pos - 1500): min(len(unescaped_html), pos + 2500)]
            window_mp4s = re.findall(r'https?://[^\s"\'<>\\]+\.mp4', window)
            if window_mp4s:
                mp4_url = window_mp4s[0]
                break

        # Strategy 2: Check direct <video> tag in main content (after stripping aside/related)
        if not mp4_url:
            for junk in soup.find_all(["aside", "footer", "nav"]):
                junk.decompose()
            vid_tag = soup.find("video")
            if vid_tag:
                src = vid_tag.get("src") or (vid_tag.find("source", src=True)["src"] if vid_tag.find("source", src=True) else None)
                if src and ".mp4" in src:
                    mp4_url = src
                if vid_tag.get("poster"):
                    thumb_url = vid_tag.get("poster")

        # Strategy 3: Check meta og:video or JSON-LD VideoObject
        if not mp4_url:
            og_vid = soup.find("meta", property="og:video") or soup.find("meta", property="og:video:url") or soup.find("meta", property="og:video:secure_url")
            if og_vid and og_vid.get("content") and ".mp4" in og_vid.get("content", ""):
                mp4_url = og_vid["content"]

        if not mp4_url:
            return None

        if not thumb_url:
            og_img = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
            if og_img and og_img.get("content"):
                thumb_url = og_img.get("content")

        return {
            "mp4_url": sanitize_url(mp4_url),
            "title": page_title,
            "tags_text": tags_text,
            "thumb_url": sanitize_url(thumb_url) if thumb_url else None
        }
    except Exception as e:
        print(f"[WARN] Error extracting details from {clean_page_url}: {e}")
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
