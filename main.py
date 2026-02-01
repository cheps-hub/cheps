import asyncio
import time
import hmac
import hashlib
import json
import os
import httpx
from telegram import Bot
from datetime import datetime, timedelta

# ================== НАЛАШТУВАННЯ ==================
TELEGRAM_TOKEN = "8548566635:AAGqg3gmUtUo8JwRMDgQQ6ODo9_cSXKZQ5g"
CHAT_ID = 287224456
ACCESS_ID = "9gecmcdum9rj8q7uymgc"
ACCESS_SECRET = "058a6a9bbe7d4beb800e65500822f413"
DEVICE_ID = "bfa197db4a74f16983d2ru"
REGION = "eu"

CHECK_INTERVAL = 60       # секунд між перевірками
DEBOUNCE_INTERVAL = 20    # секунд стабільності зміни стану
MAX_LOG_DAYS = 60         # зберігати лог останніх 60 днів

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
LOG_FILE = os.path.join(BASE_DIR, "log.json")

# ==================================================
bot = Bot(token=TELEGRAM_TOKEN)
access_token = None
token_expire_at = 0

last_online_state = None
last_change_time = None  # timestamp
pending_state = None
pending_time = None

# ================== HELPERS ==================
def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

def format_duration(seconds: int) -> str:
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
        ACCESS_SECRET.encode(), string_to_sign.encode(), hashlib.sha256
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
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            last_online_state = data.get("online")
            last_change_time = data.get("timestamp")
            print(f"✅ Завантажено стан: online={last_online_state}")
    except Exception as e:
        print(f"ERROR при читанні {STATE_FILE}:", e)

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "online": last_online_state,
                "timestamp": last_change_time
            }, f)
    except Exception as e:
        print(f"ERROR при записі {STATE_FILE}:", e)

# ================== LOG ==================
def save_log(state, duration):
    log = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r") as f:
                log = json.load(f)
        except:
            pass
    log.append({
        "timestamp": int(time.time()),
        "state": state,
        "duration": duration
    })

    # --- очищення старих записів ---
    cutoff = int(time.time()) - MAX_LOG_DAYS * 24 * 3600
    log = [entry for entry in log if entry["timestamp"] >= cutoff]

    with open(LOG_FILE, "w") as f:
        json.dump(log, f)

def summarize(period_days):
    now = int(time.time())
    start = now - period_days * 24 * 3600
    online_sec = 0
    offline_sec = 0
    if not os.path.exists(LOG_FILE):
        return online_sec, offline_sec
    with open(LOG_FILE, "r") as f:
        log = json.load(f)
    for entry in log:
        if entry["timestamp"] >= start:
            if entry["state"]:
                online_sec += entry["duration"]
            else:
                offline_sec += entry["duration"]
    return online_sec, offline_sec

# ================== TUYA ==================
async def get_access_token():
    global access_token, token_expire_at
    url = "/v1.0/token?grant_type=1"
    headers = sign_request("GET", url)
    async with httpx.AsyncClient(
        base_url=f"https://openapi.tuya{REGION}.com", timeout=10
    ) as client:
        r = await client.get(url, headers=headers)
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"Tuya token error: {data}")
        access_token = data["result"]["access_token"]
        token_expire_at = time.time() + data["result"]["expire_time"] - 60
        print("✅ Access token отримано")

async def get_device_online_status() -> bool:
    global access_token
    if not access_token or time.time() > token_expire_at:
        await get_access_token()
    url = f"/v1.0/devices/{DEVICE_ID}"
    headers = sign_request("GET", url, token=access_token)
    async with httpx.AsyncClient(
        base_url=f"https://openapi.tuya{REGION}.com", timeout=10
    ) as client:
        r = await client.get(url, headers=headers)
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"Tuya device error: {data}")
        return data["result"]["online"]

