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
from telethon.sessions import StringSession
from telethon.errors import (
    PhoneCodeInvalidError, PhoneCodeExpiredError, SessionPasswordNeededError,
    PhoneNumberInvalidError, PasswordHashInvalidError, FloodWaitError
)
from telethon.tl.functions import PingRequest
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("bot")

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
MONGO_URI = os.getenv("MONGO_URI", "")
PORT = int(os.getenv("PORT", 8080))
IST_TZ = ZoneInfo("Asia/Kolkata")

# ═══════════════════════════════════════════════
# DATABASE SETUP
# ═══════════════════════════════════════════════
db_client = None
users_col = None
sessions_col = None
sched_col = None
autocmt_col = None
pending_col = None
blocked_col = None

async def init_mongo():
    global db_client, users_col, sessions_col, sched_col, autocmt_col, pending_col, blocked_col
    db_client = AsyncIOMotorClient(MONGO_URI)
    db = db_client["schedule_x_bot"]
    users_col = db["users"]
    sessions_col = db["sessions"]
    sched_col = db["schedules"]
    autocmt_col = db["auto_comment"]
    pending_col = db["pending_logins"]
    blocked_col = db["blocked"]
    
    await users_col.create_index("owner_id", unique=True)
    await sessions_col.create_index("owner_id", unique=True)
    await sched_col.create_index([("owner_id", 1), ("status", 1)])

# ═══════════════════════════════════════════════
# USERBOT MANAGER
# ═══════════════════════════════════════════════
client: TelegramClient = None
user_bots: dict[int, TelegramClient] = {}
_TEMP_CLIENTS: dict[str, TelegramClient] = {}
PING_OFFSET = 0.085

async def _safe_disconnect(cl):
    try: await cl.disconnect()
    except: pass

async def begin_login(owner_id: int, phone: str) -> dict:
    phone = phone.strip()
    if not phone.startswith("+"): return {"ok": False, "error": "Phone `+` se start hona chahiye"}
    blk = await blocked_col.find_one({"owner_id": owner_id})
    if blk: return {"ok": False, "error": f"🚫 Aap blocked ho. Reason: {blk.get('reason','-')}"}
    if owner_id in user_bots and user_bots[owner_id].is_connected(): return {"ok": False, "error": "Already logged in. Pehle `.logout` karo."}

    temp = TelegramClient(StringSession(), API_ID, API_HASH)
    await temp.connect()
    try: sent = await temp.send_code_request(phone)
    except Exception as e:
        await _safe_disconnect(temp)
        return {"ok": False, "error": f"OTP send fail: {str(e)[:100]}"}

    await pending_col.update_one({"phone": phone}, {"$set": {
        "phone": phone, "owner_id": owner_id, "phone_code_hash": sent.phone_code_hash,
        "session": temp.session.save(), "stage": "otp", "created_at": time.time()
    }}, upsert=True)
    
    old = _TEMP_CLIENTS.pop(phone, None)
    if old: await _safe_disconnect(old)
    _TEMP_CLIENTS[phone] = temp
    return {"ok": True}

async def submit_otp(phone: str, otp: str) -> dict:
    phone = phone.strip().replace(" ", "")
    otp = otp.replace(" ", "") 
    state = await pending_col.find_one({"phone": phone})
    if not state: return {"ok": False, "error": "Login session expire. `.login` phir se karo."}
    
    temp = _TEMP_CLIENTS.get(phone)
    if temp is None:
        temp = TelegramClient(StringSession(state["session"]), API_ID, API_HASH)
        await temp.connect()
        _TEMP_CLIENTS[phone] = temp

    try:
        await temp.sign_in(phone=phone, code=otp, phone_code_hash=state["phone_code_hash"])
    except SessionPasswordNeededError:
        await pending_col.update_one({"phone": phone}, {"$set": {"stage": "2fa"}})
        return {"ok": True, "stage": "2fa"}
    except Exception as e: return {"ok": False, "error": f"OTP error: {str(e)[:100]}"}
    return await _finalize(phone, state["owner_id"])

