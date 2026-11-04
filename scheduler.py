import time
import random
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from telethon.errors import FloodWaitError
from telethon.tl.functions import PingRequest
import db # <-- Yahan change kiya
from userbot_manager import get_userbot

log = logging.getLogger("scheduler")
IST_TZ = ZoneInfo("Asia/Kolkata")
PING_OFFSET = 0.090

def fmt_ist_12h(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST_TZ).strftime("%I:%M:%S %p IST")

def fmt_ist_full(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST_TZ).strftime("%d %b %Y, %I:%M:%S %p IST")

async def runner(doc: dict, bot_client):
    try:
        target_ts = float(doc["scheduled_ts"])
        fire_ts = target_ts - PING_OFFSET
        while True:
            diff = fire_ts - time.time()
            if diff <= 0: break
            if diff > 120: await asyncio.sleep(min(60, diff - 10))
            elif diff > 10: await asyncio.sleep(2.0)
            elif diff > 2: await asyncio.sleep(0.3)
            elif diff > 0.1: await asyncio.sleep(0.02)
            else: await asyncio.sleep(0.005)

        fresh = await db.sched_col.find_one({"_id": doc["_id"]})
        if not fresh or fresh.get("status") != "pending": return

        cl = get_userbot(fresh["owner_id"])
        if cl is None:
            await db.sched_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "failed", "error": "userbot offline"}})
            return

        try: await asyncio.wait_for(cl(PingRequest(ping_id=random.randint(1, 10**9))), timeout=1.5)
        except Exception: pass

        async def _send():
            try:
                t, txt = fresh["target_id"], fresh["text"]
                if fresh.get("comment_to"):
                    try: await cl.send_message(t, txt, comment_to=int(fresh["comment_to"]))
                    except Exception: await cl.send_message(t, txt)
                elif fresh.get("reply_to_msg_id"):
                    try: await cl.send_message(t, txt, reply_to=fresh["reply_to_msg_id"])
                    except Exception: await cl.send_message(t, txt)
                else: await cl.send_message(t, txt)

                await db.sched_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "sent", "sent_at": time.time()}})
                
                if fresh.get("confirm_chat_id") and fresh.get("confirm_msg_id"):
                    try: await bot_client.delete_messages(int(fresh["confirm_chat_id"]), [int(fresh["confirm_msg_id"])])
                    except Exception: pass
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
                try:
                    await cl.send_message(fresh["target_id"], fresh["text"])
                    await db.sched_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "sent", "sent_at": time.time()}})
                except Exception as e2: await db.sched_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "failed", "error": str(e2)[:200]}})
            except Exception as e:
                await db.sched_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "failed", "error": str(e)[:200]}})
        asyncio.create_task(_send())
    except Exception as e: log.error(f"runner: {e}", exc_info=True)

async def restore_all(bot_client):
    cursor = db.sched_col.find({"status": "pending", "scheduled_ts": {"$gt": time.time() - 300}})
    count = 0
    async for d in cursor:
        asyncio.create_task(runner(d, bot_client))
        count += 1
