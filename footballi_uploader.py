# -*- coding: utf-8 -*-
import os
import re
import json
import time
import inspect
from datetime import datetime, timezone, timedelta

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

from config import (
    MAX_DAILY_UPLOADS,
    RESERVED_SUMMARY_SLOTS,
    MAX_UPLOADS_PER_RUN,
    MIN_MINUTES_BETWEEN_UPLOADS,
    STATE_FILE,
    SUMMARY_KEYWORDS,
    VIDEOCAST_KEYWORDS,
    POSHTE_KHAT_KEYWORDS,
    PLAYLIST_DEFINITIONS,
)
from scrapers import (
    crawl_footballi_videos,
    fetch_instagram_reels,
    normalize_persian,
    is_foreign_or_blacklisted,
)
import ai_seo
import media_utils

SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


def load_state() -> dict:
    default_state = {
        "uploaded_urls": [],
        "seen_mp4s": [],
        "uploaded_titles": [],
        "pending_queue": [],
        "daily_uploads": {"date": "", "count": 0},
        "last_upload_timestamp": 0,
        "last_apify_check": 0,
        "playlists": {},
        "playlist_assignments": {},
        "processed_comments": [],
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in default_state.items():
                if k not in data:
                    data[k] = v
            return data
        except Exception as e:
            print(f"[WARN] Could not parse {STATE_FILE}: {e}")
    return default_state


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_youtube_client():
    token_raw = os.environ.get("YOUTUBE_TOKEN", "").strip()
    if not token_raw:
        raise RuntimeError("YOUTUBE_TOKEN secret is missing!")
    token_info = json.loads(token_raw)
    creds = Credentials.from_authorized_user_info(token_info, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def detect_primary_category(title: str, description: str = "", is_short: bool = False) -> str:
    if is_short:
        return "SHORTS"
    norm = normalize_persian(f"{title} {description}").lower()
    for kw in SUMMARY_KEYWORDS:
        if normalize_persian(kw).lower() in norm:
            return "SUMMARY"
    for kw in VIDEOCAST_KEYWORDS:
        if normalize_persian(kw).lower() in norm:
            return "VIDEOCAST"
    for kw in POSHTE_KHAT_KEYWORDS:
        if normalize_persian(kw).lower() in norm:
            return "POSHTE_KHAT"
    return "VIDEO"


def detect_all_matching_playlists(title: str, description: str = "", is_short: bool = False) -> list:
    """
    تشخیص هوشمند تمام پلی‌لیست‌های مرتبط با یک ویدیو (Multi-Playlist Engine)
    مثلاً ویدیوی «کارشناسی داوری بازی استقلال در فوتبال برتر» هم‌زمان در
    «کارشناسی داوری»، «فوتبال برتر» و «باشگاه استقلال» قرار می‌گیرد.
    """
    matched = []
    norm_title = normalize_persian(title).lower()
    norm_full = normalize_persian(f"{title} {description}").lower()

    if is_short or "#shorts" in norm_title:
        matched.append("SHORTS")

    # بررسی تک‌تک پلی‌لیست‌های تخصصی بر اساس عنوان و توضیحات
    check_order = [
        "SUMMARY",
        "VIDEOCAST",
        "POSHTE_KHAT",
        "REFEREE",
        "FOOTBALL_BARTAR",
        "SHABHAYE_FOOTBALLI",
        "ESTEGHLAL",
        "PERSEPOLIS",
        "SEPAHAN",
        "TRACTOR",
        "TRANSFERS",
        "TEAM_MELLI",
    ]

    for key in check_order:
        pdef = PLAYLIST_DEFINITIONS.get(key, {})
        for kw in pdef.get("keywords", []):
            norm_kw = normalize_persian(kw).lower()
            # برای نام تیم‌ها و برنامه‌ها، تطابق در عنوان یا ابتدای توضیحات بررسی می‌شود
            if norm_kw in norm_title or norm_kw in norm_full[:280]:
                # جلوگیری از اشتباه گرفتن «استقلال خوزستان» با «باشگاه استقلال» وقتی استقلال تهران در بازی نیست
                if key == "ESTEGHLAL" and "استقلال خوزستان" in norm_title:
                    rem = norm_title.replace("استقلال خوزستان", "")
                    if "استقلال" not in rem:
                        continue
                if key not in matched:
                    matched.append(key)
                break

    # اگر ویدیو افقی بود و در دسته‌های خلاصه بازی/ویدیوکست/پشت خط نبود، در مصاحبه و حواشی هم قرار بگیرد
    if not is_short and not any(k in matched for k in ["SUMMARY", "VIDEOCAST", "POSHTE_KHAT"]):
        if "INTERVIEW" not in matched:
            matched.append("INTERVIEW")

    return matched


def ensure_all_playlists(youtube, state: dict) -> dict:
    """
    بررسی و ساخت ۱۴ پلی‌لیست در کانال یوتیوب + تغییر نام خودکار «پشت خط» به «پُشتِ خَط»
    """
    existing_map = {}
    try:
        req = youtube.playlists().list(part="snippet", mine=True, maxResults=50)
        res = req.execute()
        for item in res.get("items", []):
            pl_id = item["id"]
            pl_title = item["snippet"]["title"].strip()
            existing_map[pl_title] = pl_id
            existing_map[normalize_persian(pl_title)] = pl_id
    except Exception as e:
        print(f"[PLAYLIST-WARN] Could not list channel playlists: {e}")
        return state.get("playlists", {})

    playlists_state = state.get("playlists", {})

    for key, pdef in PLAYLIST_DEFINITIONS.items():
        target_title = pdef["title"]
        target_desc = pdef["description"]
        legacy_titles = pdef.get("legacy_titles", [])

        # ۱. بررسی اینکه آیا با نام دقیق فعلی وجود دارد
        if target_title in existing_map:
            playlists_state[key] = existing_map[target_title]
            continue

        # ۲. بررسی عناوین قدیمی (مثلاً تبدیل «پشت خط» بدون اعراب به «پُشتِ خَط»)
        found_legacy_id = None
        for leg in legacy_titles:
            if leg in existing_map:
                found_legacy_id = existing_map[leg]
                break
        if not found_legacy_id and normalize_persian(target_title) in existing_map:
            found_legacy_id = existing_map[normalize_persian(target_title)]

        if found_legacy_id:
            playlists_state[key] = found_legacy_id
            try:
                youtube.playlists().update(
                    part="snippet",
                    body={
                        "id": found_legacy_id,
                        "snippet": {
                            "title": target_title,
                            "description": target_desc,
                        },
                    },
                ).execute()
                print(f"✨ [PLAYLIST-RENAME] Updated playlist title to: {target_title}")
            except Exception:
                pass
            continue

        # ۳. ساخت پلی‌لیست جدید در صورت عدم وجود
        try:
            created = youtube.playlists().insert(
                part="snippet,status",
                body={
                    "snippet": {
                        "title": target_title,
                        "description": target_desc,
                    },
                    "status": {"privacyStatus": "public"},
                },
            ).execute()
            playlists_state[key] = created["id"]
            print(f"✅ [PLAYLIST-CREATED] Created new official playlist: {target_title}")
        except Exception as e:
            print(f"[PLAYLIST-WARN] Could not create playlist {target_title}: {e}")

    state["playlists"] = playlists_state
    return playlists_state


def add_video_to_playlists(youtube, video_id: str, playlist_keys: list, playlists_map: dict, state: dict) -> int:
    assignments = state.setdefault("playlist_assignments", {})
    already_assigned = set(assignments.get(video_id, []))
    added_count = 0

    for key in playlist_keys:
        if key in already_assigned:
            continue
        pl_id = playlists_map.get(key)
        if not pl_id:
            continue
        try:
            youtube.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": pl_id,
                        "resourceId": {
                            "kind": "youtube#video",
                            "videoId": video_id,
                        },
                    }
                },
            ).execute()
            already_assigned.add(key)
            added_count += 1
            print(f"📂 [PLAYLIST-ADD] Added video {video_id} -> {PLAYLIST_DEFINITIONS[key]['title']}")
        except HttpError as e:
            if e.resp.status == 409 or "videoAlreadyInPlaylist" in str(e):
                already_assigned.add(key)
            else:
                print(f"[PLAYLIST-WARN] Could not add {video_id} to {key}: {e}")
        except Exception:
            pass

    assignments[video_id] = list(already_assigned)
    return added_count


