#!/usr/bin/env python3
"""
xendr4x reuploader - Telegram-бот для перезалива файлов на файлообменники.

Поддерживаемые хостинги: VikingFile, Pixeldrain, FuckingFast, BuzzHeavier, Gofile.

Логика:
  1. Бот поднимает aria2c в режиме RPC (для получения реального прогресса скачивания).
  2. Бот слушает Telegram через long polling (getUpdates), включая нажатия кнопок.
  3. Пользователь присылает боту ссылку на файл.
  4. Бот предлагает выбрать хостинг для перезалива кнопками.
  5. Бот скачивает файл через aria2c, показывая прогресс с кнопками "Пауза" / "Стоп".
  6. Бот заливает файл на выбранный хостинг, тоже с прогрессом и кнопками управления.
  7. Бот присылает финальную ссылку на файл.

Дополнительно:
  - /speedtest - замеряет скорость аплоада на каждый поддерживаемый хостинг.
  - /language - переключение интерфейса между русским и английским.
  - /stop - останавливает текущую задачу; /shutdown - останавливает бота целиком.

Если новых сообщений нет дольше IDLE_TIMEOUT минут - бот завершает работу.
"""

import os
import re
import sys
import time
import json
import shutil
import signal
import threading
import subprocess
import uuid
import mimetypes
from html import escape as html_escape
from urllib.parse import quote, urljoin

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

# ---------------------------------------------------------------------------
# Конфигурация из переменных окружения
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BOT_DISPLAY_NAME = "xendr4x reuploader"

VIKINGFILE_USER_HASH = os.environ.get("VIKINGFILE_USER_HASH", "")
PIXELDRAIN_API_KEY = os.environ.get("PIXELDRAIN_API_KEY", "")
FUCKINGFAST_TOKEN = os.environ.get("FUCKINGFAST_TOKEN", "")
BUZZHEAVIER_TOKEN = os.environ.get("BUZZHEAVIER_TOKEN", "")
GOFILE_API_TOKEN = os.environ.get("GOFILE_API_TOKEN", "")

ALLOWED_CHAT_IDS = {
    c.strip() for c in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if c.strip()
}


def _env_int(name, default):
    val = os.environ.get(name, "").strip()
    return int(val) if val else default


def _env_float(name, default):
    val = os.environ.get(name, "").strip()
    return float(val) if val else default


IDLE_TIMEOUT = _env_int("IDLE_TIMEOUT_MINUTES", 10) * 60
HARD_TIMEOUT = _env_int("HARD_TIMEOUT_SECONDS", 20400)
PIXELDRAIN_MAX_SIZE_BYTES = int(_env_float("PIXELDRAIN_MAX_SIZE_GB", 20.0) * 1024 ** 3)
SPEEDTEST_SIZE_MB = _env_int("SPEEDTEST_SIZE_MB", 100)
TG_FILE_MAX_SIZE = 20 * 1024 * 1024  # Telegram Bot API getFile download limit

DOWNLOAD_DIR = os.path.abspath("downloads")
ARIA2_RPC_PORT = 6800
ARIA2_RPC_SECRET = "pdbotsecret"
ARIA2_RPC_URL = f"http://127.0.0.1:{ARIA2_RPC_PORT}/jsonrpc"

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

URL_RE = re.compile(r"https?://\S+")
UPLOAD_RETRIES = 5

DESTINATIONS = {
    "vikingfile": "VikingFile",
    "pixeldrain": "Pixeldrain",
    "fuckingfast": "FuckingFast",
    "buzzheavier": "BuzzHeavier",
    "gofile": "Gofile",
}

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

start_time = time.time()
last_activity_time = time.time()
should_stop_bot = threading.Event()

pending_selections = {}   # chat_id -> {"token", "url", "message_id"}
active_controls = {}      # chat_id -> TaskControl
user_lang = {}            # chat_id -> "ru" / "en"

DEFAULT_LANG = "ru"


class TaskStopped(Exception):
    """Пользователь остановил задачу кнопкой Стоп / командой /stop."""


class TaskControl:
    def __init__(self, task_id, lang=DEFAULT_LANG):
        self.task_id = task_id
        self.lang = lang
        self.stopped = threading.Event()
        self.running = threading.Event()
        self.running.set()
        self.gid = None


# ---------------------------------------------------------------------------
# Локализация
# ---------------------------------------------------------------------------

