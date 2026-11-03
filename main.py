import os
import asyncio
import logging
from aiohttp import web
from telethon import TelegramClient, events, Button

# ═══════════════ CONFIG ═══════════════
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("Verifier")

# ═══════════════ DATABASE (IP -> UserID) ═══════════════
# Ye temporary RAM me save hoga. Render restart hone par clear ho jayega.
ip_database = {} 
verified_users = set()

bot = TelegramClient('verify_bot', API_ID, API_HASH)

# ═══════════════ WEB ROUTES ═══════════════
async def handle_index(request):
    try:
        # templates folder ke andar wali index.html ko load karega
        with open('templates/index.html', 'r', encoding='utf-8') as f:
            return web.Response(text=f.read(), content_type='text/html')
    except Exception as e:
        return web.Response(text=f"Error loading UI: {e}", status=500)

async def handle_verify(request):
    """Jab user UI me 82% ke baad Verify dabata hai"""
    try:
        data = await request.json()
        user_id = int(data.get('user_id'))
        
        # User ka IP address nikalein
        ip_address = request.headers.get('X-Forwarded-For', request.remote)
        if not ip_address:
            ip_address = "unknown_ip"
            
        # Agar multiple IPs hain (proxy ke case me), toh pehla wala lo
        if ',' in ip_address:
            ip_address = ip_address.split(',')[0].strip()

        log.info(f"Checking User: {user_id} | IP: {ip_address}")

        # 1. Agar user pehle se verified hai
        if user_id in verified_users:
            return web.json_response({"status": "success"})

        # 2. Agar same IP par koi aur account pehle verify ho chuka hai
        if ip_address in ip_database:
            existing_user = ip_database[ip_address]
            if existing_user != user_id:
                log.warning(f"BLOCKED! IP {ip_address} already used by {existing_user}")
                return web.json_response({"status": "error", "message": "Multiple accounts detected"})
        
        # 3. Naya user verify kar do
        ip_database[ip_address] = user_id
        verified_users.add(user_id)
        
        return web.json_response({"status": "success"})
        
    except Exception as e:
        log.error(f"Verify Error: {e}")
        return web.json_response({"status": "error"})

# ═══════════════ BOT HANDLERS ═══════════════
@bot.on(events.NewMessage(pattern='/start'))
async def start_h(event):
    user_id = event.sender_id
    
    if user_id in verified_users:
        await event.reply("✅ **Aap verified hain!**\n\nAap bot use kar sakte hain.")
        return

    # Web App button jo website open karega
    buttons = [[Button.web_view("🛡️ Verify", WEBAPP_URL)]]
    
    await event.reply(
        "🛡️ **Verify Yourself To Start Bot**",
        buttons=buttons
    )

# ═══════════════ RUNNER ═══════════════
async def main():
    await bot.start(bot_token=BOT_TOKEN)
    log.info("Bot started...")
    
    app = web.Application()
    app.router.add_get('/', handle_index)
    app.router.add_post('/api/verify', handle_verify)
    
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    log.info(f"Web running on port {port}")
    
    await asyncio.gather(
        bot.run_until_disconnected(),
        asyncio.Event().wait()
    )

if __name__ == '__main__':
    asyncio.run(main())
