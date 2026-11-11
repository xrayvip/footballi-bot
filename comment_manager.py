# comment_manager.py
# Autonomous AI Comment Moderator & Community Engagement Agent (Full History + New Comments)
import os
import json
import time
from googleapiclient.errors import HttpError

from config import HISTORY_FILE
from ai_seo import call_ai_json
from footballi_uploader import get_authenticated_service, load_history, save_history

# در هر بار اجرا حداکثر ۲۵ کامنت (از جدیدترین تا قدیمی‌ترین) بررسی و تا ۱۰ ریپلای ارسال می‌شود
MAX_COMMENTS_TO_PROCESS_PER_RUN = 25
MAX_REPLIES_PER_RUN = 10


def extract_thread_item(item, channel_id, processed_ids_set):
    """Helper to parse a single YouTube commentThread item if unprocessed"""
    thread_id = item.get("id")
    top_comment = item.get("snippet", {}).get("topLevelComment", {})
    comment_id = top_comment.get("id") or thread_id
    if not comment_id or comment_id in processed_ids_set:
        return None

    c_snippet = top_comment.get("snippet", {})
    author_channel_id = c_snippet.get("authorChannelId", {}).get("value", "")

    # Skip comments written by our own channel
    if author_channel_id == channel_id:
        processed_ids_set.add(comment_id)
        return None

    # Check if we already replied to this comment in the past
    replies = item.get("replies", {}).get("comments", [])
    already_replied_by_us = any(
        r.get("snippet", {}).get("authorChannelId", {}).get("value") == channel_id
        for r in replies
    )
    if already_replied_by_us:
        processed_ids_set.add(comment_id)
        return None

    vid_id = item.get("snippet", {}).get("videoId", "")
    text = c_snippet.get("textDisplay", "").strip()
    author_name = c_snippet.get("authorDisplayName", "کاربر").strip()

    if not text:
        processed_ids_set.add(comment_id)
        return None

    return {
        "comment_id": comment_id,
        "video_id": vid_id,
        "author": author_name,
        "text": text
    }