async def submit_2fa(phone: str, password: str) -> dict:
    phone = phone.strip().replace(" ", "")
    state = await pending_col.find_one({"phone": phone})
    if not state or state.get("stage") != "2fa": return {"ok": False, "error": "2FA stage nahi hai."}

    temp = _TEMP_CLIENTS.get(phone)
    if temp is None:
        temp = TelegramClient(StringSession(state["session"]), API_ID, API_HASH)
        await temp.connect()
        _TEMP_CLIENTS[phone] = temp

    try: await temp.sign_in(password=password)
    except Exception as e: return {"ok": False, "error": f"2FA error: {str(e)[:100]}"}
    return await _finalize(phone, state["owner_id"])

async def _finalize(phone: str, owner_id: int) -> dict:
    temp = _TEMP_CLIENTS.get(phone)
    if temp is None: return {"ok": False, "error": "Internal error."}
    me = await temp.get_me()
    session_str = temp.session.save()

    await users_col.update_one({"owner_id": owner_id}, {"$set": {
        "owner_id": owner_id, "phone": phone, "tg_id": me.id, "username": me.username,
        "first_name": me.first_name, "active": True, "suspended": False,
        "blocked": False, "logged_in_at": time.time(),
    }}, upsert=True)
    await sessions_col.update_one({"owner_id": owner_id}, {"$set": {
        "owner_id": owner_id, "phone": phone, "session_str": session_str
    }}, upsert=True)
    
    await pending_col.delete_one({"phone": phone})
    _TEMP_CLIENTS.pop(phone, None)
    await _safe_disconnect(temp)
    await spawn_userbot(owner_id)
    return {"ok": True, "stage": "done", "tg_id": me.id}

async def spawn_userbot(owner_id: int) -> TelegramClient | None:
    if await blocked_col.find_one({"owner_id": owner_id}): return None
    u = await users_col.find_one({"owner_id": owner_id})
    if u and u.get("suspended"): return None
    if owner_id in user_bots and user_bots[owner_id].is_connected(): return user_bots[owner_id]

    sess = await sessions_col.find_one({"owner_id": owner_id})
    if not sess: return None

    cl = TelegramClient(StringSession(sess["session_str"]), API_ID, API_HASH)
    try:
        await cl.connect()
        if not await cl.is_user_authorized():
            await _safe_disconnect(cl)
            return None
        user_bots[owner_id] = cl
        return cl
    except: return None

async def kill_userbot(owner_id: int, remove_session: bool = True):
    cl = user_bots.pop(owner_id, None)
    if cl: await _safe_disconnect(cl)
    if remove_session:
        await users_col.update_one({"owner_id": owner_id}, {"$set": {"active": False}})
        await sessions_col.delete_one({"owner_id": owner_id})

async def restore_all_userbots():
    cursor = users_col.find({"active": True, "suspended": {"$ne": True}})
    async for u in cursor:
        await spawn_userbot(u["owner_id"])

# ═══════════════════════════════════════════════
# SCHEDULER
# ═══════════════════════════════════════════════
def fmt_ist_12h(ts: float) -> str: return datetime.fromtimestamp(ts, IST_TZ).strftime("%I:%M:%S %p IST")

async def sched_runner(doc: dict, bot_client):
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

        fresh = await sched_col.find_one({"_id": doc["_id"]})
        if not fresh or fresh.get("status") != "pending": return

        cl = user_bots.get(fresh["owner_id"])
        if cl is None:
            await sched_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "failed", "error": "userbot offline"}})
            return

        try: await asyncio.wait_for(cl(PingRequest(ping_id=random.randint(1, 10**9))), timeout=1.5)
        except: pass

        try:
            t, txt = fresh["target_id"], fresh["text"]
            if fresh.get("comment_to"):
                try: await cl.send_message(t, txt, comment_to=int(fresh["comment_to"]))
                except: await cl.send_message(t, txt)
            elif fresh.get("reply_to_msg_id"):
                try: await cl.send_message(t, txt, reply_to=fresh["reply_to_msg_id"])
                except: await cl.send_message(t, txt)
            else: await cl.send_message(t, txt)

            await sched_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "sent", "sent_at": time.time()}})
            
            if fresh.get("confirm_chat_id") and fresh.get("confirm_msg_id"):
                try: await bot_client.delete_messages(int(fresh["confirm_chat_id"]), [int(fresh["confirm_msg_id"])])
                except: pass
        except Exception as e:
            await sched_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "failed", "error": str(e)[:200]}})
    except Exception as e: log.error(f"runner: {e}")

