# scrapers.py
import os
import re
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
    t = re.sub(r"^\s*\d+:\d+\s*", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def normalize_persian(text):
    if not text:
        return ""
    t = text.replace("ي", "ی").replace("ك", "ک").replace("ة", "ه").replace("‌", " ")
    # Convert Persian/Arabic digits to English digits for consistent score matching
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
    if any(normalize_persian(k) in t for k in SUMMARY_KEYWORDS):
        return True
    # Also detect score patterns like "تراکتور 2 0 خیبر" or "استقلال 1 1"
    if re.search(r"\b\d+\s+\d+\b", t) and ("خلاصه" in t or "بازی" in t or "گل" in t):
        return True
    return False


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
        # Only treat as substring duplicate if the full candidate title is inside an existing title
        if len(norm_cand) >= 18 and norm_cand in norm_ex:
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
    """Extracts the cleanest title from an <a> tag, its attributes, or parent card headings"""
    for attr in ["title", "aria-label", "data-title"]:
        val = a_tag.get(attr)
        if val and len(clean_scraped_title(val)) >= 6:
            return clean_scraped_title(val)

    # Check inner heading tags (h1, h2, h3, h4, span.title)
    for h_tag in a_tag.find_all(["h1", "h2", "h3", "h4", "h5", "p", "span"]):
        txt = clean_scraped_title(h_tag.get_text(" ", strip=True))
        if len(txt) >= 8:
            return txt

    # Fallback to full text inside <a>
    txt = clean_scraped_title(a_tag.get_text(" ", strip=True))
    if len(txt) >= 6:
        return txt

    # Check parent container if <a> is just an overlay link
    parent = a_tag.parent
    if parent:
        txt = clean_scraped_title(parent.get_text(" ", strip=True))
        if 6 <= len(txt) <= 140:
            return txt

    return ""


def scrape_website_candidates(history, existing_titles, save_history_fn):
    """
    Deep site-wide crawler across Footballi.net:
    1. Scans all configured listing/tag/competition URLs.
    2. Captures both /video/<id> links AND /news/<id> articles on summary/video pages.
    3. Prioritizes Match Summaries first.
    """
    candidates = []
    seen_in_this_scan = set()
    permanently_done_urls = set(history.get("uploaded_urls", []))

    for raw_page_url in VIDEO_LIST_URLS:
        page_url = sanitize_url(raw_page_url)
        decoded_page_url = unquote(page_url)
        is_summary_hub = ("خلاصه" in decoded_page_url)
        is_video_hub = ("/news/video" in decoded_page_url or decoded_page_url.endswith("/video"))

        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=20)
            if resp.status_code != 200:
                print(f"[WARN] HTTP {resp.status_code} on {decoded_page_url}")
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            page_matches = 0

            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not href or href.startswith("#") or href.startswith("javascript:"):
                    continue

                # Determine if link points to a video page or a news article on a video/summary hub
                is_vid_link = ("/video/" in href)
                is_news_article = bool(re.search(r"/news/\d+", href))

                if not is_vid_link and not (is_news_article and (is_summary_hub or is_video_hub)):
                    continue

                full_url = sanitize_url(urljoin(BASE_URL, href).split("?")[0].rstrip("/"))

                # Skip navigation/category hubs
                if any(full_url.endswith(s) for s in ["/video", "/news/video", "/news/latest", "/news"]):
                    continue
                if "خلاصه-بازی" in unquote(full_url) and "/news/" in full_url and not re.search(r"/news/\d+", full_url):
                    continue

                if full_url in seen_in_this_scan or full_url in permanently_done_urls:
                    continue

                clean_t = extract_title_for_link(a)
                if not clean_t or len(clean_t) < 6:
                    continue

                # If it's a /news/<id> link from the general hub, only take it if it's a summary or video
                is_sum = is_summary_hub or is_match_summary(clean_t)

                if is_duplicate_title(clean_t, existing_titles):
                    seen_in_this_scan.add(full_url)
                    continue

                if not is_safe_by_keywords(clean_t):
                    seen_in_this_scan.add(full_url)
                    permanently_done_urls.add(full_url)
                    history["uploaded_urls"].append(full_url)
                    continue

                seen_in_this_scan.add(full_url)
                page_matches += 1
                candidates.append({
                    "url": full_url,
                    "title": clean_t,
                    "source": "web",
                    "is_short": False,
                    "is_summary": is_sum
                })

            if page_matches > 0:
                print(f"[CRAWLER] Found {page_matches} fresh candidates on {decoded_page_url}")

        except Exception as e:
            print(f"[WARN] Failed crawling {decoded_page_url}: {e}")

    save_history_fn(history)
    return candidates


def extract_best_poster_url(soup, html):
    candidates = []

    vid_tag = soup.find("video")
    if vid_tag and vid_tag.get("poster"):
        candidates.append(vid_tag.get("poster"))

    js_posters = re.findall(
        r'poster\s*[:=]\s*["\'](https?://[^"\']+\.(?:jpg|jpeg|png|webp))["\']',
        html,
        flags=re.IGNORECASE
    )
    candidates.extend(js_posters)

    for meta_prop in ["og:image", "twitter:image"]:
        m_tag = soup.find("meta", property=meta_prop) or soup.find("meta", attrs={"name": meta_prop})
        if m_tag and m_tag.get("content"):
            candidates.append(m_tag.get("content"))

    for raw_img in candidates:
        u = sanitize_url(raw_img)
        if not u or "logo" in u.lower() or "default" in u.lower() or "avatar" in u.lower():
            continue
        return u
    return None


def extract_video_details_from_page(page_url):
    """
    Deep extractor for both /video/<id> and /news/<id> pages on Footballi.net:
    1. Scans direct .mp4 links in HTML/JS/JSON-LD (prefers 1080p > 720p).
    2. If a /news/<id> article links to an embedded /video/<id> iframe/page, follows it automatically!
    """
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
            return {"rejected_forever": True}

        # Unescape any JSON-escaped slashes (https:\/\/...)
        unescaped_html = html.replace("\\/", "/")
        mp4_matches = re.findall(r'https?://[^\s"\'<>\\]+\.mp4', unescaped_html)

        # If this is a /news/ article without direct mp4, check if it embeds a /video/<id> link
        if not mp4_matches and "/news/" in clean_page_url:
            embedded_vid_links = re.findall(r'href=["\'](/video/\d+[^"\']*)["\']', html)
            if embedded_vid_links:
                sub_url = sanitize_url(urljoin(BASE_URL, embedded_vid_links[0]))
                sub_resp = requests.get(sub_url, headers=HEADERS, timeout=15)
                if sub_resp.status_code == 200:
                    sub_html = sub_resp.text.replace("\\/", "/")
                    mp4_matches = re.findall(r'https?://[^\s"\'<>\\]+\.mp4', sub_html)

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

        if not mp4_url:
            return None

        thumb_url = extract_best_poster_url(soup, unescaped_html)

        return {
            "mp4_url": sanitize_url(mp4_url),
            "title": title,
            "tags_text": tags_text,
            "thumb_url": thumb_url
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