TEXTS = {
    "ru": {
        "start": (
            "👋 <b>Привет! Я {bot_name}.</b>\n\n"
            "Пришли мне ссылку на файл или сам файл (документом) — я скачаю его и предложу перезалить "
            "на <b>VikingFile</b>, <b>Pixeldrain</b>, <b>FuckingFast</b>, "
            "<b>BuzzHeavier</b> и/или <b>Gofile</b> — можно выбрать сразу несколько, "
            "файл скачается один раз, а зальётся по очереди на все выбранные хостинги.\n\n"
            "На этапе скачивания и загрузки под сообщением с прогрессом будут "
            "кнопки ⏸ <b>Пауза</b> и ⏹ <b>Стоп</b>.\n\n"
            "⚙️ <b>Команды</b>\n"
            "/speedtest — замерить скорость загрузки на каждый хостинг\n"
            "/language — сменить язык интерфейса\n"
            "/stop — остановить текущую задачу (аналог кнопки ⏹ Стоп)\n"
            "/shutdown — полностью завершить работу бота"
        ),
        "no_access": "⛔ У вас нет доступа к этому боту.",
        "stop_task": "⏹ Останавливаю текущую задачу...",
        "stop_no_task": "ℹ️ Сейчас нет активной задачи. Чтобы полностью завершить бота, используй /shutdown.",
        "shutdown": "🛑 Останавливаю бота полностью...",
        "busy": "⏳ Уже выполняется другая задача. Дождись её завершения или отправь /stop.",
        "ask_link": "📎 Пришли ссылку на файл (начинается с http:// или https://) или сам файл (документом).",
        "size_line": "\n📦 Размер: ~{size}",
        "link_received": "🔗 <b>Принял ссылку</b>\n<code>{url}</code>{size_line}\n\nВыбери хостинги (можно несколько), затем нажми «Начать».",
        "file_received": "📎 <b>Принял файл</b>\n<code>{url}</code>{size_line}\n\nВыбери хостинги (можно несколько), затем нажми «Начать».",
        "tg_file_too_large": "⛔ Файл слишком большой для скачивания через Telegram Bot API (максимум {limit}). Пришли ссылку на файл вместо самого файла.",
        "stale_button": "Эта кнопка устарела",
        "pixeldrain_unavailable": "Pixeldrain недоступен: не задан PIXELDRAIN_API_KEY",
        "toolarge_alert": "⛔ Файл больше лимита {limit} для {host}. Выбери другой хостинг, например VikingFile.",
        "target_chosen": "Выбрано: {host}",
        "select_all_button": "☑️ Выбрать все",
        "clear_all_button": "◻️ Снять всё",
        "start_button": "🚀 Начать ({n})",
        "select_at_least_one": "Выбери хотя бы один хостинг",
        "overview_title": "📦 <b>Мультизалив</b>\n{div}",
        "in_progress_label": "загрузка...",
        "pending_label": "в очереди",
        "cancelled_label": "отменено",
        "overview_row_ok": "✅ <b>{host}:</b> {url}",
        "overview_row_fail": "❌ <b>{host}:</b> {err}",
        "overview_row_pending": "{icon} <b>{host}:</b> {status}",
        "multi_download_done": (
            "✅ <b>Скачивание завершено</b>\n📄 <code>{name}</code>\n📦 {size}\n\n"
            "⬆️ Начинаю заливать на {n} хостингов по очереди..."
        ),
        "multi_all_done": "🏁 Все загрузки завершены! Ссылки — в сообщении выше.",
        "task_finished_alert": "Задача уже завершена",
        "pause_alert": "⏸ Пауза",
        "resume_alert": "▶️ Продолжаю",
        "stopping_alert": "⏹ Останавливаю задачу...",
        "target_start": "🎯 <b>Хостинг:</b> {host}\n🔗 <code>{url}</code>\n\n⏳ Начинаю скачивание...",
        "download_done": (
            "✅ <b>Скачивание завершено</b>\n"
            "📄 <code>{name}</code>\n📦 {size}\n\n"
            "⬆️ Начинаю загрузку на {host}..."
        ),
        "done": (
            "🎉 <b>Готово!</b>\n{div}\n"
            "📄 <b>Файл:</b> <code>{name}</code>\n"
            "📦 <b>Размер:</b> {size}\n"
            "🌐 <b>Хостинг:</b> {host}\n"
            "🕐 <b>Затрачено времени:</b> {time}\n{div}\n"
            "🔗 <b>Ссылка:</b> {url}"
        ),
        "stopped": "⏹ <b>Остановлено пользователем</b>",
        "error": "❌ <b>Ошибка</b>\n<code>{err}</code>",
        "retry": (
            "⚠️ Сбой сети при загрузке (попытка {attempt}/{total}), "
            "пробую снова через {wait}с...\n<code>{err}</code>"
        ),
        "upload_failed_final": "Не удалось загрузить после {total} попыток: {err}{hint}",
        "pixeldrain_hint": (
            "\n\n💡 Похоже, Pixeldrain систематически рвёт соединение (SSL EOF) - "
            "это часто означает, что сервис ограничивает загрузку с IP-адресов "
            "дата-центров/CI (какие использует GitHub Actions). Попробуй прислать "
            "ссылку заново и выбрать <b>VikingFile</b> - он обычно стабильнее в такой среде."
        ),
        "pixeldrain_key_missing": "Секрет PIXELDRAIN_API_KEY не задан в репозитории.",
        "host_http_error": "Ошибка {host} ({code}): {body}",
        "host_link_missing": (
            "Ошибка {host}: сервер ответил {code}, но ссылку в ответе найти не удалось "
            "(нет ни Location, ни JSON с url, ни текстовой ссылки в теле). Тело ответа: {body}"
        ),
        "downloading_title": "Скачивание файла",
        "uploading_title": "Загрузка на {host}",
        "paused_title": "На паузе",
        "elapsed_label": "Прошло",
        "pause_button": "⏸ Пауза",
        "resume_button": "▶️ Продолжить",
        "stop_button": "⏹ Стоп",
        "language_prompt": "🌐 Выбери язык интерфейса:",
        "language_set": "✅ Язык переключён на русский.",
        "speedtest_busy": "⏳ Уже выполняется другая задача (в т.ч. другой speedtest). Дождись её завершения или отправь /stop.",
        "speedtest_start": "🚀 <b>Speedtest</b>\nЗамеряю скорость загрузки на {n} хостингов (тестовый файл {size})...",
        "speedtest_progress": "🚀 Тестирую {host} ({i}/{n})...",
        "speedtest_title": "🚀 <b>Результаты speedtest</b>\nТестовый файл: {size}\n{div}",
        "speedtest_row_ok": "{medal} <b>{host}</b>: {speed}/с ({time})",
        "speedtest_row_fail": "⚠️ <b>{host}</b>: {err}",
        "speedtest_skip_no_key": "нет API-ключа, пропущено",
    },
    "en": {
        "start": (
            "👋 <b>Hi! I'm {bot_name}.</b>\n\n"
            "Send me a link to a file or the file itself as a document — I'll download it and let you re-upload it "
            "to <b>VikingFile</b>, <b>Pixeldrain</b>, <b>FuckingFast</b>, "
            "<b>BuzzHeavier</b> and/or <b>Gofile</b> — you can pick several at once, "
            "the file is downloaded once and then uploaded to each selected host in turn.\n\n"
            "During download and upload, the progress message will have "
            "⏸ <b>Pause</b> and ⏹ <b>Stop</b> buttons under it.\n\n"
            "⚙️ <b>Commands</b>\n"
            "/speedtest — measure upload speed to each hosting service\n"
            "/language — switch interface language\n"
            "/stop — stop the current task (same as the ⏹ Stop button)\n"
            "/shutdown — fully shut down the bot"
        ),
        "no_access": "⛔ You don't have access to this bot.",
        "stop_task": "⏹ Stopping the current task...",
        "stop_no_task": "ℹ️ No active task right now. To fully shut down the bot, use /shutdown.",
        "shutdown": "🛑 Shutting down the bot completely...",
        "busy": "⏳ Another task is already running. Wait for it to finish or send /stop.",
        "ask_link": "📎 Send a link to a file (starting with http:// or https://) or the file itself as a document.",
        "size_line": "\n📦 Size: ~{size}",
        "link_received": "🔗 <b>Link received</b>\n<code>{url}</code>{size_line}\n\nChoose hosting services (you can select several), then press Start.",
        "file_received": "📎 <b>File received</b>\n<code>{url}</code>{size_line}\n\nChoose hosting services (you can select several), then press Start.",
        "tg_file_too_large": "⛔ File is too large to download via Telegram Bot API (max {limit}). Send a link to the file instead.",
        "stale_button": "This button has expired",
        "pixeldrain_unavailable": "Pixeldrain unavailable: PIXELDRAIN_API_KEY is not set",
        "toolarge_alert": "⛔ File exceeds the {limit} limit for {host}. Pick another host, e.g. VikingFile.",
        "target_chosen": "Selected: {host}",
        "select_all_button": "☑️ Select all",
        "clear_all_button": "◻️ Clear all",
        "start_button": "🚀 Start ({n})",
        "select_at_least_one": "Select at least one host",
        "overview_title": "📦 <b>Multi-reupload</b>\n{div}",
        "in_progress_label": "uploading...",
        "pending_label": "queued",
        "cancelled_label": "cancelled",
        "overview_row_ok": "✅ <b>{host}:</b> {url}",
        "overview_row_fail": "❌ <b>{host}:</b> {err}",
        "overview_row_pending": "{icon} <b>{host}:</b> {status}",
        "multi_download_done": (
            "✅ <b>Download complete</b>\n📄 <code>{name}</code>\n📦 {size}\n\n"
            "⬆️ Uploading to {n} hosts one by one..."
        ),
        "multi_all_done": "🏁 All uploads are finished! Links are in the message above.",
        "task_finished_alert": "Task already finished",
        "pause_alert": "⏸ Paused",
        "resume_alert": "▶️ Resuming",
        "stopping_alert": "⏹ Stopping task...",
        "target_start": "🎯 <b>Host:</b> {host}\n🔗 <code>{url}</code>\n\n⏳ Starting download...",
        "download_done": (
            "✅ <b>Download complete</b>\n"
            "📄 <code>{name}</code>\n📦 {size}\n\n"
            "⬆️ Starting upload to {host}..."
        ),
        "done": (
            "🎉 <b>Done!</b>\n{div}\n"
            "📄 <b>File:</b> <code>{name}</code>\n"
            "📦 <b>Size:</b> {size}\n"
            "🌐 <b>Host:</b> {host}\n"
            "🕐 <b>Time spent:</b> {time}\n{div}\n"
            "🔗 <b>Link:</b> {url}"
        ),
        "stopped": "⏹ <b>Stopped by user</b>",
        "error": "❌ <b>Error</b>\n<code>{err}</code>",
        "retry": (
            "⚠️ Network error while uploading (attempt {attempt}/{total}), "
            "retrying in {wait}s...\n<code>{err}</code>"
        ),
        "upload_failed_final": "Upload failed after {total} attempts: {err}{hint}",
        "pixeldrain_hint": (
            "\n\n💡 Pixeldrain seems to be consistently dropping the connection (SSL EOF) - "
            "this usually means the service is throttling/blocking uploads from datacenter/CI "
            "IP ranges (which is what GitHub Actions runners use). Try sending the link again "
            "and choosing <b>VikingFile</b> instead - it tends to be more reliable in this environment."
        ),
        "pixeldrain_key_missing": "The PIXELDRAIN_API_KEY secret is not set in the repository.",
        "host_http_error": "{host} error ({code}): {body}",
        "host_link_missing": (
            "{host} error: server responded {code}, but no link could be found in the response "
            "(no Location header, no JSON url field, no plain-text link body). Response body: {body}"
        ),
        "downloading_title": "Downloading file",
        "uploading_title": "Uploading to {host}",
        "paused_title": "Paused",
        "elapsed_label": "Elapsed",
        "pause_button": "⏸ Pause",
        "resume_button": "▶️ Resume",
        "stop_button": "⏹ Stop",
        "language_prompt": "🌐 Choose interface language:",
        "language_set": "✅ Language switched to English.",
        "speedtest_busy": "⏳ Another task (including another speedtest) is already running. Wait for it or send /stop.",
        "speedtest_start": "🚀 <b>Speedtest</b>\nMeasuring upload speed to {n} hosts (test file {size})...",
        "speedtest_progress": "🚀 Testing {host} ({i}/{n})...",
        "speedtest_title": "🚀 <b>Speedtest results</b>\nTest file: {size}\n{div}",
        "speedtest_row_ok": "{medal} <b>{host}</b>: {speed}/s ({time})",
        "speedtest_row_fail": "⚠️ <b>{host}</b>: {err}",
        "speedtest_skip_no_key": "no API key, skipped",
    },
}

