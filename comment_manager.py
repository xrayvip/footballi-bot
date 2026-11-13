# -*- coding: utf-8 -*-
import os
import re
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from googleapiclient.errors import HttpError

from config import PROTO, HISTORY_FILE
from footballi_uploader import get_authenticated_service, load_history, save_history

# ارسال در دسته‌های کوچک ۸ تایی تا مدل هوش مصنوعی هیچ کامنتی را جا نیندازد
BATCH_SIZE = 8
MAX_REPLIES_PER_RUN = 15


def get_tehran_reset_key() -> str:
    now_utc = datetime.now(timezone.utc)
    return (now_utc - timedelta(hours=8)).strftime("%Y-%m-%d")


def fetch_all_unreplied_channel_comments(youtube, my_channel_id: str, ignored_ids: set, deleted_ids: set) -> list:
    """
    دریافت تمام کامنت‌های کانال که هنوز توسط کانال ما ریپلای نگرفته‌اند.
    (بررسی مستقیم وجود ریپلای از سمت کانال ما، تا هیچ کامنتی به اشتباه رد نشود)
    """
    unreplied = []
    seen_thread_ids = set()

    def process_thread_items(items):
        for item in items:
            thread_id = item.get("id")
            if not thread_id or thread_id in seen_thread_ids:
                continue
            seen_thread_ids.add(thread_id)

            snip = item.get("snippet", {})
            top_comment = snip.get("topLevelComment", {})
            comment_id = top_comment.get("id", thread_id)

            if comment_id in deleted_ids or comment_id in ignored_ids:
                continue

            c_snip = top_comment.get("snippet", {})
            author_channel_id = c_snip.get("authorChannelId", {}).get("value", "")

            # اگر کامنت را خود کانال ما گذاشته، رد شو
            if author_channel_id == my_channel_id:
                continue

            # بررسی اینکه آیا کانال ما قبلاً به این کامنت ریپلای زده است یا نه
            already_replied_by_us = False
            replies_block = item.get("replies", {}).get("comments", [])
            for rep in replies_block:
                rep_author_id = rep.get("snippet", {}).get("authorChannelId", {}).get("value", "")
                if rep_author_id == my_channel_id:
                    already_replied_by_us = True
                    break

            if already_replied_by_us:
                continue

            text = (c_snip.get("textDisplay") or c_snip.get("textOriginal") or "").strip()
            author = (c_snip.get("authorDisplayName") or "کاربر").strip()
            video_id = snip.get("videoId", "")

            if text:
                unreplied.append({
                    "comment_id": comment_id,
                    "video_id": video_id,
                    "video_title": f"ویدیوی فوتبالی ({video_id})",
                    "author": author,
                    "text": text,
                })

    # ۱. اسکن سراسری تمام کامنت‌های مرتبط با کانال (تا ۲ صفحه = ۲۰۰ کامنت اخیر)
    next_page = None
    for _ in range(2):
        try:
            res = youtube.commentThreads().list(
                part="snippet,replies",
                allThreadsRelatedToChannelId=my_channel_id,
                maxResults=100,
                textFormat="plainText",
                pageToken=next_page,
            ).execute()
            process_thread_items(res.get("items", []))
            next_page = res.get("nextPageToken")
            if not next_page:
                break
        except HttpError as e:
            if "quotaExceeded" in str(e):
                raise
            print(f"[COMMENT-AGENT] Channel-wide scan note: {e}")
            break
        except Exception as e:
            print(f"[COMMENT-AGENT] Scan error: {e}")
            break

    return unreplied


