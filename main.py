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
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import Channel
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
START_MEDIA = "https://files.catbox.moe/35bl05.mp4"

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
    await autocmt_col.create_index([("owner_id", 1), ("chat_id", 1)], unique=True)

# ═══════════════════════════════════════════════
# GLOBALS & USERBOT MANAGER
# ═══════════════════════════════════════════════
bot: TelegramClient = None
user_bots: dict[int, TelegramClient] = {}
_TEMP_CLIENTS: dict[str, TelegramClient] = {}
_login_steps: dict[int, str] = {} 
PING_OFFSET = 0.089

async def _safe_disconnect(cl):
    try: await cl.disconnect()
    except: pass

async def spawn_userbot(owner_id: int) -> TelegramClient | None:
    if await blocked_col.find_one({"owner_id": owner_id}): return None
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
        await register_autocmt_handler(owner_id)
        return cl
    except: return None

async def kill_userbot(owner_id: int, remove_session: bool = True):
    cl = user_bots.pop(owner_id, None)
    if cl: await _safe_disconnect(cl)
    if remove_session:
        await users_col.update_one({"owner_id": owner_id}, {"$set": {"active": False}})
        await sessions_col.delete_one({"owner_id": owner_id})

async def restore_all_userbots():
    cursor = users_col.find({"active": True})
    async for u in cursor:
        await spawn_userbot(u["owner_id"])

# ═══════════════════════════════════════════════
# SCHEDULER CORE
# ═══════════════════════════════════════════════
def _fmt_ist_12h(ts: float) -> str: return datetime.fromtimestamp(ts, IST_TZ).strftime("%I:%M:%S %p IST")
def _fmt_ist_full(ts: float) -> str: return datetime.fromtimestamp(ts, IST_TZ).strftime("%d %b %Y, %I:%M:%S %p IST")

async def _scheduler_runner(cl: TelegramClient, doc: dict):
    try:
        target_ts = float(doc["scheduled_ts"])
        fire_ts = target_ts - PING_OFFSET 
        while True:
            diff = fire_ts - time.time()
            if diff <= 2.0: break
            if diff > 120: await asyncio.sleep(min(60, diff - 10))
            elif diff > 10: await asyncio.sleep(2.0)
            elif diff > 2: await asyncio.sleep(0.3)

        fresh = await sched_col.find_one({"_id": doc["_id"]})
        if not fresh or fresh.get("status") != "pending": return

        send_text = fresh["text"]
        target = fresh["target_id"]
        comment_to = fresh.get("comment_to")
        reply_to = fresh.get("reply_to_msg_id")

        try: await asyncio.wait_for(cl(PingRequest(ping_id=random.randint(1, 10**9))), timeout=1.5)
        except: pass

        async def _real_send():
            try:
                if comment_to:
                    try: await cl.send_message(target, send_text, comment_to=int(comment_to))
                    except: await cl.send_message(target, send_text)
                elif reply_to:
                    try: await cl.send_message(target, send_text, reply_to=reply_to)
                    except: await cl.send_message(target, send_text)
                else: await cl.send_message(target, send_text)
                
                await sched_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "sent", "sent_at": time.time()}})
                
                if fresh.get("confirm_chat_id") and fresh.get("confirm_msg_id"):
                    try: await bot.delete_messages(int(fresh["confirm_chat_id"]), [int(fresh["confirm_msg_id"])])
                    except: pass
            except Exception as e:
                await sched_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "failed", "error": str(e)[:200]}})

        while True:
            remaining = fire_ts - time.time()
            if remaining <= 0: break
            if remaining > 0.1: await asyncio.sleep(0.01)
            else: await asyncio.sleep(0)
        asyncio.create_task(_real_send())
    except Exception as e: log.error(f"runner error: {e}")

# ═══════════════════════════════════════════════
# AUTO COMMENT SYSTEM (FIXED & SMART)
# ═══════════════════════════════════════════════
DEFAULT_AUTO_CMTS = ["Wow, great update!", "Thank you so much sir!", "Finally the wait is over!", "Yes, this is amazing.", "Hii, first comment here!", "Thank you for the giveaway!", "Great work, keep it up.", "This is exactly what I needed."]

