# config.py
PROTO = "https" + "://"
BASE_URL = PROTO + "footballi" + ".net"

# Comprehensive site-wide entry points across footballi.net
VIDEO_LIST_URLS = [
    BASE_URL + "/news/%D8%AE%D9%84%D8%A7%D8%B5%D9%87-%D8%A8%D8%A7%D8%B2%DB%8C-%D9%87%D8%A7",  # خلاصه بازی ها
    BASE_URL + "/tag/%D8%AE%D9%84%D8%A7%D8%B5%D9%87-%D8%A8%D8%A7%D8%B2%DB%8C",                # تگ خلاصه بازی
    BASE_URL + "/news/video",      # آرشیو اخبار ویدیویی
    BASE_URL + "/video",           # بخش ویدیو
    BASE_URL + "/news/latest",     # آخرین اخبار (شامل ویدیوهای فوری)
    BASE_URL,                      # صفحه اصلی سایت
    BASE_URL + "/competition/14",  # لیگ برتر خلیج فارس
    BASE_URL + "/competition/1",   # جام حذفی ایران
    BASE_URL + "/competition/90",  # لیگ آزادگان
    BASE_URL + "/competition/91",  # لیگ برتر بانوان
]

HISTORY_FILE = "uploaded_matches.json"
TOKEN_FILE = "token.json"

# Daily quota resets at 11:30 AM Iran Time (08:00 UTC) matching YouTube
MAX_DAILY_UPLOADS = 30
RESERVED_FOR_SUMMARIES = 5
MAX_UPLOADS_PER_RUN = 5
APIFY_COOLDOWN_HOURS = 8
MAX_REELS_PER_RUN = 2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
}

FORBIDDEN_KEYWORDS = [
    "لیگ قهرمانان اروپا", "لیگ نخبگان", "لیگ قهرمانان آسیا", "سطح ۲ آسیا", "سطح دو آسیا",
    "لالیگا", "لیگ برتر انگلیس", "سری آ", "بوندسلیگا", "لیگ یک فرانسه",
    "رئال مادرید", "بارسلونا", "منچسترسیتی", "منچستر سیتی", "منچستریونایتد", "لیورپول",
    "آرسنال", "چلسی", "بایرن مونیخ", "دورتموند", "اینتر میلان", "یوونتوس", "آث میلان",
    "پاری سن ژرمن", "الهلال", "النصر", "الاهلی عربستان", "الاتحاد", "السد", "العین",
    "شباب الاهلی", "پاختاکور", "الشرطه", "الغرافه", "الوصل", "الریان", "اتلتیکو مادرید",
    "فیورنتینا", "جنوا", "ناپولی", "تاتنهام", "نیوکاسل", "استون ویلا", "لورکوزن",
    "champions league", "premier league", "laliga", "serie a", "bundesliga", "uefa", "afc"
]

SUMMARY_KEYWORDS = [
    "خلاصه بازی", "خلاصه مسابقه", "خلاصه دیدار", "گل های بازی", "گل‌های بازی",
    "تک گل بازی", "گل اول", "گل دوم", "گل سوم", "گل طلایی", "هایلایت بازی"
]
