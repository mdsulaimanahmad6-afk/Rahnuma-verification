import os
import asyncio
import logging
from aiohttp import web
from telethon import TelegramClient, events, Button
from motor.motor_asyncio import AsyncIOMotorClient

# Asli Web App Button ke liye import
from telethon.tl.types import KeyboardButtonWebView, ReplyInlineMarkup, KeyboardButtonRow

# ═══════════════ CONFIG ═══════════════
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")
MONGO_URI = os.environ.get("MONGO_URI", "")

# Yahan apne channel ka username daalein (Dhyan rahe bot wahan Admin hona chahiye)
REQUIRED_CHANNELS = ["@Rahnuma_e_Deen"] 

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("Verifier")

bot = TelegramClient('verify_bot', API_ID, API_HASH)
bot.parse_mode = "markdown"

# ═══════════════ DATABASE SETUP ═══════════════
db = None
ip_col = None
users_col = None

async def init_db():
    global db, ip_col, users_col
    if not MONGO_URI:
        log.warning("⚠️ MONGO_URI set nahi hai!")
        return
    try:
        client = AsyncIOMotorClient(MONGO_URI)
        db = client['verifier_bot']
        ip_col = db['ip_addresses'] 
        users_col = db['verified_users'] 
        await client.admin.command('ping')
        log.info("✅ MongoDB Connected!")
    except Exception as e:
        log.error(f"❌ MongoDB Error: {e}")

# ═══════════════ WEB ROUTES ═══════════════
async def handle_index(request):
    try:
        with open('templates/index.html', 'r', encoding='utf-8') as f:
            return web.Response(text=f.read(), content_type='text/html')
    except Exception as e:
        return web.Response(text=f"Error loading UI: {e}", status=500)

async def handle_verify(request):
    try:
        data = await request.json()
        user_id = int(data.get('user_id'))
        
        ip_address = request.headers.get('X-Forwarded-For', request.remote)
        if not ip_address:
            ip_address = "unknown_ip"
        if ',' in ip_address:
            ip_address = ip_address.split(',')[0].strip()

        if users_col is None:
            return web.json_response({"status": "error", "message": "Database error"})

        user_exists = await users_col.find_one({"user_id": user_id})
        if user_exists:
            return web.json_response({"status": "success"})

        ip_exists = await ip_col.find_one({"ip": ip_address})
        if ip_exists:
            if ip_exists['user_id'] != user_id:
                return web.json_response({"status": "error", "message": "Multiple accounts detected"})

        await users_col.insert_one({"user_id": user_id})
        if not ip_exists:
            await ip_col.insert_one({"ip": ip_address, "user_id": user_id})
            
        return web.json_response({"status": "success"})
        
    except Exception as e:
        return web.json_response({"status": "error"})

# ═══════════════ BOT HANDLERS ═══════════════
@bot.on(events.NewMessage(pattern='/start'))
async def start_h(event):
    user_id = event.sender_id
    
    try:
        if users_col is not None:
            user_exists = await users_col.find_one({"user_id": user_id})
            if user_exists:
                await event.reply("✅ **Aap verified hain!**\n\nAap bot use kar sakte hain.")
                return
        else:
            await event.reply("⚠️ Database not connected. Try later.")
            return

        # Force-Join Check
        not_joined = []
        for channel in REQUIRED_CHANNELS:
            try:
                await bot.get_permissions(channel, user_id)
            except Exception:
                not_joined.append(channel)

        if not_joined:
            text = "⚠️ **Verification ke liye pehle in channels ko join karein:**\n\n"
            buttons = []
            for ch in not_joined:
                link = f"https://t.me/{ch.replace('@', '')}"
                buttons.append([Button.url(f"Join {ch}", link)])
            buttons.append([Button.inline("✅ Maine Join Kar Liya", data=b"check_join")])
            await event.reply(text, buttons=buttons)
            return

        # ASLI WEB APP BUTTON (Telegram ke andar khulega)
        if not WEBAPP_URL or not WEBAPP_URL.startswith("https://"):
            await event.reply("❌ **WEBAPP_URL theek nahi hai.**")
            return

        webapp_btn = KeyboardButtonWebView(text="🛡️ Verify Now", url=WEBAPP_URL)
        rows = [KeyboardButtonRow([webapp_btn])]
        reply_markup = ReplyInlineMarkup(rows)

        await event.reply(
            "🛡️ **Verify Yourself To Start Bot**\n\nNiche diye gaye button par click karke apna device verify karein.",
            buttons=reply_markup
        )
        
    except Exception as e:
        log.error(f"Start error: {e}")
        await event.reply(f"❌ **Error:**\n`{str(e)}`")

# ═══════════════ CALLBACK (Force-Join ke liye) ═══════════════
@bot.on(events.CallbackQuery(data=b"check_join"))
async def check_join_callback(event):
    user_id = event.sender_id
    not_joined = []
    for channel in REQUIRED_CHANNELS:
        try:
            await bot.get_permissions(channel, user_id)
        except Exception:
            not_joined.append(channel)

    if not_joined:
        await event.answer("❌ Aapne abhi tak saare channels join nahi kiye hain!", alert=True)
    else:
        # Agar join kar liya toh naya message bhej de Web App button ke saath
        webapp_btn = KeyboardButtonWebView(text="🛡️ Verify Now", url=WEBAPP_URL)
        rows = [KeyboardButtonRow([webapp_btn])]
        reply_markup = ReplyInlineMarkup(rows)
        
        await event.respond("✅ **Channels joined!** Ab niche button dabakar verify karein.", buttons=reply_markup)
        await event.delete()

# ═══════════════ RUNNER ═══════════════
async def main():
    await init_db()
    try:
        await bot.start(bot_token=BOT_TOKEN)
        log.info("Bot started...")
    except Exception as e:
        log.error(f"❌ Bot start failed: {e}")
        return
    
    app = web.Application()
    app.router.add_get('/', handle_index)
    app.router.add_post('/api/verify', handle_verify)
    
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    await asyncio.gather(
        bot.run_until_disconnected(),
        asyncio.Event().wait()
    )

if __name__ == '__main__':
    asyncio.run(main())