async def register_autocmt_handler(uid: int):
    ub = user_bots.get(uid)
    if not ub or getattr(ub, "_autocmt_registered", False): return
    ub._autocmt_registered = True

    @ub.on(events.NewMessage(incoming=True))
    async def handler(event):
        if not event.is_channel or event.out: return
        try:
            cid = str(event.chat_id)
            # Database check
            s = await autocmt_col.find_one({"owner_id": uid, "chat_id": cid, "enabled": True})
            if not s: return
            
            txt = random.choice(s.get("replies") or DEFAULT_AUTO_CMTS)
            
            try:
                # Direct comment approach
                await ub.send_message(event.chat_id, txt, comment_to=event.message.id)
                log.info(f"⚡ Auto-comment Success on {cid}")
            except Exception as e:
                log.warning(f"Auto-comment fail try 1: {e}")
        except Exception as e: log.error(f"autocmt handler {uid}: {e}")

# ═══════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════
def get_args(m) -> str:
    if not m or not m.message: return ""
    p = m.message.split(None, 1)
    return p[1] if len(p) > 1 else ""

def _parse_schedule_time(date_str: str, time_str: str):
    dt_date = None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try: dt_date = datetime.strptime(date_str.strip(), fmt); break
        except: pass
    if dt_date is None: raise ValueError("Date `YYYY-MM-DD` ya `DD-MM-YYYY`")

    t = time_str.strip().upper().replace(".", "")
    dt_time = None
    if "AM" in t or "PM" in t:
        for fmt in ("%I:%M:%S %p", "%I:%M %p", "%I %p"):
            try: dt_time = datetime.strptime(t, fmt); break
            except: pass
    else:
        for fmt in ("%H:%M:%S", "%H:%M"):
            try: dt_time = datetime.strptime(t, fmt); break
            except: pass
    if dt_time is None: raise ValueError("Time format galat hai.")
    return dt_date.replace(hour=dt_time.hour, minute=dt_time.minute, second=dt_time.second, tzinfo=IST_TZ)

# ═══════════════════════════════════════════════
# USER COMMANDS
# ═══════════════════════════════════════════════
async def start_cmd(event):
    uid = event.sender_id
    u = await users_col.find_one({"owner_id": uid})
    if u and u.get("active"):
        caption = ("✅ **ᴜꜱᴇʀʙᴏᴛ ᴀʟʀᴇᴀᴅʏ ʟᴏɢɢᴇᴅ ɪɴ!**\n"
                   "━━━━━━━━━━━━━━━━━━━━\n\n"
                   "**🔹 ᴀᴠᴀɪʟᴀʙʟᴇ ᴄᴏᴍᴍᴀɴᴅs**\n"
                   "• `/help` — ʜᴇʟᴘ ᴍᴇɴᴜ\n"
                   "• `/status` — ʟᴏɢɪɴ sᴛᴀᴛᴜs\n"
                   "• `/logout` — ʟᴏɢᴏᴜᴛ\n\n"
                   "━━━━━━━━━━━━━━━━━━━━\n"
                   "✨ **ᴘᴏᴡᴇʀᴇᴅ ʙʏ sᴄʜᴇᴅᴜʟᴇʀ ʙᴏᴛ** 😎")
    else:
        caption = ("🎉 ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ sᴄʜᴇᴅᴜʟᴇʀ ʙᴏᴛ 🎉\n"
                   "━━━━━━━━━━━━━━━━━━━━\n\n"
                   "🛑 ʟᴏɢɪɴ sᴛᴇᴘs:\n\n"
                   "🔷  sᴇɴᴅ /ʟᴏɢɪɴ\n\n"
                   "🔷  sᴇɴᴅ ᴘʜᴏɴᴇ ɴᴜᴍʙᴇʀ (ᴡɪᴛʜ ᴄᴏᴜɴᴛʀʏ ᴄᴏᴅᴇ)\n\n"
                   "🔷  sᴇɴᴅ ᴏᴛᴘ\n\n"
                   "🔷  sᴇɴᴅ 𝟸ꜰᴀ ᴘᴀssᴡᴏʀᴅ (ɪꜰ ᴇɴᴀʙʟᴇᴅ)\n\n"
                   "━━━━━━━━━━━━━━━━━━━━\n"
                   "🔐 ʟᴏɢɪɴ? ᴛᴏʜ: /ʟᴏɢɪɴ")
    try: await bot.send_file(event.chat_id, START_MEDIA, caption=caption, parse_mode='markdown')
    except: await event.reply(caption, parse_mode='markdown')

