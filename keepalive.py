"""Ping loop per userbot — auto-reconnect."""
import asyncio
import random
import logging
from telethon.tl.functions import PingRequest
from userbot_manager import user_bots, spawn_userbot

log = logging.getLogger("keepalive")

async def keepalive_loop(owner_id: int):
    while True:
        await asyncio.sleep(15)
        if owner_id not in user_bots: return
        cl = user_bots[owner_id]
        try:
            if not cl.is_connected():
                await cl.connect()
            await asyncio.wait_for(cl(PingRequest(ping_id=random.randint(1, 10**9))), timeout=10)
        except asyncio.TimeoutError:
            try:
                await cl.disconnect()
                await asyncio.sleep(2)
                await cl.connect()
            except Exception as e:
                log.error(f"reconnect fail {owner_id}: {e}")
                await spawn_userbot(owner_id)
        except Exception as e:
            try:
                if not cl.is_connected(): await cl.connect()
            except Exception: pass
