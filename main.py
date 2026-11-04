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

# ═══════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════
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


# ═══════════════════════════════════════════════
# COMMAND FUNCTIONS (Without Decorators)
# ═══════════════════════════════════════════════
async def login_cmd(event):
    args = get_args(event.message).strip()
    if not args: return await event.reply("`.login +919876543210`")
    res = await begin_login(event.sender_id, args.split()[0])
    if not res["ok"]: return await event.reply(f"❌ {res['error']}")
    await event.reply(f"📩 OTP bheja gaya. Ab: `.otp <code>`")

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

async def tfa_cmd(event):
    args = get_args(event.message).strip()
    if not args: return await event.reply("`.2fa yourpassword`")
    state = await pending_col.find_one({"owner_id": event.sender_id, "stage": "2fa"})
    if not state: return await event.reply("❌ 2FA stage nahi.")
    res = await submit_2fa(state["phone"], args)
    if not res["ok"]: return await event.reply(f"❌ {res['error']}")
    await event.reply("✅ Login Success!")
    asyncio.create_task(keepalive_loop(event.sender_id))

async def logout_cmd(event):
    await kill_userbot(event.sender_id, remove_session=True)
    await event.reply("✅ Logged out.")

async def mybots_cmd(event):
    u = await users_col.find_one({"owner_id": event.sender_id, "active": True})
    if not u: return await event.reply("❌ Koi userbot nahi.")
    cl = user_bots.get(event.sender_id)
    st = "🟢 Online" if (cl and cl.is_connected()) else "🔴 Offline"
    await event.reply(f"📱 `{u['phone']}`\n⚡ {st}")

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

async def schedules_cmd(event):
    cursor = sched_col.find({"owner_id": event.sender_id, "status": "pending"}).sort("scheduled_ts", 1)
    rows = await cursor.to_list(length=50)
    if not rows: return await event.reply("📭 Koi pending schedule nahi.")
    text = f"⏰ **PENDING ({len(rows)})**\n"
    for r in rows: text += f"`{r['_id'][:8]}` — {r['text'][:20]}... ({r['scheduled_short']})\n"
    await event.reply(text, parse_mode="markdown")

async def cancelschedule_cmd(event):
    args = get_args(event.message).strip()
    if not args: return await event.reply("`.cancelschedule <id>`")
    prefix = args.split()[0]
    m = await sched_col.find({"owner_id": event.sender_id, "status": "pending", "_id": {"$regex": f"^{re.escape(prefix)}"}}).to_list(length=5)
    if not m: return await event.reply("❌ Not found.")
    await sched_col.update_one({"_id": m[0]["_id"]}, {"$set": {"status": "cancelled"}})
    await event.reply(f"✅ Cancelled `{m[0]['_id'][:8]}`")

async def sync_cmd(event):
    if not await require_userbot(event): return
    ub = user_bots[event.sender_id]
    msg = await event.reply("🔄 Syncing...")
    try:
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            await ub(PingRequest(ping_id=random.randint(1, 10**9)))
            times.append(time.perf_counter() - t0)
            await asyncio.sleep(0.1)
        avg = sum(times) / len(times)
        scheduler.PING_OFFSET = avg + 0.005
        await msg.edit(f"✅ **Sync!**\n📡 `{avg*1000:.1f}ms`", parse_mode="markdown")
    except Exception as e:
        await msg.edit(f"❌ {str(e)[:200]}")


# ═══════════════════════════════════════════════
# HEALTH SERVER
# ═══════════════════════════════════════════════
async def health_server():
    async def handle(_req):
        active = sum(1 for u in user_bots.values() if u.is_connected())
        return web.Response(text=f"✅ Running | {active}/{len(user_bots)}", content_type="text/plain")
    app = web.Application(); app.router.add_get("/", handle); app.router.add_get("/health", handle)
    runner = web.AppRunner(app); await runner.setup(); await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080))).start()


