import os
import re
import html
import json
import time
import asyncio
import logging
import threading
from pathlib import Path
from typing import Dict, Set, Tuple, List

import requests
import phonenumbers
import pycountry
from flask import Flask, jsonify

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

# =========================
# ENV / CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))

CHANNEL_1_NAME = os.getenv("CHANNEL_1_NAME", "NUMBER")
CHANNEL_1_URL = os.getenv("CHANNEL_1_URL", "https://t.me/your_channel_1")

CHANNEL_2_NAME = os.getenv("CHANNEL_2_NAME", "CHANNEL")
CHANNEL_2_URL = os.getenv("CHANNEL_2_URL", "https://t.me/your_channel_2")

PORT = int(os.getenv("PORT", "10000"))

TIMEOUT = 20
CHECK_INTERVAL = 10
DEFAULT_FETCH_RECORDS = 20
SEEN_DB_FILE = "seen_records.json"
MAX_SEEN_RECORDS = 10000

# =========================
# API LIST HERE
# =========================
CR_APIS = [
    {
        "name": "API-1",
        "token": "Sk9XRTRSQlWEi1R-a4BSi0OLUoZYZlGGen9TdX2LjUJrUmd6ZoFQ",
        "url": "http://147.135.212.197/crapi/had/viewstats",
    },
    # {
    #     "name": "API-2",
    #     "token": "YOUR_CR_TOKEN_2",
    #     "url": "http://YOUR_SERVER_2/crapi/had/viewstats",
    # },
    # {
    #     "name": "API-3",
    #     "token": "YOUR_CR_TOKEN_3",
    #     "url": "http://YOUR_SERVER_3/crapi/had/viewstats",
    # },
]

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("safe_zone_sms_bot")

# =========================
# FILE STORAGE
# =========================
def load_json_file(path: str, default):
    file_path = Path(path)
    if not file_path.exists():
        return default
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json_file(path: str, data) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    Path(tmp_path).replace(path)


def load_seen_records() -> Set[str]:
    data = load_json_file(SEEN_DB_FILE, [])
    if isinstance(data, list):
        return set(str(x) for x in data)
    return set()


def save_seen_records(seen: Set[str]) -> None:
    items = list(seen)
    if len(items) > MAX_SEEN_RECORDS:
        items = items[-MAX_SEEN_RECORDS:]
    save_json_file(SEEN_DB_FILE, items)


seen_records: Set[str] = load_seen_records()

# =========================
# HELPERS
# =========================
def sanitize_records(value) -> int:
    try:
        n = int(value)
        if n < 1:
            return 10
        return min(n, 200)
    except Exception:
        return 10


def build_params(token: str, records: int = DEFAULT_FETCH_RECORDS) -> Dict[str, str]:
    return {
        "token": token,
        "records": str(sanitize_records(records)),
    }


def fetch_cr_data(api_url: str, params: Dict[str, str]) -> Dict:
    response = requests.get(api_url, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def build_record_id(item: Dict, api_name: str, api_url: str) -> str:
    dt = str(item.get("dt", "")).strip()
    num = str(item.get("num", "")).strip()
    cli = str(item.get("cli", "")).strip()
    message = str(item.get("message", "")).strip()
    payout = str(item.get("payout", "")).strip()
    return f"{api_name}|{api_url}|{dt}|{num}|{cli}|{message}|{payout}"


def mask_number(number: str) -> str:
    digits = "".join(ch for ch in str(number) if ch.isdigit())
    if len(digits) >= 6:
        return f"{digits[:3]}TWBTECH{digits[-3:]}"
    if len(digits) >= 4:
        return f"{digits[:2]}TWBTECH{digits[-2:]}"
    return "TWBTECH"


def get_flag_emoji(country_code: str) -> str:
    if not country_code or len(country_code) != 2:
        return "🌍"
    country_code = country_code.upper()
    return chr(ord(country_code[0]) + 127397) + chr(ord(country_code[1]) + 127397)


def get_country_info(number: str) -> str:
    if not number:
        return "🌍 Unknown"

    raw = str(number).strip()
    digits = "".join(ch for ch in raw if ch.isdigit() or ch == "+")

    if not digits:
        return "🌍 Unknown"

    candidates = [digits]
    if not digits.startswith("+"):
        candidates.insert(0, f"+{digits}")

    for candidate in candidates:
        try:
            parsed = phonenumbers.parse(candidate, None)
            if not phonenumbers.is_possible_number(parsed):
                continue

            region_code = phonenumbers.region_code_for_number(parsed)
            if not region_code:
                continue

            country = pycountry.countries.get(alpha_2=region_code.upper())
            country_name = country.name if country else region_code.upper()
            flag = get_flag_emoji(region_code)
            return f"{flag} {country_name}"
        except Exception:
            continue

    return "🌍 Unknown"

# =========================
# CODE DETECTION
# =========================
CODE_PATTERNS = [
    re.compile(r"\b([A-Za-z0-9]{4,20})\b(?=\s+is\s+your\s+[\w\s.\-]*code\b)", re.I),
    re.compile(r"\b(?:your\s+)?[\w\s.\-]*code\s+is\s+([A-Za-z0-9]{4,20})\b", re.I),
    re.compile(r"\b(?:otp|pin|passcode)\b[\s:;\-]*([A-Za-z0-9]{4,20})\b", re.I),
    re.compile(r"\bverification\s+code\b[\s:;\-]*([A-Za-z0-9]{4,20})\b", re.I),
    re.compile(r"\b(?:facebook|instagram|google|whatsapp|telegram|discord|twitter|x)\s+code\b[\s:;\-]*([A-Za-z0-9]{4,20})\b", re.I),
    re.compile(r"\bcode\b[\s:;\-]*([A-Za-z0-9]{4,20})\b", re.I),
]
FALLBACK_DIGIT_PATTERN = re.compile(r"\b(\d{4,8})\b")


def extract_code(message: str) -> str:
    if not message:
        return ""

    text = str(message).strip()

    for pattern in CODE_PATTERNS:
        match = pattern.search(text)
        if match:
            code = match.group(1).strip()
            if code:
                return code

    fallback = FALLBACK_DIGIT_PATTERN.search(text)
    if fallback:
        return fallback.group(1)

    return ""

# =========================
# FORMAT MESSAGE
# =========================
def format_single_item(item: Dict) -> Tuple[str, InlineKeyboardMarkup]:
    num = item.get("num", "")
    service = item.get("cli", "-")
    message = item.get("message", "")

    hidden_number = mask_number(num)
    country_info = get_country_info(num)
    code = extract_code(str(message))

    safe_service = html.escape(str(service))
    safe_message = html.escape(str(message))
    safe_code = html.escape(code if code else "No code found")

    text = (
        f"<b>📞 Number:</b> <code>{hidden_number}</code>\n"
        f"<b>🌍 Country:</b> {country_info}\n"
        f"<b>📡Service:</b> {safe_service}\n\n"
        f"<b>🗝️ Code:</b> "
        f"<code>{safe_code}</code>"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(CHANNEL_1_NAME, url=CHANNEL_1_URL),
                InlineKeyboardButton(CHANNEL_2_NAME, url=CHANNEL_2_URL),
            ],
        ]
    )
    return text, keyboard


