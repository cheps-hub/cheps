import asyncio
import time
import hmac
import hashlib
import json
import os
import httpx
from telegram import Bot
from datetime import datetime, timedelta
from aiohttp import web

# ================== ENV / SETTINGS ==================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set")

CHAT_ID = int(os.getenv("CHAT_ID", "287224456"))

ACCESS_ID = os.getenv("ACCESS_ID", "").strip()
ACCESS_SECRET = os.getenv("ACCESS_SECRET", "").strip()
DEVICE_ID = os.getenv("DEVICE_ID", "").strip()
REGION = os.getenv("REGION", "eu").strip()

PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()
if not PUBLIC_URL:
    raise ValueError("PUBLIC_URL not set")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
if not WEBHOOK_SECRET:
    raise ValueError("WEBHOOK_SECRET not set")

# ru | uk | en
LOCALE = os.getenv("LOCALE", "ru").strip().lower()

PORT = int(os.getenv("PORT", "8080"))

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
DEBOUNCE_INTERVAL = int(os.getenv("DEBOUNCE_INTERVAL", "20"))
MAX_LOG_DAYS = int(os.getenv("MAX_LOG_DAYS", "60"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
LOG_FILE = os.path.join(BASE_DIR, "log.json")

# ====================================================

bot = Bot(token=TELEGRAM_TOKEN)

access_token = None
token_expire_at = 0

last_online_state = None
last_change_time = None
pending_state = None
pending_time = None

START_TS = time.time()


# ================== TIME FORMAT (NO SECONDS) ==================

def _day_suffix() -> str:
    return "days" if LOCALE == "en" else "дн"

def hhmm(seconds: int) -> str:
    minutes = int(seconds) // 60
    h = minutes // 60
    m = minutes % 60
    return f"{h:02}:{m:02}"

def days_hhmm(seconds: int) -> str:
    minutes = int(seconds) // 60
    days = minutes // (24 * 60)
    rest = minutes % (24 * 60)
    h = rest // 60
    m = rest % 60
    if days > 0:
        return f"{days}{_day_suffix()} {h:02}:{m:02}"
    return f"{h:02}:{m:02}"

def ts_hm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

def normalize_cmd(text: str) -> str:
    if not text:
        return ""
    return text.strip().split()[0].split("@")[0].lower()


# ================== TUYA ==================

def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

def sign_request(method: str, url: str, body: str = "", token: str = "") -> dict:
    if not ACCESS_ID or not ACCESS_SECRET:
        raise ValueError("ACCESS_ID/ACCESS_SECRET not set")

    t = str(int(time.time() * 1000))
    body_hash = sha256_hex(body)
    string_to_sign = ACCESS_ID + token + t + method + "\n" + body_hash + "\n\n" + url

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

async def get_device_online_status() -> bool:
    global access_token
    if not DEVICE_ID:
        raise ValueError("DEVICE_ID not set")

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


# ================== STATE ==================

def load_state():
    global last_online_state, last_change_time
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        last_online_state = d.get("online")
        last_change_time = d.get("timestamp")
    except Exception:
        # тихо: production без зайвого шуму
        pass

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"online": last_online_state, "timestamp": last_change_time}, f)
    except Exception:
        pass


# ================== LOG ==================

def _read_log():
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or []
    except Exception:
        return []

def save_log(state: bool, duration: int):
    log = _read_log()
    log.append({
        "timestamp": int(time.time()),
        "state": bool(state),
        "duration": int(duration),
    })

    cutoff = int(time.time()) - MAX_LOG_DAYS * 86400
    log = [x for x in log if int(x.get("timestamp", 0)) >= cutoff]

    try:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(log, f)
    except Exception:
        pass

def summarize_range(start_ts: int, end_ts: int):
    online = 0
    offline = 0
    log = _read_log()

    for e in log:
        ts = int(e.get("timestamp", 0))
        if start_ts <= ts < end_ts:
            if e.get("state"):
                online += int(e.get("duration", 0))
            else:
                offline += int(e.get("duration", 0))

    return online, offline

def summarize(days: int):
    now = int(time.time())
    return summarize_range(now - days * 86400, now)

def prev_month_range(now: datetime):
    first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_prev = first_this - timedelta(days=1)
    first_prev = last_prev.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first_prev, first_this


# ================== MONITOR ==================

async def monitor():
    global last_online_state, last_change_time, pending_state, pending_time
    load_state()

    while True:
        try:
            online = await get_device_online_status()
            now = time.time()

            if last_online_state is None:
                last_online_state = online
                last_change_time = now
                save_state()

            elif online != last_online_state:
                if pending_state != online:
                    pending_state = online
                    pending_time = now

                elif now - pending_time >= DEBOUNCE_INTERVAL:
                    dur = int(now - last_change_time)

                    # Тут ДНІ НЕ потрібні — тільки HH:MM
                    msg = (
                        f"💡 Світло зʼявилось\n🌑 Було без світла: {hhmm(dur)}"
                        if pending_state
                        else
                        f"❌ Світло зникло\n⚡ Було зі світлом: {hhmm(dur)}"
                    )

                    try:
                        await bot.send_message(CHAT_ID, msg)
                    except Exception:
                        pass

                    save_log(last_online_state, dur)

                    last_online_state = pending_state
                    last_change_time = now
                    pending_state = None
                    pending_time = None
                    save_state()

            else:
                pending_state = None
                pending_time = None

        except Exception:
            pass

        await asyncio.sleep(CHECK_INTERVAL)