async def restore_schedules(bot_client):
    cursor = sched_col.find({"status": "pending", "scheduled_ts": {"$gt": time.time() - 300}})
    async for d in cursor:
        asyncio.create_task(sched_runner(d, bot_client))

# ═══════════════════════════════════════════════
# HELPER & COMMANDS
# ═══════════════════════════════════════════════
def get_args(m) -> str:
    if not m or not m.message: return ""
    p = m.message.split(None, 1)
    return p[1] if len(p) > 1 else ""

def parse_sched(date_str, time_str):
    d = None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try: d = datetime.strptime(date_str.strip(), fmt); break
        except: pass
    if d is None: raise ValueError("Date `YYYY-MM-DD` ya `DD-MM-YYYY`")
    
    ts = time_str.strip().upper().replace(".", "")
    t = None
    if "AM" in ts or "PM" in ts:
        for fmt in ("%I:%M:%S %p", "%I:%M %p", "%I %p"):
            try: t = datetime.strptime(ts, fmt); break
            except: pass
    else:
        for fmt in ("%H:%M:%S", "%H:%M"):
            try: t = datetime.strptime(ts, fmt); break
            except: pass
    if t is None: raise ValueError("Time format galat hai.")
    return d.replace(hour=t.hour, minute=t.minute, second=t.second, tzinfo=IST_TZ)

async def require_userbot(event) -> bool:
    if event.sender_id not in user_bots or not user_bots[event.sender_id].is_connected():
        await event.reply("❌ Pehle login karo: `.login +91xxxxxxxxxx`")
        return False
    return True

# --- START COMMAND ---
async def start_cmd(event):
    text = (
        "🎉 **ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ sᴄʜᴇᴅᴜʟᴇʀ ʙᴏᴛ** 🎉\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🛑 **ʟᴏɢɪɴ sᴛᴇᴘs:**\n\n"
        "🔷 **sᴇɴᴅ** `.login`\n\n"
        "🔷 **sᴇɴᴅ ᴘʜᴏɴᴇ ɴᴜᴍʙᴇʀ** (ᴡɪᴛʜ ᴄᴏᴜɴᴛʀʏ ᴄᴏᴅᴇ)\n"
        "    ᴇ.ɢ. `.login +919876543210`\n\n"
        "🔷 **sᴇɴᴅ ᴏᴛᴘ** (sᴘᴀᴄᴇ ᴋᴇ sᴀᴀᴛʜ ʏᴀ ʙɪɴᴀ)\n"
        "    ᴇ.ɢ. `.otp 1 2 3 4 5`\n\n"
        "🔷 **sᴇɴᴅ 𝟸ꜰᴀ ᴘᴀssᴡᴏʀᴅ** (ɪꜰ ᴇɴᴀʙʟᴇᴅ)\n"
        "    ᴇ.ɢ. `.2fa yourpassword`\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔐 **ʟᴏɢɪɴ? ᴛᴏʜ:** `.login`"
    )
    await event.reply(text, parse_mode="markdown")

# --- HELP COMMAND ---
async def help_cmd(event):
    text = (
        "🤖 **sᴄʜᴇᴅᴜʟᴇʀ ʙᴏᴛ ᴍᴇɴᴜ**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔐 **ᴀᴄᴄᴏᴜɴᴛ ᴄᴏᴍᴍᴀɴᴅs:**\n"
        "▸ `.login` — Userbot login karein\n"
        "▸ `.logout` — Userbot logout karein\n"
        "▸ `.mybots` — Apne userbot ka status dekhein\n\n"
        "⏰ **sᴄʜᴇᴅᴜʟᴇʀ ᴄᴏᴍᴍᴀɴᴅs:**\n"
        "▸ `.schedule <date> <time> <target> <msg>` — Message schedule karein\n"
        "▸ `.schedules` — Pending messages ki list\n"
        "▸ `.cancelschedule <id>` — Schedule cancel karein\n"
        "▸ `.sync` — Ping sync karein (Precise timing ke liye)\n\n"
        "🤖 **ᴀᴜᴛᴏ-ᴄᴏᴍᴍᴇɴᴛ ᴄᴏᴍᴍᴀɴᴅs:**\n"
        "▸ `.autocmt on <link>` — Auto-comment ON\n"
        "▸ `.autocmt off <link>` — Auto-comment OFF\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 _Tip: Exact time pe message bhejne ke liye `.schedule` use karein._"
    )
    await event.reply(text, parse_mode="markdown")

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
    res = await submit_otp(state["phone"], args)
    if not res["ok"]: return await event.reply(f"❌ {res['error']}")
    if res.get("stage") == "2fa": await event.reply("🔐 2FA ON hai. Ab: `.2fa <password>`")
    else: await event.reply("✅ Login Success! Ab `.mybots` try karo.")

