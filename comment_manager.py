# comment_manager.py
# Autonomous AI Comment Moderator & Community Engagement Agent for Footballi Official
import os
import json
import time
from googleapiclient.errors import HttpError

from config import HISTORY_FILE
from ai_seo import call_ai_json
from footballi_uploader import get_authenticated_service, load_history, save_history

MAX_COMMENTS_TO_PROCESS_PER_RUN = 15
MAX_REPLIES_PER_RUN = 6


def fetch_unprocessed_comments(youtube, channel_id, processed_ids_set):
    """
    Fetches the latest top-level comments across the entire channel that haven't been reviewed yet,
    excluding comments written by the channel itself.
    """
    unprocessed = []
    try:
        req = youtube.commentThreads().list(
            part="snippet,replies",
            allThreadsRelatedToChannelId=channel_id,
            maxResults=40,
            textFormat="plainText"
        )
        res = req.execute()

        video_ids_to_lookup = set()
        raw_threads = []

        for item in res.get("items", []):
            thread_id = item.get("id")
            top_comment = item.get("snippet", {}).get("topLevelComment", {})
            comment_id = top_comment.get("id") or thread_id
            if not comment_id or comment_id in processed_ids_set:
                continue

            c_snippet = top_comment.get("snippet", {})
            author_channel_id = c_snippet.get("authorChannelId", {}).get("value", "")
            # Skip comments posted by our own channel
            if author_channel_id == channel_id:
                processed_ids_set.add(comment_id)
                continue

            # Skip if we already replied to this thread
            replies = item.get("replies", {}).get("comments", [])
            already_replied_by_us = any(
                r.get("snippet", {}).get("authorChannelId", {}).get("value") == channel_id
                for r in replies
            )
            if already_replied_by_us:
                processed_ids_set.add(comment_id)
                continue

            vid_id = item.get("snippet", {}).get("videoId", "")
            text = c_snippet.get("textDisplay", "").strip()
            author_name = c_snippet.get("authorDisplayName", "کاربر").strip()

            if not text:
                processed_ids_set.add(comment_id)
                continue

            if vid_id:
                video_ids_to_lookup.add(vid_id)

            raw_threads.append({
                "comment_id": comment_id,
                "video_id": vid_id,
                "author": author_name,
                "text": text
            })

            if len(raw_threads) >= MAX_COMMENTS_TO_PROCESS_PER_RUN:
                break

        # Fetch video titles so the AI knows the exact match/interview context for each comment
        video_titles_map = {}
        if video_ids_to_lookup:
            v_res = youtube.videos().list(
                part="snippet",
                id=",".join(list(video_ids_to_lookup)[:25])
            ).execute()
            for v_item in v_res.get("items", []):
                video_titles_map[v_item["id"]] = v_item.get("snippet", {}).get("title", "")

        for t in raw_threads:
            t["video_title"] = video_titles_map.get(t["video_id"], "ویدیوی فوتبالی")
            unprocessed.append(t)

    except Exception as e:
        print(f"[COMMENT-AGENT] Note while fetching comments: {e}")

    return unprocessed


def ai_evaluate_comments(comments_batch):
    """
    Sends the batch of new comments (with their video titles) to the AI Agent.
    The AI autonomously decides for each comment:
    - 'DELETE': Profanity, insults, slurs, hate speech, spam links, or policy violations.
    - 'REPLY': Constructive fan opinion, football discussion, question, or praise -> generates an official, warm, neutral journalistic reply.
    - 'IGNORE': Meaningless single characters or comments that shouldn't receive a reply.
    """
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

Review each user comment below (along with the title of the video it was posted on) and autonomously decide the best action based on official media ethics, YouTube Community Guidelines, and audience engagement best practices:

1. ACTION = "DELETE":
   - Choose "DELETE" if the comment contains profanity, vulgar language (فحاشی و الفاظ رکیک), personal/family insults, ethnic/racial slurs, spam/advertising links, scams, or severe disrespect violating YouTube policies.
   - Note: Normal, passionate football rivalry or tactical criticism of a coach/referee/team WITHOUT profanity or vulgar insults is healthy fan engagement and must NOT be deleted!

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
    try:
        # First try setModerationStatus to 'rejected' (works for channel owners on any user's comment)
        youtube.comments().setModerationStatus(
            id=comment_id,
            moderationStatus="rejected"
        ).execute()
        print(f"🗑️ [MODERATOR] Deleted/Rejected offensive comment ({comment_id}).")
        return True
    except HttpError:
        try:
            youtube.comments().delete(id=comment_id).execute()
            print(f"🗑️ [MODERATOR] Deleted offensive comment ({comment_id}).")
            return True
        except Exception as e:
            print(f"[WARN] Could not delete comment {comment_id}: {e}")
            return False


def post_official_reply(youtube, parent_comment_id, reply_text):
    """Posts an official reply under the user's comment thread"""
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
    print("[COMMENT-AGENT] Starting Footballi AI Comment Moderator & Engagement Agent...")
    history = load_history()
    history.setdefault("processed_comments", [])
    processed_set = set(history["processed_comments"])

    youtube = get_authenticated_service()

    # Get our channel ID
    ch_res = youtube.channels().list(part="id", mine=True).execute()
    items = ch_res.get("items", [])
    if not items:
        print("[WARN] Channel ID not found.")
        return
    channel_id = items[0]["id"]

    new_comments = fetch_unprocessed_comments(youtube, channel_id, processed_set)
    if not new_comments:
        history["processed_comments"] = list(processed_set)[-2000:]
        save_history(history)
        print("[COMMENT-AGENT] No new unprocessed comments found.")
        return

    print(f"[COMMENT-AGENT] Analyzing {len(new_comments)} new comments with AI...")
    decisions = ai_evaluate_comments(new_comments)

    deleted_count = 0
    replied_count = 0

    for idx, c in enumerate(new_comments):
        c_id = c["comment_id"]
        decision = decisions.get(idx)
        if not decision:
            continue

        action = decision.get("action", "IGNORE").upper()
        reason = decision.get("reason", "")

        if action == "DELETE":
            print(f"🚫 [OFFENSIVE] Comment by {c['author']}: '{c['text'][:50]}' -> Reason: {reason}")
            delete_offensive_comment(youtube, c_id)
            deleted_count += 1
            processed_set.add(c_id)

        elif action == "REPLY" and replied_count < MAX_REPLIES_PER_RUN:
            reply_txt = decision.get("reply_text", "").strip()
            if reply_txt:
                print(f"✨ [ENGAGE] Comment on '{c['video_title'][:35]}': '{c['text'][:45]}'")
                if post_official_reply(youtube, c_id, reply_txt):
                    replied_count += 1
                    time.sleep(2)
            processed_set.add(c_id)

        else:
            processed_set.add(c_id)

    history["processed_comments"] = list(processed_set)[-2000:]
    save_history(history)
    print(f"[COMMENT-AGENT] Done! Deleted offensive: {deleted_count} | Official replies posted: {replied_count}")


if __name__ == "__main__":
    main()