SIZE_UNITS = {
    "ru": ["Б", "КБ", "МБ", "ГБ", "ТБ", "ПБ"],
    "en": ["B", "KB", "MB", "GB", "TB", "PB"],
}


def t(lang, key, **kwargs):
    lang = lang if lang in TEXTS else DEFAULT_LANG
    template = TEXTS.get(lang, {}).get(key)
    if template is None:
        template = TEXTS[DEFAULT_LANG].get(key, key)
    try:
        return template.format(**kwargs)
    except Exception:
        return template


def get_lang(chat_id, tg_user=None):
    if chat_id in user_lang:
        return user_lang[chat_id]
    lang = DEFAULT_LANG
    if tg_user:
        code = (tg_user.get("language_code") or "").lower()
        lang = "ru" if code.startswith("ru") else "en"
    user_lang[chat_id] = lang
    return lang


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def tg_call(method, **params):
    resp = requests.post(f"{TELEGRAM_API}/{method}", data=params, timeout=30)
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"Telegram API error in {method}: HTTP {resp.status_code} {resp.text[:300]}")
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}: {data.get('description', data)}")
    return data["result"]


def send_message(chat_id, text, reply_markup=None, **kwargs):
    kwargs.setdefault("parse_mode", "HTML")
    kwargs.setdefault("disable_web_page_preview", True)
    if reply_markup is not None:
        kwargs["reply_markup"] = json.dumps(reply_markup)
    return tg_call("sendMessage", chat_id=chat_id, text=text, **kwargs)


def edit_message(chat_id, message_id, text, reply_markup=None, **kwargs):
    kwargs.setdefault("parse_mode", "HTML")
    kwargs.setdefault("disable_web_page_preview", True)
    if reply_markup is not None:
        kwargs["reply_markup"] = json.dumps(reply_markup)
    try:
        return tg_call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, **kwargs)
    except RuntimeError as e:
        if "message is not modified" in str(e):
            return None
        raise


def answer_callback(callback_id, text=None, show_alert=False):
    try:
        tg_call("answerCallbackQuery", callback_query_id=callback_id, text=text or "", show_alert=show_alert)
    except RuntimeError:
        pass


def get_updates(offset):
    resp = requests.post(
        f"{TELEGRAM_API}/getUpdates",
        data={
            "offset": offset,
            "timeout": 25,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        },
        timeout=35,
    )
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"Telegram API error in getUpdates: HTTP {resp.status_code} {resp.text[:300]}")
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error in getUpdates: {data.get('description', data)}")
    return data["result"]


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

def build_language_keyboard():
    return {
        "inline_keyboard": [[
            {"text": "🇷🇺 Русский", "callback_data": "lang|ru"},
            {"text": "🇬🇧 English", "callback_data": "lang|en"},
        ]]
    }


HOST_ICONS = {
    "vikingfile": "🦁",
    "pixeldrain": "🟢",
    "fuckingfast": "⚡",
    "buzzheavier": "🐝",
    "gofile": "📁",
}


def build_multiselect_keyboard(token, selected, file_size, lang):
    def label(dest):
        mark = "✅" if dest in selected else "⬜"
        return f"{mark} {HOST_ICONS[dest]} {DESTINATIONS[dest]}"

    pixeldrain_too_large = file_size is not None and file_size > PIXELDRAIN_MAX_SIZE_BYTES

    row1 = [{"text": label("vikingfile"), "callback_data": f"select|vikingfile|{token}"}]
    if pixeldrain_too_large:
        row1.append({
            "text": f"⛔ 🟢 Pixeldrain >{human_size(PIXELDRAIN_MAX_SIZE_BYTES, lang)}",
            "callback_data": f"toolarge|pixeldrain|{token}",
        })
    else:
        row1.append({"text": label("pixeldrain"), "callback_data": f"select|pixeldrain|{token}"})
    row1.append({"text": label("fuckingfast"), "callback_data": f"select|fuckingfast|{token}"})

    row2 = [
        {"text": label("buzzheavier"), "callback_data": f"select|buzzheavier|{token}"},
        {"text": label("gofile"), "callback_data": f"select|gofile|{token}"},
    ]

    selectable = [d for d in DESTINATIONS if not (d == "pixeldrain" and pixeldrain_too_large)]
    all_selected = selected and all(d in selected for d in selectable)
    row3 = [
        {
            "text": t(lang, "clear_all_button") if all_selected else t(lang, "select_all_button"),
            "callback_data": f"selectall|{token}",
        },
        {
            "text": t(lang, "start_button", n=len(selected)),
            "callback_data": f"confirm|{token}",
        },
    ]

    return {"inline_keyboard": [row1, row2, row3]}


def build_progress_keyboard(task_id, control):
    lang = control.lang
    toggle_label = t(lang, "pause_button") if control.running.is_set() else t(lang, "resume_button")
    return {
        "inline_keyboard": [[
            {"text": toggle_label, "callback_data": f"toggle|{task_id}"},
            {"text": t(lang, "stop_button"), "callback_data": f"stop|{task_id}"},
        ]]
    }


# ---------------------------------------------------------------------------
# Визуал: прогресс-бары, форматирование
# ---------------------------------------------------------------------------

def human_size(n, lang=DEFAULT_LANG):
    units = SIZE_UNITS.get(lang, SIZE_UNITS[DEFAULT_LANG])
    n = float(n)
    for unit in units:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} {units[-1]}"


def human_time(seconds, lang=DEFAULT_LANG):
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if lang == "en":
        if h:
            return f"{h}h {m:02d}m {s:02d}s"
        if m:
            return f"{m}m {s:02d}s"
        return f"{s}s"
    if h:
        return f"{h}ч {m:02d}м {s:02d}с"
    if m:
        return f"{m}м {s:02d}с"
    return f"{s}с"


def progress_bar(fraction, width=18):
    fraction = max(0.0, min(1.0, fraction))
    filled = int(width * fraction)
    partial_chars = "▏▎▍▌▋▊▉"
    remainder = (width * fraction) - filled
    bar = "█" * filled
    if filled < width and remainder > 0:
        bar += partial_chars[int(remainder * len(partial_chars))]
        filled += 1
    bar += "░" * (width - filled)
    return bar


