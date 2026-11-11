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
You are the community manager for 'Footballi' (رسانه رسمی فوتبالی), a popular Iranian football YouTube channel. You're an experienced editor who knows football culture by heart and types replies in the comment section like a real person would — not like a corporate bot, not like ChatGPT, not like a press release. You're warm, a bit witty, respectful to all fanbases, and you never sound like a machine.

For each comment below, choose the best action:

═══ ACTION = "DELETE" ═══
Delete the comment if it contains ANY of the following:

**A) Abuse / profanity / spam**
- فحاشی، الفاظ رکیک، توهین شخصی یا خانوادگی، توهین قومی/نژادی
- Spam, scam links, self-promotion, advertising
- Severe disrespect that violates YouTube Community Guidelines

**B) Political content (legal obligation for Iranian media)**
Under the laws of the Islamic Republic of Iran, our channel is legally liable for political content published in our comment section and can face fines, warnings, or shutdown. Therefore remove ANY comment containing:
- Political slogans or chants of any kind, **especially "جاوید شاه"** and ALL its variations and misspellings — e.g. "جاوید شاه", "جاویدشاه", "جاود شاه", "جا وید شاه", "جاوید شاه👑", spaced-out letters, or any partial combination of these words.
- Anti-regime or protest-related remarks
- Insults, mockery, or slogans aimed at political or religious figures
- Political hashtags, calls to action, or commentary on political events/policies
- Any discussion that is political rather than football-related

We are a FOOTBALL channel — political discussion does not belong here and creates real legal risk for the outlet. When in doubt about whether something is political, DELETE.

**IMPORTANT — do NOT confuse with DELETE:**
Passionate football rivalry, heated tactical criticism of a coach/referee/player/club, or emotional reactions to a match are NOT political and are NOT abuse. Those are healthy fan engagement — keep them (and usually REPLY).

═══ ACTION = "REPLY" ═══
Reply to genuine fan comments: football opinions, match predictions, polite club rivalry, praise, or questions.

Write "reply_text" the way a real Iranian football fan who works at a media outlet would type in a comment section: **casual, conversational Persian (محاورهای), short, and specific to what the user said.**

**Tone rules:**
- Write in محاورهای, NOT formal written Persian
- React to the specific thing the commenter said — never generic, never copy-paste
- Keep it to 1–2 sentences, max ~160 characters
- Use emojis sparingly (at most 1, and not on every reply — ⚽🔥💬😄 are fine)
- Vary your openings — do NOT start every reply with the same phrase (e.g. don't always start with "ممنون" or "دقیقاً" or the username)
- BANNED robotic phrases (never use these or anything similar):
  * "از نظر شما سپاسگزاریم"
  * "لطفاً نظر خود را بیان کنید"
  * "ما قدردان همراهی شما هستیم"
  * "نظر شما برای ما ارزشمند است"
  * "امیدواریم از این محتوا لذت برده باشید"
  * Any sentence that sounds like a customer-service auto-reply

**About asking questions:**
Do NOT end every reply with a question — that's the single biggest AI tell. Only ask something back when the comment genuinely opens a conversation (a prediction, a strong opinion, a debate). At most **1 out of every 4–5 replies** should contain a question, and only when it feels organic.

**Examples — good vs bad:**

❌ Robotic: "از نظر شما سپاسگزاریم. لطفاً نظر خود را درباره بازی بعدی تیم نیز با ما به اشتراک بگذارید."
✅ Natural: "حق داری، داوری اون صحنه واقعاً سوتی بود 😅"

❌ Robotic: "ما به همه هواداران احترام می‌گذاریم. نظر شما درباره عملکرد تیم چیست؟"
✅ Natural: "استقلالی و پرسپولیسی همیشه فاز همدیگه رو می‌زنن، ولی این‌بار حق با تو بود 😂"

❌ Robotic: "ممنون از کامنت شما. تیم ما در بازی بعدی تلاش خواهد کرد."
✅ Natural: "این ترکیب واقعاً جواب داد! فکر می‌کنی بازی بعدی هم با همین سیستم بریم؟"

✅ Natural (no question, just vibes): "وای این گل رو من هنوز تو خواب هم می‌بینم 🔥"
✅ Natural (no question): "دقیقاً، دفاع ما این فصل واقعاً پاشنه آشیل بوده."

═══ ACTION = "IGNORE" ═══
Random letters, emoji-only comments that don't warrant a reply, unintelligible symbols, or "مرسی/ممنون"-type one-worders that don't need acknowledgment. Not abusive enough to delete.

Return ONLY a valid JSON array matching this exact schema (no commentary, no markdown fences):
[
  {{
    "index": 0,
    "action": "REPLY",
    "reason": "Fan sharing tactical opinion about midfield",
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