# ================== MONITOR ==================
async def monitor():
    global last_online_state, last_change_time, pending_state, pending_time
    load_state()
    print("🤖 Моніторинг ONLINE / OFFLINE запущено")

    if last_online_state is not None:
        duration = 0
        if last_change_time is not None:
            duration = int(time.time() - last_change_time)
        duration_str = format_duration(duration)
        if last_online_state:
            await bot.send_message(
                CHAT_ID,
                f"💡 Світло вже увімкнено\n⏱ Час світла: {duration_str}"
            )
        else:
            await bot.send_message(
                CHAT_ID,
                f"❌ Світло вимкнено\n⏱ Проміжок темряви: {duration_str}"
            )

    while True:
        try:
            is_online = await get_device_online_status()
            now = time.time()

            # --- дебаунс ---
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
                    duration_str = format_duration(duration)
                    if pending_state:
                        await bot.send_message(
                            CHAT_ID,
                            f"💡 Світло з'явилося\n🌑 Проміжок темряви: {duration_str}"
                        )
                    else:
                        await bot.send_message(
                            CHAT_ID,
                            f"❌ Світло зникло\n⏱ Час світла: {duration_str}"
                        )
                    save_log(last_online_state, duration)
                    last_online_state = pending_state
                    last_change_time = now
                    pending_state = None
                    pending_time = None
                    save_state()
            else:
                pending_state = None
                pending_time = None

            log_time = time.strftime("%H:%M:%S")
            print(f"{log_time} online = {is_online}")

        except Exception as e:
            print("ERROR:", e)

        await asyncio.sleep(CHECK_INTERVAL)

# ================== SUMMARY SCHEDULER ==================
async def summary_scheduler():
    while True:
        now = datetime.now()
        # День о 00:01
        if now.hour == 0 and now.minute == 1:
            online, offline = summarize(1)
            await bot.send_message(
                CHAT_ID,
                f"📊 Підсумки за день:\nONLINE {format_duration(online)}, OFFLINE {format_duration(offline)}"
            )
            await asyncio.sleep(61)
            continue
        # Тиждень щопонеділка о 00:01
        if now.weekday() == 0 and now.hour == 0 and now.minute == 1:
            online, offline = summarize(7)
            await bot.send_message(
                CHAT_ID,
                f"📊 Підсумки за тиждень:\nONLINE {format_duration(online)}, OFFLINE {format_duration(offline)}"
            )
            await asyncio.sleep(61)
            continue
        # Місяць 1-го числа о 00:01
        if now.day == 1 and now.hour == 0 and now.minute == 1:
            online, offline = summarize(30)
            await bot.send_message(
                CHAT_ID,
                f"📊 Підсумки за місяць:\nONLINE {format_duration(online)}, OFFLINE {format_duration(offline)}"
            )
            await asyncio.sleep(61)
            continue

        await asyncio.sleep(30)

# ================== TELEGRAM COMMANDS ==================
async def telegram_commands():
    offset = None
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=10)
            for update in updates:
                offset = update.update_id + 1
                if not update.message:
                    continue
                text = update.message.text
                chat_id = update.message.chat.id
                if chat_id != CHAT_ID:
                    continue  # відповідаємо лише у потрібний чат

                if text == "/summary_day":
                    online, offline = summarize(1)
                    await bot.send_message(
                        CHAT_ID,
                        f"📊 Ручні підсумки за день:\nONLINE {format_duration(online)}, OFFLINE {format_duration(offline)}"
                    )
                elif text == "/summary_week":
                    online, offline = summarize(7)
                    await bot.send_message(
                        CHAT_ID,
                        f"📊 Ручні підсумки за тиждень:\nONLINE {format_duration(online)}, OFFLINE {format_duration(offline)}"
                    )
                elif text == "/summary_month":
                    online, offline = summarize(30)
                    await bot.send_message(
                        CHAT_ID,
                        f"📊 Ручні підсумки за місяць:\nONLINE {format_duration(online)}, OFFLINE {format_duration(offline)}"
                    )

        except Exception as e:
            print("ERROR in telegram_commands:", e)
        await asyncio.sleep(1)

# ================== MAIN ==================
async def main():
    await asyncio.gather(
        monitor(),
        summary_scheduler(),
        telegram_commands()
    )

if __name__ == "__main__":
    asyncio.run(main())