def render_progress(icon, title, done, total, speed_bps, elapsed, lang, paused=False):
    if total > 0:
        frac = done / total
        pct = frac * 100
        eta = (total - done) / speed_bps if speed_bps > 0 else None
    else:
        frac = 0
        pct = 0
        eta = None

    bar = progress_bar(frac)
    speed_str = f"{human_size(speed_bps, lang)}/с" if lang == "ru" and speed_bps else (
        f"{human_size(speed_bps, lang)}/s" if speed_bps else "—"
    )
    eta_str = human_time(eta, lang) if eta is not None else "—"

    status_line = f"⏸ <b>{t(lang, 'paused_title')}</b>" if paused else f"{icon} <b>{title}</b>"

    lines = [
        status_line,
        "",
        f"<code>[{bar}] {pct:5.1f}%</code>",
        f"📦 {human_size(done, lang)} / {human_size(total, lang) if total else '?'}",
        f"⚡ {speed_str}   ⏳ ETA: {eta_str}",
        f"🕐 {t(lang, 'elapsed_label')}: {human_time(elapsed, lang)}",
    ]
    return "\n".join(lines)


def divider():
    return "━━━━━━━━━━━━━━━━━━"


# ---------------------------------------------------------------------------
# aria2c RPC
# ---------------------------------------------------------------------------

_aria2_proc = None
_rpc_id_counter = 0


def start_aria2c():
    global _aria2_proc
    _aria2_proc = subprocess.Popen(
        [
            "aria2c",
            "--enable-rpc",
            f"--rpc-listen-port={ARIA2_RPC_PORT}",
            f"--rpc-secret={ARIA2_RPC_SECRET}",
            "--rpc-listen-all=false",
            "--dir", DOWNLOAD_DIR,
            "--max-connection-per-server=16",
            "--split=16",
            "--min-split-size=1M",
            "--continue=true",
            "--max-tries=5",
            "--retry-wait=5",
            "--quiet=true",
            "--allow-overwrite=true",
            "--pause=false",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        try:
            aria2_rpc("aria2.getVersion")
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("Failed to bring up aria2c RPC")


def stop_aria2c():
    if _aria2_proc:
        _aria2_proc.terminate()
        try:
            _aria2_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _aria2_proc.kill()


def aria2_rpc(method, params=None):
    global _rpc_id_counter
    _rpc_id_counter += 1
    full_params = [f"token:{ARIA2_RPC_SECRET}"]
    if params:
        full_params += params
    payload = {"jsonrpc": "2.0", "id": str(_rpc_id_counter), "method": method, "params": full_params}
    resp = requests.post(ARIA2_RPC_URL, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"aria2 RPC error: {data['error']}")
    return data["result"]


def download_with_progress(url, chat_id, status_msg_id, task_id, control):
    gid = aria2_rpc("aria2.addUri", [[url]])
    control.gid = gid
    lang = control.lang

    last_edit_text = None
    last_edit_time = 0
    stage_start = time.time()
    paused_in_aria2 = False

    try:
        while True:
            if control.stopped.is_set():
                try:
                    aria2_rpc("aria2.forceRemove", [gid])
                except Exception:
                    pass
                raise TaskStopped()

            if not control.running.is_set():
                if not paused_in_aria2:
                    try:
                        aria2_rpc("aria2.pause", [gid])
                    except Exception:
                        pass
                    paused_in_aria2 = True
                    status = aria2_rpc("aria2.tellStatus", [gid, ["totalLength", "completedLength"]])
                    total = int(status.get("totalLength") or 0)
                    done = int(status.get("completedLength") or 0)
                    text = render_progress("⬇️", t(lang, "downloading_title"), done, total, 0,
                                            time.time() - stage_start, lang, paused=True)
                    edit_message(chat_id, status_msg_id, text, reply_markup=build_progress_keyboard(task_id, control))
                control.running.wait(timeout=1)
                continue
            else:
                if paused_in_aria2:
                    try:
                        aria2_rpc("aria2.unpause", [gid])
                    except Exception:
                        pass
                    paused_in_aria2 = False

            status = aria2_rpc("aria2.tellStatus", [
                gid, ["status", "totalLength", "completedLength", "downloadSpeed", "files", "errorMessage"]
            ])

            state = status["status"]
            total = int(status.get("totalLength") or 0)
            done = int(status.get("completedLength") or 0)
            speed = int(status.get("downloadSpeed") or 0)

            if state == "error":
                raise RuntimeError(f"aria2c error: {status.get('errorMessage', 'unknown error')}")

            now = time.time()
            if now - last_edit_time >= 3:
                text = render_progress("⬇️", t(lang, "downloading_title"), done, total, speed,
                                        now - stage_start, lang)
                if text != last_edit_text:
                    edit_message(chat_id, status_msg_id, text, reply_markup=build_progress_keyboard(task_id, control))
                    last_edit_text = text
                last_edit_time = now

            if state == "complete":
                return status["files"][0]["path"]

            if state not in ("active", "waiting", "paused"):
                raise RuntimeError(f"Unexpected aria2c status: {state}")

            time.sleep(1)
    finally:
        control.gid = None


# ---------------------------------------------------------------------------
# Скачивание файла, присланного пользователем прямо в Telegram
# ---------------------------------------------------------------------------

def _build_file_info(media, media_type):
    file_id = media["file_id"]
    file_name = media.get("file_name")
    if not file_name:
        if media_type == "photo":
            ext = ".jpg"
        elif media_type == "voice":
            ext = ".ogg"
        elif media_type == "video_note":
            ext = ".mp4"
        elif media_type == "sticker":
            ext = ".webp"
        else:
            ext = mimetypes.guess_extension(media.get("mime_type", "")) or ""
        uid = media.get("file_unique_id") or file_id[-12:]
        file_name = f"{media_type}_{uid}{ext}"
    return {
        "file_id": file_id,
        "file_name": file_name,
        "file_size": media.get("file_size", 0),
    }


def extract_tg_file(message):
    for key in ("document", "video", "audio", "animation", "voice", "video_note", "sticker"):
        media = message.get(key)
        if media and "file_id" in media:
            return _build_file_info(media, key)
    photo = message.get("photo")
    if photo:
        return _build_file_info(photo[-1], "photo")
    return None


def download_tg_file(source, chat_id, status_msg_id, task_id, control):
    lang = control.lang
    file_id = source["file_id"]
    file_name = source["file_name"]
    expected_size = source.get("file_size", 0)

    result = tg_call("getFile", file_id=file_id)
    file_path_tg = result["file_path"]
    download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path_tg}"

    safe_name = os.path.basename(file_name) or f"tg_{file_id[-12:]}"
    local_path = os.path.join(DOWNLOAD_DIR, safe_name)
    if os.path.exists(local_path):
        base, ext = os.path.splitext(safe_name)
        local_path = os.path.join(DOWNLOAD_DIR, f"{base}_{uuid.uuid4().hex[:4]}{ext}")

    last_edit_time = 0
    last_done = 0
    last_time = time.time()
    stage_start = time.time()
    paused_shown = False

    resp = requests.get(download_url, stream=True, timeout=(30, 60))
    try:
        if resp.status_code >= 400:
            raise RuntimeError(f"Telegram file download failed (HTTP {resp.status_code}): {resp.text[:300]}")
        total = int(resp.headers.get("Content-Length", 0)) or expected_size
        with open(local_path, "wb") as f:
            done = 0
            for chunk in resp.iter_content(131072):
                if control.stopped.is_set():
                    raise TaskStopped()

                if not control.running.is_set():
                    if not paused_shown:
                        text = render_progress("⬇️", t(lang, "downloading_title"), done, total, 0,
                                                time.time() - stage_start, lang, paused=True)
                        edit_message(chat_id, status_msg_id, text,
                                     reply_markup=build_progress_keyboard(task_id, control))
                        paused_shown = True
                    control.running.wait(timeout=1)
                    continue
                else:
                    paused_shown = False

                f.write(chunk)
                done += len(chunk)

                now = time.time()
                if now - last_edit_time >= 3:
                    dt = now - last_time
                    speed = (done - last_done) / dt if dt > 0 else 0
                    text = render_progress("⬇️", t(lang, "downloading_title"), done, total, speed,
                                            now - stage_start, lang)
                    edit_message(chat_id, status_msg_id, text,
                                 reply_markup=build_progress_keyboard(task_id, control))
                    last_edit_time = now
                    last_done = done
                    last_time = now
    except BaseException:
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
            except OSError:
                pass
        raise
    finally:
        resp.close()

    return local_path


# ---------------------------------------------------------------------------
# Общая обёртка для чтения файла с поддержкой паузы/стопа (используется PUT-загрузками)
# ---------------------------------------------------------------------------

class ControlledFileReader:
    def __init__(self, path, control, on_progress):
        self._f = open(path, "rb")
        self.size = os.fstat(self._f.fileno()).st_size
        self._read_bytes = 0
        self.control = control
        self.on_progress = on_progress
        self._paused_shown = False

    def __len__(self):
        return self.size

    def read(self, size=-1):
        if self.control.stopped.is_set():
            raise TaskStopped()

        if not self.control.running.is_set():
            if not self._paused_shown:
                self.on_progress(self._read_bytes, self.size, 0, paused=True)
                self._paused_shown = True
            self.control.running.wait()
            self._paused_shown = False
            if self.control.stopped.is_set():
                raise TaskStopped()

        chunk = self._f.read(size)
        self._read_bytes += len(chunk)
        self.on_progress(self._read_bytes, self.size, 0, paused=False)
        return chunk

    def close(self):
        self._f.close()


# ---------------------------------------------------------------------------
# Загрузка на VikingFile
# ---------------------------------------------------------------------------

def get_vikingfile_upload_server():
    resp = requests.get("https://vikingfile.com/api/get-server", timeout=20)
    resp.raise_for_status()
    data = resp.json()
    server = data.get("server")
    if not server:
        raise RuntimeError(f"VikingFile did not return an upload server: {data}")
    return server


def upload_to_vikingfile_once(file_path, chat_id, status_msg_id, task_id, control, stage_start):
    lang = control.lang
    server_url = get_vikingfile_upload_server()
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)

    fh = open(file_path, "rb")
    encoder = MultipartEncoder(fields={
        "file": (file_name, fh, "application/octet-stream"),
        "user": VIKINGFILE_USER_HASH,
    })

    state = {"last_edit": 0, "last_done": 0, "last_time": time.time(), "paused_shown": False}

    def progress_callback(monitor):
        if control.stopped.is_set():
            raise TaskStopped()

        if not control.running.is_set():
            if not state["paused_shown"]:
                text = render_progress("⬆️", t(lang, "uploading_title", host="VikingFile"),
                                        monitor.bytes_read, monitor.len, 0,
                                        time.time() - stage_start, lang, paused=True)
                edit_message(chat_id, status_msg_id, text, reply_markup=build_progress_keyboard(task_id, control))
                state["paused_shown"] = True
            control.running.wait()
            state["paused_shown"] = False
            if control.stopped.is_set():
                raise TaskStopped()

        now = time.time()
        if now - state["last_edit"] < 3:
            return
        done = monitor.bytes_read
        dt = now - state["last_time"]
        speed = (done - state["last_done"]) / dt if dt > 0 else 0
        text = render_progress("⬆️", t(lang, "uploading_title", host="VikingFile"), done, monitor.len, speed,
                                now - stage_start, lang)
        edit_message(chat_id, status_msg_id, text, reply_markup=build_progress_keyboard(task_id, control))
        state["last_edit"] = now
        state["last_done"] = done
        state["last_time"] = now

    monitor = MultipartEncoderMonitor(encoder, progress_callback)

    try:
        resp = requests.post(
            server_url, data=monitor,
            headers={"Content-Type": monitor.content_type},
            timeout=(30, None),
        )
    finally:
        fh.close()

    if resp.status_code >= 400:
        raise RuntimeError(t(lang, "host_http_error", host="VikingFile", code=resp.status_code, body=resp.text[:300]))

    data = resp.json()
    if not data.get("url"):
        raise RuntimeError(f"VikingFile error: {data}")

    return {"name": data.get("name", file_name), "size": data.get("size", file_size), "url": data["url"]}