# ═══════════════════════════════════════════════
# MAIN STARTUP
# ═══════════════════════════════════════════════
async def main():
    global client
    await init_mongo()
    client = TelegramClient("schedule_x_bot_bot", API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    log.info("✅ Bot Online!")

    # ── USER COMMANDS REGISTER ──
    client.add_event_handler(login_cmd, events.NewMessage(pattern=r"^[./]login(?:\s|$)", func=lambda e: e.is_private and not e.out))
    client.add_event_handler(otp_cmd, events.NewMessage(pattern=r"^[./]otp(?:\s|$)", func=lambda e: e.is_private and not e.out))
    client.add_event_handler(tfa_cmd, events.NewMessage(pattern=r"^[./]2fa(?:\s|$)", func=lambda e: e.is_private and not e.out))
    client.add_event_handler(logout_cmd, events.NewMessage(pattern=r"^[./]logout(?:\s|$)", func=lambda e: e.is_private and not e.out))
    client.add_event_handler(mybots_cmd, events.NewMessage(pattern=r"^[./]mybots(?:\s|$)", func=lambda e: e.is_private and not e.out))
    client.add_event_handler(schedule_cmd, events.NewMessage(pattern=r"^[./]schedule(?:\s|$)", func=lambda e: e.is_private and not e.out))
    client.add_event_handler(schedules_cmd, events.NewMessage(pattern=r"^[./]schedules(?:\s|$)", func=lambda e: e.is_private and not e.out))
    client.add_event_handler(cancelschedule_cmd, events.NewMessage(pattern=r"^[./]cancelschedule(?:\s|$)", func=lambda e: e.is_private and not e.out))
    client.add_event_handler(sync_cmd, events.NewMessage(pattern=r"^[./]sync(?:\s|$)", func=lambda e: e.is_private and not e.out))

    # ── OWNER COMMANDS REGISTER ──
    client.add_event_handler(lambda e: admin.admin_cmd(e), events.NewMessage(pattern=r"^[./]admin$", func=lambda e: e.is_private and not e.out and e.sender_id == OWNER_ID))
    client.add_event_handler(lambda e: admin.users_cmd(e), events.NewMessage(pattern=r"^[./]users$", func=lambda e: e.is_private and not e.out and e.sender_id == OWNER_ID))
    client.add_event_handler(lambda e: admin.userinfo_cmd(e, get_args(e.message)), events.NewMessage(pattern=r"^[./]userinfo(?:\s|$)", func=lambda e: e.is_private and not e.out and e.sender_id == OWNER_ID))
    client.add_event_handler(lambda e: admin.blockuser_cmd(e, get_args(e.message)), events.NewMessage(pattern=r"^[./]blockuser(?:\s|$)", func=lambda e: e.is_private and not e.out and e.sender_id == OWNER_ID))
    client.add_event_handler(lambda e: admin.unblockuser_cmd(e, get_args(e.message)), events.NewMessage(pattern=r"^[./]unblockuser(?:\s|$)", func=lambda e: e.is_private and not e.out and e.sender_id == OWNER_ID))
    client.add_event_handler(lambda e: admin.removeuser_cmd(e, get_args(e.message)), events.NewMessage(pattern=r"^[./]removeuser(?:\s|$)", func=lambda e: e.is_private and not e.out and e.sender_id == OWNER_ID))
    client.add_event_handler(lambda e: admin.suspend_cmd(e, get_args(e.message)), events.NewMessage(pattern=r"^[./]suspend(?:\s|$)", func=lambda e: e.is_private and not e.out and e.sender_id == OWNER_ID))
    client.add_event_handler(lambda e: admin.resume_cmd(e, get_args(e.message)), events.NewMessage(pattern=r"^[./]resume(?:\s|$)", func=lambda e: e.is_private and not e.out and e.sender_id == OWNER_ID))
    client.add_event_handler(lambda e: admin.forcekill_cmd(e, get_args(e.message)), events.NewMessage(pattern=r"^[./]forcekill(?:\s|$)", func=lambda e: e.is_private and not e.out and e.sender_id == OWNER_ID))
    client.add_event_handler(lambda e: admin.broadcast_cmd(e, get_args(e.message)), events.NewMessage(pattern=r"^[./]broadcast(?:\s|$)", func=lambda e: e.is_private and not e.out and e.sender_id == OWNER_ID))
    client.add_event_handler(lambda e: admin.blockedlist_cmd(e), events.NewMessage(pattern=r"^[./]blockedlist$", func=lambda e: e.is_private and not e.out and e.sender_id == OWNER_ID))
    client.add_event_handler(lambda e: admin.sysinfo_cmd(e), events.NewMessage(pattern=r"^[./]sysinfo$", func=lambda e: e.is_private and not e.out and e.sender_id == OWNER_ID))

    # ── RESTORE & START ──
    await restore_all_userbots()
    await restore_schedules(client)
    for uid in list(user_bots.keys()): asyncio.create_task(keepalive_loop(uid))
    asyncio.create_task(health_server())
    
    log.info("🚀 BOT READY")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try: asyncio.run(main())
    except Exception as e: log.error(f"Fatal: {e}", exc_info=True)