def fetch_channel_existing_videos(youtube, max_items: int = 120) -> list:
    """دریافت لیست ویدیوهای فعلی کانال با مصرف فقط ۱ واحد سهمیه API"""
    videos = []
    try:
        ch_res = youtube.channels().list(part="contentDetails", mine=True).execute()
        items = ch_res.get("items", [])
        if not items:
            return []
        uploads_pl = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

        next_page = None
        while len(videos) < max_items:
            pl_res = youtube.playlistItems().list(
                part="snippet",
                playlistId=uploads_pl,
                maxResults=50,
                pageToken=next_page,
            ).execute()
            for it in pl_res.get("items", []):
                snip = it["snippet"]
                vid_id = snip.get("resourceId", {}).get("videoId")
                title = snip.get("title", "")
                desc = snip.get("description", "")
                if vid_id and title:
                    videos.append({"video_id": vid_id, "title": title, "description": desc})
            next_page = pl_res.get("nextPageToken")
            if not next_page:
                break
    except Exception as e:
        print(f"[WARN] Could not index channel videos: {e}")
    return videos


def organize_existing_channel_videos(youtube, channel_videos: list, playlists_map: dict, state: dict, max_ops_per_run: int = 12):
    """
    دسته‌بندی خودکار تمامی ویدیوهای قبلی کانال در ۱۴ پلی‌لیست جدید
    (در هر اجرا حداکثر ۱۲ عملیات درج انجام می‌شود تا سهمیه API یوتیوب حفظ شود)
    """
    ops = 0
    assignments = state.setdefault("playlist_assignments", {})

    for vid in channel_videos:
        if ops >= max_ops_per_run:
            break
        vid_id = vid["video_id"]
        title = vid["title"]
        desc = vid.get("description", "")
        is_short = "#shorts" in title.lower() or "#shorts" in desc.lower()

        target_keys = detect_all_matching_playlists(title, desc, is_short=is_short)
        current_keys = set(assignments.get(vid_id, []))
        missing_keys = [k for k in target_keys if k not in current_keys]

        if missing_keys:
            added = add_video_to_playlists(youtube, vid_id, missing_keys[:3], playlists_map, state)
            ops += added


