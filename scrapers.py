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
    PROTO,
    HEADERS,
    ENTRY_URLS,
    FOREIGN_BLACKLIST,
    IRANIAN_DOMESTIC_CLUBS,
    IRANIAN_SAFE_SHOWS,
    IRANIAN_FOOTBALL_ENTITIES,
)

# حداکثر اختلاف شماره سریال ویدیو نسبت به جدیدترین ویدیوی سایت (حدود ۳۶ تا ۴۸ ساعت اخیر)
MAX_VIDEO_ID_AGE_GAP = 650
MIN_ABSOLUTE_VIDEO_ID = 1073500


def normalize_persian(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"[\u064B-\u065F\u0670]", "", text)
    text = text.replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
    # تبدیل اعداد فارسی و عربی به انگلیسی برای بررسی دقیق تاریخ
    persian_digits = "۰۱۲۳۴۵۶۷۸۹"
    arabic_digits = "٠١٢٣٤٥٦٧٨٩"
    for i in range(10):
        text = text.replace(persian_digits[i], str(i)).replace(arabic_digits[i], str(i))
    return re.sub(r"\s+", " ", text).strip()


def extract_numeric_id(url: str) -> int:
    m = re.search(r"/video/(\d+)", str(url))
    return int(m.group(1)) if m else 0


EXTENDED_IRANIAN_FIGURES = IRANIAN_FOOTBALL_ENTITIES + [
    "قلی زاده", "قلی‌زاده", "صیادمنش", "سردار آزمون", "طارمی", "جهانبخش", "قایدی",
    "قدوس", "محبی", "عزت اللهی", "عزت‌اللهی", "کنعانی", "شجاع", "بیرانوند", "نیازمند",
    "حامد لک", "سیدحسین حسینی", "فرعباسی", "اخباری", "پورعلی گنجی", "حردانی",
    "رضاییان", "ترابی", "اورونوف", "آسانی", "ماشاریپوف", "گندوز", "چشمی", "رفیعی",
    "عالیشاه", "امیری", "سحرخیزان", "مغانلو", "حسین زاده", "آل کثیر", "قربانی",
    "هاشمیان", "وحید هاشمیان", "نامجو مطلق", "دین محمدی", "حلالی", "اسماعیل حلالی",
    "خاکپور", "قاسمپور", "قاسم پور", "مطهری", "حمید مطهری", "سراج", "اصغرزاده",
    "توتونچی", "میثاقی", "فردوسی پور", "مظفری", "فغانی", "مرادی", "جباری", "صفیری",
    "حیدری", "بنیادی فر", "عرب براقی", "زاهدی فر", "نصیرزاده", "تاج", "طالقانی",
    "نکونام", "تارتار", "بختیاری", "موسیمانه", "گاریدو", "اسکوچیچ", "کارترون",
    "قلعه نویی", "درویش", "خطیر", "سمیعی", "زنوزی", "الهامی", "رحمتی", "عنایتی",
    "منصوریان", "مظلومی", "درخشان", "پیروانی", "پروین", "دایی", "کریمی", "مجیدی",
    "باقری", "برومند", "رافت", "سامره", "بیک زاده", "ابوالقاسم پور", "فتاحی",
    "ویسی", "جلالی", "آشوبی", "مسلمان", "جمشیدی", "علیدوستی", "شمسایی", "تهامی",
    "عمران زاده", "پنجعلی", "بیانی", "اسفندیارپور", "پیشکسوتان",
]


