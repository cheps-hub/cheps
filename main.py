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

# ================== VARIABLES ==================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = int(os.getenv("CHAT_ID", "287224456"))

ACCESS_ID = os.getenv("ACCESS_ID", "")
ACCESS_SECRET = os.getenv("ACCESS_SECRET", "")
DEVICE_ID = os.getenv("DEVICE_ID", "")
REGION = os.getenv("REGION", "eu")

PUBLIC_URL = os.getenv("PUBLIC_URL", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

LOCALE = os.getenv("LOCALE", "ru").lower()   # ru | uk | en
PORT = int(os.getenv("PORT", "8080"))

CHECK_INTERVAL = 60
DEBOUNCE_INTERVAL = 20
MAX_LOG_DAYS = 60

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
LOG_FILE = os.path.join(BASE_DIR, "log.json")

# =================================================

bot = Bot(token=TELEGRAM_TOKEN)

access_token = None
token_expire_at = 0

last_online_state = None
last_change_time = None
pending_state = None
pending_time = None

START_TS = time.time()

# ================== TIME FORMAT ==================

def day_suffix():
    return "days" if LOCALE == "en" else "дн"

def hhmm(seconds: int) -> str:
    minutes = seconds // 60
    h = minutes // 60
    m = minutes % 60
    return f"{h:02}:{m:02}"

def days_hhmm(seconds: int) -> str:
    minutes = seconds // 60
    days = minutes // (24 * 60)
    rest = minutes % (24 * 60)
    h = rest // 60
    m = rest % 60
    if days > 0:
        return f"{days}{day_suffix()} {h:02}:{m:02}"
    return f"{h:02}:{m:02}"

def ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

def normalize_cmd(text: str) -> str:
    if not text:
        return ""
    return text.strip().split()[0].split("@")[0].lower()

# ================== TUYA ==================

def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

def sign_request(method, url, body="", token=""):
    t = str(int(time.time() * 1000))
    body_hash = sha256_hex(body)
    string = ACCESS_ID + token + t + method + "\n" + body_hash + "\n\n" + url
    sign = hmac.new(ACCESS_SECRET.encode(), string.encode(), hashlib.sha256).hexdigest().upper()
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
    async with httpx.AsyncClient(base_url=f"https://openapi.tuya{REGION}.com") as c:
        r = await c.get("/v1.0/token?grant_type=1", headers=sign_request("GET", "/v1.0/token?grant_type=1"))
        data = r.json()
        access_token = data["result"]["access_token"]
        token_expire_at = time.time() + data["result"]["expire_time"] - 60

async def get_device_online_status():
    global access_token
    if not access_token or time.time() > token_expire_at:
        await get_access_token()
    async with httpx.AsyncClient(base_url=f"https://openapi.tuya{REGION}.com") as c:
        r = await c.get(
            f"/v1.0/devices/{DEVICE_ID}",
            headers=sign_request("GET", f"/v1.0/devices/{DEVICE_ID}", token=access_token)
        )
        return bool(r.json()["result"]["online"])

# ================== STATE & LOG ==================

def load_state():
    global last_online_state, last_change_time
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            d = json.load(f)
            last_online_state = d["online"]
            last_change_time = d["timestamp"]

def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({"online": last_online_state, "timestamp": last_change_time}, f)

def read_log():
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE) as f:
        return json.load(f)

def save_log(state, duration):
    log = read_log()
    log.append({"timestamp": int(time.time()), "state": state, "duration": duration})
    cutoff = int(time.time()) - MAX_LOG_DAYS * 86400
    log = [x for x in log if x["timestamp"] >= cutoff]
    with open(LOG_FILE, "w") as f:
        json.dump(log, f)

def summarize_range(start, end):
    o = f = 0
    for e in read_log():
        if start <= e["timestamp"] < end:
            (o if e["state"] else f) += e["duration"]
    return o, f

def summarize(days):
    now = int(time.time())
    return summarize_range(now - days * 86400, now)

