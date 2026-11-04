"""Login flow: phone → OTP → 2FA → StringSession saved → live client."""
import os
import time
import asyncio
import logging
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    PhoneCodeInvalidError, PhoneCodeExpiredError, SessionPasswordNeededError,
    PhoneNumberInvalidError, PasswordHashInvalidError, FloodWaitError,
)
from db import users_col, sessions_col, pending_col, blocked_col

log = logging.getLogger("userbot_mgr")
API_ID   = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")

user_bots: dict[int, TelegramClient] = {}
_TEMP_CLIENTS: dict[str, TelegramClient] = {}

async def _safe_disconnect(cl):
    try: await cl.disconnect()
    except Exception: pass

async def begin_login(owner_id: int, phone: str) -> dict:
    phone = phone.strip()
    if not phone.startswith("+"):
        return {"ok": False, "error": "Phone `+` se start hona chahiye (e.g. `+919876543210`)"}

    if await blocked_col.find_one({"owner_id": owner_id}):
        blk = await blocked_col.find_one({"owner_id": owner_id})
        return {"ok": False, "error": f"🚫 Aap blocked ho.\nReason: `{blk.get('reason','-')}`"}

    if owner_id in user_bots and user_bots[owner_id].is_connected():
        return {"ok": False, "error": "Aap already logged in ho. Pehle `.logout` karo."}

    temp = TelegramClient(StringSession(), API_ID, API_HASH)
    try: await temp.connect()
    except Exception as e: return {"ok": False, "error": f"Connect fail: {str(e)[:100]}"}

    try: sent = await temp.send_code_request(phone)
    except PhoneNumberInvalidError:
        await _safe_disconnect(temp)
        return {"ok": False, "error": "❌ Invalid phone number."}
    except FloodWaitError as e:
        await _safe_disconnect(temp)
        return {"ok": False, "error": f"⏰ FloodWait {e.seconds}s — thodi der baad try karo."}
    except Exception as e:
        await _safe_disconnect(temp)
        return {"ok": False, "error": f"OTP send fail: {str(e)[:150]}"}

    await pending_col.update_one(
        {"phone": phone},
        {"$set": {
            "phone": phone, "owner_id": owner_id, "phone_code_hash": sent.phone_code_hash,
            "session": temp.session.save(), "stage": "otp", "created_at": time.time(),
        }}, upsert=True,
    )
    old = _TEMP_CLIENTS.pop(phone, None)
    if old: await _safe_disconnect(old)
    _TEMP_CLIENTS[phone] = temp
    return {"ok": True}

async def submit_otp(phone: str, otp: str) -> dict:
    phone = phone.strip().replace(" ", "")
    state = await pending_col.find_one({"phone": phone})
    if not state: return {"ok": False, "error": "Login session expire. `.login` phir se karo."}
    
    temp = _TEMP_CLIENTS.get(phone)
    if temp is None:
        temp = TelegramClient(StringSession(state["session"]), API_ID, API_HASH)
        await temp.connect()
        _TEMP_CLIENTS[phone] = temp

    try:
        await temp.sign_in(phone=phone, code=otp.strip(), phone_code_hash=state["phone_code_hash"])
    except SessionPasswordNeededError:
        await pending_col.update_one({"phone": phone}, {"$set": {"stage": "2fa"}})
        return {"ok": True, "stage": "2fa"}
    except PhoneCodeInvalidError: return {"ok": False, "error": "❌ Galat OTP."}
    except PhoneCodeExpiredError:
        await _cleanup(phone)
        return {"ok": False, "error": "OTP expire. `.login` phir se karo."}
    except Exception as e: return {"ok": False, "error": f"OTP error: {str(e)[:150]}"}

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
    except PasswordHashInvalidError: return {"ok": False, "error": "❌ Galat 2FA password."}
    except Exception as e: return {"ok": False, "error": f"2FA error: {str(e)[:150]}"}

    return await _finalize(phone, state["owner_id"])

async def _finalize(phone: str, owner_id: int) -> dict:
    temp = _TEMP_CLIENTS.get(phone)
    if temp is None: return {"ok": False, "error": "Internal: temp client missing."}

    try:
        me = await temp.get_me()
        session_str = temp.session.save()
    except Exception as e: return {"ok": False, "error": f"Finalize failed: {e}"}

    await users_col.update_one(
        {"owner_id": owner_id},
        {"$set": {
            "owner_id": owner_id, "phone": phone, "tg_id": me.id, "username": me.username,
            "first_name": me.first_name, "active": True, "suspended": False,
            "blocked": False, "logged_in_at": time.time(),
        }}, upsert=True,
    )
    await sessions_col.update_one(
        {"owner_id": owner_id},
        {"$set": {"owner_id": owner_id, "phone": phone, "session_str": session_str, "updated_at": time.time()}},
        upsert=True,
    )
    await _cleanup(phone, disconnect=False)
    await _safe_disconnect(temp)
    await spawn_userbot(owner_id)
    return {"ok": True, "stage": "done", "tg_id": me.id, "username": me.username}

async def _cleanup(phone: str, disconnect: bool = True):
    temp = _TEMP_CLIENTS.pop(phone, None)
    if temp and disconnect: await _safe_disconnect(temp)
    try: await pending_col.delete_one({"phone": phone})
    except Exception: pass

async def cancel_login(phone: str): await _cleanup(phone)

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
        me = await cl.get_me()
        log.info(f"🟢 userbot {owner_id} as @{me.username or me.id}")
    except Exception as e:
        log.error(f"spawn {owner_id}: {e}")
        return None

    user_bots[owner_id] = cl
    return cl

async def restore_all_userbots():
    cursor = users_col.find({"active": True, "suspended": {"$ne": True}})
    async for u in cursor:
        try: await spawn_userbot(u["owner_id"])
        except Exception as e: log.warning(f"restore {u['owner_id']}: {e}")

async def kill_userbot(owner_id: int, remove_session: bool = True):
    cl = user_bots.pop(owner_id, None)
    if cl: await _safe_disconnect(cl)
    if remove_session:
        await users_col.update_one({"owner_id": owner_id}, {"$set": {"active": False}})
        await sessions_col.delete_one({"owner_id": owner_id})
        log.info(f"🔴 userbot killed {owner_id}")

def get_userbot(owner_id: int) -> TelegramClient | None:
    cl = user_bots.get(owner_id)
    if cl and cl.is_connected(): return cl
    return None
