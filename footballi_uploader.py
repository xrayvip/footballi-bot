# footballi_uploader.py
import os
import re
import json
import time
from datetime import datetime, timezone, timedelta

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

from config import (
    PROTO, HISTORY_FILE, TOKEN_FILE,
    MAX_DAILY_UPLOADS, RESERVED_FOR_SUMMARIES, MAX_UPLOADS_PER_RUN
)
import config
from ai_seo import ai_batch_evaluate
from scrapers import (
    scrape_website_candidates, extract_video_details_from_page,
    fetch_instagram_reels, is_match_summary, is_duplicate_title, normalize_persian
)
from media_utils import (
    inspect_video_file, enhance_short_to_1080p, download_file, prepare_clean_thumbnail
)

VIDEOCAST_KEYWORDS = getattr(
    config,
    "VIDEOCAST_KEYWORDS",
    ["ویدیوکست", "ویدیو کست", "ویدئوکست", "ویدئو کست", "پادکست"]
)


def is_videocast(title, tags=""):
    combined = normalize_persian(f"{title} {tags}")
    return any(normalize_persian(k) in combined for k in VIDEOCAST_KEYWORDS)


def item_priority_key(item):
    """
    Sorts queue so the most valuable content always uploads first when quota opens:
    Priority 0: Match Summaries ('خلاصه بازی‌ها')
    Priority 1: Exclusive Footballi Videocasts ('ویدیوکست')
    Priority 2: Shorts & Fresh News
    """
    if item.get("is_summary"):
        return 0
    if item.get("is_videocast") or is_videocast(item.get("title", ""), item.get("tags", "")):
        return 1
    return 2


def deduplicate_queue(queue_list, seen_urls, seen_mp4s, existing_titles):
    clean_queue = []
    q_urls = set()
    q_mp4s = set()
    q_titles = set()

    for item in queue_list:
        if not isinstance(item, dict):
            continue
        u = item.get("url", "").strip()
        mp4 = item.get("mp4_url", "").strip()
        orig_t = item.get("title", "").strip()
        seo_t = item.get("ai_info", {}).get("seo_title", "").strip()
        norm_t = normalize_persian(orig_t)

        if not u or not mp4 or not norm_t:
            continue

        if mp4 in seen_mp4s:
            continue
        if is_duplicate_title(orig_t, existing_titles) or (seo_t and is_duplicate_title(seo_t, existing_titles)):
            continue

        if u in q_urls or mp4 in q_mp4s or norm_t in q_titles:
            continue
        if is_duplicate_title(orig_t, q_titles):
            continue

        item["is_videocast"] = is_videocast(orig_t, item.get("tags", ""))

        q_urls.add(u)
        q_mp4s.add(mp4)
        q_titles.add(norm_t)
        if seo_t:
            q_titles.add(normalize_persian(seo_t))

        clean_queue.append(item)

    clean_queue.sort(key=item_priority_key)
    return clean_queue[:45]


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {
                        "uploaded_urls": data,
                        "uploaded_mp4s": [],
                        "uploaded_titles": [],
                        "pending_queue": [],
                        "last_apify_run": 0
                    }
                if isinstance(data, dict):
                    data.setdefault("uploaded_urls", [])
                    data.setdefault("uploaded_mp4s", [])
                    data.setdefault("uploaded_titles", [])
                    data.setdefault("pending_queue", [])
                    data.setdefault("last_apify_run", 0)
                    return data
        except Exception as e:
            print(f"[WARN] Could not read history file: {e}")
    return {
        "uploaded_urls": [],
        "uploaded_mp4s": [],
        "uploaded_titles": [],
        "pending_queue": [],
        "last_apify_run": 0
    }


def save_history(history):
    try:
        history["uploaded_urls"] = list(dict.fromkeys(history.get("uploaded_urls", [])))[-2000:]
        history["uploaded_mp4s"] = list(dict.fromkeys(history.get("uploaded_mp4s", [])))[-2000:]
        history["uploaded_titles"] = list(dict.fromkeys(history.get("uploaded_titles", [])))[-1500:]

        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] Could not save history file: {e}")