async def help_cmd(event):
    uid = event.sender_id
    text = (
        "🛑 **sᴄʜᴇᴅᴜʟᴇʀ ʙᴏᴛ ᴍᴇɴᴜ**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔐 **ᴀᴄᴄᴏᴜɴᴛ:**\n"
        "▸ `/login` — Userbot login\n"
        "▸ `/logout` — Userbot logout\n"
        "▸ `/status` — Check login status\n\n"
        "⏰ **sᴄʜᴇᴅᴜʟᴇʀ:**\n"
        "▸ `.scdl` — Message schedule karein\n"
        "▸ `.scdlch` — Channel post pe comment schedule karein\n"
        "▸ `.schedules` — Pending list\n"
        "▸ `.cancelschedule` — Cancel karein\n\n"
        "📢 **ᴀᴜᴛᴏ-ᴄᴏᴍᴍᴇɴᴛ:**\n"
        "▸ `.autocmt on/off/list`\n"
    )
    if uid == OWNER_ID:
        text += (
            "\n👑 **ᴏᴡɴᴇʀ ᴄᴏᴍᴍᴀɴᴅs:**\n"
            "▸ `/users` — Total logins\n"
            "▸ `/remove <id>` — Remove user\n"
            "▸ `/block <id>` — Block user\n"
            "▸ `/unblock <id>` — Unblock user\n"
            "▸ `/broadcast <msg>` — Send to all\n"
            "▸ `/stats` — Bot stats\n"
        )
    await event.reply(text, parse_mode="markdown")

async def login_cmd(event):
    uid = event.sender_id
    if await blocked_col.find_one({"owner_id": uid}): return await event.reply("🚫 Aap block ho.")
    u = await users_col.find_one({"owner_id": uid})
    if u and u.get("active"): return await event.reply("✅ Aap already logged in ho. Pehle `/logout` karo.")
    await event.reply("📱 **Phone number bhejo** (country code ke saath)\nExample: `+919876543210`")
    _login_steps[uid] = "await_phone"

async def logout_cmd(event):
    await kill_userbot(event.sender_id, remove_session=True)
    _login_steps.pop(event.sender_id, None)
    await event.reply("✅ **Logged Out!**")

async def status_cmd(event):
    u = await users_col.find_one({"owner_id": event.sender_id, "active": True})
    if not u: return await event.reply("❌ Login nahi ho. `/login`")
    st = "🟢 Online" if user_bots.get(event.sender_id) else "🔴 Offline"
    await event.reply(f"👤 `{u.get('first_name', 'N/A')}`\n🆔 `{u.get('tg_id', 'N/A')}`\n⚡ Status: {st}")

async def login_flow(event):
    uid = event.sender_id
    step = _login_steps.get(uid)
    if not step: return
    text = (event.text or "").strip()

    if step == "await_phone":
        if not text.startswith("+") or len(re.sub(r'\D', '', text)) < 8:
            return await event.reply("❌ Galat. Example: `+919876543210`")
        try:
            await event.reply("⏳ Client ban raha hai...")
            cli = TelegramClient(StringSession(), API_ID, API_HASH)
            await cli.connect()
            sent = await cli.send_code_request(text)
            _TEMP_CLIENTS[uid] = cli
            await pending_col.update_one({"owner_id": uid}, {"$set": {"phone": text, "phone_code_hash": sent.phone_code_hash, "created_at": time.time()}}, upsert=True)
            _login_steps[uid] = "await_otp"
            await event.reply("📨 **OTP BHEJA GAYA!**\n\nExample: `1 2 3 4 5`")
        except Exception as e:
            _login_steps.pop(uid, None)
            await event.reply(f"❌ `{str(e)[:200]}`")

    elif step == "await_otp":
        otp = text.replace(" ", "")
        cli = _TEMP_CLIENTS.get(uid)
        p_data = await pending_col.find_one({"owner_id": uid})
        if not cli or not p_data: return await event.reply("❌ Session expire. `/login`")
        try:
            await cli.sign_in(phone=p_data["phone"], code=otp, phone_code_hash=p_data["phone_code_hash"])
            await finalize_login(uid, cli, event)
        except SessionPasswordNeededError:
            _login_steps[uid] = "await_2fa"
            await event.reply("🔐 **Two-Step Verification Password Required!**")
        except Exception as e:
            await event.reply(f"❌ `{str(e)[:200]}`")

    elif step == "await_2fa":
        cli = _TEMP_CLIENTS.get(uid)
        if not cli: return await event.reply("❌ Session expire. `/login`")
        try:
            await cli.sign_in(password=text)
            await finalize_login(uid, cli, event)
        except Exception as e:
            await event.reply(f"❌ `{str(e)[:200]}`")

