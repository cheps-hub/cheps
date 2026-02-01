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

# ================== НАСТРОЙКИ (Variables) ==================

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
    raise ValueError("PUBLIC_URL not set (e.g. https://xxxxx.up.railway.app)")

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

# ============================================================

bot = Bot(token=TELEGRAM_TOKEN)

access_token = None
token_expire_at = 0

last_online_state = None
last_change_time = None
pending_state = None
pending_time = None

START_TS = time.time()

# ================== Форматы времени (без секунд) ==================

def _day_suffix() -> str:
    # просили именно: "дн" и "days"
    return "days" if LOCALE == "en" else "дн"

def format_hhmm(seconds: int) -> str:
    """Всегда HH:MM (без дней, без секунд)."""
    minutes = int(seconds) // 60
    h = minutes // 60
    m = minutes % 60
    return f"{h:02}:{m:02}"

def format_days_hhmm(seconds: int) -> str:
    """
    Для недели/месяца:
    - если days == 0 -> HH:MM
    - если days > 0  -> 'Xd <дн/days> HH:MM'
    """
    minutes = int(seconds) // 60
    days = minutes // (24 * 60)
    minutes_left = minutes % (24 * 60)
    h = minutes_left // 60
    m = minutes_left % 60

    if days > 0:
        return f"{days}{_day_suffix()} {h:02}:{m:02}"
    return f"{h:02}:{m:02}"

def ts_to_str(ts: float) -> str:
    """Дата-время без секунд."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

def normalize_cmd(text: str) -> str:
    """
    /cmd, /cmd@botname, /cmd extra -> "/cmd"
    """
    if not text:
        return ""
    return text.strip().split()[0].split("@")[0].lower()

# ================== TUYA ==================

def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

def sign_request(method: str, url: str, body: str = "", token: str = "") -> dict:
    # если Tuya ключи не заданы — лучше упасть сразу
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

# ================== STATE & LOG ==================

def load_state():
    global last_online_state, last_change_time
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        last_online_state = d.get("online")
        last_change_time = d.get("timestamp")
    except Exception as e:
        print("ERROR load_state:", e)

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"online": last_online_state, "timestamp": last_change_time}, f)
    except Exception as e:
        print("ERROR save_state:", e)

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
        "duration": int(duration)
    })

    cutoff = int(time.time()) - MAX_LOG_DAYS * 86400
    log = [x for x in log if int(x.get("timestamp", 0)) >= cutoff]

    try:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(log, f)
    except Exception as e:
        print("ERROR save_log:", e)

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
    """
    Прошлый календарный месяц:
    start = 1-е число прошлого месяца 00:00
    end   = 1-е число текущего месяца 00:00
    """
    first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_prev = first_this - timedelta(days=1)
    first_prev = last_prev.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first_prev, first_this

# ================== MONITOR ==================

async def monitor():
    global last_online_state, last_change_time, pending_state, pending_time
    load_state()
    print("🤖 monitor started")

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

                    # тут ДНИ НЕ НУЖНЫ — только HH:MM
                    msg = (
                        f"💡 Світло зʼявилось\n🌑 Було: {format_hhmm(duration)}"
                        if pending_state
                        else
                        f"❌ Світло зникло\n⚡ Було: {format_hhmm(duration)}"
                    )

                    try:
                        await bot.send_message(CHAT_ID, msg)
                    except Exception as e:
                        print("send light-change error:", e)

                    save_log(last_online_state, duration)

                    last_online_state = pending_state
                    last_change_time = now
                    pending_state = None
                    pending_time = None
                    save_state()

            # лог статуса в консоль можно оставить
            print(f"{datetime.now().strftime('%H:%M')} online = {is_online}")

        except Exception as e:
            print("ERROR monitor:", e)

        await asyncio.sleep(CHECK_INTERVAL)

# ================== AUTO SUMMARY ==================

async def summary_scheduler():
    """
    - Неделя: каждый понедельник в 00:01
    - Месяц: каждое 1-е число в 00:01 (прошлый календарный месяц)
    """
    while True:
        try:
            now = datetime.now()

            if now.hour == 0 and now.minute == 1:
                # Месяц — 1-го числа
                if now.day == 1:
                    s, e = prev_month_range(now)
                    o, f = summarize_range(int(s.timestamp()), int(e.timestamp()))
                    label = s.strftime("%Y-%m")
                    await bot.send_message(
                        CHAT_ID,
                        f"📊 Місяць {label}\nONLINE {format_days_hhmm(o)} / OFFLINE {format_days_hhmm(f)}"
                    )

                # Неделя — понедельник
                if now.weekday() == 0:
                    o, f = summarize(7)
                    await bot.send_message(
                        CHAT_ID,
                        f"📊 Тиждень\nONLINE {format_days_hhmm(o)} / OFFLINE {format_days_hhmm(f)}"
                    )

                await asyncio.sleep(61)

        except Exception as e:
            print("ERROR summary_scheduler:", e)

        await asyncio.sleep(30)

# ================== COMMANDS ==================

def help_text() -> str:
    return (
        "/status\n"
        "/uptime\n"
        "/last_change\n"
        "/summary_day\n"
        "/summary_week\n"
        "/summary_month\n"
        "/help"
    )

async def handle_update(update: dict):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat_id = (msg.get("chat") or {}).get("id")
    if chat_id != CHAT_ID:
        return

    cmd = normalize_cmd(msg.get("text", ""))

    try:
        if cmd == "/help":
            await bot.send_message(CHAT_ID, help_text())

        elif cmd == "/status":
            if last_change_time is None or last_online_state is None:
                await bot.send_message(CHAT_ID, "Статус ще невідомий")
            else:
                d = format_hhmm(int(time.time() - last_change_time))  # без дней
                s = "ONLINE ⚡" if last_online_state else "OFFLINE 🌑"
                await bot.send_message(CHAT_ID, f"{s}\nУ цьому стані: {d}")

        elif cmd == "/uptime":
            await bot.send_message(CHAT_ID, f"Uptime: {format_hhmm(int(time.time() - START_TS))}")

        elif cmd == "/last_change":
            if last_change_time:
                await bot.send_message(CHAT_ID, f"Остання зміна:\n{ts_to_str(last_change_time)}")

        elif cmd == "/summary_day":
            o, f = summarize(1)
            # день — БЕЗ дней
            await bot.send_message(
                CHAT_ID,
                f"День\nONLINE {format_hhmm(o)} / OFFLINE {format_hhmm(f)}"
            )

        elif cmd == "/summary_week":
            o, f = summarize(7)
            # неделя — С днями (но 0д скрываем)
            await bot.send_message(
                CHAT_ID,
                f"Тиждень\nONLINE {format_days_hhmm(o)} / OFFLINE {format_days_hhmm(f)}"
            )

        elif cmd == "/summary_month":
            s, e = prev_month_range(datetime.now())
            o, f = summarize_range(int(s.timestamp()), int(e.timestamp()))
            label = s.strftime("%Y-%m")
            # месяц — С днями (но 0д скрываем)
            await bot.send_message(
                CHAT_ID,
                f"Місяць {label}\nONLINE {format_days_hhmm(o)} / OFFLINE {format_days_hhmm(f)}"
            )

    except Exception as e:
        print("ERROR handle_update:", e)

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
    print(f"✅ web server on {PORT}")

# ================== MAIN ==================

async def main():
    print(f"✅ START chat_id={CHAT_ID}, locale={LOCALE}")
    await start_server()
    await set_webhook()
    await asyncio.gather(
        monitor(),
        summary_scheduler()
    )

if __name__ == "__main__":
    asyncio.run(main())