def get_authenticated_service():
    yt_scope = PROTO + "www.googleapis" + ".com/auth/youtube"
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
        pl_res = youtube.playlistItems().list(
            part="snippet", playlistId=uploads_playlist_id, maxResults=50
        ).execute()

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


def get_or_create_playlist(youtube, playlist_title, cache):
    if playlist_title in cache:
        return cache[playlist_title]
    try:
        res = youtube.playlists().list(part="snippet", mine=True, maxResults=50).execute()
        for item in res.get("items", []):
            title = item["snippet"]["title"]
            cache[title] = item["id"]
            if title == playlist_title:
                return item["id"]

        body = {
            "snippet": {
                "title": playlist_title,
                "description": f"مجموعه رسمی {playlist_title} در رسانه فوتبالی (Footballi)"
            },
            "status": {"privacyStatus": "public"}
        }
        created = youtube.playlists().insert(part="snippet,status", body=body).execute()
        pl_id = created["id"]
        cache[playlist_title] = pl_id
        print(f"[PLAYLIST] Created new official playlist: '{playlist_title}'")
        return pl_id
    except Exception as e:
        print(f"[WARN] Could not get/create playlist '{playlist_title}': {e}")
        return None


def add_video_to_playlist(youtube, video_id, playlist_id):
    if not playlist_id or not video_id:
        return
    body = {
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id}
        }
    }
    for attempt in range(1, 4):
        try:
            time.sleep(3 * attempt)
            youtube.playlistItems().insert(part="snippet", body=body).execute()
            print("[PLAYLIST] Video successfully added to playlist.")
            return
        except HttpError as e:
            status_code = getattr(e.resp, "status", None)
            if status_code in (409, 500, 503) and attempt < 3:
                print(f"[PLAYLIST] Backend conflict (HTTP {status_code}), retrying in {attempt * 3}s...")
                time.sleep(attempt * 3)
            else:
                print(f"[WARN] Failed adding video to playlist: {e}")
                break
        except Exception as e:
            print(f"[WARN] Failed adding video to playlist: {e}")
            break


def upload_to_youtube(youtube, video_path, thumb_path, title, description, tags):
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": tags[:25],
            "categoryId": "17",
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
                videoId=video_id, media_body=MediaFileUpload(thumb_path)
            ).execute()
            print("[THUMBNAIL] Polished 1280x720 HD poster applied.")
        except Exception as e:
            print(f"[INFO] Thumbnail upload note: {e}")

    return video_id