def fetch_unprocessed_comments(youtube, channel_id, processed_ids_set):
    """
    1. Paginates through channel-wide commentThreads (both new and older existing comments).
    2. Also scans individual existing videos on the channel so zero older comments are missed.
    """
    raw_threads = []
    seen_in_batch = set()
    video_ids_to_lookup = set()

    # Pass 1: Channel-wide multi-page scan (up to 3 pages = 300 recent & older comments)
    page_token = None
    for _ in range(3):
        if len(raw_threads) >= MAX_COMMENTS_TO_PROCESS_PER_RUN:
            break
        try:
            req = youtube.commentThreads().list(
                part="snippet,replies",
                allThreadsRelatedToChannelId=channel_id,
                maxResults=100,
                pageToken=page_token,
                textFormat="plainText"
            )
            res = req.execute()
            for item in res.get("items", []):
                parsed = extract_thread_item(item, channel_id, processed_ids_set)
                if parsed and parsed["comment_id"] not in seen_in_batch:
                    seen_in_batch.add(parsed["comment_id"])
                    if parsed["video_id"]:
                        video_ids_to_lookup.add(parsed["video_id"])
                    raw_threads.append(parsed)
                    if len(raw_threads) >= MAX_COMMENTS_TO_PROCESS_PER_RUN:
                        break

            page_token = res.get("nextPageToken")
            if not page_token:
                break
        except Exception as e:
            print(f"[COMMENT-AGENT] Channel-wide scan note: {e}")
            break

    # Pass 2: Deep scan across existing uploaded videos if batch still has room
    if len(raw_threads) < MAX_COMMENTS_TO_PROCESS_PER_RUN:
        try:
            ch_res = youtube.channels().list(part="contentDetails", mine=True).execute()
            uploads_pl = ch_res["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
            pl_res = youtube.playlistItems().list(
                part="snippet", playlistId=uploads_pl, maxResults=50
            ).execute()

            for pl_item in pl_res.get("items", []):
                if len(raw_threads) >= MAX_COMMENTS_TO_PROCESS_PER_RUN:
                    break
                v_id = pl_item.get("snippet", {}).get("resourceId", {}).get("videoId")
                if not v_id:
                    continue
                try:
                    v_threads = youtube.commentThreads().list(
                        part="snippet,replies",
                        videoId=v_id,
                        maxResults=50,
                        textFormat="plainText"
                    ).execute()
                    for item in v_threads.get("items", []):
                        parsed = extract_thread_item(item, channel_id, processed_ids_set)
                        if parsed and parsed["comment_id"] not in seen_in_batch:
                            seen_in_batch.add(parsed["comment_id"])
                            video_ids_to_lookup.add(v_id)
                            raw_threads.append(parsed)
                            if len(raw_threads) >= MAX_COMMENTS_TO_PROCESS_PER_RUN:
                                break
                except Exception:
                    continue
        except Exception:
            pass

    # Fetch video titles so AI knows the exact match/interview topic for every comment
    video_titles_map = {}
    vid_list = list(video_ids_to_lookup)
    for i in range(0, len(vid_list), 25):
        chunk = vid_list[i:i + 25]
        try:
            v_res = youtube.videos().list(part="snippet", id=",".join(chunk)).execute()
            for v_item in v_res.get("items", []):
                video_titles_map[v_item["id"]] = v_item.get("snippet", {}).get("title", "")
        except Exception:
            pass

    for t in raw_threads:
        t["video_title"] = video_titles_map.get(t["video_id"], "ویدیوی فوتبالی")

    return raw_threads


def ai_evaluate_comments(comments_batch):
    if not comments_batch:
        return {}

    payload_items = []
    for idx, c in enumerate(comments_batch):
        payload_items.append({
            "index": idx,
            "video_title": c["video_title"],
            "author": c["author"],
            "comment_text": c["text"]
        })

    prompt = f"""
You are the Official Community Manager and Editorial AI Agent for 'Footballi' (رسانه رسمی فوتبالی), Iran's premier football news and broadcast network on YouTube.

Review each user comment below (both older existing comments and new comments, along with the title of the video it was posted on) and autonomously decide the best action based on official media ethics, YouTube Community Guidelines, and audience engagement best practices:

1. ACTION = "DELETE":
   - Choose "DELETE" if the comment contains profanity, vulgar language (فحاشی و الفاظ رکیک), personal/family insults, ethnic/racial slurs, spam/advertising links, scams, or severe disrespect violating YouTube policies.
   - Note: Passionate football rivalry or tactical criticism of a coach/referee/team WITHOUT profanity or vulgar insults is healthy fan engagement and must NOT be deleted!

2. ACTION = "REPLY":
   - Choose "REPLY" for genuine fan comments, football opinions, match predictions, polite club rivalry, praise, or questions.
   - Write "reply_text" in fluent, natural, warm, and authoritative Persian representing 'رسانه رسمی فوتبالی':
     * Maintain journalistic impartiality (respect fans of all clubs: Esteghlal, Persepolis, Sepahan, Tractor, Malavan, etc.).
     * Directly reference the topic of the video or the user's point (never use robotic copy-paste replies!).
     * Keep the reply concise (1 to 2 sentences, max 180 characters) and when appropriate, end with an engaging football question to spark further discussion in the comments.
     * Use at most 1 tasteful emoji (⚽, 🙏, 🔥, 💬).

3. ACTION = "IGNORE":
   - Choose "IGNORE" if the comment is just random letters, unintelligible symbols, or doesn't warrant a public reply, but isn't abusive enough to delete.

Return ONLY a valid JSON array matching this exact schema:
[
  {{
    "index": 0,
    "action": "REPLY",
    "reason": "Constructive fan comment about the match",
    "reply_text": "..."
  }}
]

Comments to review:
{json.dumps(payload_items, ensure_ascii=False)}
"""

    parsed = call_ai_json(prompt)
    if isinstance(parsed, list):
        return {item["index"]: item for item in parsed if isinstance(item, dict) and "index" in item}
    return {}


def delete_offensive_comment(youtube, comment_id):
    """Removes an offensive/spam comment using YouTube moderation/delete API"""
    for status_type in ["rejected", "heldForReview"]:
        try:
            youtube.comments().setModerationStatus(
                id=comment_id,
                moderationStatus=status_type
            ).execute()
            print(f"🗑️ [MODERATOR] Removed offensive comment ({comment_id}) via status='{status_type}'.")
            return True
        except HttpError:
            continue
        except Exception:
            continue

    try:
        youtube.comments().delete(id=comment_id).execute()
        print(f"🗑️ [MODERATOR] Deleted offensive comment ({comment_id}).")
        return True
    except Exception as e:
        print(f"[WARN] Could not delete comment {comment_id}: {e}")
        return False


def post_official_reply(youtube, parent_comment_id, reply_text):
    try:
        body = {
            "snippet": {
                "parentId": parent_comment_id,
                "textOriginal": reply_text[:450]
            }
        }
        youtube.comments().insert(part="snippet", body=body).execute()
        print(f"💬 [REPLIED] Sent official reply: '{reply_text}'")
        return True
    except Exception as e:
        print(f"[WARN] Could not post reply to {parent_comment_id}: {e}")
        return False


def main():
    print("[COMMENT-AGENT] Starting Footballi AI Comment Moderator (Scanning Existing + New Comments)...")
    history = load_history()
    history.setdefault("processed_comments", [])
    processed_set = set(history["processed_comments"])

    youtube = get_authenticated_service()

    ch_res = youtube.channels().list(part="id", mine=True).execute()
    items = ch_res.get("items", [])
    if not items:
        print("[WARN] Channel ID not found.")
        return
    channel_id = items[0]["id"]

    unprocessed_comments = fetch_unprocessed_comments(youtube, channel_id, processed_set)
    if not unprocessed_comments:
        history["processed_comments"] = list(processed_set)[-3000:]
        save_history(history)
        print("[COMMENT-AGENT] All existing and new comments on the channel have been reviewed!")
        return

    print(f"[COMMENT-AGENT] Found {len(unprocessed_comments)} unreviewed comments across the channel. Analyzing with AI...")
    decisions = ai_evaluate_comments(unprocessed_comments)

    deleted_count = 0
    replied_count = 0

    for idx, c in enumerate(unprocessed_comments):
        c_id = c["comment_id"]
        decision = decisions.get(idx)
        if not decision:
            continue

        action = decision.get("action", "IGNORE").upper()
        reason = decision.get("reason", "")

        if action == "DELETE":
            print(f"🚫 [OFFENSIVE] Comment by {c['author']} on '{c['video_title'][:30]}': '{c['text'][:50]}' -> {reason}")
            delete_offensive_comment(youtube, c_id)
            deleted_count += 1
            processed_set.add(c_id)

        elif action == "REPLY":
            if replied_count < MAX_REPLIES_PER_RUN:
                reply_txt = decision.get("reply_text", "").strip()
                if reply_txt:
                    print(f"✨ [ENGAGE] Replying to {c['author']} on '{c['video_title'][:35]}': '{c['text'][:45]}'")
                    if post_official_reply(youtube, c_id, reply_txt):
                        replied_count += 1
                        time.sleep(2)
                processed_set.add(c_id)
            else:
                # Keep unprocessed for next run so we reply to it in the next 15-min cycle!
                continue

        else:
            processed_set.add(c_id)

    history["processed_comments"] = list(processed_set)[-3000:]
    save_history(history)
    print(f"[COMMENT-AGENT] Run complete! Deleted offensive: {deleted_count} | Official replies posted: {replied_count}")


if __name__ == "__main__":
    main()