async def finalize_login(uid, cli, event):
    me = await cli.get_me()
    sess_str = cli.session.save()
    now_str = datetime.now(IST_TZ).strftime("%d %b %Y, %I:%M %p")
    
    await users_col.update_one({"owner_id": uid}, {"$set": {"owner_id": uid, "phone": me.phone, "tg_id": me.id, "first_name": me.first_name, "active": True, "logged_in_at": time.time()}}, upsert=True)
    await sessions_col.update_one({"owner_id": uid}, {"$set": {"owner_id": uid, "session_str": sess_str}}, upsert=True)
    
    _login_steps.pop(uid, None)
    _TEMP_CLIENTS.pop(uid, None)
    await pending_col.delete_one({"owner_id": uid})
    await spawn_userbot(uid)
    
    # Notify User
    await event.reply(f"🎉 **USERBOT ACTIVE!**\n👤 `{me.first_name}`\n🆔 `{me.id}`\n\nTest karne ke liye `.alive` bhejo.")
    
    # Notify Owner
    try:
        owner_msg = f"🔔 **New Login**\n👤 `{me.first_name}`\n🆔 `{me.id}`\n📛 @{me.username or 'None'}\n🕐 `{now_str}`"
        await bot.send_message(OWNER_ID, owner_msg, parse_mode='markdown')
    except: pass

# ─────────────── SCHEDULE COMMANDS ───────────────
async def schedule_cmd(event):
    cli = user_bots.get(event.sender_id)
    if not cli: return await event.reply("❌ Pehle `/login` karo.")
    args = get_args(event.message).strip()
    if not args: return await event.reply("**Format:** `.scdl <date> <time> <target> <msg>`")
    parts = args.split()
    date_str, time_str = parts[0], parts[1]
    consumed = 2
    if len(parts) >= 3 and parts[2].upper() in ("AM", "PM"):
        time_str += f" {parts[2].upper()}"; consumed = 3
    rest = " ".join(parts[consumed:]).strip()
    try:
        sched_ts = _parse_schedule_time(date_str, time_str).timestamp()
        if sched_ts <= time.time(): raise Exception("Time past me hai.")
    except Exception as e: return await event.reply(f"❌ `{str(e)[:100]}`")

    target_id, msg_text, reply_to, tname = None, rest, None, "Unknown"
    if event.is_reply:
        rm = await event.get_reply_message(); target_id = rm.sender_id; reply_to = rm.id
        if not msg_text: msg_text = (rm.message or "").strip()
        tname = getattr(rm.sender, 'first_name', 'Unknown')
    else:
        tp = rest.split(" ", 1); target_str = tp[0]; msg_text = tp[1].strip() if len(tp) > 1 else ""
        try:
            ent = await cli.get_entity(target_str) if target_str.startswith("@") else await cli.get_entity(int(target_str))
            target_id = ent.id
            tname = getattr(ent, 'first_name', getattr(ent, 'title', str(target_id)))
        except Exception as e: return await event.reply(f"❌ Target not found: `{target_str}`")

    doc = {"_id": str(ObjectId()), "owner_id": event.sender_id, "target_id": target_id, "text": msg_text, "reply_to_msg_id": reply_to, "comment_to": None, "scheduled_ts": sched_ts, "scheduled_short": _fmt_ist_12h(sched_ts), "scheduled_str": _fmt_ist_full(sched_ts), "status": "pending", "created_at": time.time()}
    await sched_col.insert_one(doc)
    asyncio.create_task(_scheduler_runner(cli, doc))
    
    eta = int(sched_ts - time.time()); h, rem = divmod(eta, 3600); m, s = divmod(rem, 60)
    preview = msg_text[:80] + ("..." if len(msg_text) > 80 else "")
    confirm_msg = await event.reply(f"✅ **SCHEDULED!**\n🆔 `{doc['_id'][:8]}`\n🎯 `{tname}`\n🕐 `{doc['scheduled_short']}`\n⏳ `{h}h {m}m {s}s`\n📝 {preview}", parse_mode='markdown')
    await sched_col.update_one({"_id": doc["_id"]}, {"$set": {"confirm_chat_id": confirm_msg.chat_id, "confirm_msg_id": confirm_msg.id}})