# =========================
# BUTTON HANDLER
# =========================
async def show_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    message = query.message
    if not message or message.chat.id != GROUP_CHAT_ID:
        await query.answer()
        return

    data = query.data or ""
    if data.startswith("showcode|"):
        code = data.split("|", 1)[1]
        await query.answer(text=code, show_alert=True)
        return

    await query.answer()


# =========================
# FETCH + SEND
# =========================
async def process_all_apis_and_send(app: Application) -> None:
    global seen_records

    for api in CR_APIS:
        api_name = str(api.get("name", "API")).strip()
        api_token = str(api.get("token", "")).strip()
        api_url = str(api.get("url", "")).strip()

        if not api_token or not api_url:
            logger.warning("Skipped API due to missing token/url: %s", api)
            continue

        try:
            params = build_params(api_token, DEFAULT_FETCH_RECORDS)
            result = fetch_cr_data(api_url, params)

            if result.get("status") != "success":
                logger.warning("%s returned non-success status", api_name)
                continue

            data = result.get("data", [])
            if not isinstance(data, list):
                continue

            for item in reversed(data):
                if not isinstance(item, dict):
                    continue

                record_id = build_record_id(item, api_name, api_url)
                if record_id in seen_records:
                    continue

                message_text = str(item.get("message", ""))
                code = extract_code(message_text)

                seen_records.add(record_id)

                if not code:
                    continue

                text, reply_markup = format_single_item(item)

                try:
                    await app.bot.send_message(
                        chat_id=GROUP_CHAT_ID,
                        text=text,
                        parse_mode="HTML",
                        reply_markup=reply_markup,
                    )
                except Exception as exc:
                    logger.error("Send message failed for %s: %s", api_name, exc)

        except requests.HTTPError as exc:
            logger.error("HTTP error from %s: %s", api_name, exc)
        except requests.RequestException as exc:
            logger.error("Request error from %s: %s", api_name, exc)
        except Exception as exc:
            logger.exception("Unexpected error in %s: %s", api_name, exc)

    save_seen_records(seen_records)


# =========================
# TELEGRAM BOT LOOP
# =========================
async def bot_runner() -> None:
    while True:
        application = None
        try:
            if not BOT_TOKEN:
                raise ValueError("BOT_TOKEN set korun.")
            if not GROUP_CHAT_ID:
                raise ValueError("GROUP_CHAT_ID set korun.")
            if not CR_APIS:
                raise ValueError("CR_APIS list e API add korun.")

            application = Application.builder().token(BOT_TOKEN).build()
            application.add_handler(
                CallbackQueryHandler(show_code_callback, pattern=r"^showcode\|")
            )

            await application.initialize()
            await application.start()
            await application.updater.start_polling(drop_pending_updates=True)

            logger.info("Telegram bot started.")

            while True:
                try:
                    await process_all_apis_and_send(application)
                except Exception:
                    logger.exception("Auto fetch loop crashed but recovered")
                await asyncio.sleep(CHECK_INTERVAL)

        except (NetworkError, TimedOut) as exc:
            logger.error("Telegram network error, reconnecting: %s", exc)
            await asyncio.sleep(5)

        except Exception as exc:
            logger.exception("Bot crashed, restarting soon: %s", exc)
            await asyncio.sleep(5)

        finally:
            if application:
                try:
                    if application.updater and application.updater.running:
                        await application.updater.stop()
                except Exception:
                    pass
                try:
                    if application.running:
                        await application.stop()
                except Exception:
                    pass
                try:
                    await application.shutdown()
                except Exception:
                    pass


def start_bot_thread() -> None:
    def runner():
        asyncio.run(bot_runner())

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()


# =========================
# KEEP ALIVE WEB SERVER
# =========================
web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "SMS Bot is running", 200


@web_app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


# =========================
# MAIN
# =========================
if __name__ == "__main__":
    start_bot_thread()
    logger.info("Starting keep alive web server on port %s", PORT)
    web_app.run(host="0.0.0.0", port=PORT)