async def tfa_cmd(event):
    args = get_args(event.message).strip()
    if not args: return await event.reply("`.2fa yourpassword`")
    state = await pending_col.find_one({"owner_id": event.sender_id, "stage": "2fa"})
    if not state: return await event.reply("❌ 2FA stage nahi.")
    res = await submit_2fa(state["phone"], args)
    if not res["ok"]: return await event.reply(f"❌ {res['error']}")
    await event.reply("✅ Login Success!")

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
    
    ub = user_bots[event.sender_id]
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
    
    doc = {"_id": str(ObjectId()), "owner_id": event.sender_id, "target_id": target_id, "text": msg_text, "reply_to_msg_id": reply_to, "comment_to": None, "scheduled_ts": sched_ts, "scheduled_short": fmt_ist_12h(sched_ts), "status": "pending"}
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

# --- OWNER COMMANDS ---
async def admin_cmd(event):
    if event.sender_id != OWNER_ID: return
    total_users = await users_col.count_documents({"active": True})
    online = sum(1 for u in user_bots.values() if u.is_connected())
    await event.reply(f"👑 **OWNER DASHBOARD**\n👥 Users: `{total_users}` (online: `{online}`)", parse_mode="markdown")

# ═══════════════════════════════════════════════
# HEALTH & STARTUP
# ═══════════════════════════════════════════════
async def health_server():
    async def handle(_req):
        active = sum(1 for u in user_bots.values() if u.is_connected())
        return web.Response(text=f"✅ Running | {active}/{len(user_bots)}", content_type="text/plain")
    app = web.Application(); app.router.add_get("/", handle); app.router.add_get("/health", handle)
    runner = web.AppRunner(app); await runner.setup(); await web.TCPSite(runner, "0.0.0.0", PORT).start()

async def main():
    global client
    log.info("⏳ Connecting to MongoDB...")
    await init_mongo()
    log.info("✅ MongoDB Connected!")
    
    client = TelegramClient("schedule_x_bot_bot", API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    log.info("✅ Bot Online!")

    # Register Commands
    client.add_event_handler(start_cmd, events.NewMessage(pattern=r"^[./]start$", func=lambda e: e.is_private and not e.out))
    client.add_event_handler(help_cmd, events.NewMessage(pattern=r"^[./]help$", func=lambda e: e.is_private and not e.out))
    client.add_event_handler(login_cmd, events.NewMessage(pattern=r"^[./]login(?:\s|$)", func=lambda e: e.is_private and not e.out))
    client.add_event_handler(otp_cmd, events.NewMessage(pattern=r"^[./]otp(?:\s|$)", func=lambda e: e.is_private and not e.out))
    client.add_event_handler(tfa_cmd, events.NewMessage(pattern=r"^[./]2fa(?:\s|$)", func=lambda e: e.is_private and not e.out))
    client.add_event_handler(logout_cmd, events.NewMessage(pattern=r"^[./]logout(?:\s|$)", func=lambda e: e.is_private and not e.out))
    client.add_event_handler(mybots_cmd, events.NewMessage(pattern=r"^[./]mybots(?:\s|$)", func=lambda e: e.is_private and not e.out))
    client.add_event_handler(schedule_cmd, events.NewMessage(pattern=r"^[./]schedule(?:\s|$)", func=lambda e: e.is_private and not e.out))
    client.add_event_handler(schedules_cmd, events.NewMessage(pattern=r"^[./]schedules(?:\s|$)", func=lambda e: e.is_private and not e.out))
    
    client.add_event_handler(admin_cmd, events.NewMessage(pattern=r"^[./]admin$"))

    await restore_all_userbots()
    await restore_schedules(client)
    asyncio.create_task(health_server())
    
    log.info("🚀 BOT READY")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
