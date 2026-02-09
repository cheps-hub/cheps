import asyncio
import time
import hmac
import hashlib
import json
import os
import httpx
from telegram import Bot
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "10"))
DEBOUNCE_INTERVAL = int(os.getenv("DEBOUNCE_INTERVAL", "20"))
MAX_LOG_DAYS = int(os.getenv("MAX_LOG_DAYS", "60"))

# OFFLINE = немає світла (після таймауту)
OFFLINE_TIMEOUT = int(os.getenv("OFFLINE_TIMEOUT", "30"))  # секунд

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
LOG_FILE = os.path.join(BASE_DIR, "log.json")

# ====================================================

KYIV_TZ = ZoneInfo("Europe/Kyiv")

bot = Bot(token=TELEGRAM_TOKEN)

access_token = None
token_expire_at = 0

last_online_state = None      # True=Світло, False=Темрява (останній відомий/прийнятий стан)
last_change_time = None       # epoch seconds: час ОСТАННЬОЇ РЕАЛЬНОЇ (або примусової OFFLINE) зміни (для /status)
segment_start_time = None     # epoch seconds: старт поточного "сегмента" для логів/звітів

pending_state = None
pending_time = None

# Tuya reachability tracking
tuya_online = True            # чи Tuya відповідає зараз
offline_since = None          # epoch seconds: коли вперше помітили недоступність
last_seen = None              # epoch seconds: коли востаннє Tuya точно відповідала

# scheduler guards (YYYY-MM-DD)
last_rollover_date = None
last_daily_summary_date = None
last_weekly_summary_date = None
last_monthly_summary_date = None

START_TS = time.time()

STATE_LOCK = asyncio.Lock()

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

def ymd(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")

# ================== CALENDAR RANGES (KYIV) ==================

def start_of_day_kyiv(dt: datetime) -> datetime:
    dt = dt.astimezone(KYIV_TZ)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)

def start_of_week_kyiv(dt: datetime) -> datetime:
    d0 = start_of_day_kyiv(dt)
    return d0 - timedelta(days=d0.weekday())  # Monday 00:00

def start_of_month_kyiv(dt: datetime) -> datetime:
    d0 = start_of_day_kyiv(dt)
    return d0.replace(day=1)

def prev_day_range_kyiv(now: datetime) -> tuple[int, int]:
    end_dt = start_of_day_kyiv(now)          # today 00:00
    start_dt = end_dt - timedelta(days=1)    # yesterday 00:00
    return int(start_dt.timestamp()), int(end_dt.timestamp())

def prev_week_range_kyiv(now: datetime) -> tuple[int, int]:
    end_dt = start_of_week_kyiv(now)         # this Monday 00:00
    start_dt = end_dt - timedelta(days=7)    # prev Monday 00:00
    return int(start_dt.timestamp()), int(end_dt.timestamp())

def prev_month_range_kyiv(now: datetime) -> tuple[int, int]:
    end_dt = start_of_month_kyiv(now)        # first day of this month 00:00
    prev_last_day = end_dt - timedelta(days=1)
    start_dt = start_of_month_kyiv(prev_last_day)
    return int(start_dt.timestamp()), int(end_dt.timestamp())

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
    """
    Повертає поле result.online від Tuya.
    У цій системі: online=True => Світло; online=False => Темрява.
    Якщо запит падає — викликає Exception (ми обробляємо вище як OFFLINE).
    """
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
    global last_online_state, last_change_time, segment_start_time
    global last_rollover_date, last_daily_summary_date, last_weekly_summary_date, last_monthly_summary_date
    global tuya_online, offline_since, last_seen

    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)

        last_online_state = d.get("online")
        last_change_time = d.get("timestamp")
        segment_start_time = d.get("segment_start_time")

        # new fields
        tuya_online = d.get("tuya_online", True)
        offline_since = d.get("offline_since")
        last_seen = d.get("last_seen")

        last_rollover_date = d.get("last_rollover_date")
        last_daily_summary_date = d.get("last_daily_summary_date")
        last_weekly_summary_date = d.get("last_weekly_summary_date")
        last_monthly_summary_date = d.get("last_monthly_summary_date")
    except Exception:
        pass

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "online": last_online_state,
                    "timestamp": last_change_time,
                    "segment_start_time": segment_start_time,

                    "tuya_online": tuya_online,
                    "offline_since": offline_since,
                    "last_seen": last_seen,

                    "last_rollover_date": last_rollover_date,
                    "last_daily_summary_date": last_daily_summary_date,
                    "last_weekly_summary_date": last_weekly_summary_date,
                    "last_monthly_summary_date": last_monthly_summary_date,
                },
                f
            )
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