# ================== AUTO SUMMARY ==================

async def summary_scheduler():
    """
    - Тиждень: кожен понеділок 00:01
    - Місяць: кожне 1-е число 00:01 (попередній календарний місяць)
    """
    while True:
        try:
            now = datetime.now()

            if now.hour == 0 and now.minute == 1:
                # Місяць (1-го числа)
                if now.day == 1:
                    s, e = prev_month_range(now)
                    o, f = summarize_range(int(s.timestamp()), int(e.timestamp()))
                    label = s.strftime("%Y-%m")
                    try:
                        await bot.send_message(
                            CHAT_ID,
                            f"📅 Підсумки за місяць {label}\n🟢 ONLINE {days_hhmm(o)}\n🔴 OFFLINE {days_hhmm(f)}"
                        )
                    except Exception:
                        pass

                # Тиждень (понеділок)
                if now.weekday() == 0:
                    o, f = summarize(7)
                    try:
                        await bot.send_message(
                            CHAT_ID,
                            f"📅 Підсумки за тиждень\n🟢 ONLINE {days_hhmm(o)}\n🔴 OFFLINE {days_hhmm(f)}"
                        )
                    except Exception:
                        pass

                await asyncio.sleep(61)

        except Exception:
            pass

        await asyncio.sleep(30)


# ================== COMMANDS ==================

def help_text() -> str:
    return (
        "ℹ️ Команди:\n"
        "/status\n"
        "/last_change\n"
        "/summary_day\n"
        "/summary_week\n"
        "/summary_month\n"
        "/uptime\n"
        "/help"
    )

async def handle_update(update: dict):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id != CHAT_ID:
        return

    cmd = normalize_cmd(msg.get("text", ""))

    try:
        if cmd == "/help":
            await bot.send_message(CHAT_ID, help_text())

        elif cmd == "/status":
            if last_online_state is None or last_change_time is None:
                await bot.send_message(CHAT_ID, "📡 Поточний статус:\nℹ️ Ще немає даних")
            else:
                state = "ONLINE ⚡" if last_online_state else "OFFLINE 🌑"
                dur = hhmm(int(time.time() - last_change_time))
                await bot.send_message(
                    CHAT_ID,
                    f"📡 Поточний статус:\n{state}\n⏱ У цьому стані: {dur}"
                )

        elif cmd == "/last_change":
            if last_online_state is None or last_change_time is None:
                await bot.send_message(CHAT_ID, "🕒 Остання зміна:\nℹ️ Ще немає даних")
            else:
                state = "ONLINE ⚡" if last_online_state else "OFFLINE 🌑"
                await bot.send_message(
                    CHAT_ID,
                    f"🕒 Остання зміна:\n{state}\n{ts_hm(last_change_time)}"
                )

        elif cmd == "/uptime":
            await bot.send_message(CHAT_ID, f"⏳ Uptime: {hhmm(int(time.time() - START_TS))}")

        elif cmd == "/summary_day":
            o, f = summarize(1)
            # День — без днів
            await bot.send_message(
                CHAT_ID,
                f"📊 За день:\n🟢 ONLINE {hhmm(o)}\n🔴 OFFLINE {hhmm(f)}"
            )

        elif cmd == "/summary_week":
            o, f = summarize(7)
            # Тиждень — з днями (0дн не показуємо)
            await bot.send_message(
                CHAT_ID,
                f"📊 За тиждень:\n🟢 ONLINE {days_hhmm(o)}\n🔴 OFFLINE {days_hhmm(f)}"
            )

        elif cmd == "/summary_month":
            s, e = prev_month_range(datetime.now())
            o, f = summarize_range(int(s.timestamp()), int(e.timestamp()))
            label = s.strftime("%Y-%m")
            # Місяць — з днями (0дн не показуємо)
            await bot.send_message(
                CHAT_ID,
                f"📊 За місяць {label}:\n🟢 ONLINE {days_hhmm(o)}\n🔴 OFFLINE {days_hhmm(f)}"
            )

    except Exception:
        pass


# ================== WEBHOOK ==================

async def webhook_handler(request: web.Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return web.Response(status=403, text="forbidden")

    try:
        update = await request.json()
    except Exception:
        return web.Response(status=400, text="bad json")

    asyncio.create_task(handle_update(update))
    return web.Response(text="ok")

async def set_webhook():
    url = f"{PUBLIC_URL.rstrip('/')}/webhook"
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    payload = {"url": url, "secret_token": WEBHOOK_SECRET, "drop_pending_updates": True}

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(api, json=payload)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"setWebhook failed: {data}")

async def start_server():
    app = web.Application()
    app.router.add_post("/webhook", webhook_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()


# ================== MAIN ==================

async def main():
    await start_server()
    await set_webhook()
    await asyncio.gather(
        monitor(),
        summary_scheduler(),
    )

if __name__ == "__main__":
    asyncio.run(main())
