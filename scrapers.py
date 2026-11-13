# -*- coding: utf-8 -*-
import os
import re
import json
import time
import shutil
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from config import (
    HEADERS,
    ENTRY_URLS,
    FOREIGN_BLACKLIST,
    IRANIAN_DOMESTIC_CLUBS,
    IRANIAN_FOOTBALL_ENTITIES,
    APIFY_COOLDOWN_HOURS,
)


def normalize_persian(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"[\u064B-\u065F\u0670]", "", text)
    text = text.replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
    return re.sub(r"\s+", " ", text).strip()


def is_foreign_or_blacklisted(title: str, desc: str = "") -> bool:
    """
    قفل دوگانه ضد کپی‌رایت (Zero-Trust Copyright Filter):
    ۱. هرگونه خلاصه بازی، هایلایت یا گل باید مربوط به باشگاه‌های داخل ایران باشد و هیچ تیم/تورنمنت خارجی یا آسیایی در آن نباشد.
    ۲. هر ویدیویی که حداقل یکی از نام‌های فوتبال داخلی ایران (لیست سفید) را در عنوانش نداشته باشد، فوراً رد می‌شود.
    """
    clean_title = re.sub(r"\|.*$", "", title).strip()
    norm_title = normalize_persian(clean_title).lower()

    # کلمات نشان‌دهنده ویدیوی مسابقه (خلاصه بازی، گل، هایلایت و...)
    match_footage_words = [
        "خلاصه بازی", "خلاصه دیدار", "خلاصه مسابقه", "هایلایت", "بازی کامل",
        "گل اول", "گل دوم", "گل سوم", "گل چهارم", "گل پنجم", "گل های",
        "تمام گل", "کاشته های", "سریعترین گل", "پنالتی های", "کامبک",
    ]
    is_match_footage = any(normalize_persian(w).lower() in norm_title for w in match_footage_words)

    # الف) بررسی لیست سیاه خارجی و آسیایی روی تمام ویدیوهای مسابقه
    for bad_word in FOREIGN_BLACKLIST:
        if normalize_persian(bad_word).lower() in norm_title:
            # فقط اگر ویدیوکست یا پشت خط یا مصاحبه کلامی درباره نماینده ایران در آسیا بود مجاز است، نه خلاصه بازی!
            if not is_match_footage and any(
                normalize_persian(sw).lower() in norm_title
                for sw in ["ویدیوکست", "ویدیو کست", "پشت خط", "نشست خبری", "مصاحبه", "صحبت های"]
            ):
                if any(normalize_persian(club).lower() in norm_title for club in IRANIAN_DOMESTIC_CLUBS + ["تیم ملی ایران"]):
                    continue
            return True

    # ب) اگر ویدیو «خلاصه بازی / گل / هایلایت» است، حتماً باید نام حداقل یک باشگاه ایرانی داخل عنوانش باشد!
    if is_match_footage:
        has_iranian_club = any(normalize_persian(club).lower() in norm_title for club in IRANIAN_DOMESTIC_CLUBS)
        if not has_iranian_club:
            return True

    # ج) قانون لیست سفید سراسری: عنوان ویدیو حتماً باید حداقل یک موجودیت فوتبال داخلی ایران داشته باشد
    has_domestic_entity = any(normalize_persian(ent).lower() in norm_title for ent in IRANIAN_FOOTBALL_ENTITIES)
    if not has_domestic_entity:
        return True

    return False