# ---------------------------------------------------------------------------
# Загрузка на Pixeldrain
# ---------------------------------------------------------------------------

def upload_to_pixeldrain_once(file_path, chat_id, status_msg_id, task_id, control, stage_start):
    lang = control.lang
    if not PIXELDRAIN_API_KEY:
        raise RuntimeError(t(lang, "pixeldrain_key_missing"))

    file_name = os.path.basename(file_path)
    encoded_name = quote(file_name)

    state = {"last_edit": 0, "last_done": 0, "last_time": time.time()}

    def on_progress(done, total, _speed, paused=False):
        now = time.time()
        if not paused and now - state["last_edit"] < 3:
            return
        dt = now - state["last_time"]
        speed = (done - state["last_done"]) / dt if (dt > 0 and not paused) else 0
        text = render_progress("⬆️", t(lang, "uploading_title", host="Pixeldrain"), done, total, speed,
                                now - stage_start, lang, paused=paused)
        edit_message(chat_id, status_msg_id, text, reply_markup=build_progress_keyboard(task_id, control))
        state["last_edit"] = now
        state["last_done"] = done
        state["last_time"] = now

    reader = ControlledFileReader(file_path, control, on_progress)
    try:
        resp = requests.put(
            f"https://pixeldrain.com/api/file/{encoded_name}",
            data=reader,
            headers={"Content-Length": str(len(reader))},
            auth=("", PIXELDRAIN_API_KEY),
            timeout=(30, None),
        )
    finally:
        reader.close()

    if resp.status_code >= 400:
        raise RuntimeError(t(lang, "host_http_error", host="Pixeldrain", code=resp.status_code, body=resp.text[:300]))

    data = resp.json()
    file_id = data.get("id")
    if not file_id:
        raise RuntimeError(f"Pixeldrain error: {data}")

    return {
        "name": file_name,
        "size": os.path.getsize(file_path),
        "url": f"https://pixeldrain.com/u/{file_id}",
    }


# ---------------------------------------------------------------------------
# Загрузка на FuckingFast / BuzzHeavier
# ---------------------------------------------------------------------------
# Обе платформы используют одинаковый API: анонимный PUT с телом файла на
# поддомен w.<домен>/{имя_файла}. Ссылка на результат может прийти как:
#  - заголовок Location (частый случай для HTTP 201 Created);
#  - JSON с полем url/link/...;
#  - обычный текст со ссылкой в теле ответа (см. официальные curl-примеры,
#    где тело просто печатают в stdout через `| cat`).

def upload_to_wstyle_once(domain, display_name, token, file_path, chat_id, status_msg_id, task_id, control, stage_start):
    lang = control.lang
    file_name = os.path.basename(file_path)
    upload_url = f"https://w.{domain}/{quote(file_name)}"

    state = {"last_edit": 0, "last_done": 0, "last_time": time.time()}

    def on_progress(done, total, _speed, paused=False):
        now = time.time()
        if not paused and now - state["last_edit"] < 3:
            return
        dt = now - state["last_time"]
        speed = (done - state["last_done"]) / dt if (dt > 0 and not paused) else 0
        text = render_progress("⬆️", t(lang, "uploading_title", host=display_name), done, total, speed,
                                now - stage_start, lang, paused=paused)
        edit_message(chat_id, status_msg_id, text, reply_markup=build_progress_keyboard(task_id, control))
        state["last_edit"] = now
        state["last_done"] = done
        state["last_time"] = now

    reader = ControlledFileReader(file_path, control, on_progress)
    headers = {"Content-Length": str(len(reader))}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.put(upload_url, data=reader, headers=headers, timeout=(30, None))
    finally:
        reader.close()

    if resp.status_code >= 400:
        raise RuntimeError(t(lang, "host_http_error", host=display_name, code=resp.status_code, body=resp.text[:300]))

    link = None

    # Основной формат ответа этой платформы: {"data": {"id": "..."}} -
    # ссылку нужно собрать самим как https://<домен>/<id>. На всякий случай
    # проверяем и другие возможные формы ответа (плоский id, явное поле url,
    # заголовок Location, голый текст в теле).
    try:
        data = resp.json()
        if isinstance(data, dict):
            file_id = (data.get("data") or {}).get("id") or data.get("id")
            link = (
                data.get("url") or data.get("link") or data.get("downloadUrl")
                or data.get("downloadPage") or data.get("href")
            )
            if not link and file_id:
                link = f"https://{domain}/{file_id}"
    except ValueError:
        pass

    if not link:
        location = resp.headers.get("Location")
        if location:
            if location.startswith("http"):
                link = location
            else:
                link = urljoin(f"https://{domain}/", location)

    if not link:
        text = resp.text.strip()
        if text.startswith("http"):
            link = text

    if not link:
        raise RuntimeError(t(lang, "host_link_missing", host=display_name, code=resp.status_code,
                             body=resp.text[:300] or "<empty>"))

    return {"name": file_name, "size": os.path.getsize(file_path), "url": link}