def deduplicate_queue(queue: list, uploaded_urls: set, seen_mp4s: set, channel_norm_titles: set) -> list:
    clean = []
    q_urls = set()
    q_mp4s = set()
    q_titles = set()

    for item in queue:
        u = item.get("url", "")
        mp4 = item.get("mp4_url", "")
        t = normalize_persian(item.get("title", "")).lower()

        if not u or not mp4 or not t:
            continue
        if is_foreign_or_blacklisted(item.get("title", ""), item.get("description", "")):
            continue
        if u in uploaded_urls or u in q_urls or mp4 in q_mp4s or t in q_titles or t in channel_norm_titles:
            continue

        q_urls.add(u)
        q_mp4s.add(mp4)
        q_titles.add(t)
        clean.append(item)

    return clean


def call_prepare_thumbnail(video_path: str, poster_url: str, output_path: str) -> str:
    """فراخوانی سازگار با هر امضای تابع prepare_clean_thumbnail در media_utils"""
    fn = getattr(media_utils, "prepare_clean_thumbnail", None)
    if not fn:
        return None
    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        if len(params) >= 3:
            return fn(video_path, poster_url, output_path)
        elif len(params) == 2:
            return fn(poster_url, output_path)
        else:
            return fn(poster_url)
    except Exception as e:
        print(f"[THUMB-WARN] Thumbnail preparation fallback note: {e}")
        return None


def call_generate_seo(title: str, desc: str, category: str, is_short: bool) -> dict:
    """فراخوانی سازگار با ماژول ai_seo برای تولید تیتر، توضیحات و تگ‌ها"""
    for fn_name in ["generate_seo_metadata", "ai_generate_seo_metadata", "build_seo_package"]:
        fn = getattr(ai_seo, fn_name, None)
        if fn:
            try:
                sig = inspect.signature(fn)
                if len(sig.parameters) >= 4:
                    return fn(title, desc, category, is_short)
                elif len(sig.parameters) == 3:
                    return fn(title, desc, category)
                else:
                    return fn(title, desc)
            except Exception:
                pass

    suffix = " | خلاصه بازی" if category == "SUMMARY" else (" #Shorts" if is_short else " | فوتبال ایران")
    clean_t = title[: 95 - len(suffix)] + suffix
    full_desc = (
        f"{title}\n\n{desc}\n\n"
        "⚽ رسانه رسمی فوتبالی در یوتیوب | پوشش لحظه‌ای اخبار، خلاصه بازی‌ها، ویدیوکست و پُشتِ خَط\n"
        "#فوتبالی #فوتبال_ایران #لیگ_برتر #استقلال #پرسپولیس"
    )
    tags = ["فوتبالی", "فوتبال ایران", "لیگ برتر ایران", "استقلال", "پرسپولیس", "خلاصه بازی"]
    return {"title": clean_t, "description": full_desc, "tags": tags}


