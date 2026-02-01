import asyncio
import time
import hmac
import hashlib
import json
import os
import httpx
from telegram import Bot
from datetime import datetime
from aiohttp import web

# ================== НАЛАШТУВАННЯ ==================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
if not TELEGRAM_TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN не знайдено у Variables")

CHAT_ID = int(os.getenv("CHAT_ID", "287224456"))

ACCESS_ID = os.getenv("ACCESS_ID", "").strip() or "9gecmcdum9rj8q7uymgc"
ACCESS_SECRET = os.getenv("ACCESS_SECRET", "").strip() or "058a6a9bbe7d4beb800e65500822f413"
DEVICE_ID = os.getenv("DEVICE_ID", "").strip() or "bfa197db4a74f16983d2ru"
REGION = os.getenv("REGION", "eu").strip()

# Для webhook:
# PUBLIC_URL — твой публичный домен Railway вида https://xxxxx.up.railway.app
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()
if not PUBLIC_URL:
    raise ValueError("❌ PUBLIC_URL не задан. Вкажи https://<твій-домен>.up.railway.app")

# Секрет для webhook (любой сложный набор символов)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
if not WEBHOOK_SECRET:
    raise ValueError("❌ WEBHOOK_SECRET не задан. Додай його в Variables (будь-який складний рядок)")

PORT = int(os.getenv("PORT", "8080"))

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
DEBOUNCE_INTERVAL = int(os.getenv("DEBOUNCE_INTERVAL", "20"))
MAX_LOG_DAYS = int(os.getenv("MAX_LOG_DAYS", "60"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
LOG_FILE = os.path.join(BASE_DIR, "log.json")

# ==================================================

bot = Bot(token=TELEGRAM_TOKEN)
access_token = None
token_expire_at = 0

last_online_state = None
last_change_time = None
pending_state = None
pending_time = None


# ================== HELPERS ==================

def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02}:{m:02}:{s:02}"

def sign_request(method: str, url: str, body: str = "", token: str = "") -> dict:
    t = str(int(time.time() * 1000))
    body_hash = sha256_hex(body)
    string_to_sign = (
        ACCESS_ID + token + t + method + "\n" + body_hash + "\n\n" + url
    )
    sign = hmac.new(
        ACCESS_SECRET.encode(),
        string_to_sign.encode(),
        hashlib.sha256
    ).hexdigest().upper()

    headers = {
        "client_id": ACCESS_ID,
        "t": t,
        "sign": sign,
        "sign_method": "HMAC-SHA256",
    }
    if token:
        headers["access_token"] = token
    return headers


# ================== STATE ==================

def load_state():
    global last_online_state, last_change_time
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            last_online_state = data.get("online")
            last_change_time = data.get("timestamp")
            print(f"✅ Стан відновлено: online={last_online_state}")
    except Exception as e:
        print("ERROR load_state:", e)

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"online": last_online_state, "timestamp": last_change_time},
                f
            )
    except Exception as e:
        print("ERROR save_state:", e)


# ================== LOG ==================

def save_log(state, duration):
    log = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                log = json.load(f)
        except Exception:
            log = []

    log.append({
        "timestamp": int(time.time()),
        "state": bool(state),
        "duration": int(duration)
    })

    cutoff = int(time.time()) - MAX_LOG_DAYS * 24 * 3600
    log = [x for x in log if x.get("timestamp", 0) >= cutoff]

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f)

def summarize(days):
    now = int(time.time())
    start = now - int(days) * 24 * 3600
    online = 0
    offline = 0

    if not os.path.exists(LOG_FILE):
        return online, offline

    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            log = json.load(f)
    except Exception:
        return online, offline

    for entry in log:
        if entry.get("timestamp", 0) >= start:
            if entry.get("state"):
                online += int(entry.get("duration", 0))
            else:
                offline += int(entry.get("duration", 0))

    return online, offline


# ================== TUYA ==================

async def get_access_token():
    global access_token, token_expire_at
    url = "/v1.0/token?grant_type=1"
    headers = sign_request("GET", url)

    async with httpx.AsyncClient(
        base_url=f"https://openapi.tuya{REGION}.com",
        timeout=15
    ) as client:
        r = await client.get(url, headers=headers)
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(data)
        access_token = data["result"]["access_token"]
        token_expire_at = time.time() + data["result"]["expire_time"] - 60
        print("✅ Tuya access_token отримано")

async def get_device_online_status() -> bool:
    global access_token
    if not access_token or time.time() > token_expire_at:
        await get_access_token()

    url = f"/v1.0/devices/{DEVICE_ID}"
    headers = sign_request("GET", url, token=access_token)

    async with httpx.AsyncClient(
        base_url=f"https://openapi.tuya{REGION}.com",
        timeout=15
    ) as client:
        r = await client.get(url, headers=headers)
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(data)
        return bool(data["result"]["online"])


# ================== MONITOR ==================

