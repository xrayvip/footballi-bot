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
from ai_seo import ai_batch_evaluate
from scrapers import (
    scrape_website_candidates, extract_video_details_from_page,
    fetch_instagram_reels, is_match_summary, is_duplicate_title
)
from media_utils import (
    inspect_video_file, enhance_short_to_1080p, download_file, prepare_clean_thumbnail
)


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
        # Keep up to 50 items in pending_queue, sorted with Match Summaries first
        pq = history.get("pending_queue", [])
        unique_pq = {}
        for item in pq:
            if isinstance(item, dict) and item.get("mp4_url"):
                unique_pq[item["mp4_url"]] = item
        sorted_pq = sorted(unique_pq.values(), key=lambda x: (0 if x.get("is_summary") else 1))
        history["pending_queue"] = sorted_pq[:50]

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
    print("[START] Footballi Bot starting (Persistent Queue & Multi-Tier AI Edition)...")
    history = load_history()
    youtube = get_authenticated_service()

    existing_titles, uploads_today = get_channel_status_and_24h_count(youtube)
    for t in history.get("uploaded_titles", []):
        existing_titles.add(t)

    seen_mp4s = set(history.get("uploaded_mp4s", []))

    # 1. Load any previously queued videos that were waiting for YouTube quota to open
    pending_queue = [
        q for q in history.get("pending_queue", [])
        if q.get("mp4_url") not in seen_mp4s and not is_duplicate_title(q.get("title", ""), existing_titles)
    ]
    if pending_queue:
        print(f"[QUEUE] Loaded {len(pending_queue)} saved videos from Pending Queue waiting for upload!")

    # 2. Discover new videos from website and Instagram
    web_candidates = scrape_website_candidates(history, existing_titles, save_history)
    reel_candidates = fetch_instagram_reels(history, existing_titles, save_history)

    summaries = [c for c in web_candidates if c.get("is_summary")]
    others = [c for c in web_candidates if not c.get("is_summary")]
    fresh_pool = (summaries + reel_candidates + others)[:20]

    # Enrich only newly discovered candidates (queued items already have mp4_url & AI SEO!)
    newly_enriched = []
    queued_mp4s = {q.get("mp4_url") for q in pending_queue}

    for item in fresh_pool:
        if item["source"] == "web":
            details = extract_video_details_from_page(item["url"])
            if not details:
                continue
            if details.get("rejected_forever"):
                history["uploaded_urls"].append(item["url"])
                continue

            mp4_u = details["mp4_url"]
            if mp4_u in seen_mp4s or mp4_u in queued_mp4s:
                continue

            real_title = details["title"] or item["title"]
            if is_duplicate_title(real_title, existing_titles):
                history["uploaded_urls"].append(item["url"])
                continue

            queued_mp4s.add(mp4_u)
            item["mp4_url"] = mp4_u
            item["title"] = real_title
            item["tags"] = details["tags_text"]
            item["thumb_url"] = details["thumb_url"]
            item["is_summary"] = is_match_summary(real_title)
        else:
            mp4_u = item.get("mp4_url", "")
            if mp4_u in seen_mp4s or mp4_u in queued_mp4s:
                continue
            queued_mp4s.add(mp4_u)

        newly_enriched.append(item)

    # Evaluate newly discovered items with AI and add safe ones to pending_queue
    if newly_enriched:
        ai_results = ai_batch_evaluate(newly_enriched)
        for idx, item in enumerate(newly_enriched):
            ai_info = ai_results.get(idx)
            if not ai_info or not ai_info.get("safe", False):
                reason = ai_info.get("reason", "Rejected") if ai_info else "No AI response"
                print(f"[AI REJECT] ({reason}): {item['title']}")
                if ai_info and not ai_info.get("safe", True):
                    history["uploaded_urls"].append(item["url"])
                continue

            item["ai_info"] = ai_info
            pending_queue.append(item)

    # Sort combined queue so Match Summaries are ALWAYS at the top of the queue
    pending_queue.sort(key=lambda x: (0 if x.get("is_summary") else 1))
    history["pending_queue"] = pending_queue
    save_history(history)

    if not pending_queue:
        print("[IDLE] No videos in queue or website to upload.")
        return

    if uploads_today >= MAX_DAILY_UPLOADS:
        print(f"[QUOTA-WAIT] Daily quota ({uploads_today}/{MAX_DAILY_UPLOADS}) full. {len(pending_queue)} videos safely saved in queue for next reset!")
        return

    playlist_cache = {}
    uploaded_in_this_run = 0
    remaining_queue = []
    youtube_limit_hit = False

    for idx, item in enumerate(pending_queue):
        # If YouTube hit limit or run cap reached, keep the rest in remaining_queue for the next run!
        if youtube_limit_hit or uploaded_in_this_run >= MAX_UPLOADS_PER_RUN or uploads_today >= MAX_DAILY_UPLOADS:
            remaining_queue.append(item)
            continue

        is_sum = item.get("is_summary", False)
        if not is_sum and uploads_today >= (MAX_DAILY_UPLOADS - RESERVED_FOR_SUMMARIES):
            print(f"[RESERVE] Keeping '{item['title'][:40]}' in queue (saving today's last slots for Match Summaries).")
            remaining_queue.append(item)
            continue

        video_file = f"temp_video_{idx}.mp4"
        thumb_file = f"temp_thumb_{idx}.jpg"

        if not download_file(item["mp4_url"], video_file):
            # If download failed temporarily, keep in queue
            remaining_queue.append(item)
            continue

        width, height, duration = inspect_video_file(video_file)
        is_vertical = height > width
        is_short_video = is_vertical or (not is_sum and duration <= 120)

        if not is_vertical:
            if is_sum and duration < 90:
                print(f"[FAST SKIP] Clip too short for Match Summary ({int(duration)}s): {item['title']}")
                os.remove(video_file)
                history["uploaded_urls"].append(item["url"])
                continue
            if not is_sum and duration < 180:
                print(f"[FAST SKIP] Horizontal video shorter than 3 mins ({int(duration)}s): {item['title']}")
                os.remove(video_file)
                history["uploaded_urls"].append(item["url"])
                continue

        if is_vertical:
            enhance_short_to_1080p(video_file, width, height)

        original_title = item["title"]
        ai_info = item.get("ai_info", {})
        seo_title = ai_info.get("seo_title") or original_title
        seo_desc = ai_info.get("seo_description") or original_title
        seo_tags = ai_info.get("seo_tags") or ["فوتبالی", "فوتبال ایران", "لیگ برتر ایران"]

        if is_short_video and not is_sum:
            target_playlist = "ویدیوهای کوتاه (Shorts)"
            if "#shorts" not in seo_title.lower():
                seo_title = f"{seo_title[:88]} #shorts"
            thumb_to_use = None
        else:
            seo_title = re.sub(r"#shorts", "", seo_title, flags=re.IGNORECASE).strip()
            target_playlist = "خلاصه بازی‌ها" if is_sum else "مصاحبه و حواشی فوتبال ایران"

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
            print(f"[UPLOAD] Publishing ({'MATCH SUMMARY' if is_sum else 'VIDEO'}): {seo_title}")
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
                print("💾 [QUEUE-SAVED] YouTube limit reached! Keeping this video and remaining videos in queue for later!")
                remaining_queue.append(item)
                youtube_limit_hit = True
        except Exception as e:
            print("[ERROR] Unexpected upload error:", str(e))
            remaining_queue.append(item)
        finally:
            if os.path.exists(video_file):
                os.remove(video_file)
            if os.path.exists(thumb_file):
                os.remove(thumb_file)

    # Save updated queue and history
    history["pending_queue"] = remaining_queue
    save_history(history)
    print(f"[DONE] Uploaded: {uploaded_in_this_run} | Safely saved in Pending Queue for next runs: {len(remaining_queue)}")


if __name__ == "__main__":
    main()
