import os
import re
import time
import random
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from aiohttp import web
from bson import ObjectId
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.functions import PingRequest

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("bot")

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
IST_TZ = ZoneInfo("Asia/Kolkata")

import db
from db import init_mongo, users_col, sessions_col, sched_col, autocmt_col, pending_col, blocked_col
from userbot_manager import begin_login, submit_otp, submit_2fa, cancel_login, user_bots, spawn_userbot, restore_all_userbots, kill_userbot, get_userbot
import scheduler
from scheduler import runner as sched_runner, restore_all as restore_schedules, fmt_ist_12h, fmt_ist_full
import admin
from keepalive import keepalive_loop

client: TelegramClient = None

def cmd(name: str, delete_cmd: bool = False):
    def deco(fn):
        async def wrapper(event):
            try:
                if delete_cmd:
                    try: await event.delete()
                    except: pass
                await fn(event)
            except Exception as e: log.error(f"cmd {name}: {e}", exc_info=True)
        client.add_event_handler(wrapper, events.NewMessage(pattern=rf"^[./]{name}(?:\s|$)", func=lambda e: e.is_private and not e.out))
        return fn
    return deco

def owner_cmd(name: str):
    def deco(fn):
        async def wrapper(event):
            try: await fn(event)
            except Exception as e: log.error(f"owner_cmd {name}: {e}", exc_info=True)
        client.add_event_handler(wrapper, events.NewMessage(pattern=rf"^[./]{name}(?:\s|$)", func=lambda e: e.is_private and not e.out and e.sender_id == OWNER_ID))
        return fn
    return deco

def get_args(m) -> str:
    if not m or not m.message: return ""
    p = m.message.split(None, 1)
    return p[1] if len(p) > 1 else ""

async def require_userbot(event) -> bool:
    if event.sender_id not in user_bots or not user_bots[event.sender_id].is_connected():
        await event.reply("❌ Pehle login karo: `.login +91xxxxxxxxxx`")
        return False
    return True

def parse_sched(date_str, time_str):
    d = None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try: d = datetime.strptime(date_str.strip(), fmt); break
        except ValueError: pass
    if d is None: raise ValueError("Date `YYYY-MM-DD` ya `DD-MM-YYYY`")
    ts = time_str.strip().upper().replace(".", "")
    t = None
    if "AM" in ts or "PM" in ts:
        for fmt in ("%I:%M:%S %p", "%I:%M %p", "%I %p"):
            try: t = datetime.strptime(ts, fmt); break
            except: pass
        if t is None: raise ValueError("12h: `hh:MM:SS AM/PM`")
    else:
        for fmt in ("%H:%M:%S", "%H:%M"):
            try: t = datetime.strptime(ts, fmt); break
            except: pass
        if t is None: raise ValueError("24h: `HH:MM:SS`")
    return d.replace(hour=t.hour, minute=t.minute, second=t.second, tzinfo=IST_TZ)

@cmd("login", delete_cmd=True)
async def login_cmd(event):
    args = get_args(event.message).strip()
    if not args: return await event.reply("`.login +919876543210`")
    res = await begin_login(event.sender_id, args.split()[0])
    if not res["ok"]: return await event.reply(f"❌ {res['error']}")
    await event.reply(f"📩 OTP bheja gaya. Ab: `.otp <code>`")

@cmd("otp", delete_cmd=True)
async def otp_cmd(event):
    args = get_args(event.message).strip()
    if not args: return await event.reply("`.otp 12345`")
    state = await pending_col.find_one({"owner_id": event.sender_id, "stage": "otp"})
    if not state: return await event.reply("❌ Koi active login nahi.")
    res = await submit_otp(state["phone"], args.split()[0])
    if not res["ok"]: return await event.reply(f"❌ {res['error']}")
    if res.get("stage") == "2fa": await event.reply("🔐 2FA ON hai. Ab: `.2fa <password>`")
    else:
        await event.reply("✅ Login Success!")
        asyncio.create_task(keepalive_loop(event.sender_id))

@cmd("2fa", delete_cmd=True)
async def tfa_cmd(event):
    args = get_args(event.message).strip()
    if not args: return await event.reply("`.2fa yourpassword`")
    state = await pending_col.find_one({"owner_id": event.sender_id, "stage": "2fa"})
    if not state: return await event.reply("❌ 2FA stage nahi.")
    res = await submit_2fa(state["phone"], args)
    if not res["ok"]: return await event.reply(f"❌ {res['error']}")
    await event.reply("✅ Login Success!")
    asyncio.create_task(keepalive_loop(event.sender_id))

@cmd("logout", delete_cmd=True)
async def logout_cmd(event):
    await kill_userbot(event.sender_id, remove_session=True)
    await event.reply("✅ Logged out.")