async def schedulech_cmd(event):
    cli = user_bots.get(event.sender_id)
    if not cli: return await event.reply("❌ Pehle `/login` karo.")
    args = get_args(event.message).strip()
    if not args: return await event.reply("**Format:** `.scdlch <date> <time> <link> <msg>`")
    parts = args.split()
    date_str, time_str = parts[0], parts[1]
    consumed = 2
    if len(parts) >= 3 and parts[2].upper() in ("AM", "PM"):
        time_str += f" {parts[2].upper()}"; consumed = 3
    rest = " ".join(parts[consumed:]).strip()
    try:
        sched_ts = _parse_schedule_time(date_str, time_str).timestamp()
        if sched_ts <= time.time(): raise Exception("Time past me hai.")
    except Exception as e: return await event.reply(f"❌ `{str(e)[:100]}`")

    tp = rest.split(" ", 1); post_link = tp[0]; msg_text = tp[1].strip() if len(tp) > 1 else ""
    m = re.match(r'^https?://t\.me/(?:c/)?([^/\s]+)/(\d+)', post_link)
    if not m: return await event.reply("❌ Invalid post link.")
    chat_part = m.group(1); post_id = int(m.group(2))
    try:
        channel_id = int(chat_part) if chat_part.startswith('-') else int("-100" + chat_part) if chat_part.isdigit() else await cli.get_entity(chat_part)
        channel = await cli.get_entity(channel_id)
        cname = getattr(channel, 'title', chat_part)
    except Exception as e: return await event.reply(f"❌ Channel not found: `{str(e)[:100]}`")

    doc = {"_id": str(ObjectId()), "owner_id": event.sender_id, "target_id": channel.id, "text": msg_text, "reply_to_msg_id": None, "comment_to": post_id, "scheduled_ts": sched_ts, "scheduled_short": _fmt_ist_12h(sched_ts), "scheduled_str": _fmt_ist_full(sched_ts), "status": "pending", "created_at": time.time(), "post_link": post_link}
    await sched_col.insert_one(doc)
    asyncio.create_task(_scheduler_runner(cli, doc))
    
    eta = int(sched_ts - time.time()); h, rem = divmod(eta, 3600); m2, s = divmod(rem, 60)
    preview = msg_text[:80] + ("..." if len(msg_text) > 80 else "")
    confirm_msg = await event.reply(f"✅ **CHANNEL COMMENT SCHEDULED!**\n🆔 `{doc['_id'][:8]}`\n📢 `{cname}`\n🕐 `{doc['scheduled_short']}`\n⏳ `{h}h {m2}m {s}s`\n📝 {preview}", parse_mode='markdown')
    await sched_col.update_one({"_id": doc["_id"]}, {"$set": {"confirm_chat_id": confirm_msg.chat_id, "confirm_msg_id": confirm_msg.id}})

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
    doc = m[0]
    await sched_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "cancelled"}})
    try:
        if doc.get("confirm_chat_id") and doc.get("confirm_msg_id"):
            await bot.delete_messages(int(doc["confirm_chat_id"]), [int(doc["confirm_msg_id"])])
    except: pass
    await event.reply(f"✅ **Cancelled** `{doc['_id'][:8]}`")