def is_video_too_old(title: str, page_text_sample: str = "", video_id: int = 0, max_site_id: int = 0) -> bool:
    """
    بررسی دقیق تاریخ و قدمت ویدیو:
    فقط ویدیوهای ۲۴ تا ۴۸ ساعت اخیر مجاز هستند و ویدیوهای قدیمی‌تر فوراً رد می‌شوند.
    """
    # ۱. بررسی بر اساس شماره سریال افزایشی سایت فوتبالی
    if video_id > 0:
        if video_id < MIN_ABSOLUTE_VIDEO_ID:
            return True
        if max_site_id > MIN_ABSOLUTE_VIDEO_ID and (max_site_id - video_id) > MAX_VIDEO_ID_AGE_GAP:
            return True

    norm_t = normalize_persian(title).lower()
    norm_page = normalize_persian(page_text_sample).lower()

    # ۲. سال‌های قدیمی در عنوان
    old_years = ["1398", "1399", "1400", "1401", "1402", "1403", "1404", "2020", "2021", "2022", "2023", "2024", "2025"]
    for y in old_years:
        if y in norm_t:
            return True

    # ۳. بررسی عبارت زمان انتشار در بالای صفحه ویدیو («هفته پیش»، «ماه پیش»، «سال پیش»، یا بیش از ۲ روز پیش)
    if any(old_unit in norm_page for old_unit in ["هفته پیش", "ماه پیش", "سال پیش"]):
        return True

    m_days = re.search(r"(\d+)\s*روز پیش", norm_page)
    if m_days and int(m_days.group(1)) > 2:
        return True

    return False


def is_match_broadcast_footage(norm_title: str) -> bool:
    match_words = [
        "خلاصه بازی", "خلاصه دیدار", "خلاصه مسابقه", "هایلایت بازی", "بازی کامل",
        "گل اول", "گل دوم", "گل سوم", "گل چهارم", "گل پنجم", "گل های بازی",
        "گلهای بازی", "تمام گل های", "گل های برتر هفته", "ضربات پنالتی بازی",
        "شبیه سازی گل",
    ]
    return any(normalize_persian(w).lower() in norm_title for w in match_words)


def is_foreign_or_blacklisted(title: str, desc: str = "") -> bool:
    clean_title = re.sub(r"\|.*$", "", title).strip()
    norm_title = normalize_persian(clean_title).lower()

    if is_video_too_old(clean_title):
        return True

    if is_match_broadcast_footage(norm_title):
        for bad_word in FOREIGN_BLACKLIST:
            if normalize_persian(bad_word).lower() in norm_title:
                return True
        has_iranian_club = any(normalize_persian(club).lower() in norm_title for club in IRANIAN_DOMESTIC_CLUBS)
        return not has_iranian_club

    is_safe_show = any(normalize_persian(s).lower() in norm_title for s in IRANIAN_SAFE_SHOWS)
    has_iranian_figure = any(normalize_persian(e).lower() in norm_title for e in EXTENDED_IRANIAN_FIGURES)

    if is_safe_show or has_iranian_figure:
        pure_foreign_clip_words = [
            "گلها و مهارتهای", "تکنیک و گلهای", "دروازه بانی کایل واکر",
            "کاشته های برتر بوندسلیگا", "دریبل های تماشایی لالیگا",
            "برندگان لیگ قهرمانان اروپا", "آموزش حرکت های تکنیکی جدید بازی",
        ]
        if any(normalize_persian(pw).lower() in norm_title for pw in pure_foreign_clip_words):
            return True
        return False

    for bad_word in FOREIGN_BLACKLIST:
        if normalize_persian(bad_word).lower() in norm_title:
            return True

    foreign_viral_patterns = [
        "دیوانه وارترین", "تحقیرآمیزترین", "باورنکردنی ترین", "عجیب ترین و خنده دارترین",
        "بهترین گل های لحظه آخری تاریخ", "گل های کاملاً تحقیرآمیز", "یک در میلیارد",
        "چالش دقت و تمرکز", "کری خوانی به سبک فوتبال آمریکایی", "فوتبال در بین برزیلی ها",
        "در لیگ ترکیه", "عملکرد جالب و دیدنی روماریو", "ضربات پنالتی تاریخ",
    ]
    if any(normalize_persian(fp).lower() in norm_title for fp in foreign_viral_patterns):
        return True

    if ":" in clean_title and any(
        w in norm_title for w in ["لیگ", "تیم ملی", "فوتبال", "داور", "بازیکن", "مربی", "جام", "باشگاه", "استادیوم", "هوادار"]
    ):
        return False

    return True


