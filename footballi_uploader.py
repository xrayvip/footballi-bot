# footballi_uploader.py
import os
import re
import json
import time
import subprocess
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
from ai_seo import ai_batch_evaluate, sanitize_url
from scrapers import (
    scrape_website_candidates, extract_video_details_from_page,
    fetch_instagram_reels, is_match_summary, is_duplicate_title
)
from media_utils import (
    inspect_video_file, enhance_short_to_1080p, download_file,
    build_hd_thumbnail, render_branded_overlay
)


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {
                        "uploaded_urls": data,
                        "uploaded_titles": [],
                        "fixed_thumbs": [],
                        "last_apify_run": 0
                    }
                if isinstance(data, dict):
                    data.setdefault("uploaded_urls", [])
                    data.setdefault("uploaded_titles", [])
                    data.setdefault("fixed_thumbs", [])
                    data.setdefault("last_apify_run", 0)
                    return data
        except Exception as e:
            print(f"[WARN] Could not read history file: {e}")
    return {"uploaded_urls": [], "uploaded_titles": [], "fixed_thumbs": [], "last_apify_run": 0}


def save_history(history):
    try:
        history["uploaded_urls"] = list(dict.fromkeys(history.get("uploaded_urls", [])))[-2000:]
        history["uploaded_titles"] = list(dict.fromkeys(history.get("uploaded_titles", [])))[-1500:]
        history["fixed_thumbs"] = list(dict.fromkeys(history.get("fixed_thumbs", [])))[-1000:]
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
    channel_videos = []

    try:
        now_utc = datetime.now(timezone.utc)
        reset_time = now_utc.replace(hour=8, minute=0, second=0, microsecond=0)
        if now_utc < reset_time:
            reset_time -= timedelta(days=1)

        ch_res = youtube.channels().list(part="contentDetails", mine=True).execute()
        items = ch_res.get("items", [])
        if not items:
            return existing_titles, 0, []

        uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        pl_res = youtube.playlistItems().list(
            part="snippet", playlistId=uploads_playlist_id, maxResults=50
        ).execute()

        for item in pl_res.get("items", []):
            snippet = item.get("snippet", {})
            vid_id = snippet.get("resourceId", {}).get("videoId", "")
            title = snippet.get("title", "").strip()
            desc = snippet.get("description", "")
            pub_str = snippet.get("publishedAt", "")

            if title:
                existing_titles.add(title)

            orig_ref = ""
            for line in desc.splitlines():
                if line.strip().startswith("RefTitle:"):
                    orig_ref = line.split("RefTitle:", 1)[1].strip()
                    if orig_ref:
                        existing_titles.add(orig_ref)

            if vid_id:
                channel_videos.append({
                    "video_id": vid_id,
                    "title": title,
                    "ref_title": orig_ref or title,
                    "description": desc
                })

            if pub_str:
                pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                if pub_dt >= reset_time:
                    uploads_since_reset += 1

        print(
            f"[STATUS] Channel indexed titles: {len(existing_titles)} | "
            f"Uploads since 11:30 AM reset: {uploads_since_reset}/{MAX_DAILY_UPLOADS} "
            f"(Reserved for Match Summaries: {RESERVED_FOR_SUMMARIES})"
        )
        return existing_titles, uploads_since_reset, channel_videos
    except Exception as e:
        print(f"[WARN] Failed to read channel upload history: {e}")
        return existing_titles, 0, []


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
            print("[THUMBNAIL] Branded 1280x720 HD thumbnail applied.")
        except Exception as e:
            print(f"[INFO] Thumbnail upload note: {e}")

    return video_id