def upload_to_fuckingfast_once(file_path, chat_id, status_msg_id, task_id, control, stage_start):
    return upload_to_wstyle_once(
        "fuckingfast.net", "FuckingFast", FUCKINGFAST_TOKEN,
        file_path, chat_id, status_msg_id, task_id, control, stage_start,
    )


def upload_to_buzzheavier_once(file_path, chat_id, status_msg_id, task_id, control, stage_start):
    return upload_to_wstyle_once(
        "buzzheavier.com", "BuzzHeavier", BUZZHEAVIER_TOKEN,
        file_path, chat_id, status_msg_id, task_id, control, stage_start,
    )


# ---------------------------------------------------------------------------
# Загрузка на Gofile
# ---------------------------------------------------------------------------

def get_gofile_server():
    resp = requests.get("https://api.gofile.io/servers", timeout=20)
    resp.raise_for_status()
    data = resp.json()
    servers = (data.get("data") or {}).get("servers") or []
    if not servers:
        raise RuntimeError(f"Gofile did not return a server list: {data}")
    return servers[0]["name"]


def upload_to_gofile_once(file_path, chat_id, status_msg_id, task_id, control, stage_start):
    lang = control.lang
    server = get_gofile_server()
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)

    fh = open(file_path, "rb")
    fields = {"file": (file_name, fh, "application/octet-stream")}
    if GOFILE_API_TOKEN:
        fields["token"] = GOFILE_API_TOKEN

    encoder = MultipartEncoder(fields=fields)
    state = {"last_edit": 0, "last_done": 0, "last_time": time.time(), "paused_shown": False}

    def progress_callback(monitor):
        if control.stopped.is_set():
            raise TaskStopped()

        if not control.running.is_set():
            if not state["paused_shown"]:
                text = render_progress("⬆️", t(lang, "uploading_title", host="Gofile"),
                                        monitor.bytes_read, monitor.len, 0,
                                        time.time() - stage_start, lang, paused=True)
                edit_message(chat_id, status_msg_id, text, reply_markup=build_progress_keyboard(task_id, control))
                state["paused_shown"] = True
            control.running.wait()
            state["paused_shown"] = False
            if control.stopped.is_set():
                raise TaskStopped()

        now = time.time()
        if now - state["last_edit"] < 3:
            return
        done = monitor.bytes_read
        dt = now - state["last_time"]
        speed = (done - state["last_done"]) / dt if dt > 0 else 0
        text = render_progress("⬆️", t(lang, "uploading_title", host="Gofile"), done, monitor.len, speed,
                                now - stage_start, lang)
        edit_message(chat_id, status_msg_id, text, reply_markup=build_progress_keyboard(task_id, control))
        state["last_edit"] = now
        state["last_done"] = done
        state["last_time"] = now

    monitor = MultipartEncoderMonitor(encoder, progress_callback)

    try:
        resp = requests.post(
            f"https://{server}.gofile.io/contents/uploadfile", data=monitor,
            headers={"Content-Type": monitor.content_type},
            timeout=(30, None),
        )
    finally:
        fh.close()

    if resp.status_code >= 400:
        raise RuntimeError(t(lang, "host_http_error", host="Gofile", code=resp.status_code, body=resp.text[:300]))

    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"Gofile error: {data}")

    d = data["data"]
    download_page = d.get("downloadPage") or d.get("downloadLink") or d.get("url")
    if not download_page:
        raise RuntimeError(f"Gofile error: no download link in response: {data}")
    return {"name": d.get("fileName", file_name), "size": file_size, "url": download_page}


# ---------------------------------------------------------------------------
# Общий диспетчер загрузки с ретраями
# ---------------------------------------------------------------------------

UPLOAD_FUNCS = {
    "vikingfile": upload_to_vikingfile_once,
    "pixeldrain": upload_to_pixeldrain_once,
    "fuckingfast": upload_to_fuckingfast_once,
    "buzzheavier": upload_to_buzzheavier_once,
    "gofile": upload_to_gofile_once,
}


def upload_file(dest, file_path, chat_id, status_msg_id, task_id, control):
    lang = control.lang
    stage_start = time.time()
    upload_once = UPLOAD_FUNCS[dest]
    last_error = None
    for attempt in range(1, UPLOAD_RETRIES + 1):
        try:
            return upload_once(file_path, chat_id, status_msg_id, task_id, control, stage_start)
        except TaskStopped:
            raise
        except (requests.RequestException, RuntimeError) as e:
            last_error = e
            if attempt < UPLOAD_RETRIES:
                wait_s = min(60, 8 * attempt)
                edit_message(
                    chat_id, status_msg_id,
                    t(lang, "retry", attempt=attempt, total=UPLOAD_RETRIES, wait=wait_s,
                      err=html_escape(str(e))[:200]),
                    reply_markup=build_progress_keyboard(task_id, control),
                )
                time.sleep(wait_s)

    hint = t(lang, "pixeldrain_hint") if dest == "pixeldrain" else ""
    raise RuntimeError(t(lang, "upload_failed_final", total=UPLOAD_RETRIES, err=last_error, hint=hint))


# ---------------------------------------------------------------------------
# Обработка задачи целиком (в отдельном потоке)
# ---------------------------------------------------------------------------

def task_worker(chat_id, url, dest, status_msg_id, task_id, control):
    lang = control.lang
    task_start = time.time()
    file_path = None
    try:
        edit_message(
            chat_id, status_msg_id,
            t(lang, "target_start", host=DESTINATIONS[dest], url=html_escape(url)),
            reply_markup=build_progress_keyboard(task_id, control),
        )

        file_path = download_with_progress(url, chat_id, status_msg_id, task_id, control)
        size = os.path.getsize(file_path)

        edit_message(
            chat_id, status_msg_id,
            t(lang, "download_done", name=html_escape(os.path.basename(file_path)),
              size=human_size(size, lang), host=DESTINATIONS[dest]),
            reply_markup=build_progress_keyboard(task_id, control),
        )

        result = upload_file(dest, file_path, chat_id, status_msg_id, task_id, control)
        total_time = time.time() - task_start

        edit_message(
            chat_id, status_msg_id,
            t(lang, "done", div=divider(), name=html_escape(result["name"]),
              size=human_size(result["size"], lang), host=DESTINATIONS[dest],
              time=human_time(total_time, lang), url=result["url"]),
        )

    except TaskStopped:
        edit_message(chat_id, status_msg_id, t(lang, "stopped"))
    except Exception as e:
        edit_message(chat_id, status_msg_id, t(lang, "error", err=html_escape(str(e))[:500]))
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass
        active_controls.pop(chat_id, None)


def render_overview(lang, dest_list, results, in_progress=None, stopped=False):
    lines = [t(lang, "overview_title", div=divider())]
    for dest in dest_list:
        host = DESTINATIONS[dest]
        if dest in results:
            status, val = results[dest]
            if status == "ok":
                lines.append(t(lang, "overview_row_ok", host=host, url=val["url"]))
            else:
                lines.append(t(lang, "overview_row_fail", host=host, err=val))
        elif dest == in_progress:
            lines.append(t(lang, "overview_row_pending", icon="🔄", host=host, status=t(lang, "in_progress_label")))
        elif stopped:
            lines.append(t(lang, "overview_row_pending", icon="⏹", host=host, status=t(lang, "cancelled_label")))
        else:
            lines.append(t(lang, "overview_row_pending", icon="⏳", host=host, status=t(lang, "pending_label")))
    return "\n".join(lines)