def main():
    print("[START] Footballi Bot starting (Chrome Browser + 14 Multi-Playlist Edition)...")
    state = load_state()

    # محاسبه ریست روزانه ساعت ۱۱:۳۰ صبح به وقت تهران (۰۸:۰۰ UTC)
    now_utc = datetime.now(timezone.utc)
    reset_Key = (now_utc - timedelta(hours=8)).strftime("%Y-%m-%d")
    if state["daily_uploads"].get("date") != reset_Key:
        state["daily_uploads"] = {"date": reset_Key, "count": 0}

    youtube = get_youtube_client()

    # ۱. ایندکس ویدیوهای فعلی کانال
    channel_videos = fetch_channel_existing_videos(youtube, max_items=120)
    channel_norm_titles = {normalize_persian(v["title"]).lower() for v in channel_videos}
    uploads_today = state["daily_uploads"].get("count", 0)

    print(
        f"[STATUS] Channel indexed titles: {len(channel_videos)} | "
        f"Uploads since 11:30 AM reset: {uploads_today}/{MAX_DAILY_UPLOADS} "
        f"(Reserved for Match Summaries: {RESERVED_SUMMARY_SLOTS})"
    )

    # ۲. اطمینان از وجود هر ۱۴ پلی‌لیست و مرتب‌سازی تدریجی ویدیوهای قبلی کانال
    playlists_map = ensure_all_playlists(youtube, state)
    organize_existing_channel_videos(youtube, channel_videos, playlists_map, state, max_ops_per_run=10)

    # ۳. پالایش صف فعلی
    uploaded_urls = set(state.get("uploaded_urls", []))
    seen_mp4s = set(state.get("seen_mp4s", []))
    state["pending_queue"] = deduplicate_queue(
        state.get("pending_queue", []), uploaded_urls, seen_mp4s, channel_norm_titles
    )
    print(f"[QUEUE] {len(state['pending_queue'])} unique verified videos ready in Pending Queue.")

    # ۴. اسکن سایت فوتبالی با مرورگر کروم + اینستاگرام
    new_web_videos = crawl_footballi_videos(state, max_new_candidates=20)
    new_reels = fetch_instagram_reels(state)

    for item in new_web_videos + new_reels:
        item["category"] = detect_primary_category(
            item["title"], item.get("description", ""), item.get("is_short", False)
        )
        state["pending_queue"].append(item)

    state["pending_queue"] = deduplicate_queue(
        state["pending_queue"], uploaded_urls, seen_mp4s, channel_norm_titles
    )

    # اولویت‌بندی صف: اول خلاصه بازی‌ها، بعد ویدیوکست و پُشتِ خَط، بعد بقیه
    priority_order = {"SUMMARY": 0, "VIDEOCAST": 1, "POSHTE_KHAT": 2, "SHORTS": 3, "VIDEO": 4}
    state["pending_queue"].sort(key=lambda x: priority_order.get(x.get("category", "VIDEO"), 5))
    save_state(state)

    if not state["pending_queue"]:
        print("[DONE] No videos in queue to upload right now.")
        return

    # ۵. بررسی فاصله زمانی ۴۵ دقیقه‌ای (مگر اینکه ویدیو خلاصه بازی باشد)
    candidate = state["pending_queue"][0]
    cat = candidate.get("category", "VIDEO")
    mins_since_last = (time.time() - float(state.get("last_upload_timestamp", 0))) / 60.0

    if cat != "SUMMARY" and mins_since_last < MIN_MINUTES_BETWEEN_UPLOADS:
        wait_rem = MIN_MINUTES_BETWEEN_UPLOADS - mins_since_last
        print(f"⏳ [PACING] Waiting {wait_rem:.1f}m before next regular upload to maximize impressions.")
        save_state(state)
        return

    if uploads_today >= (MAX_DAILY_UPLOADS - RESERVED_SUMMARY_SLOTS) and cat != "SUMMARY":
        print("[QUOTA] Remaining daily slots are reserved strictly for Match Summaries.")
        save_state(state)
        return

    # ۶. دانلود و آماده‌سازی ویدیو و کاور HD
    video_file = "temp_video.mp4"
    thumb_file = "temp_thumb.jpg"

    dl_fn = getattr(media_utils, "download_file", None)
    if dl_fn:
        ok = dl_fn(candidate["mp4_url"], video_file)
    else:
        import requests
        r = requests.get(candidate["mp4_url"], stream=True, timeout=60)
        ok = r.status_code == 200
        if ok:
            with open(video_file, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)

    if not ok or not os.path.exists(video_file):
        print(f"[WARN] Could not download MP4: {candidate['mp4_url']}")
        state["pending_queue"].pop(0)
        save_state(state)
        return

    is_short = candidate.get("is_short", False)
    inspect_fn = getattr(media_utils, "inspect_video_file", None)
    if inspect_fn:
        info = inspect_fn(video_file)
        if info and info.get("is_vertical") and info.get("duration", 999) <= 175:
            is_short = True

    thumb_to_use = None
    if not is_short:
        thumb_to_use = call_prepare_thumbnail(video_file, candidate.get("poster_url", ""), thumb_file)

    seo = call_generate_seo(candidate["title"], candidate.get("description", ""), cat, is_short)
    final_title = seo.get("title", candidate["title"])[:100]
    final_desc = seo.get("description", candidate.get("description", ""))
    final_tags = seo.get("tags", ["فوتبالی", "فوتبال ایران"])

    print(f"[UPLOAD] Publishing 1 video ({cat}): {final_title}")

    try:
        body = {
            "snippet": {
                "title": final_title,
                "description": final_desc,
                "tags": final_tags,
                "categoryId": "17",
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
            },
        }
        media = MediaFileUpload(video_file, chunksize=4 * 1024 * 1024, resumable=True, mimetype="video/mp4")
        req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            _, response = req.next_chunk()

        vid_id = response["id"]
        print(f"✅ [SUCCESS] Uploaded Video ID: {vid_id}")

        # ست کردن تامبنیل رسمی HD
        if thumb_to_use and os.path.exists(thumb_to_use) and not is_short:
            try:
                youtube.thumbnails().set(
                    videoId=vid_id,
                    media_body=MediaFileUpload(thumb_to_use, mimetype="image/jpeg"),
                ).execute()
                print("🖼️ [THUMBNAIL] Official HD poster attached to video.")
            except Exception as te:
                print(f"[THUMB-WARN] Could not attach custom thumbnail: {te}")

        # اضافه کردن هم‌زمان ویدیو به تمام پلی‌لیست‌های مرتبط (Multi-Playlist)
        matched_playlists = detect_all_matching_playlists(
            final_title, final_desc, is_short=is_short
        )
        add_video_to_playlists(youtube, vid_id, matched_playlists, playlists_map, state)

        # ثبت در لیست آپلودشده‌ها و حذف از صف
        state["pending_queue"].pop(0)
        state["uploaded_urls"].append(candidate["url"])
        state["seen_mp4s"].append(candidate["mp4_url"])
        state["uploaded_titles"].append(final_title)
        state["daily_uploads"]["count"] = uploads_today + 1
        state["last_upload_timestamp"] = time.time()
        save_state(state)
        print(f"[DONE] Uploaded: 1 | Unique videos waiting in Queue: {len(state['pending_queue'])}")

    except HttpError as e:
        err_str = str(e)
        print(f" YouTube API Upload error: {e}")
        if "uploadLimitExceeded" in err_str or "quotaExceeded" in err_str:
            print("💾 [QUEUE-SAVED] YouTube limit reached! Holding remaining unique videos in queue for next reset.")
            save_state(state)
            print(f"[DONE] Uploaded: 0 | Unique videos waiting in Queue: {len(state['pending_queue'])}")
        else:
            state["pending_queue"].pop(0)
            save_state(state)


if __name__ == "__main__":
    main()