def ai_evaluate_comments(comments_batch: list) -> dict:
    if not comments_batch:
        return {}

    payload_items = []
    for idx, c in enumerate(comments_batch):
        payload_items.append({
            "index": idx,
            "video_title": c["video_title"],
            "author": c["author"],
            "comment_text": c["text"],
        })

    prompt = f"""
You are the community manager for 'Footballi' (رسانه رسمی فوتبالی), a popular Iranian football YouTube channel. You type replies in the comment section like a real person — casual, warm, witty, short, never robotic.

═══ OVERRIDING PRINCIPLE: WHEN IN DOUBT, DO NOT DELETE ═══
A false positive (deleting a real football fan's comment) is MUCH WORSE than a false negative (letting a rude comment stay). Deleting wrongly damages trust with our audience and looks like censorship. Only delete when the violation is CLEAR, UNAMBIGUOUS, and FULLY VISIBLE in the text.

Additional hard rules:
- If the comment text is truncated, cut off, incomplete, or you cannot read the full sentence → DO NOT delete. Choose REPLY.
- If you are between 50/50 on whether something counts as a violation → DO NOT delete.
- Do not infer, guess, or "fill in" missing words to justify deletion.
- A rude or angry football comment is NOT the same as profanity. Anger at a referee is not a violation.

═══ ACTION = "DELETE" ═══
Delete ONLY when the comment CLEARLY contains one of these:

**A) Explicit profanity / severe abuse**
- Explicit sexual profanity (فحش رکیک جنسی — e.g. کص، کیر، خایه، گایید، کون، دیوث)
- Insults explicitly directed at someone's FAMILY (مادرت، خواهرت، پدرت، ناموست + insult)
- Ethnic/racial slurs
- Spam, scam links, advertising, self-promotion

**B) Political content — CLOSED, NARROW LIST ONLY**
Under Iranian law our channel is legally liable for political content published in our comment section. Delete ONLY comments that clearly contain one of the following:
1. "جاوید شاه" and every possible spelling, spacing, or emoji variation.
2. Explicit anti-regime slogans or chants ("مرگ بر...", "زن زندگی آزادی", etc.)
3. Direct insults or mockery of top political/religious figures (خامنه‌ای، خمینی، رئیسی، etc.)
4. Calls for protests, regime change, or political action
5. Political hashtags, party names, or references to political events.

If it is NOT clearly one of the five categories above → it is NOT political → DO NOT delete under this rule.

**🚫 CRITICAL — these are NOT political and MUST NOT be deleted:**
- Referee criticism of ANY kind: «داور پولیسی»، «داور رشوه‌گیر»، «داور بیشرف»، «داور فاسد»، «داور سوتی»
- Allegations of corruption inside FOOTBALL institutions (فدراسیون فوتبال، کمیته داوران، سازمان لیگ، باشگاه‌ها)
- Criticism of club managers, coaches, players, agents, or sports broadcasters
- Anger at VAR, offside calls, penalty decisions, red cards
- Classic Iranian football fan banter («لنگی»، «کیسه‌کش»، «تراختوری», etc.)

═══ ACTION = "REPLY" (DEFAULT FOR ALL NORMAL COMMENTS) ═══
For EVERY comment that is NOT "DELETE" (unless it is meaningless random characters), you MUST choose "REPLY" and write a short, natural, conversational Persian (فارسی محاوره‌ای) reply (1 short sentence, max 12 words, 0 or 1 emoji).
- Sound like a witty, knowledgeable Iranian football fan running the page.
- Never use formal/robotic phrases like «با تشکر از نظر شما» or «همراه گرامی».

Return ONLY a valid JSON object mapping EVERY index ("0", "1", ...) to its decision:
{{
  "0": {{"action": "REPLY", "reply": "متن پاسخ صمیمی و کوتاه", "reason": "fan comment"}},
  "1": {{"action": "DELETE", "reply": "", "reason": "explicit sexual profanity"}}
}}

Comments to evaluate:
{json.dumps(payload_items, ensure_ascii=False, indent=2)}
"""

    arvan_key = os.environ.get("ARVAN_API_KEY", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()

    # ۱. تلاش با مدل‌های سریع و ارزان Arvan
    if arvan_key:
        for model_name in ["Gemini-2.5-Flash-lite", "GPT-4.1-Nano", "GPT-4o-mini"]:
            try:
                resp = requests.post(
                    f"{PROTO}ai.najafivip.ir/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {arvan_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model_name,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3,
                    },
                    timeout=25,
                )
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"]
                    m = re.search(r"\{.*\}", content, re.DOTALL)
                    if m:
                        parsed = json.loads(m.group(0))
                        if parsed:
                            print(f"[AI-TIER-2] Evaluated batch of {len(comments_batch)} comments with {model_name}.")
                            return parsed
            except Exception as e:
                print(f"[WARN] Arvan {model_name} note: {e}")

    # ۲. تلاش با جمینای رایگان گوگل
    if gemini_key:
        try:
            g_url = f"{PROTO}generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
            resp = requests.post(
                g_url,
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=25,
            )
            if resp.status_code == 200:
                content = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                m = re.search(r"\{.*\}", content, re.DOTALL)
                if m:
                    return json.loads(m.group(0))
        except Exception as e:
            print(f"[WARN] Gemini direct note: {e}")

    return {}


