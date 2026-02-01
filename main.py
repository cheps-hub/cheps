import asyncio
import time
import hmac
import hashlib
import json
import os
import httpx
from telegram import Bot
from datetime import datetime

# ================== НАЛАШТУВАННЯ ==================

TELEGRAM_TOKEN = "8548566635:AAHp5kldVqeVkfzm-V09diSgrNBVtkMQVKc"   # <-- СЮДА ВСТАВИШ TOKEN

CHAT_ID = 287224456

ACCESS_ID = "9gecmcdum9rj8q7uymgc"
ACCESS_SECRET = "058a6a9bbe7d4beb800e65500822f413"
DEVICE_ID = "bfa197db4a74f16983d2ru"
REGION = "eu"

CHECK_INTERVAL = 60        # секунд між перевірками
DEBOUNCE_INTERVAL = 20     # секунд стабільності
MAX_LOG_DAYS = 60          # зберігати лог 60 днів

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
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            last_online_state = data.get("online")
            last_change_time = data.get("timestamp")
            print(f"✅ Стан відновлено: online={last_online_state}")
    except Exception as e:
        print("ERROR load_state:", e)

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
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
            with open(LOG_FILE, "r") as f:
                log = json.load(f)
        except:
            log = []

    log.append({
        "timestamp": int(time.time()),
        "state": state,
        "duration": duration
    })

    cutoff = int(time.time()) - MAX_LOG_DAYS * 24 * 3600
    log = [x for x in log if x["timestamp"] >= cutoff]

    with open(LOG_FILE, "w") as f:
        json.dump(log, f)

def summarize(days):
    now = int(time.time())
    start = now - days * 24 * 3600
    online = 0
    offline = 0

    if not os.path.exists(LOG_FILE):
        return online, offline

    with open(LOG_FILE, "r") as f:
        log = json.load(f)

    for entry in log:
        if entry["timestamp"] >= start:
            if entry["state"]:
                online += entry["duration"]
            else:
                offline += entry["duration"]

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
        print("✅ Access token отримано")

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
        return data["result"]["online"]


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
    while True:
        try:
            now = datetime.now()

            if now.hour == 0 and now.minute == 1:
                online, offline = summarize(1)
                await bot.send_message(
                    CHAT_ID,
                    f"📊 Підсумки за день:\nONLINE {format_duration(online)}, OFFLINE {format_duration(offline)}"
                )
                await asyncio.sleep(61)
                continue

            if now.weekday() == 0 and now.hour == 0 and now.minute == 1:
                online, offline = summarize(7)
                await bot.send_message(
                    CHAT_ID,
                    f"📊 Підсумки за тиждень:\nONLINE {format_duration(online)}, OFFLINE {format_duration(offline)}"
                )
                await asyncio.sleep(61)
                continue

        except Exception as e:
            print("ERROR summary:", e)

        await asyncio.sleep(30)


# ================== TELEGRAM COMMANDS ==================

async def telegram_commands():
    offset = None
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=10)
            for u in updates:
                offset = u.update_id + 1
                if not u.message:
                    continue
                if u.message.chat.id != CHAT_ID:
                    continue

                if u.message.text == "/summary_day":
                    o, f = summarize(1)
                    await bot.send_message(
                        CHAT_ID,
                        f"📊 За день:\nONLINE {format_duration(o)}, OFFLINE {format_duration(f)}"
                    )

        except Exception as e:
            print("ERROR telegram:", e)

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
