# config.py
PROTO = "https" + "://"
BASE_URL = PROTO + "footballi" + ".net"

VIDEO_LIST_URLS = [
    BASE_URL + "/news/video",      # Main Footballi Video News Archive
    BASE_URL + "/video",
    BASE_URL,
    BASE_URL + "/competition/14",  # Persian Gulf Pro League
    BASE_URL + "/competition/1",   # Hazfi Cup
    BASE_URL + "/competition/90",  # Azadegan League
    BASE_URL + "/competition/91",  # Women's League
]

HISTORY_FILE = "uploaded_matches.json"
TOKEN_FILE = "token.json"

# Quota & Limits (Resets at 11:30 AM Iran Time / 08:00 UTC)
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