def main():
    print("[START] Footballi Bot starting (Videocast + Priority Queue Edition)...")
    history = load_history()
    youtube = get_authenticated_service()

    existing_titles, uploads_today = get_channel_status_and_24h_count(youtube)
    for t in history.get("uploaded_titles", []):
        existing_titles.add(t)

    seen_urls = set(history.get("uploaded_urls", []))
    seen_mp4s = set(history.get("uploaded_mp4s", []))

    pending_queue = deduplicate_queue(
        history.get("pending_queue", []), seen_urls, seen_mp4s, existing_titles
    )
    if pending_queue:
        print(f"[QUEUE] {len(pending_queue)} unique verified videos ready in Pending Queue.")

    queued_urls = {q["url"] for q in pending_queue}
    queued_mp4s = {q["mp4_url"] for q in pending_queue}
    queued_titles = {q["title"] for q in pending_queue}
    combined_known_titles = existing_titles | queued_titles

    # Always scan for brand-new videos (especially Match Summaries & Videocasts)
    web_candidates = scrape_website_candidates(history, combined_known_titles, save_history)
    reel_candidates = fetch_instagram_reels(history, combined_known_titles, save_history)

    summaries = [c for c in web_candidates if c.get("is_summary") and c["url"] not in queued_urls]
    videocasts = [c for c in web_candidates if not c.get("is_summary") and is_videocast(c.get("title", "")) and c["url"] not in queued_urls]
    others = [c for c in web_candidates if not c.get("is_summary") and not is_videocast(c.get("title", "")) and c["url"] not in queued_urls]
    fresh_reels = [r for r in reel_candidates if r["url"] not in queued_urls]

    fresh_pool = (summaries + videocasts + fresh_reels + others)[:20]

    newly_enriched = []
    for item in fresh_pool:
        if item["url"] in queued_urls or item["url"] in seen_urls:
            continue

        if item["source"] == "web":
            details = extract_video_details_from_page(item["url"])
            if not details:
                continue
            if details.get("rejected_forever"):
                history["uploaded_urls"].append(item["url"])
                seen_urls.add(item["url"])
                continue

            mp4_u = details["mp4_url"]
            if mp4_u in seen_mp4s or mp4_u in queued_mp4s:
                history["uploaded_urls"].append(item["url"])
                seen_urls.add(item["url"])
                continue

            real_title = details["title"] or item["title"]
            if is_duplicate_title(real_title, combined_known_titles):
                history["uploaded_urls"].append(item["url"])
                seen_urls.add(item["url"])
                continue

            queued_urls.add(item["url"])
            queued_mp4s.add(mp4_u)
            combined_known_titles.add(real_title)

            item["mp4_url"] = mp4_u
            item["title"] = real_title
            item["tags"] = details["tags_text"]
            item["thumb_url"] = details["thumb_url"]
            item["is_summary"] = is_match_summary(real_title)
            item["is_videocast"] = is_videocast(real_title, details["tags_text"])
        else:
            mp4_u = item.get("mp4_url", "")
            if mp4_u in seen_mp4s or mp4_u in queued_mp4s:
                continue
            queued_urls.add(item["url"])
            queued_mp4s.add(mp4_u)
            combined_known_titles.add(item["title"])
            item["is_videocast"] = False

        newly_enriched.append(item)

    if newly_enriched:
        ai_results = ai_batch_evaluate(newly_enriched)
        for idx, item in enumerate(newly_enriched):
            ai_info = ai_results.get(idx)
            history["uploaded_urls"].append(item["url"])
            seen_urls.add(item["url"])

            if not ai_info or not ai_info.get("safe", False):
                reason = ai_info.get("reason", "Rejected") if ai_info else "No AI response"
                print(f"[AI REJECT] ({reason}): {item['title']}")
                continue

            item["ai_info"] = ai_info
            pending_queue.append(item)

    pending_queue = deduplicate_queue(pending_queue, seen_urls, seen_mp4s, existing_titles)
    history["pending_queue"] = pending_queue
    save_history(history)

    if not pending_queue:
        print("[IDLE] No videos in queue or website to upload.")
        return

    if uploads_today >= MAX_DAILY_UPLOADS:
        print(f"[QUOTA-WAIT] Daily quota ({uploads_today}/{MAX_DAILY_UPLOADS}) full. {len(pending_queue)} unique videos safely held in queue.")
        return

    playlist_cache = {}
    uploaded_in_this_run = 0
    remaining_queue = []
    youtube_limit_hit = False

    for idx, item in enumerate(pending_queue):
        if youtube_limit_hit or uploaded_in_this_run >= MAX_UPLOADS_PER_RUN or uploads_today >= MAX_DAILY_UPLOADS:
            remaining_queue.append(item)
            continue

        if item["mp4_url"] in seen_mp4s or is_duplicate_title(item["title"], existing_titles):
            print(f"[DUP-GUARD] Skipping already-uploaded video: {item['title'][:45]}")
            continue

        is_sum = item.get("is_summary", False)
        is_vc = item.get("is_videocast", False) or is_videocast(item.get("title", ""), item.get("tags", ""))

        if not is_sum and uploads_today >= (MAX_DAILY_UPLOADS - RESERVED_FOR_SUMMARIES):
            print(f"[RESERVE] Keeping '{item['title'][:40]}' in queue (saving slots for Match Summaries).")
            remaining_queue.append(item)
            continue

        video_file = f"temp_video_{idx}.mp4"
        thumb_file = f"temp_thumb_{idx}.jpg"

        if not download_file(item["mp4_url"], video_file):
            item["retries"] = item.get("retries", 0) + 1
            if item["retries"] <= 2:
                remaining_queue.append(item)
            continue

        width, height, duration = inspect_video_file(video_file)
        is_vertical = height > width
        is_short_video = is_vertical or (not is_sum and not is_vc and duration <= 120)

        if not is_vertical:
            if is_sum and duration < 90:
                print(f"[FAST SKIP] Clip too short for Match Summary ({int(duration)}s): {item['title']}")
                os.remove(video_file)
                continue
            if not is_sum and not is_vc and duration < 180:
                print(f"[FAST SKIP] Horizontal video shorter than 3 mins ({int(duration)}s): {item['title']}")
                os.remove(video_file)
                continue

        if is_vertical:
            enhance_short_to_1080p(video_file, width, height)

        original_title = item["title"]
        ai_info = item.get("ai_info", {})
        seo_title = ai_info.get("seo_title") or original_title
        seo_desc = ai_info.get("seo_description") or original_title
        seo_tags = ai_info.get("seo_tags") or ["فوتبالی", "فوتبال ایران", "لیگ برتر ایران", "ویدیوکست"]

        # انتخاب دقیق پلی‌لیست (شامل پلی‌لیست جدید ویدیوکست)
        if is_short_video and not is_sum and not is_vc:
            target_playlist = "ویدیوهای کوتاه (Shorts)"
            if "#shorts" not in seo_title.lower():
                seo_title = f"{seo_title[:88]} #shorts"
            thumb_to_use = None
        else:
            seo_title = re.sub(r"#shorts", "", seo_title, flags=re.IGNORECASE).strip()
            if is_sum:
                target_playlist = "خلاصه بازی‌ها"
            elif is_vc:
                target_playlist = "ویدیوکست فوتبالی (برنامه اختصاصی)"
            else:
                target_playlist = "مصاحبه و حواشی فوتبال ایران"

            thumb_to_use = prepare_clean_thumbnail(
                video_file,
                item.get("thumb_url"),
                thumb_file,
                duration=duration
            )

        final_description = (
            f"{seo_desc}\n\n"
            f"🌐 رسانه رسمی فوتبالی | Footballi Official\n"
            f"RefTitle: {original_title}"
        )

        try:
            label = "MATCH SUMMARY" if is_sum else ("VIDEOCAST" if is_vc else "VIDEO")
            print(f"[UPLOAD] Publishing ({label}): {seo_title}")
            vid_id = upload_to_youtube(
                youtube, video_file, thumb_to_use, seo_title, final_description, seo_tags
            )

            pl_id = get_or_create_playlist(youtube, target_playlist, playlist_cache)
            add_video_to_playlist(youtube, vid_id, pl_id)

            history["uploaded_urls"].append(item["url"])
            history["uploaded_mp4s"].append(item["mp4_url"])
            history["uploaded_titles"].append(original_title)
            history["uploaded_titles"].append(seo_title)

            existing_titles.add(original_title)
            existing_titles.add(seo_title)
            seen_mp4s.add(item["mp4_url"])
            uploads_today += 1
            uploaded_in_this_run += 1

        except HttpError as e:
            err_str = str(e)
            print("[ERROR] YouTube API Upload error:", err_str)
            if "uploadLimitExceeded" in err_str or "quotaExceeded" in err_str:
                print("💾 [QUEUE-SAVED] YouTube limit reached! Holding remaining unique videos in queue for next reset.")
                remaining_queue.append(item)
                youtube_limit_hit = True
        except Exception as e:
            print("[ERROR] Unexpected upload error:", str(e))
        finally:
            if os.path.exists(video_file):
                os.remove(video_file)
            if os.path.exists(thumb_file):
                os.remove(thumb_file)

    history["pending_queue"] = deduplicate_queue(remaining_queue, seen_urls, seen_mp4s, existing_titles)
    save_history(history)
    print(f"[DONE] Uploaded: {uploaded_in_this_run} | Unique videos waiting in Queue: {len(history['pending_queue'])}")


if __name__ == "__main__":
    main()