async def autocmt_cmd(event):
    uid = event.sender_id
    if not user_bots.get(uid): return await event.reply("❌ Pehle `/login` karo.")
    args = get_args(event.message).strip()
    if not args: return await event.reply("**Format:** `.autocmt on/off/list <link>`")
    parts = args.split()
    action = parts[0].lower()
    if action == 'list':
        cursor = autocmt_col.find({"owner_id": uid, "enabled": True})
        rows = await cursor.to_list(length=50)
        if not rows: return await event.reply("📭 Koi channel set nahi hai.")
        return await event.reply("\n".join([f"📢 `{r['chat_id']}`" for r in rows]))
    if len(parts) < 2: return await event.reply("❌ Channel link do.")
    channel_input = parts[1]
    try:
        un = channel_input.replace("https://t.me/", "").replace("t.me/", "").split("/")[0]
        ent = await user_bots[uid].get_entity(un) if not un.isdigit() else await user_bots[uid].get_entity(int(channel_input))
        cid = str(ent.id)
    except Exception as e: return await event.reply(f"❌ Not found: `{str(e)[:100]}`")
    
    if action == 'off':
        await autocmt_col.update_one({"owner_id": uid, "chat_id": cid}, {"$set": {"enabled": False}}, upsert=True)
        return await event.reply(f"✅ **Auto-Comment OFF** `{cid}`")
    if action == 'on':
        custom = [x.strip() for x in " ".join(parts[2:]).split("|") if x.strip()] if len(parts) > 2 else []
        await autocmt_col.update_one({"owner_id": uid, "chat_id": cid}, {"$set": {"enabled": True, "replies": custom}}, upsert=True)
        await register_autocmt_handler(uid)
        return await event.reply(f"✅ **Auto-Comment ON** `{cid}`")

async def alive_cmd(event):
    await event.reply("✨ **Bot is Alive!** ✨\n⚡ Status: " + ("Online" if user_bots.get(event.sender_id) else "Offline"))

async def sync_cmd(event):
    global PING_OFFSET
    cli = user_bots.get(event.sender_id)
    if not cli: return await event.reply("❌ Pehle `/login` karo.")
    msg = await event.reply("🔄 Syncing...")
    try:
        times = []
        for _ in range(5):
            t0 = time.perf_counter(); await cli(PingRequest(ping_id=random.randint(1, 10**9))); times.append(time.perf_counter() - t0)
            await asyncio.sleep(0.1)
        PING_OFFSET = (sum(times) / len(times)) + 0.005 
        await msg.edit(f"✅ **Sync Complete!**\n⚙️ Offset: `{PING_OFFSET * 1000:.1f} ms`", parse_mode='markdown')
    except Exception as e: await msg.edit(f"❌ Failed: `{str(e)[:100]}`")

# ═══════════════════════════════════════════════
# OWNER COMMANDS
# ═══════════════════════════════════════════════
async def users_cmd(event):
    if event.sender_id != OWNER_ID: return
    cursor = users_col.find({"active": True}).sort("logged_in_at", -1)
    rows = await cursor.to_list(length=100)
    if not rows: return await event.reply("📭 Koi user login nahi hai.")
    text = f"👥 **TOTAL USERS ({len(rows)})**\n\n"
    for i, u in enumerate(rows, 1):
        st = "🟢" if user_bots.get(u["owner_id"]) else "🔴"
        text += f"{i}. {st} **{u.get('first_name', 'Unknown')}**\n   🆔 `{u['owner_id']}` | 📱 `{u['phone']}`\n\n"
    await event.reply(text, parse_mode="markdown")

async def remove_cmd(event):
    if event.sender_id != OWNER_ID: return
    args = get_args(event.message).strip()
    if not args: return await event.reply("`/remove <id>`")
    try:
        uid = int(args)
        await kill_userbot(uid, remove_session=True)
        await event.reply(f"✅ Removed user `{uid}`")
    except: await event.reply("❌ Invalid ID.")

async def block_cmd(event):
    if event.sender_id != OWNER_ID: return
    args = get_args(event.message).strip()
    if not args: return await event.reply("`/block <id>`")
    try:
        uid = int(args)
        await blocked_col.update_one({"owner_id": uid}, {"$set": {"owner_id": uid, "blocked_at": time.time()}}, upsert=True)
        await kill_userbot(uid, remove_session=True)
        await event.reply(f"🚫 User `{uid}` Blocked.")
    except: await event.reply("❌ Invalid ID.")

async def unblock_cmd(event):
    if event.sender_id != OWNER_ID: return
    args = get_args(event.message).strip()
    if not args: return await event.reply("`/unblock <id>`")
    try:
        uid = int(args)
        await blocked_col.delete_one({"owner_id": uid})
        await event.reply(f"✅ User `{uid}` Unblocked.")
    except: await event.reply("❌ Invalid ID.")