def delete_comment_safely(youtube, comment_id: str) -> bool:
    try:
        youtube.comments().setModerationStatus(
            id=comment_id,
            moderationStatus="rejected",
        ).execute()
        return True
    except Exception:
        try:
            youtube.comments().delete(id=comment_id).execute()
            return True
        except Exception as e:
            print(f"[WARN] Could not remove comment {comment_id}: {e}")
            return False


def reply_to_comment(youtube, parent_id: str, reply_text: str) -> bool:
    try:
        youtube.comments().insert(
            part="snippet",
            body={
                "snippet": {
                    "parentId": parent_id,
                    "textOriginal": reply_text,
                }
            },
        ).execute()
        return True
    except Exception as e:
        print(f"[WARN] Could not reply to {parent_id}: {e}")
        return False


def main():
    print("[COMMENT-AGENT] Starting Footballi AI Comment Moderator (Full Unreplied Scan in 8-Item Batches)...")
    state = load_history()
    reset_key = get_tehran_reset_key()

    if state.get("quota_exhausted_date") == reset_key:
        print("⏸️ [COMMENT-AGENT] Daily YouTube API quota is full for today. Skipping until 11:30 AM reset.")
        return

    try:
        youtube = get_authenticated_service()
        ch_res = youtube.channels().list(part="id", mine=True).execute()
        my_channel_id = ch_res["items"][0]["id"]
    except HttpError as e:
        if "quotaExceeded" in str(e):
            state["quota_exhausted_date"] = reset_key
            save_history(state)
            print("⏸️ [COMMENT-AGENT] Daily YouTube API quota reached.")
            return
        print(f"[COMMENT-AGENT] Auth/Channel error: {e}")
        return

    # فقط کامنت‌های حذف‌شده یا کاملاً بی‌معنی (IGNORE صریح) رد می‌شوند؛ هر کامنت بدون ریپلای بررسی می‌شود!
    ignored_ids = set(state.get("ignored_comments", []))
    deleted_ids = set(state.get("deleted_comments", []))

    try:
        unreplied_comments = fetch_all_unreplied_channel_comments(
            youtube, my_channel_id, ignored_ids, deleted_ids
        )
    except HttpError as e:
        if "quotaExceeded" in str(e):
            state["quota_exhausted_date"] = reset_key
            save_history(state)
            print("⏸️ [COMMENT-AGENT] Daily YouTube API quota reached during scan.")
            return
        unreplied_comments = []

    if not unreplied_comments:
        print("[COMMENT-AGENT] All existing and new comments on the channel have been reviewed and replied to!")
        return

    print(f"[COMMENT-AGENT] Found {len(unreplied_comments)} unreplied comments across the channel. Processing in batches of {BATCH_SIZE}...")

    deleted_count = 0
    replied_count = 0

    for start_idx in range(0, len(unreplied_comments), BATCH_SIZE):
        if replied_count >= MAX_REPLIES_PER_RUN:
            break

        batch = unreplied_comments[start_idx : start_idx + BATCH_SIZE]
        decisions = ai_evaluate_comments(batch)
        if not decisions:
            continue

        for idx, c in enumerate(batch):
            decision = decisions.get(str(idx)) or decisions.get(idx)
            # اگر هوش مصنوعی این آیتم را جا انداخته، آن را علامت نزن تا در اجرای بعدی بررسی شود!
            if not isinstance(decision, dict):
                continue

            action = str(decision.get("action", "")).upper().strip()
            reply_text = str(decision.get("reply", "")).strip()
            reason = str(decision.get("reason", "")).strip()
            cid = c["comment_id"]

            if action == "DELETE":
                print(f"🚫 [OFFENSIVE] Comment by {c['author']}: '{c['text'][:55]}' -> {reason}")
                if delete_comment_safely(youtube, cid):
                    deleted_ids.add(cid)
                    deleted_count += 1
                    print(f"🗑️ [MODERATOR] Removed offensive comment ({cid}).")

            elif action == "REPLY" and reply_text:
                if replied_count < MAX_REPLIES_PER_RUN:
                    print(f"✨ [ENGAGE] Replying to {c['author']}: '{c['text'][:50]}'")
                    if reply_to_comment(youtube, cid, reply_text):
                        replied_count += 1
                        print(f"💬 [REPLIED] Sent official reply: '{reply_text}'")
                        time.sleep(1.5)

            elif action == "IGNORE":
                ignored_ids.add(cid)

    state["ignored_comments"] = list(ignored_ids)
    state["deleted_comments"] = list(deleted_ids)
    save_history(state)
    print(f"[COMMENT-AGENT] Run complete! Deleted offensive: {deleted_count} | Official replies posted: {replied_count}")


if __name__ == "__main__":
    main()