@cmd("schedule", delete_cmd=True)
async def schedule_cmd(event):
    if not await require_userbot(event): return
    args = get_args(event.message).strip()
    if not args: return await event.reply("`.schedule <date> <time> <target> <msg>`")
    parts = args.split()
    if len(parts) < 2: return await event.reply("❌ Format galat.")
    date_str, time_str = parts[0], parts[1]
    consumed = 2
    if len(parts) >= 3 and parts[2].upper() in ("AM", "PM"):
        time_str += f" {parts[2].upper()}"; consumed = 3
    rest = " ".join(parts[consumed:]).strip()
    try: sched_ts = parse_sched(date_str, time_str).timestamp()
    except Exception as e: return await event.reply(f"❌ {e}")
    if sched_ts <= time.time(): return await event.reply("❌ Past time.")
    ub = user_bots[event.sender_id]
    if not rest and not event.is_reply: return await event.reply("❌ Target + msg chahiye.")
    target_id, msg_text, reply_to = None, rest, None
    if event.is_reply:
        rm = await event.get_reply_message(); target_id = rm.sender_id; reply_to = rm.id
        if not msg_text: msg_text = (rm.message or "").strip()
    else:
        tp = rest.split(" ", 1); target_str = tp[0]; msg_text = tp[1].strip() if len(tp) > 1 else ""
        try:
            ent = await ub.get_entity(target_str) if target_str.startswith("@") else await ub.get_entity(int(target_str))
            target_id = ent.id
        except Exception as e: return await event.reply(f"❌ Not found: {e}")
    if not msg_text: return await event.reply("❌ Empty message.")
    
    doc = {"_id": str(ObjectId()), "owner_id": event.sender_id, "target_id": target_id, "text": msg_text, "reply_to_msg_id": reply_to, "comment_to": None, "scheduled_ts": sched_ts, "scheduled_str": fmt_ist_full(sched_ts), "scheduled_short": fmt_ist_12h(sched_ts), "chat_id": event.chat_id, "created_at": time.time(), "status": "pending"}
    await sched_col.insert_one(doc)
    asyncio.create_task(sched_runner(doc, client))
    await event.reply(f"✅ **SCHEDULED!**\n🆔 `{doc['_id'][:8]}`\n🕐 `{doc['scheduled_short']}`", parse_mode="markdown")

@cmd("schedules", delete_cmd=False)
async def schedules_cmd(event):
    cursor = sched_col.find({"owner_id": event.sender_id, "status": "pending"}).sort("scheduled_ts", 1)
    rows = await cursor.to_list(length=50)
    if not rows: return await event.reply("📭 Koi pending schedule nahi.")
    text = f"⏰ **PENDING ({len(rows)})**\n"
    for r in rows: text += f"`{r['_id'][:8]}` — {r['text'][:20]}... ({r['scheduled_short']})\n"
    await event.reply(text, parse_mode="markdown")

@cmd("cancelschedule", delete_cmd=False)
async def cancelschedule_cmd(event):
    args = get_args(event.message).strip()
    if not args: return await event.reply("`.cancelschedule <id>`")
    prefix = args.split()[0]
    m = await sched_col.find({"owner_id": event.sender_id, "status": "pending", "_id": {"$regex": f"^{re.escape(prefix)}"}}).to_list(length=5)
    if not m: return await event.reply("❌ Not found.")
    await sched_col.update_one({"_id": m[0]["_id"]}, {"$set": {"status": "cancelled"}})
    await event.reply(f"✅ Cancelled `{m[0]['_id'][:8]}`")

@owner_cmd("admin")
async def _admin_cmd(event): await admin.admin_cmd(event)
@owner_cmd("users")
async def _users_cmd(event): await admin.users_cmd(event)
@owner_cmd("userinfo")
async def _userinfo_cmd(event): await admin.userinfo_cmd(event, get_args(event.message))
@owner_cmd("blockuser")
async def _blockuser_cmd(event): await admin.blockuser_cmd(event, get_args(event.message))
@owner_cmd("unblockuser")
async def _unblockuser_cmd(event): await admin.unblockuser_cmd(event, get_args(event.message))
@owner_cmd("removeuser")
async def _removeuser_cmd(event): await admin.removeuser_cmd(event, get_args(event.message))
@owner_cmd("sysinfo")
async def _sysinfo_cmd(event): await admin.sysinfo_cmd(event)

async def health_server():
    async def handle(_req):
        active = sum(1 for u in user_bots.values() if u.is_connected())
        return web.Response(text=f"✅ Running | {active}/{len(user_bots)}", content_type="text/plain")
    app = web.Application(); app.router.add_get("/", handle); app.router.add_get("/health", handle)
    runner = web.AppRunner(app); await runner.setup(); await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080))).start()

async def main():
    global client
    await init_mongo()
    client = TelegramClient("schedule_x_bot_bot", API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    await restore_all_userbots()
    await restore_schedules(client)
    for uid in list(user_bots.keys()): asyncio.create_task(keepalive_loop(uid))
    asyncio.create_task(health_server())
    log.info("🚀 BOT READY")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try: asyncio.run(main())
    except Exception as e: log.error(f"Fatal: {e}", exc_info=True)