def ai_batch_verify_borderline_titles(candidates: list) -> list:
    if not candidates:
        return []

    arvan_key = os.environ.get("ARVAN_API_KEY", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not arvan_key and not gemini_key:
        return [c for c in candidates if not is_foreign_or_blacklisted(c["title"], c.get("description", ""))]

    approved = []
    borderline = []

    for c in candidates:
        norm_t = normalize_persian(c["title"]).lower()
        if is_match_broadcast_footage(norm_t):
            if not is_foreign_or_blacklisted(c["title"], c.get("description", "")):
                approved.append(c)
            else:
                print(f"🚫 [SKIP-FOREIGN-MATCH] Rejecting foreign match footage: {c['title']}")
            continue

        if not is_foreign_or_blacklisted(c["title"], c.get("description", "")):
            approved.append(c)
        else:
            has_explicit_foreign = any(normalize_persian(bw).lower() in norm_t for bw in FOREIGN_BLACKLIST)
            if not has_explicit_foreign:
                borderline.append(c)
            else:
                print(f"🚫 [SKIP-FOREIGN] Rejecting foreign clip: {c['title']}")

    if not borderline:
        return approved

    prompt_items = [{"index": i, "title": c["title"]} for i, c in enumerate(borderline)]
    prompt = (
        "You are the content filter for 'Footballi', an Iranian domestic football YouTube channel.\n"
        "Review these Persian video titles. Approve (true) ANY video that is an interview, press conference, "
        "or news item about Iranian football, Iranian players/coaches/veterans/legionnaires, or domestic Iranian sports.\n"
        "Reject (false) generic foreign viral football compilations or foreign leagues.\n"
        "Return ONLY valid JSON mapping index to boolean, e.g. {\"0\": true, \"1\": false}.\n\n"
        f"Videos:\n{json.dumps(prompt_items, ensure_ascii=False)}"
    )

    decisions = {}
    if arvan_key:
        try:
            r = requests.post(
                f"{PROTO}ai.najafivip.ir/v1/chat/completions",
                headers={"Authorization": f"Bearer {arvan_key}", "Content-Type": "application/json"},
                json={
                    "model": "Gemini-2.5-Flash-lite",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                },
                timeout=15,
            )
            if r.status_code == 200:
                txt = r.json()["choices"][0]["message"]["content"]
                m = re.search(r"\{.*\}", txt, re.DOTALL)
                if m:
                    decisions = json.loads(m.group(0))
        except Exception:
            pass

    for i, c in enumerate(borderline):
        if decisions.get(str(i)) is True:
            print(f"✅ [AI-APPROVED] Verified Iranian football interview/video: {c['title']}")
            approved.append(c)
        else:
            print(f"🚫 [SKIP-FOREIGN] Rejecting non-domestic video: {c['title']}")

    return approved


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


def browser_discover_links(max_pages: int = 6) -> list:
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
                    for _ in range(2):
                        page.evaluate("window.scrollBy(0, 1000)")
                        page.wait_for_timeout(500)

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


def extract_video_page_details(page_url: str, max_site_id: int = 0, session: requests.Session = None) -> dict:
    video_id = extract_numeric_id(page_url)
    if video_id <= 0:
        return None

    # رد فوری ویدیوهای قدیمی قبل از درخواست شبکه
    if is_video_too_old("", "", video_id=video_id, max_site_id=max_site_id):
        return None

    req = session or requests
    try:
        resp = req.get(page_url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return None
        html = resp.text
    except Exception:
        return None

    soup = BeautifulSoup(html, "html.parser")

    title = ""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    title = re.sub(r"\s*[-|]\s*فوتبالی.*$", "", title).strip()

    # بررسی تاریخ نسبی داخل بدنه صفحه (مثلاً «۳ هفته پیش» یا «۲ ماه پیش»)
    for tag in soup.find_all(["aside", "footer", "nav"]):
        tag.decompose()
    main_text_sample = soup.get_text(" ", strip=True)[:600]
    if is_video_too_old(title, main_text_sample, video_id=video_id, max_site_id=max_site_id):
        print(f"⏳ [SKIP-OLD] Skipping archival/old video ({video_id}): {title}")
        return None

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
    }


def crawl_footballi_videos(state: dict, max_new_candidates: int = 20) -> list:
    uploaded_urls = set(state.get("uploaded_urls", []))
    seen_mp4s = set(state.get("seen_mp4s", []))
    queued_urls = {item.get("url") for item in state.get("pending_queue", []) if item.get("url")}
    queued_mp4s = {item.get("mp4_url") for item in state.get("pending_queue", []) if item.get("mp4_url")}

    known_ids = set()
    for u in uploaded_urls | queued_urls:
        vid_id = extract_numeric_id(u)
        if vid_id:
            known_ids.add(vid_id)

    raw_discovered_links = []
    for link in browser_discover_links():
        raw_discovered_links.append(link)

    session = requests.Session()
    for hub_url in ENTRY_URLS:
        try:
            r = session.get(hub_url, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                for m in re.finditer(r"https?://(?:www\.)?footballi\.net/video/(\d+)(?:/[^\s\"\'<>?#\\]*)?", r.text):
                    raw_discovered_links.append(m.group(0))
                for m in re.finditer(r"\"/video/(\d+)(?:/[^\s\"\'<>?#\\]*)?\"", r.text):
                    raw_discovered_links.append(urljoin("https://footballi.net", m.group(0).strip('"')))
        except Exception:
            continue

    # محاسبه بالاترین شماره سریال ویدیوهای روز سایت برای فیلتر کردن ویدیوهای قدیمی
    all_ids = [extract_numeric_id(u) for u in raw_discovered_links if extract_numeric_id(u) > 0]
    max_site_id = max(all_ids) if all_ids else MIN_ABSOLUTE_VIDEO_ID
    state["max_seen_video_id"] = max(int(state.get("max_seen_video_id", 0)), max_site_id)
    max_site_id = state["max_seen_video_id"]

    candidate_links = []
    seen_candidate_ids = set()

    # مرتب‌سازی از جدیدترین شماره سریال به قدیم و حذف فوری ویدیوهای بیش از ۴۸ ساعت قبل
    for u in sorted(raw_discovered_links, key=extract_numeric_id, reverse=True):
        vid_id = extract_numeric_id(u)
        if vid_id <= 0 or vid_id in known_ids or vid_id in seen_candidate_ids:
            continue
        if is_video_too_old("", "", video_id=vid_id, max_site_id=max_site_id):
            continue
        seen_candidate_ids.add(vid_id)
        candidate_links.append(u)

    print(
        f"[CRAWLER] Latest site video ID: {max_site_id} | "
        f"Found {len(candidate_links)} fresh (<48h) candidate video pages to inspect."
    )

    raw_extracted = []
    for link in candidate_links[: max_new_candidates * 2]:
        details = extract_video_page_details(link, max_site_id=max_site_id, session=session)
        if not details:
            continue

        if details["mp4_url"] in seen_mp4s or details["mp4_url"] in queued_mp4s:
            continue

        raw_extracted.append({
            "video_id": details["video_id"],
            "url": details["url"],
            "title": details["title"],
            "description": details["description"],
            "mp4_url": details["mp4_url"],
            "poster_url": details["poster_url"],
            "source": "footballi_web",
            "is_short": False,
        })

    verified = ai_batch_verify_borderline_titles(raw_extracted)
    results = []
    for item in verified[:max_new_candidates]:
        seen_mp4s.add(item["mp4_url"])
        results.append(item)

    print(f"[CRAWLER] Verified {len(results)} brand-new (<48h) domestic Iranian videos!")
    return results


def fetch_instagram_reels(state: dict) -> list:
    return []