def prev_month(now):
    first = now.replace(day=1, hour=0, minute=0, second=0)
    last = first - timedelta(days=1)
    return last.replace(day=1, hour=0, minute=0), first

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
                    msg = (
                        f"💡 Світло зʼявилось\n🌑 Було без світла: {hhmm(dur)}"
                        if pending_state
                        else
                        f"❌ Світло зникло\n⚡ Було зі світлом: {hhmm(dur)}"
                    )
                    await bot.send_message(CHAT_ID, msg)
                    save_log(last_online_state, dur)
                    last_online_state = pending_state
                    last_change_time = now
                    pending_state = None
                    pending_time = None
                    save_state()
        except:
            pass
        await asyncio.sleep(CHECK_INTERVAL)

# ================== AUTO SUMMARY ==================

async def summary_scheduler():
    while True:
        now = datetime.now()
        if now.hour == 0 and now.minute == 1:
            if now.day == 1:
                s, e = prev_month(now)
                o, f = summarize_range(int(s.timestamp()), int(e.timestamp()))
                await bot.send_message(
                    CHAT_ID,
                    f"📅 Підсумки за місяць {s.strftime('%Y-%m')}\n"
                    f"🟢 ONLINE {days_hhmm(o)}\n"
                    f"🔴 OFFLINE {days_hhmm(f)}"
                )
            if now.weekday() == 0:
                o, f = summarize(7)
                await bot.send_message(
                    CHAT_ID,
                    f"📅 Підсумки за тиждень\n"
                    f"🟢 ONLINE {days_hhmm(o)}\n"
                    f"🔴 OFFLINE {days_hhmm(f)}"
                )
            await asyncio.sleep(61)
        await asyncio.sleep(30)

# ================== COMMANDS ==================

async def handle_update(update):
    msg = update.get("message") or update.get("edited_message")
    if not msg or msg["chat"]["id"] != CHAT_ID:
        return

    cmd = normalize_cmd(msg.get("text", ""))

    if cmd == "/status":
        state = "ONLINE ⚡" if last_online_state else "OFFLINE 🌑"
        dur = hhmm(int(time.time() - last_change_time))
        await bot.send_message(
            CHAT_ID,
            f"📡 Поточний статус:\n{state}\n⏱ У цьому стані: {dur}"
        )

    elif cmd == "/last_change":
        await bot.send_message(
            CHAT_ID,
            f"🕒 Остання зміна:\n"
            f"{'ONLINE ⚡' if last_online_state else 'OFFLINE 🌑'}\n"
            f"{ts(last_change_time)}"
        )

    elif cmd == "/summary_day":
        o, f = summarize(1)
        await bot.send_message(
            CHAT_ID,
            f"📊 За день:\n🟢 ONLINE {hhmm(o)}\n🔴 OFFLINE {hhmm(f)}"
        )

    elif cmd == "/summary_week":
        o, f = summarize(7)
        await bot.send_message(
            CHAT_ID,
            f"📊 За тиждень:\n🟢 ONLINE {days_hhmm(o)}\n🔴 OFFLINE {days_hhmm(f)}"
        )

    elif cmd == "/summary_month":
        s, e = prev_month(datetime.now())
        o, f = summarize_range(int(s.timestamp()), int(e.timestamp()))
        await bot.send_message(
            CHAT_ID,
            f"📊 За місяць {s.strftime('%Y-%m')}:\n"
            f"🟢 ONLINE {days_hhmm(o)}\n"
            f"🔴 OFFLINE {days_hhmm(f)}"
        )

    elif cmd == "/uptime":
        await bot.send_message(CHAT_ID, f"⏳ Uptime: {hhmm(int(time.time() - START_TS))}")

    elif cmd == "/help":
        await bot.send_message(
            CHAT_ID,
            "ℹ️ Команди:\n"
            "/status\n"
            "/last_change\n"
            "/summary_day\n"
            "/summary_week\n"
            "/summary_month\n"
            "/uptime"
        )

# ================== WEBHOOK ==================

async def webhook_handler(request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return web.Response(status=403)
    update = await request.json()
    asyncio.create_task(handle_update(update))
    return web.Response(text="ok")

async def set_webhook():
    async with httpx.AsyncClient() as c:
        await c.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
            json={
                "url": f"{PUBLIC_URL}/webhook",
                "secret_token": WEBHOOK_SECRET,
                "drop_pending_updates": True
            }
        )

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
        summary_scheduler()
    )

if __name__ == "__main__":
    asyncio.run(main())