async def broadcast_cmd(event):
    if event.sender_id != OWNER_ID: return
    args = get_args(event.message).strip()
    if not args: return await event.reply("`/broadcast <msg>`")
    cursor = users_col.find({"active": True})
    users = await cursor.to_list(length=500)
    prog = await event.reply(f"📢 Broadcasting to {len(users)} users...")
    ok, fail = 0, 0
    for u in users:
        try:
            await bot.send_message(u["owner_id"], f"📢 **Announcement**\n\n{args}")
            ok += 1
        except: fail += 1
        await asyncio.sleep(0.05)
    await prog.edit(f"✅ Done! Sent: {ok} | Failed: {fail}")

async def stats_cmd(event):
    if event.sender_id != OWNER_ID: return
    total_u = await users_col.count_documents({"active": True})
    total_s = await sched_col.count_documents({"status": "pending"})
    online = sum(1 for u in user_bots.values() if u.is_connected())
    await event.reply(f"📊 **Bot Stats**\n\n👥 Users: `{total_u}` (Online: `{online}`)\n⏰ Pending Schedules: `{total_s}`")

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
    global bot
    log.info("⏳ Connecting to MongoDB...")
    await init_mongo()
    log.info("✅ MongoDB Connected!")
    
    bot = TelegramClient("schedule_x_bot_bot", API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    log.info("✅ Bot Online!")

    # ── BOT DM COMMANDS ──
    bot.add_event_handler(start_cmd, events.NewMessage(pattern=r"^[./]start$", func=lambda e: e.is_private and not e.out))
    bot.add_event_handler(help_cmd, events.NewMessage(pattern=r"^[./]help$", func=lambda e: e.is_private and not e.out))
    bot.add_event_handler(login_cmd, events.NewMessage(pattern=r"^[./]login$", func=lambda e: e.is_private and not e.out))
    bot.add_event_handler(logout_cmd, events.NewMessage(pattern=r"^[./]logout$", func=lambda e: e.is_private and not e.out))
    bot.add_event_handler(status_cmd, events.NewMessage(pattern=r"^[./]status$", func=lambda e: e.is_private and not e.out))
    bot.add_event_handler(login_flow, events.NewMessage(func=lambda e: e.is_private and not e.out and not (e.text or "").startswith('/')))
    
    # ── GLOBAL COMMANDS ──
    bot.add_event_handler(schedule_cmd, events.NewMessage(pattern=r"^[./]scdl(?:\s|$)"))
    bot.add_event_handler(schedulech_cmd, events.NewMessage(pattern=r"^[./]scdlch(?:\s|$)"))
    bot.add_event_handler(schedules_cmd, events.NewMessage(pattern=r"^[./]schedules$"))
    bot.add_event_handler(cancelschedule_cmd, events.NewMessage(pattern=r"^[./]cancelschedule(?:\s|$)"))
    bot.add_event_handler(autocmt_cmd, events.NewMessage(pattern=r"^[./]autocmt(?:\s|$)"))
    bot.add_event_handler(alive_cmd, events.NewMessage(pattern=r"^[./]alive$"))
    bot.add_event_handler(sync_cmd, events.NewMessage(pattern=r"^[./]sync$"))

    # ── OWNER COMMANDS ──
    bot.add_event_handler(users_cmd, events.NewMessage(pattern=r"^[./]users$", func=lambda e: e.is_private and not e.out))
    bot.add_event_handler(remove_cmd, events.NewMessage(pattern=r"^[./]remove(?:\s|$)", func=lambda e: e.is_private and not e.out))
    bot.add_event_handler(block_cmd, events.NewMessage(pattern=r"^[./]block(?:\s|$)", func=lambda e: e.is_private and not e.out))
    bot.add_event_handler(unblock_cmd, events.NewMessage(pattern=r"^[./]unblock(?:\s|$)", func=lambda e: e.is_private and not e.out))
    bot.add_event_handler(broadcast_cmd, events.NewMessage(pattern=r"^[./]broadcast(?:\s|$)", func=lambda e: e.is_private and not e.out))
    bot.add_event_handler(stats_cmd, events.NewMessage(pattern=r"^[./]stats$", func=lambda e: e.is_private and not e.out))

    await restore_all_userbots()
    asyncio.create_task(health_server())
    
    log.info("🚀 BOT READY")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
