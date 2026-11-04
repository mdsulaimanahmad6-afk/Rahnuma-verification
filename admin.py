import os
import time
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from telethon.tl.functions import PingRequest
import db # <-- Yahan change kiya
from userbot_manager import user_bots, spawn_userbot, kill_userbot

log = logging.getLogger("admin")
IST_TZ = ZoneInfo("Asia/Kolkata")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

def is_owner(uid: int) -> bool: return uid == OWNER_ID

def fmt_ts(ts):
    if not ts: return "-"
    return datetime.fromtimestamp(ts, IST_TZ).strftime("%d %b %Y, %I:%M %p")

def fmt_ago(ts):
    if not ts: return "-"
    d = int(time.time() - ts)
    if d < 60: return f"{d}s ago"
    if d < 3600: return f"{d//60}m ago"
    if d < 86400: return f"{d//3600}h ago"
    return f"{d//86400}d ago"

async def resolve_user(query: str):
    query = query.strip()
    if not query: return None
    if query.startswith("+"): return await db.users_col.find_one({"phone": query})
    if query.startswith("@"): return await db.users_col.find_one({"username": query[1:]})
    if query.isdigit():
        n = int(query)
        u = await db.users_col.find_one({"owner_id": n})
        if u: return u
        return await db.users_col.find_one({"tg_id": n})
    return None

async def user_stats(uid):
    return {
        "pending": await db.sched_col.count_documents({"owner_id": uid, "status": "pending"}),
        "sent":    await db.sched_col.count_documents({"owner_id": uid, "status": "sent"}),
        "failed":  await db.sched_col.count_documents({"owner_id": uid, "status": "failed"}),
        "acmt":    await db.autocmt_col.count_documents({"owner_id": uid, "enabled": True}),
    }

async def admin_cmd(event):
    if not is_owner(event.sender_id): return await event.reply("❌ Sirf owner.")
    total_users = await db.users_col.count_documents({"active": True})
    online = sum(1 for u in user_bots.values() if u.is_connected())
    text = f"👑 **OWNER DASHBOARD**\n{'═' * 25}\n\n👥 Users: `{total_users}` (online: `{online}`)\n\n`.users` `.userinfo <id>`\n`.blockuser <id> [reason]` `.unblockuser <id>`\n`.removeuser <id>`\n`.suspend <id>` `.resume <id>` `.forcekill <id>`\n`.blockedlist` `.broadcast <msg>` `.sysinfo`"
    await event.reply(text, parse_mode="markdown")

async def users_cmd(event):
    if not is_owner(event.sender_id): return
    cursor = db.users_col.find({"active": True}).sort("logged_in_at", -1)
    rows = await cursor.to_list(length=100)
    if not rows: return await event.reply("📭 Koi user login nahi.")
    text = f"👥 **ALL USERS ({len(rows)})**\n{'═' * 25}\n\n"
    for i, u in enumerate(rows, 1):
        st = "🟢" if user_bots.get(u["owner_id"]) else "🔴"
        text += f"{i}. {st} `{u['owner_id']}` — `{u['phone']}` (@{u.get('username') or '-'})\n"
    await event.reply(text, parse_mode="markdown")

async def userinfo_cmd(event, args):
    if not is_owner(event.sender_id) or not args.strip(): return
    u = await resolve_user(args.strip().split()[0])
    if not u: return await event.reply("❌ User not found.")
    st = "🟢" if user_bots.get(u["owner_id"]) else "🔴"
    await event.reply(f"👤 **USER INFO**\n🆔 `{u['owner_id']}`\n📱 `{u['phone']}`\n👤 @{u.get('username') or '-'}\n⚡ Status: {st}\n📅 Logged: {fmt_ts(u.get('logged_in_at'))}", parse_mode="markdown")

async def blockuser_cmd(event, args):
    if not is_owner(event.sender_id): return
    parts = args.strip().split(None, 1)
    if not parts: return await event.reply("❌ `.blockuser <id> [reason]`")
    u = await resolve_user(parts[0])
    if not u: return await event.reply("❌ User not found.")
    uid = u["owner_id"]
    if uid == OWNER_ID: return await event.reply("❌ Owner ko block nahi kar sakte.")
    reason = parts[1] if len(parts) > 1 else "No reason"
    await db.blocked_col.update_one({"owner_id": uid}, {"$set": {"owner_id": uid, "phone": u["phone"], "reason": reason, "blocked_at": time.time()}}, upsert=True)
    await kill_userbot(uid, remove_session=True)
    await db.users_col.update_one({"owner_id": uid}, {"$set": {"active": False, "blocked": True}})
    await event.reply(f"🚫 **BLOCKED** `{uid}`\nReason: {reason}")