def find_system_chrome() -> str:
    candidates = [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        shutil.which("google-chrome"),
        shutil.which("chromium-browser"),
        shutil.which("chromium"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return ""


def browser_discover_links(max_pages: int = 8) -> list:
    discovered_urls = []
    seen = set()
    chrome_path = find_system_chrome()

    if not chrome_path:
        return []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    print(f"[AI-BROWSER] Launching pre-installed System Chrome ({chrome_path}) to render footballi.net...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=chrome_path,
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1366, "height": 900},
                locale="fa-IR",
            )
            page = context.new_page()

            def handle_response(response):
                try:
                    if "footballi.net" in response.url and "json" in response.headers.get("content-type", ""):
                        text = response.text()
                        for m in re.finditer(r"/video/(\d+)(?:/[^\s\"\'<>\\]*)?", text):
                            full_u = urljoin("https://footballi.net", m.group(0))
                            vid_id = m.group(1)
                            if vid_id not in seen:
                                seen.add(vid_id)
                                discovered_urls.append(full_u)
                except Exception:
                    pass

            page.on("response", handle_response)

            for url in ENTRY_URLS[:max_pages]:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    for _ in range(4):
                        page.evaluate("window.scrollBy(0, 1400)")
                        page.wait_for_timeout(600)

                    anchors = page.eval_on_selector_all(
                        "a[href*='/video/']",
                        "elements => elements.map(el => ({href: el.href, text: (el.innerText || el.title || '').trim()}))",
                    )
                    for item in anchors:
                        href = item.get("href", "")
                        m = re.search(r"https?://(?:www\.)?footballi\.net/video/(\d+)(?:/[^\s\"\'<>?#]*)?", href)
                        if m:
                            vid_id = m.group(1)
                            if vid_id not in seen:
                                seen.add(vid_id)
                                discovered_urls.append(href.split("?")[0])
                except Exception as e:
                    print(f"[BROWSER-WARN] Timeout/skip on {url}: {e}")

            browser.close()
    except Exception as e:
        print(f"[BROWSER-WARN] Headless Chrome scan note: {e}")

    print(f"[AI-BROWSER] Browser rendered & discovered {len(discovered_urls)} unique video links.")
    return discovered_urls


def extract_video_page_details(page_url: str, session: requests.Session = None) -> dict:
    m_id = re.search(r"/video/(\d+)", page_url)
    if not m_id:
        return None
    video_id = m_id.group(1)

    req = session or requests
    try:
        resp = req.get(page_url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return None
        html = resp.text
    except Exception:
        return None

    soup = BeautifulSoup(html, "html.parser")

    related_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        rm = re.search(r"/video/(\d+)(?:/[^\s\"\'<>?#]*)?", href)
        if rm and rm.group(1) != video_id:
            related_links.append(urljoin("https://footballi.net", rm.group(0)))

    title = ""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    title = re.sub(r"\s*[-|]\s*فوتبالی.*$", "", title).strip()

    poster_url = ""
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        poster_url = og_img["content"].strip()

    description = ""
    og_desc = soup.find("meta", property="og:description")
    if og_desc and og_desc.get("content"):
        description = og_desc["content"].strip()

    mp4_url = ""
    for meta_prop in ["og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"]:
        m_tag = soup.find("meta", property=meta_prop) or soup.find("meta", attrs={"name": meta_prop})
        if m_tag and m_tag.get("content") and ".mp4" in m_tag["content"]:
            mp4_url = m_tag["content"].strip()
            break

    if not mp4_url:
        for tag in soup.find_all(["aside", "footer", "nav"]):
            tag.decompose()
        video_tag = soup.find("video")
        if video_tag:
            if video_tag.get("src") and ".mp4" in video_tag["src"]:
                mp4_url = video_tag["src"].strip()
            else:
                src_tag = video_tag.find("source")
                if src_tag and src_tag.get("src") and ".mp4" in src_tag["src"]:
                    mp4_url = src_tag["src"].strip()
            if not poster_url and video_tag.get("poster"):
                poster_url = video_tag["poster"].strip()

    if not mp4_url:
        idx = html.find(str(video_id))
        while idx != -1:
            window = html[max(0, idx - 1500) : min(len(html), idx + 2500)]
            mp4_matches = re.findall(r"https?://[^\s\"\'<>\\]+\.mp4", window)
            if mp4_matches:
                mp4_url = mp4_matches[0]
                break
            idx = html.find(str(video_id), idx + len(str(video_id)))

    if not mp4_url:
        all_mp4s = re.findall(r"https?://[^\s\"\'<>\\]+\.mp4", html)
        if all_mp4s:
            mp4_url = all_mp4s[0]

    if not mp4_url or not title:
        return None

    return {
        "video_id": video_id,
        "url": page_url,
        "title": title,
        "description": description or title,
        "mp4_url": mp4_url,
        "poster_url": poster_url,
        "related_links": related_links[:12],
    }


def crawl_footballi_videos(state: dict, max_new_candidates: int = 25) -> list:
    uploaded_urls = set(state.get("uploaded_urls", []))
    seen_mp4s = set(state.get("seen_mp4s", []))
    queued_urls = {item.get("url") for item in state.get("pending_queue", []) if item.get("url")}
    queued_mp4s = {item.get("mp4_url") for item in state.get("pending_queue", []) if item.get("mp4_url")}

    known_ids = set()
    for u in uploaded_urls | queued_urls:
        m = re.search(r"/video/(\d+)", str(u))
        if m:
            known_ids.add(m.group(1))

    candidate_links = []
    seen_candidate_ids = set()

    def add_link(u: str):
        m = re.search(r"/video/(\d+)", u)
        if not m:
            return
        vid_id = m.group(1)
        if vid_id not in known_ids and vid_id not in seen_candidate_ids:
            seen_candidate_ids.add(vid_id)
            candidate_links.append(u)

    for link in browser_discover_links():
        add_link(link)

    session = requests.Session()
    for hub_url in ENTRY_URLS:
        try:
            r = session.get(hub_url, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                for m in re.finditer(r"https?://(?:www\.)?footballi\.net/video/(\d+)(?:/[^\s\"\'<>?#\\]*)?", r.text):
                    add_link(m.group(0))
                for m in re.finditer(r"\"/video/(\d+)(?:/[^\s\"\'<>?#\\]*)?\"", r.text):
                    add_link(urljoin("https://footballi.net", m.group(0).strip('"')))
        except Exception:
            continue

    print(f"[CRAWLER] Found {len(candidate_links)} fresh candidate video pages to inspect.")

    results = []
    idx = 0
    while idx < len(candidate_links) and len(results) < max_new_candidates:
        link = candidate_links[idx]
        idx += 1

        details = extract_video_page_details(link, session=session)
        if not details:
            continue

        for rel_url in details.get("related_links", []):
            if len(candidate_links) < 80:
                add_link(rel_url)

        if details["mp4_url"] in seen_mp4s or details["mp4_url"] in queued_mp4s:
            continue

        if is_foreign_or_blacklisted(details["title"], details["description"]):
            print(f"🚫 [SKIP-FOREIGN] Rejecting non-domestic/copyrighted video: {details['title']}")
            # ثبت لینک در لیست دیده‌شده‌ها تا در ران‌های بعدی دوباره بررسی نشود
            state.setdefault("uploaded_urls", []).append(details["url"])
            continue

        seen_mp4s.add(details["mp4_url"])
        results.append({
            "url": details["url"],
            "title": details["title"],
            "description": details["description"],
            "mp4_url": details["mp4_url"],
            "poster_url": details["poster_url"],
            "source": "footballi_web",
            "is_short": False,
        })

    print(f"[CRAWLER] Verified {len(results)} brand-new strictly domestic Iranian videos from Footballi.net!")
    return results


def fetch_instagram_reels(state: dict) -> list:
    return []
