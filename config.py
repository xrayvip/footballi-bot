# config.py
PROTO = "https" + "://"
BASE_URL = PROTO + "footballi" + ".net"

# Verified 200-OK Video Hubs & Iranian Pro League Club Pages (which list all 'گل‌ها و خلاصه‌بازی‌ها')
VIDEO_LIST_URLS = [
    BASE_URL + "/news/video",      # آرشیو اصلی ویدیوها
    BASE_URL + "/video",           # صفحه اول ویدیوها
    BASE_URL,                      # صفحه اصلی فوتبالی
    BASE_URL + "/team/830",        # استقلال (شامل خلاصه بازی‌ها و ویدیوها)
    BASE_URL + "/team/829",        # پرسپولیس
    BASE_URL + "/team/833",        # سپاهان
    BASE_URL + "/team/836",        # تراکتور
    BASE_URL + "/team/918",        # گل گهر سیرجان
    BASE_URL + "/team/831",        # فولاد
    BASE_URL + "/team/832",        # ذوب آهن
    BASE_URL + "/team/834",        # ملوان
]

HISTORY_FILE = "uploaded_matches.json"
TOKEN_FILE = "token.json"

MAX_DAILY_UPLOADS = 30
RESERVED_FOR_SUMMARIES = 5
MAX_UPLOADS_PER_RUN = 4
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
    "قهرمانان آسیا دو", "آرکاداغ", "الجزیره", "الشرطه", "الغرافه", "الوصل", "الریان",
    "لالیگا", "لیگ برتر انگلیس", "سری آ", "بوندسلیگا", "لیگ یک فرانسه", "کوپا دل ری",
    "رئال مادرید", "بارسلونا", "اتلتیکو", "مادرید", "منچسترسیتی", "منچستر سیتی",
    "منچستریونایتد", "لیورپول", "آرسنال", "چلسی", "بایرن مونیخ", "دورتموند",
    "اینتر میلان", "اینتر", "یوونتوس", "آث میلان", "میلان", "پاری سن ژرمن", "الهلال", "النصر",
    "الاهلی", "الاتحاد", "السد", "العین", "شباب الاهلی", "پاختاکور", "فیورنتینا", "جنوا",
    "ناپولی", "لاتزیو", "تاتنهام", "نیوکاسل", "استون ویلا", "ساندرلند", "کریستال پالاس",
    "لورکوزن", "والنسیا", "سانتاندر", "سیمئونه", "امباپه", "بلینگهام", "لواندوفسکی",
    "ترابزون", "آلانیا", "گالاتاسرای", "فنرباغچه", "کومو", "تروا", "اینتر میامی", "شارلوت",
    "شامپیونزلیگ", "اروپا", "اسپانیا", "انگلیس", "ایتالیا", "آلمان", "فرانسه", "عربستان", "امارات",
    "champions league", "premier league", "laliga", "serie a", "bundesliga", "uefa", "afc"
]

# Strictly Full Match Summaries
SUMMARY_KEYWORDS = [
    "خلاصه بازی", "خلاصه مسابقه", "خلاصه دیدار"
]

VIDEOCAST_KEYWORDS = [
    "ویدیوکست", "ویدیو کست", "ویدئوکست", "ویدئو کست", "پادکست"
]

POSHTE_KHAT_KEYWORDS = [
    "پشت خط", "پشت‌خط", "برنامه پشت خط"
]