async def monitor():
    global last_online_state, last_change_time, pending_state, pending_time

    load_state()
    print("🤖 Моніторинг ONLINE / OFFLINE запущено")

    while True:
        try:
            is_online = await get_device_online_status()
            now = time.time()

            if last_online_state is None:
                last_online_state = is_online
                last_change_time = now
                save_state()

            elif is_online != last_online_state:
                if pending_state != is_online:
                    pending_state = is_online
                    pending_time = now
                elif now - pending_time >= DEBOUNCE_INTERVAL:
                    duration = int(now - last_change_time)

                    msg = (
                        f"💡 Світло зʼявилось\n🌑 Темрява: {format_duration(duration)}"
                        if pending_state
                        else
                        f"❌ Світло зникло\n⏱ Світло було: {format_duration(duration)}"
                    )

                    await bot.send_message(CHAT_ID, msg)
                    save_log(last_online_state, duration)

                    last_online_state = pending_state
                    last_change_time = now
                    pending_state = None
                    pending_time = None
                    save_state()
            else:
                pending_state = None
                pending_time = None

            print(f"{time.strftime('%H:%M:%S')} online = {is_online}")

        except Exception as e:
            print("ERROR monitor:", e)

        await asyncio.sleep(CHECK_INTERVAL)


# ================== SUMMARY ==================

async def summary_scheduler():
    """
    Каждый день в 00:01 — дневной отчёт.
    Каждый понедельник в 00:01 — недельный отчёт.
    """
    while True:
        try:
            now = datetime.now()

            if now.hour == 0 and now.minute == 1:
                if now.weekday() == 0:
                    online, offline = summarize(7)
                    await bot.send_message(
                        CHAT_ID,
                        f"📊 Підсумки за тиждень:\nONLINE {format_duration(online)}, OFFLINE {format_duration(offline)}"
                    )
                    await asyncio.sleep(61)
                    continue

                online, offline = summarize(1)
                await bot.send_message(
                    CHAT_ID,
                    f"📊 Підсумки за день:\nONLINE {format_duration(online)}, OFFLINE {format_duration(offline)}"
                )
                await asyncio.sleep(61)
                continue

        except Exception as e:
            print("ERROR summary:", e)

        await asyncio.sleep(30)


# ================== WEBHOOK ==================

async def set_telegram_webhook():
    # URL БЕЗ секрета в пути (секрет будет только в заголовке)
    webhook_url = f"{PUBLIC_URL.rstrip('/')}/webhook"

    info_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getWebhookInfo"
    set_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"

    async with httpx.AsyncClient(timeout=15) as client:
        info = (await client.get(info_url)).json()
        current = (info.get("result") or {}).get("url", "")

        if current == webhook_url:
            print("✅ Webhook вже встановлено (нічого не міняю)")
            return

        payload = {
            "url": webhook_url,
            "drop_pending_updates": True,
            "secret_token": WEBHOOK_SECRET,  # Telegram будет слать этот секрет в заголовке
        }
        r = await client.post(set_url, json=payload)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"setWebhook failed: {data}")

        print("✅ Webhook встановлено")


async def handle_update_safe(update: dict):
    try:
        await handle_update(update)
    except Exception as e:
        print("ERROR handle_update:", e)


async def handle_update(update: dict):
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat_id = (message.get("chat") or {}).get("id")
    text = (message.get("text") or "").strip()

    # Логируем, чтобы понять что реально приходит
    if text:
        print(f"📩 incoming: chat_id={chat_id} text={text}")

    # Проверка на нужный чат
    if chat_id != CHAT_ID:
        return

    if text == "/summary_day":
        o, f = summarize(1)
        await bot.send_message(
            CHAT_ID,
            f"📊 За день:\nONLINE {format_duration(o)}, OFFLINE {format_duration(f)}"
        )

    elif text == "/summary_week":
        o, f = summarize(7)
        await bot.send_message(
            CHAT_ID,
            f"📊 За тиждень:\nONLINE {format_duration(o)}, OFFLINE {format_duration(f)}"
        )


async def webhook_handler(request):
    # Проверяем секрет из заголовка Telegram
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")

    if secret_header != WEBHOOK_SECRET:
        # ВАЖНО: логируем, иначе ты не поймёшь, что это оно
        print("❌ webhook: bad secret header")
        return web.Response(status=403, text="forbidden")

    try:
        update = await request.json()
    except Exception:
        return web.Response(status=400, text="bad json")

    # Быстро отвечаем Telegram, обработку делаем отдельно
    asyncio.create_task(handle_update_safe(update))
    return web.Response(text="ok")


async def start_web_server():
    app = web.Application()
    app.router.add_post("/webhook", webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"✅ Web server слухає порт {PORT}")

    while True:
        await asyncio.sleep(3600)



# ================== MAIN ==================

async def main():
    # 1) Поднимаем веб-сервер
    server_task = asyncio.create_task(start_web_server())

    # 2) Ставим webhook (после старта сервера)
    await asyncio.sleep(1)
    await set_telegram_webhook()

    # 3) Запускаем твои фоновые задачи
    await asyncio.gather(
        monitor(),
        summary_scheduler(),
        server_task
    )

if __name__ == "__main__":
    asyncio.run(main())