def fix_existing_thumbnails_fast(youtube, channel_videos, history):
    """
    Lightweight, ultra-fast thumbnail backfill: checks at most 2 videos per run
    with a strict 15s timeout so the workflow never gets stuck.
    """
    fixed_set = set(history.get("fixed_thumbs", []))
    candidates = [
        v for v in channel_videos
        if v["video_id"] not in fixed_set and "#shorts" not in v["title"].lower()
    ]

    if not candidates:
        return

    print(f"[THUMB-REPAIR] Quick check on {len(candidates)} legacy videos...")
    web_urls = [u for u in reversed(history.get("uploaded_urls", [])) if "/video/" in str(u)][:12]

    vf_scale = "scale=1280:720:flags=lanczos:force_original_aspect_ratio=increase,crop=1280:720,unsharp=5:5:0.7:3:3:0.0"

    for v in candidates[:2]:
        vid_id = v["video_id"]
        matched_mp4 = None

        for page_u in web_urls:
            details = extract_video_details_from_page(page_u)
            if details and is_duplicate_title(details.get("title", ""), {v["ref_title"], v["title"]}):
                matched_mp4 = details.get("mp4_url")
                break

        if matched_mp4:
            out_jpg = f"repair_thumb_{vid_id}.jpg"
            cmd = ["ffmpeg", "-y", "-v", "error", "-ss", "20", "-i", sanitize_url(matched_mp4), "-vf", f"thumbnail=15,{vf_scale}", "-frames:v", "1", "-q:v", "2", out_jpg]
            try:
                subprocess.run(cmd, timeout=20)
                if os.path.exists(out_jpg) and os.path.getsize(out_jpg) > 10_000:
                    short_hl = v["title"].split("|")[0].strip()
                    render_branded_overlay(out_jpg, short_hl, is_summary=is_match_summary(v["title"]))
                    youtube.thumbnails().set(videoId=vid_id, media_body=MediaFileUpload(out_jpg)).execute()
                    print(f"[THUMB-REPAIR] Repaired: {short_hl[:35]}")
            except Exception:
                pass
            finally:
                if os.path.exists(out_jpg):
                    os.remove(out_jpg)

        history["fixed_thumbs"].append(vid_id)
        save_history(history)