def multi_task_worker(chat_id, source, dest_list, overview_msg_id, progress_msg_id, task_id, control):
    lang = control.lang
    file_path = None
    results = {}
    try:
        if source["type"] == "url":
            file_path = download_with_progress(source["url"], chat_id, progress_msg_id, task_id, control)
        else:
            file_path = download_tg_file(source, chat_id, progress_msg_id, task_id, control)
        size = os.path.getsize(file_path)

        edit_message(
            chat_id, progress_msg_id,
            t(lang, "multi_download_done", name=html_escape(os.path.basename(file_path)),
              size=human_size(size, lang), n=len(dest_list)),
            reply_markup=build_progress_keyboard(task_id, control),
        )

        for dest in dest_list:
            if control.stopped.is_set():
                raise TaskStopped()

            edit_message(chat_id, overview_msg_id, render_overview(lang, dest_list, results, in_progress=dest))

            try:
                result = upload_file(dest, file_path, chat_id, progress_msg_id, task_id, control)
                results[dest] = ("ok", result)
            except TaskStopped:
                raise
            except Exception as e:
                results[dest] = ("error", html_escape(str(e))[:200])

            edit_message(chat_id, overview_msg_id, render_overview(lang, dest_list, results, in_progress=None))

        edit_message(chat_id, progress_msg_id, t(lang, "multi_all_done"), reply_markup={"inline_keyboard": []})

    except TaskStopped:
        edit_message(chat_id, progress_msg_id, t(lang, "stopped"), reply_markup={"inline_keyboard": []})
        edit_message(chat_id, overview_msg_id, render_overview(lang, dest_list, results, in_progress=None, stopped=True))
    except Exception as e:
        edit_message(chat_id, progress_msg_id, t(lang, "error", err=html_escape(str(e))[:500]),
                     reply_markup={"inline_keyboard": []})
        edit_message(chat_id, overview_msg_id, render_overview(lang, dest_list, results, in_progress=None, stopped=True))
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass
        active_controls.pop(chat_id, None)


# ---------------------------------------------------------------------------
# Speedtest
# ---------------------------------------------------------------------------

def generate_speedtest_file():
    path = os.path.join(DOWNLOAD_DIR, "speedtest_payload.bin")
    size = SPEEDTEST_SIZE_MB * 1024 * 1024
    chunk = os.urandom(1024 * 1024)
    with open(path, "wb") as f:
        written = 0
        while written < size:
            f.write(chunk)
            written += len(chunk)
    return path, size


def speedtest_worker(chat_id, status_msg_id, task_id, control):
    lang = control.lang
    file_path, size = generate_speedtest_file()
    results = []

    try:
        hosts = list(UPLOAD_FUNCS.keys())
        for i, dest in enumerate(hosts, 1):
            if control.stopped.is_set():
                break

            if dest == "pixeldrain" and not PIXELDRAIN_API_KEY:
                results.append((dest, None, None, t(lang, "speedtest_skip_no_key")))
                continue

            edit_message(chat_id, status_msg_id,
                         t(lang, "speedtest_progress", host=DESTINATIONS[dest], i=i, n=len(hosts)))
            upload_once = UPLOAD_FUNCS[dest]
            start = time.time()
            try:
                upload_once(file_path, chat_id, status_msg_id, task_id, control, start)
                elapsed = time.time() - start
                speed = size / elapsed if elapsed > 0 else 0
                results.append((dest, speed, elapsed, None))
            except TaskStopped:
                raise
            except Exception as e:
                results.append((dest, None, None, html_escape(str(e))[:150]))
    except TaskStopped:
        edit_message(chat_id, status_msg_id, t(lang, "stopped"))
        active_controls.pop(chat_id, None)
        if os.path.exists(file_path):
            os.remove(file_path)
        return
    finally:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass

    ranked = sorted(results, key=lambda r: (r[1] is None, -(r[1] or 0)))
    medals = ["🥇", "🥈", "🥉"]
    lines = [t(lang, "speedtest_title", size=human_size(size, lang), div=divider())]
    for idx, (dest, speed, elapsed, err) in enumerate(ranked):
        host = DESTINATIONS[dest]
        if speed is not None:
            medal = medals[idx] if idx < len(medals) else "▪️"
            lines.append(t(lang, "speedtest_row_ok", medal=medal, host=host,
                            speed=human_size(speed, lang), time=human_time(elapsed, lang)))
        else:
            lines.append(t(lang, "speedtest_row_fail", host=host, err=err))

    edit_message(chat_id, status_msg_id, "\n".join(lines))
    active_controls.pop(chat_id, None)


# ---------------------------------------------------------------------------
# Обработка сообщений и нажатий кнопок
# ---------------------------------------------------------------------------

def handle_message(message):
    global last_activity_time

    chat_id = str(message["chat"]["id"])
    text = (message.get("text") or "").strip()
    lang = get_lang(chat_id, message.get("from"))

    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        send_message(chat_id, t(lang, "no_access"))
        return

    last_activity_time = time.time()

    if text in ("/start", "/help"):
        send_message(chat_id, t(lang, "start", bot_name=BOT_DISPLAY_NAME))
        return

    if text == "/language":
        send_message(chat_id, t(lang, "language_prompt"), reply_markup=build_language_keyboard())
        return

    if text == "/stop":
        control = active_controls.get(chat_id)
        if control:
            control.stopped.set()
            control.running.set()
            send_message(chat_id, t(lang, "stop_task"))
        else:
            send_message(chat_id, t(lang, "stop_no_task"))
        return

    if text == "/shutdown":
        send_message(chat_id, t(lang, "shutdown"))
        should_stop_bot.set()
        return

    if text == "/speedtest":
        if chat_id in active_controls:
            send_message(chat_id, t(lang, "speedtest_busy"))
            return

        task_id = uuid.uuid4().hex[:8]
        control = TaskControl(task_id, lang=lang)
        active_controls[chat_id] = control

        status = send_message(
            chat_id,
            t(lang, "speedtest_start", n=len(UPLOAD_FUNCS), size=human_size(SPEEDTEST_SIZE_MB * 1024 * 1024, lang)),
        )
        thread = threading.Thread(
            target=speedtest_worker,
            args=(chat_id, status["message_id"], task_id, control),
            daemon=True,
        )
        thread.start()
        return

    if chat_id in active_controls:
        send_message(chat_id, t(lang, "busy"))
        return

    tg_file = extract_tg_file(message)
    if tg_file:
        file_size = tg_file.get("file_size", 0)
        if file_size > TG_FILE_MAX_SIZE:
            send_message(chat_id, t(lang, "tg_file_too_large", limit=human_size(TG_FILE_MAX_SIZE, lang)))
            return

        token = uuid.uuid4().hex[:8]
        size_bytes = file_size if file_size else None
        size_line = t(lang, "size_line", size=human_size(size_bytes, lang)) if size_bytes else ""
        display_name = html_escape(tg_file["file_name"])

        selected = set()
        status = send_message(
            chat_id,
            t(lang, "file_received", url=display_name, size_line=size_line),
            reply_markup=build_multiselect_keyboard(token, selected, size_bytes, lang),
        )
        pending_selections[chat_id] = {
            "token": token, "url": tg_file["file_name"], "message_id": status["message_id"],
            "selected": selected, "size": size_bytes, "size_line": size_line,
            "text_key": "file_received",
            "source": {"type": "tg", "file_id": tg_file["file_id"],
                       "file_name": tg_file["file_name"], "file_size": file_size},
        }
        return

    search_text = text or (message.get("caption") or "").strip()
    match = URL_RE.search(search_text)
    if not match:
        send_message(chat_id, t(lang, "ask_link"))
        return

    url = match.group(0)
    token = uuid.uuid4().hex[:8]

    size_bytes = get_remote_file_size(url)
    size_line = t(lang, "size_line", size=human_size(size_bytes, lang)) if size_bytes else ""

    selected = set()
    status = send_message(
        chat_id,
        t(lang, "link_received", url=html_escape(url), size_line=size_line),
        reply_markup=build_multiselect_keyboard(token, selected, size_bytes, lang),
    )
    pending_selections[chat_id] = {
        "token": token, "url": url, "message_id": status["message_id"],
        "selected": selected, "size": size_bytes, "size_line": size_line,
        "text_key": "link_received",
        "source": {"type": "url", "url": url},
    }