async def unblockuser_cmd(event, args):
    if not is_owner(event.sender_id): return
    u = await resolve_user(args.strip())
    if not u: return await event.reply("❌ User not found.")
    await db.blocked_col.delete_one({"owner_id": u["owner_id"]})
    await db.users_col.update_one({"owner_id": u["owner_id"]}, {"$set": {"blocked": False}})
    await event.reply(f"✅ **UNBLOCKED** `{u['owner_id']}`")

async def removeuser_cmd(event, args):
    if not is_owner(event.sender_id): return
    u = await resolve_user(args.strip())
    if not u: return await event.reply("❌ User not found.")
    await kill_userbot(u["owner_id"], remove_session=True)
    await event.reply(f"🗑️ **REMOVED** `{u['owner_id']}`")

async def suspend_cmd(event, args):
    if not is_owner(event.sender_id): return
    u = await resolve_user(args.strip())
    if not u: return await event.reply("❌ User not found.")
    cl = user_bots.pop(u["owner_id"], None)
    if cl: await cl.disconnect()
    await db.users_col.update_one({"owner_id": u["owner_id"]}, {"$set": {"suspended": True}})
    await event.reply(f"⏸️ **SUSPENDED** `{u['owner_id']}`")

async def resume_cmd(event, args):
    if not is_owner(event.sender_id): return
    u = await resolve_user(args.strip())
    if not u: return await event.reply("❌ User not found.")
    await db.users_col.update_one({"owner_id": u["owner_id"]}, {"$set": {"suspended": False}})
    if await spawn_userbot(u["owner_id"]): await event.reply(f"▶️ **RESUMED** `{u['owner_id']}`")
    else: await event.reply("⚠️ Spawn fail.")

async def forcekill_cmd(event, args):
    if not is_owner(event.sender_id): return
    u = await resolve_user(args.strip())
    if u and u["owner_id"] in user_bots:
        await user_bots[u["owner_id"]].disconnect()
        user_bots.pop(u["owner_id"], None)
        await event.reply(f"⚡ Killed `{u['owner_id']}`")
    else: await event.reply("ℹ️ Not active.")

async def broadcast_cmd(event, args):
    if not is_owner(event.sender_id) or not args.strip(): return
    cursor = db.users_col.find({"active": True})
    users = await cursor.to_list(length=1000)
    prog = await event.reply(f"📢 Broadcasting to {len(users)}...")
    ok = fail = 0
    for i, u in enumerate(users, 1):
        try: await event.client.send_message(u["owner_id"], f"📢 **ANNOUNCEMENT**\n\n{args.strip()}"); ok+=1
        except: fail+=1
        await asyncio.sleep(0.05)
    await prog.edit(f"✅ Done! Sent: `{ok}` Failed: `{fail}`")

async def blockedlist_cmd(event):
    if not is_owner(event.sender_id): return
    cursor = db.blocked_col.find({})
    rows = await cursor.to_list(length=100)
    if not rows: return await event.reply("✅ No blocked users.")
    text = "🚫 **BLOCKED**\n\n"
    for b in rows: text += f"`{b['owner_id']}` — `{b.get('phone','-')}`\n"
    await event.reply(text, parse_mode="markdown")

async def sysinfo_cmd(event):
    if not is_owner(event.sender_id): return
    try:
        import psutil
        cpu = f"{psutil.cpu_percent(interval=0.5)}%"
        ram = psutil.virtual_memory()
        ram_str = f"{ram.percent}% ({ram.used // (1024**2)}MB / {ram.total // (1024**2)}MB)"
    except: cpu = ram_str = "N/A"
    active = sum(1 for u in user_bots.values() if u.is_connected())
    await event.reply(f"🖥️ **SYS INFO**\n🤖 Userbots: `{active}/{len(user_bots)}`\n🧠 CPU: `{cpu}`\n💾 RAM: `{ram_str}`", parse_mode="markdown")
