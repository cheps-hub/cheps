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
        "sig