def get_remote_file_size(url):
    try:
        resp = requests.head(url, allow_redirects=True, timeout=10)
        size = resp.headers.get("Content-Length")
        if resp.status_code < 400 and size:
            return int(size)
    except requests.RequestException:
        pass

    try:
        with requests.get(url, stream=True, timeout=10) as resp:
            size = resp.headers.get("Content-Length")
            if resp.status_code < 400 and size:
                return int(size)
    except requests.RequestException:
        pass

    return None


def handle_callback_query(cq):
    global last_activity_time

    data = cq.get("data", "")
    chat_id = str(cq["message"]["chat"]["id"])
    message_id = cq["message"]["message_id"]
    lang = get_lang(chat_id, cq.get("from"))

    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        answer_callback(cq["id"], t(lang, "no_access"), show_alert=True)
        return

    last_activity_time = time.time()
    parts = data.split("|")
    action = parts[0]

    if action == "lang":
        new_lang = parts[1] if parts[1] in TEXTS else DEFAULT_LANG
        user_lang[chat_id] = new_lang
        answer_callback(cq["id"], t(new_lang, "language_set"))
        edit_message(chat_id, message_id, t(new_lang, "language_set"))
        return

    if action == "toolarge":
        dest = parts[1]
        answer_callback(
            cq["id"],
            t(lang, "toolarge_alert", limit=human_size(PIXELDRAIN_MAX_SIZE_BYTES, lang), host=DESTINATIONS.get(dest, dest)),
            show_alert=True,
        )
        return

    if action == "select":
        dest, token = parts[1], parts[2]
        pending = pending_selections.get(chat_id)
        if not pending or pending["token"] != token:
            answer_callback(cq["id"], t(lang, "stale_button"), show_alert=True)
            return

        if dest == "pixeldrain" and not PIXELDRAIN_API_KEY:
            answer_callback(cq["id"], t(lang, "pixeldrain_unavailable"), show_alert=True)
            return

        selected = pending["selected"]
        if dest in selected:
            selected.discard(dest)
        else:
            selected.add(dest)

        answer_callback(cq["id"])
        edit_message(
            chat_id, message_id,
            t(lang, pending.get("text_key", "link_received"), url=html_escape(pending["url"]), size_line=pending["size_line"]),
            reply_markup=build_multiselect_keyboard(token, selected, pending["size"], lang),
        )
        return

    if action == "selectall":
        token = parts[1]
        pending = pending_selections.get(chat_id)
        if not pending or pending["token"] != token:
            answer_callback(cq["id"], t(lang, "stale_button"), show_alert=True)
            return

        size_bytes = pending["size"]
        pixeldrain_too_large = size_bytes is not None and size_bytes > PIXELDRAIN_MAX_SIZE_BYTES
        selectable = [
            d for d in DESTINATIONS
            if not (d == "pixeldrain" and (pixeldrain_too_large or not PIXELDRAIN_API_KEY))
        ]

        selected = pending["selected"]
        if selected and all(d in selected for d in selectable):
            selected.clear()
        else:
            selected.clear()
            selected.update(selectable)

        answer_callback(cq["id"])
        edit_message(
            chat_id, message_id,
            t(lang, pending.get("text_key", "link_received"), url=html_escape(pending["url"]), size_line=pending["size_line"]),
            reply_markup=build_multiselect_keyboard(token, selected, size_bytes, lang),
        )
        return

    if action == "confirm":
        token = parts[1]
        pending = pending_selections.get(chat_id)
        if not pending or pending["token"] != token:
            answer_callback(cq["id"], t(lang, "stale_button"), show_alert=True)
            return

        selected = pending["selected"]
        if not selected:
            answer_callback(cq["id"], t(lang, "select_at_least_one"), show_alert=True)
            return

        del pending_selections[chat_id]
        answer_callback(cq["id"])

        dest_list = [d for d in DESTINATIONS if d in selected]

        task_id = uuid.uuid4().hex[:8]
        control = TaskControl(task_id, lang=lang)
        active_controls[chat_id] = control

        overview_text = render_overview(lang, dest_list, {}, in_progress=None)
        edit_message(chat_id, message_id, overview_text, reply_markup={"inline_keyboard": []})

        progress_status = send_message(chat_id, t(lang, "target_start",
                                                    host=", ".join(DESTINATIONS[d] for d in dest_list),
                                                    url=html_escape(pending["url"])))

        thread = threading.Thread(
            target=multi_task_worker,
            args=(chat_id, pending["source"], dest_list, message_id, progress_status["message_id"], task_id, control),
            daemon=True,
        )
        thread.start()
        return

    if action in ("toggle", "stop"):
        control = active_controls.get(chat_id)
        if not control or control.task_id != parts[1]:
            answer_callback(cq["id"], t(lang, "task_finished_alert"), show_alert=True)
            return

        if action == "toggle":
            if control.running.is_set():
                control.running.clear()
                answer_callback(cq["id"], t(control.lang, "pause_alert"))
            else:
                control.running.set()
                answer_callback(cq["id"], t(control.lang, "resume_alert"))
        elif action == "stop":
            control.stopped.set()
            control.running.set()
            answer_callback(cq["id"], t(control.lang, "stopping_alert"))
        return

    answer_callback(cq["id"])


def handle_update(update):
    if "message" in update:
        handle_message(update["message"])
    elif "callback_query" in update:
        handle_callback_query(update["callback_query"])


# ---------------------------------------------------------------------------
# Главный цикл
# ---------------------------------------------------------------------------

def main():
    if not ALLOWED_CHAT_IDS:
        print(
            "WARNING: TELEGRAM_ALLOWED_CHAT_IDS is not set. "
            "The bot will respond to anyone who finds it.",
            file=sys.stderr,
        )
    if not PIXELDRAIN_API_KEY:
        print("PIXELDRAIN_API_KEY is not set - the Pixeldrain option will be unavailable.", file=sys.stderr)

    try:
        tg_call("setMyName", name=BOT_DISPLAY_NAME)
    except Exception as e:
        print(f"Could not set bot display name (not critical): {e}", file=sys.stderr)

    try:
        tg_call("deleteWebhook", drop_pending_updates="false")
    except Exception as e:
        print(f"Could not delete webhook (not critical): {e}", file=sys.stderr)

    print("Starting aria2c...")
    start_aria2c()

    def handle_sigterm(signum, frame):
        should_stop_bot.set()

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    offset = 0
    print(f"{BOT_DISPLAY_NAME} is running, waiting for Telegram updates...")

    try:
        while not should_stop_bot.is_set():
            if time.time() - start_time > HARD_TIMEOUT:
                print("Reached overall time limit, shutting down.")
                break
            if time.time() - last_activity_time > IDLE_TIMEOUT:
                print("No activity for too long, shutting down.")
                break

            try:
                updates = get_updates(offset)
            except Exception as e:
                if "Conflict" in str(e) or "terminated by other" in str(e):
                    print(f"Conflict with another running bot instance (previous run may still be "
                          f"shutting down): {e}. Retrying in 5s.")
                else:
                    print(f"Error while polling Telegram: {e}, retrying in 5s")
                time.sleep(5)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                except Exception as e:
                    print(f"Error handling update: {e}")

    finally:
        for control in list(active_controls.values()):
            control.stopped.set()
            control.running.set()
        stop_aria2c()
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

    print("Bot stopped.")


if __name__ == "__main__":
    main()
