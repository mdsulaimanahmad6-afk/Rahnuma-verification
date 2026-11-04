"""MongoDB initialization + collection handles."""
import os
import logging
from motor.motor_asyncio import AsyncIOMotorClient

log = logging.getLogger("db")

_client = None
_db = None

users_col = None
sessions_col = None
sched_col = None
autocmt_col = None
pending_col = None
blocked_col = None

async def init_mongo():
    # Yahan global declare karna sabse zaroori tha
    global _client, _db
    global users_col, sessions_col, sched_col, autocmt_col, pending_col, blocked_col

    uri = os.getenv("MONGO_URI")
    if not uri:
        raise RuntimeError("MONGO_URI missing")

    _client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000)
    await _client.admin.command("ping")
    
    _db = _client["schedule_x_bot"]

    # Ab ye variables update ho jayenge
    users_col    = _db["users"]
    sessions_col = _db["sessions"]
    sched_col    = _db["schedules"]
    autocmt_col  = _db["auto_comment"]
    pending_col  = _db["pending_logins"]
    blocked_col  = _db["blocked"]

    await users_col.create_index("owner_id", unique=True)
    await users_col.create_index("phone")
    await sessions_col.create_index("owner_id", unique=True)
    await sched_col.create_index([("owner_id", 1), ("status", 1)])
    await sched_col.create_index("scheduled_ts")
    await autocmt_col.create_index([("owner_id", 1), ("chat_id", 1)], unique=True)
    await pending_col.create_index("phone", unique=True)
    await blocked_col.create_index("owner_id", unique=True)
    await blocked_col.create_index("phone")

    try:
        await pending_col.create_index("created_at", expireAfterSeconds=600)
    except Exception:
        pass

    log.info("✅ MongoDB connected & indexed")