def main():
    print("[START] Footballi Official News Agency Bot starting (Fast HD Thumbnail Edition)...")
    history = load_history()
    youtube = get_authenticated_service()

    existing_titles, uploads_today, channel_videos = get_channel_status_and_24h_count(youtube)
    for t in history.get("uploaded_titles", []):
        existing_titles.add(t)

    # Quick & light check (max 2 videos per run)
    fix_existing_thumbnails_fast(youtube, channel_videos, history)

    if uploads_today >= MAX_DAILY_UPLOADS:
        print("[STOP] Daily quota reached. Waiting until 11:30 AM tomorrow.")
        return

    web_candidates = scrape_website_candidates(history, existing_titles, save_history)
    reel_candidates = fetch_instagram_reels(history, existing_titles, save_history)

    summaries = [c for c in web_candidates if c.get("is_summary")]
    others = [c for c in web_candidates if not c.get("is_summary")]

    non_summary_cap = MAX_DAILY_UPLOADS - RESERVED_FOR_SUMMARIES
    if uploads_today >= non_summary_cap:
        print("[QUOTA] Reserved summary zone active! Processing Match Summaries only.")
        pool = summaries[:10]
    else:
        pool = (summaries + reel_candidates + others)[:20]

    if not pool:
        save_history(history)
        print("[IDLE] No new candidate videos found.")
        return

    enriched_pool = []
    for item in pool:
        if item["source"] == "web":
            details = extract_video_details_from_page(item["url"])
            if not details:
                history["uploaded_urls"].append(item["url"])
                continue
            item["mp4_url"] = details["mp4_url"]
            item["title"] = details["title"] or item["title"]
            item["tags"] = details["tags_text"]
            item["thumb_url"] = details["thumb_url"]
            item["is_summary"] = is_match_summary(item["title"])
        enriched_pool.append(item)

    if not enriched_pool:
        save_history(history)
        print("[IDLE] No valid videos remained after page inspection.")
        return

    ai_results = ai_batch_evaluate(enriched_pool)

    playlist_cache = {}
    uploaded_in_this_run = 0

    for idx, item in enumerate(enriched_pool):
        if uploaded_in_this_run >= MAX_UPLOADS_PER_RUN or uploads_today >= MAX_DAILY_UPLOADS:
            break

        is_sum = item.get("is_summary", False)
        if not is_sum and uploads_today >= (MAX_DAILY_UPLOADS - RESERVED_FOR_SUMMARIES):
            print(f"[RESERVE] Skipping non-summary '{item['title'][:40]}' (saving last 3 slots for Match Summaries).")
            continue

        ai_info = ai_results.get(idx)
        if ai_info is not None and not ai_info.get("safe", False):
            print(f"[AI REJECT] ({ai_info.get('reason')}): {item['title']}")
            history["uploaded_urls"].append(item["url"])
            save_history(history)
            continue

        video_file = f"temp_video_{idx}.mp4"
        thumb_file = f"temp_thumb_{idx}.jpg"

        if not download_file(item["mp4_url"], video_file):
            continue

        width, height, duration = inspect_video_file(video_file)
        is_vertical = height > width
        is_short_video = is_vertical or duration <= 180

        if not is_vertical and not is_sum and duration < 180:
            print(f"[FAST SKIP] Horizontal video shorter than 3 mins ({int(duration)}s): {item['title']}")
            os.remove(video_file)
            history["uploaded_urls"].append(item["url"])
            save_history(history)
            continue

        if is_vertical:
            enhance_short_to_1080p(video_file, width, height)

        original_title = item["title"]

        if ai_info:
            seo_title = ai_info.get("seo_title") or original_title
            thumb_headline = ai_info.get("thumb_text") or seo_title.split("|")[0].strip()
            seo_desc = ai_info.get("seo_description") or original_title
            seo_tags = ai_info.get("seo_tags") or ["فوتبالی", "فوتبال ایران", "لیگ برتر ایران"]
        else:
            seo_title = original_title
            thumb_headline = original_title[:32]
            seo_desc = (
                f"{original_title}\n\n"
                "رسانه رسمی فوتبالی؛ پوشش زنده و لحظه‌به‌لحظه مسابقات، خلاصه بازی‌ها و ویدیوهای اختصاصی فوتبال ایران.\n"
                "#فوتبالی #فوتبال_ایران #لیگ_برتر"
            )
            seo_tags = ["فوتبالی", "فوتبال ایران", "لیگ برتر ایران", "خلاصه بازی", "ورزش"]

        if is_short_video:
            target_playlist = "ویدیوهای کوتاه (Shorts)"
            if "#shorts" not in seo_title.lower():
                seo_title = f"{seo_title[:88]} #shorts"
            thumb_to_use = None
        else:
            seo_title = re.sub(r"#shorts", "", seo_title, flags=re.IGNORECASE).strip()
            target_playlist = "خلاصه بازی‌ها" if is_sum else "مصاحبه و حواشی فوتبال ایران"

            thumb_to_use = build_hd_thumbnail(
                video_file,
                item.get("thumb_url"),
                thumb_file,
                duration=duration,
                headline_text=thumb_headline,
                is_summary=is_sum
            )

        final_description = (
            f"{seo_desc}\n\n"
            f"🌐 رسانه رسمی فوتبالی | Footballi Official\n"
            f"RefTitle: {original_title}"
        )

        try:
            print(f"[UPLOAD] Publishing with Official SEO Title: {seo_title}")
            vid_id = upload_to_youtube(
                youtube, video_file, thumb_to_use, seo_title, final_description, seo_tags
            )

            pl_id = get_or_create_playlist(youtube, target_playlist, playlist_cache)
            add_video_to_playlist(youtube, vid_id, pl_id)

            history["uploaded_urls"].append(item["url"])
            history["uploaded_titles"].append(original_title)
            history["uploaded_titles"].append(seo_title)
            history["fixed_thumbs"].append(vid_id)
            save_history(history)

            existing_titles.add(original_title)
            existing_titles.add(seo_title)
            uploads_today += 1
            uploaded_in_this_run += 1

        except HttpError as e:
            err_str = str(e)
            print("[ERROR] YouTube API Upload error:", err_str, "- skipping.")
            if "uploadLimitExceeded" in err_str:
                print("[STOP] YouTube daily upload limit reached for this account.")
                break
        except Exception as e:
            print("[ERROR] Unexpected upload error:", str(e), "- skipping.")
        finally:
            if os.path.exists(video_file):
                os.remove(video_file)
            if os.path.exists(thumb_file):
                os.remove(thumb_file)

    print(f"[DONE] Run completed. Total videos uploaded in this run: {uploaded_in_this_run}")


if __name__ == "__main__":
    main()