def save_log(state: bool, duration: int, end_ts: int | None = None):
    log = _read_log()
    log.append({
        "timestamp": int(end_ts if end_ts is not None else time.time()),  # момент завершення сегмента
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
    light = 0
    dark = 0
    log = _read_log()

    for e in log:
        ts = int(e.get("timestamp", 0))
        if start_ts <= ts < end_ts:
            if e.get("state"):
                light += int(e.get("duration", 0))
            else:
                dark += int(e.get("duration", 0))

    return light, dark

# ================== TEXT HELPERS ==================

def state_line(is_light: bool) -> str:
    return "Світло 💡" if is_light else "Темрява 🌑"

# ================== MONITOR ==================

async def monitor():
    global last_online_state, last_change_time, segment_start_time, pending_state, pending_time
    global tuya_online, offline_since, last_seen

    load_state()

    while True:
        now_ts = time.time()
        tuya_ok = False
        is_light = None  # type: ignore

        try:
            is_light = await get_device_online_status()
            tuya_ok = True
        except Exception:
            tuya_ok = False

        async with STATE_LOCK:
            # init if needed (без зміни логіки)
            if last_online_state is None or last_change_time is None:
                # якщо Tuya не відповідає з першого старту — вважаємо "немає світла" після таймауту,
                # але стартовий стан ставимо обережно як False (бо тобі треба OFFLINE=немає світла)
                last_online_state = bool(is_light) if tuya_ok else False
                last_change_time = now_ts
                segment_start_time = now_ts
                tuya_online = tuya_ok
                last_seen = now_ts if tuya_ok else last_seen
                offline_since = None if tuya_ok else (offline_since or now_ts)
                save_state()
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            # update tuya reachability
            if tuya_ok:
                tuya_online = True
                last_seen = now_ts
                offline_since = None
            else:
                tuya_online = False
                if offline_since is None:
                    offline_since = now_ts

            # effective state rule: OFFLINE >= timeout => "немає світла" (False)
            forced_offline = False
            effective_is_light = None

            if tuya_ok:
                effective_is_light = bool(is_light)
            else:
                # ще чекаємо timeout, щоб не спамити при коротких збоях
                offline_age = now_ts - float(offline_since or now_ts)
                if offline_age >= OFFLINE_TIMEOUT:
                    forced_offline = True
                    effective_is_light = False
                else:
                    # до таймауту не змінюємо стан, просто чекаємо
                    effective_is_light = last_online_state

            # якщо спрацював OFFLINE timeout і ми були в "Світло" — робимо миттєве переключення
            if forced_offline and last_online_state is True:
                # завершився попередній стан
                dur_for_message = int(now_ts - last_change_time)
                msg = f"❌ Світло зникло\n💡 Час світла: {hhmm(dur_for_message)}"

                try:
                    await bot.send_message(CHAT_ID, msg)
                except Exception:
                    pass

                if segment_start_time is None:
                    segment_start_time = last_change_time

                dur_for_log = int(now_ts - segment_start_time)
                if dur_for_log > 0:
                    save_log(last_online_state, dur_for_log, end_ts=int(now_ts))

                last_online_state = False
                last_change_time = now_ts
                segment_start_time = now_ts

                pending_state = None
                pending_time = None
                save_state()

                await asyncio.sleep(CHECK_INTERVAL)
                continue

            # звичайна логіка змін (debounce)
            if effective_is_light != last_online_state:
                if pending_state != effective_is_light:
                    pending_state = effective_is_light
                    pending_time = now_ts

                elif pending_time is not None and now_ts - pending_time >= DEBOUNCE_INTERVAL:
                    dur_for_message = int(now_ts - last_change_time)

                    msg = (
                        f"💡 Світло зʼявилось\n🌑 Темрява була: {hhmm(dur_for_message)}"
                        if pending_state
                        else
                        f"❌ Світло зникло\n💡 Час світла: {hhmm(dur_for_message)}"
                    )

                    try:
                        await bot.send_message(CHAT_ID, msg)
                    except Exception:
                        pass

                    if segment_start_time is None:
                        segment_start_time = last_change_time

                    dur_for_log = int(now_ts - segment_start_time)
                    if dur_for_log > 0:
                        save_log(last_online_state, dur_for_log, end_ts=int(now_ts))

                    last_online_state = pending_state
                    last_change_time = now_ts
                    segment_start_time = now_ts

                    pending_state = None
                    pending_time = None
                    save_state()

            else:
                pending_state = None
                pending_time = None

            save_state()

        await asyncio.sleep(CHECK_INTERVAL)

# ================== DAILY ROLLOVER (00:01) ==================

async def daily_rollover_if_needed(now: datetime):
    """
    О 00:01–00:04 (Kyiv):
    - робимо запит статуса (або OFFLINE-logic)
    - ДОПИСУЄМО сегмент у лог
    - СТАВИМО segment_start_time = now
    - НЕ чіпаємо last_change_time, якщо стан не змінився
    """
    global last_online_state, last_change_time, segment_start_time, last_rollover_date
    global tuya_online, offline_since, last_seen

    today = ymd(now)
    in_window = (now.hour == 0 and 1 <= now.minute <= 4)
    if not in_window:
        return

    async with STATE_LOCK:
        if last_rollover_date == today:
            return

    now_ts = time.time()

    # беремо "effective" статус з OFFLINE-логікою
    tuya_ok = False
    current_is_light = None  # type: ignore
    try:
        current_is_light = await get_device_online_status()
        tuya_ok = True
    except Exception:
        tuya_ok = False

    async with STATE_LOCK:
        # init
        if last_online_state is None or last_change_time is None:
            last_online_state = bool(current_is_light) if tuya_ok else False
            last_change_time = now_ts
            segment_start_time = now_ts
            last_rollover_date = today

            tuya_online = tuya_ok
            last_seen = now_ts if tuya_ok else last_seen
            offline_since = None if tuya_ok else (offline_since or now_ts)

            save_state()
            return

        # update reachability tracking (persist for /status)
        if tuya_ok:
            tuya_online = True
            last_seen = now_ts
            offline_since = None
            effective = bool(current_is_light)
        else:
            tuya_online = False
            if offline_since is None:
                offline_since = now_ts
            offline_age = now_ts - float(offline_since or now_ts)
            # якщо вже довго OFFLINE — вважаємо "немає світла", інакше не міняємо стан
            effective = False if offline_age >= OFFLINE_TIMEOUT else last_online_state

        if segment_start_time is None:
            segment_start_time = last_change_time

        # якщо статус змінився, але monitor "проспав" — трактуємо як реальну зміну зараз
        if effective != last_online_state:
            dur = int(now_ts - segment_start_time)
            if dur > 0:
                save_log(last_online_state, dur, end_ts=int(now_ts))

            last_online_state = effective
            last_change_time = now_ts
            segment_start_time = now_ts
        else:
            dur = int(now_ts - segment_start_time)
            if dur > 0:
                save_log(last_online_state, dur, end_ts=int(now_ts))
            segment_start_time = now_ts

        last_rollover_date = today
        save_state()

# ================== AUTO SUMMARY (08:00) ==================

async def send_daily_summary(now: datetime):
    global last_daily_summary_date
    start_ts, end_ts = prev_day_range_kyiv(now)
    light, dark = summarize_range(start_ts, end_ts)

    try:
        await bot.send_message(
            CHAT_ID,
            "📊 Підсумки за день \n"
            f"💡 Світло {hhmm(light)}\n"
            f"🌑 Темрява {hhmm(dark)}"
        )
    except Exception:
        pass

    last_daily_summary_date = ymd(now)
    save_state()

async def send_weekly_summary(now: datetime):
    global last_weekly_summary_date
    start_ts, end_ts = prev_week_range_kyiv(now)
    light, dark = summarize_range(start_ts, end_ts)

    try:
        await bot.send_message(
            CHAT_ID,
            "📅 Підсумки за тиждень \n"
            f"💡 Світло {days_hhmm(light)}\n"
            f"🌑 Темрява {days_hhmm(dark)}"
        )
    except Exception:
        pass

    last_weekly_summary_date = ymd(now)
    save_state()

async def send_monthly_summary(now: datetime):
    global last_monthly_summary_date
    start_ts, end_ts = prev_month_range_kyiv(now)
    prev_month_label = datetime.fromtimestamp(start_ts, KYIV_TZ).strftime("%Y-%m")

    light, dark = summarize_range(start_ts, end_ts)

    try:
        await bot.send_message(
            CHAT_ID,
            f"📅 Підсумки за місяць {prev_month_label} \n"
            f"💡 Світло {days_hhmm(light)}\n"
            f"🌑 Темрява {days_hhmm(dark)}"
        )
    except Exception:
        pass

    last_monthly_summary_date = ymd(now)
    save_state()

async def summary_scheduler():
    """
    00:01–00:04 Kyiv: daily rollover
    08:00–08:04 Kyiv: daily; Monday weekly; 1st monthly
    """
    load_state()

    while True:
        try:
            now = datetime.now(KYIV_TZ)
            today = ymd(now)

            await daily_rollover_if_needed(now)

            in_summary_window = (now.hour == 8 and 0 <= now.minute <= 4)
            if in_summary_window:
                if last_daily_summary_date != today:
                    await send_daily_summary(now)

                if now.weekday() == 0 and last_weekly_summary_date != today:
                    await send_weekly_summary(now)

                if now.day == 1 and last_monthly_summary_date != today:
                    await send_monthly_summary(now)

                await asyncio.sleep(90)

        except Exception:
            pass

        await asyncio.sleep(20)

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
            async with STATE_LOCK:
                if last_online_state is None or last_change_time is None:
                    await bot.send_message(CHAT_ID, "📡 Поточний статус:\nℹ️ Ще немає даних")
                else:
                    dur = hhmm(int(time.time() - last_change_time))
                    extra = ""

                    if not tuya_online:
                        if offline_since:
                            off_dur = int(time.time() - float(offline_since))
                            extra = (
                                f"\n⚠️ Tuya OFFLINE: {hhmm(off_dur)}"
                                f"\n(після {OFFLINE_TIMEOUT}с OFFLINE вважаємо: немає світла)"
                            )
                        else:
                            extra = "\n⚠️ Tuya OFFLINE"

                    await bot.send_message(
                        CHAT_ID,
                        f"📡 Поточний статус:\n{state_line(last_online_state)}\n⏱ У цьому стані: {dur}{extra}"
                    )

        elif cmd == "/last_change":
            async with STATE_LOCK:
                if last_online_state is None or last_change_time is None:
                    await bot.send_message(CHAT_ID, "🕒 Остання зміна:\nℹ️ Ще немає даних")
                else:
                    await bot.send_message(
                        CHAT_ID,
                        f"🕒 Остання зміна:\n{state_line(last_online_state)}\n{ts_hm(last_change_time)}"
                    )

        elif cmd == "/uptime":
            await bot.send_message(CHAT_ID, f"⏳ Uptime: {hhmm(int(time.time() - START_TS))}")

        elif cmd == "/summary_day":
            now = datetime.now(KYIV_TZ)
            start_ts, end_ts = prev_day_range_kyiv(now)
            light, dark = summarize_range(start_ts, end_ts)
            await bot.send_message(
                CHAT_ID,
                "📊 За день (вчора 00:00→сьогодні 00:00):\n"
                f"💡 Світло {hhmm(light)}\n"
                f"🌑 Темрява {hhmm(dark)}"
            )

        elif cmd == "/summary_week":
            now = datetime.now(KYIV_TZ)
            start_ts, end_ts = prev_week_range_kyiv(now)
            light, dark = summarize_range(start_ts, end_ts)
            await bot.send_message(
                CHAT_ID,
                "📊 За тиждень (попередній Пн→Пн):\n"
                f"💡 Світло {days_hhmm(light)}\n"
                f"🌑 Темрява {days_hhmm(dark)}"
            )

        elif cmd == "/summary_month":
            now = datetime.now(KYIV_TZ)
            start_ts, end_ts = prev_month_range_kyiv(now)
            label = datetime.fromtimestamp(start_ts, KYIV_TZ).strftime("%Y-%m")
            light, dark = summarize_range(start_ts, end_ts)
            await bot.send_message(
                CHAT_ID,
                f"📊 За місяць {label} (попередній):\n"
                f"💡 Світло {days_hhmm(light)}\n"
                f"🌑 Темрява {days_hhmm(dark)}"
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
    load_state()
    print("KYIV now:", datetime.now(KYIV_TZ).isoformat())
    await start_server()
    await set_webhook()
    await asyncio.gather(
        monitor(),
        summary_scheduler(),
    )

if __name__ == "__main__":
    asyncio.run(main())
