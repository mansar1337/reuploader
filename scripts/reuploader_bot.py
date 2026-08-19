#!/usr/bin/env python3
"""
Telegram-бот для перезалива файлов на VikingFile или Pixeldrain.

Логика:
  1. Бот поднимает aria2c в режиме RPC (для получения реального прогресса скачивания).
  2. Бот слушает Telegram через long polling (getUpdates), включая нажатия кнопок.
  3. Пользователь присылает боту ссылку на файл.
  4. Бот предлагает выбрать хостинг для перезалива (VikingFile / Pixeldrain) кнопками.
  5. Бот скачивает файл через aria2c, показывая прогресс с кнопками "Пауза" / "Стоп".
  6. Бот заливает файл на выбранный хостинг, тоже с прогрессом и кнопками управления.
  7. Бот присылает финальную ссылку на файл.

Пауза на скачивании — настоящая (aria2c реально приостанавливает соединение).
Пауза на загрузке — "мягкая": поток чтения файла блокируется, HTTP-соединение
при этом может провиснуть; если хостинг оборвёт его при долгой паузе, бот
предложит повторить загрузку.

Если новых сообщений нет дольше IDLE_TIMEOUT минут — бот завершает работу.
Команда /stop в чате завершает работу бота целиком (не путать с кнопкой "Стоп",
которая останавливает только текущую задачу).
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
from html import escape as html_escape
from urllib.parse import quote

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

# ---------------------------------------------------------------------------
# Конфигурация из переменных окружения
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
VIKINGFILE_USER_HASH = os.environ.get("VIKINGFILE_USER_HASH", "")   # пусто = анонимная загрузка
PIXELDRAIN_API_KEY = os.environ.get("PIXELDRAIN_API_KEY", "")       # нужен только если выбран Pixeldrain
FUCKINGFAST_TOKEN = os.environ.get("FUCKINGFAST_TOKEN", "")         # опционально, для привязки к аккаунту
BUZZHEAVIER_TOKEN = os.environ.get("BUZZHEAVIER_TOKEN", "")         # опционально, для привязки к аккаунту
GOFILE_API_TOKEN = os.environ.get("GOFILE_API_TOKEN", "")           # опционально, иначе гостевая загрузка

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

# Ожидающие выбора хостинга: chat_id -> {"token", "url", "message_id"}
pending_selections = {}
# Активные задачи: chat_id -> TaskControl
active_controls = {}


class TaskStopped(Exception):
    """Пользователь остановил задачу кнопкой Стоп."""


class TaskControl:
    def __init__(self, task_id):
        self.task_id = task_id
        self.stopped = threading.Event()
        self.running = threading.Event()
        self.running.set()   # изначально не на паузе
        self.gid = None       # gid текущей задачи aria2 (на этапе скачивания)


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def tg_call(method, **params):
    resp = requests.post(f"{TELEGRAM_API}/{method}", data=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}: {data}")
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
        pass  # callback мог устареть - не критично


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
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error in getUpdates: {data}")
    return data["result"]


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

def get_remote_file_size(url):
    """Пытается узнать размер файла по заголовкам, не скачивая его. Возвращает
    размер в байтах или None, если узнать не удалось (не критично)."""
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


def build_target_keyboard(token, file_size=None):
    row1 = [{"text": "🦁 VikingFile", "callback_data": f"target|vikingfile|{token}"}]

    pixeldrain_too_large = (
        file_size is not None
        and file_size > PIXELDRAIN_MAX_SIZE_BYTES
    )
    if pixeldrain_too_large:
        row1.append({
            "text": f"🟢 Pixeldrain ⛔ >{human_size(PIXELDRAIN_MAX_SIZE_BYTES)}",
            "callback_data": f"toolarge|pixeldrain|{token}",
        })
    else:
        row1.append({"text": "🟢 Pixeldrain", "callback_data": f"target|pixeldrain|{token}"})

    row1.append({"text": "⚡ FuckingFast", "callback_data": f"target|fuckingfast|{token}"})

    row2 = [
        {"text": "🐝 BuzzHeavier", "callback_data": f"target|buzzheavier|{token}"},
        {"text": "📁 Gofile", "callback_data": f"target|gofile|{token}"},
    ]

    return {"inline_keyboard": [row1, row2]}


def build_progress_keyboard(task_id, control):
    toggle_label = "⏸ Пауза" if control.running.is_set() else "▶️ Продолжить"
    return {
        "inline_keyboard": [[
            {"text": toggle_label, "callback_data": f"toggle|{task_id}"},
            {"text": "⏹ Стоп", "callback_data": f"stop|{task_id}"},
        ]]
    }


# ---------------------------------------------------------------------------
# Визуал: прогресс-бары, форматирование
# ---------------------------------------------------------------------------

def human_size(n):
    n = float(n)
    for unit in ["Б", "КБ", "МБ", "ГБ", "ТБ"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ПБ"


def human_time(seconds):
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
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


def render_progress(icon, title, done, total, speed_bps, elapsed, paused=False):
    if total > 0:
        frac = done / total
        pct = frac * 100
        eta = (total - done) / speed_bps if speed_bps > 0 else None
    else:
        frac = 0
        pct = 0
        eta = None

    bar = progress_bar(frac)
    speed_str = f"{human_size(speed_bps)}/с" if speed_bps else "—"
    eta_str = human_time(eta) if eta is not None else "—"

    status_line = "⏸ <b>На паузе</b>" if paused else f"{icon} <b>{title}</b>"

    lines = [
        status_line,
        "",
        f"<code>[{bar}] {pct:5.1f}%</code>",
        f"📦 {human_size(done)} / {human_size(total) if total else '?'}",
        f"⚡ {speed_str}   ⏳ ETA: {eta_str}",
        f"🕐 Прошло: {human_time(elapsed)}",
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
    raise RuntimeError("Не удалось поднять aria2c RPC")


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
    """Скачивает файл через aria2c с поддержкой паузы/стопа. Возвращает путь к файлу."""
    gid = aria2_rpc("aria2.addUri", [[url]])
    control.gid = gid

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
                    text = render_progress("⬇️", "Скачивание файла", done, total, 0, time.time() - stage_start, paused=True)
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
                raise RuntimeError(f"Ошибка aria2c: {status.get('errorMessage', 'неизвестная ошибка')}")

            now = time.time()
            if now - last_edit_time >= 3:
                text = render_progress("⬇️", "Скачивание файла", done, total, speed, now - stage_start)
                if text != last_edit_text:
                    edit_message(chat_id, status_msg_id, text, reply_markup=build_progress_keyboard(task_id, control))
                    last_edit_text = text
                last_edit_time = now

            if state == "complete":
                return status["files"][0]["path"]

            if state not in ("active", "waiting", "paused"):
                raise RuntimeError(f"Неожиданный статус aria2c: {state}")

            time.sleep(1)
    finally:
        control.gid = None


# ---------------------------------------------------------------------------
# Загрузка на VikingFile
# ---------------------------------------------------------------------------

def get_vikingfile_upload_server():
    resp = requests.get("https://vikingfile.com/api/get-server", timeout=20)
    resp.raise_for_status()
    data = resp.json()
    server = data.get("server")
    if not server:
        raise RuntimeError(f"VikingFile не вернул адрес сервера загрузки: {data}")
    return server


def upload_to_vikingfile_once(file_path, chat_id, status_msg_id, task_id, control, stage_start):
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
                text = render_progress("⬆️", "Загрузка на VikingFile", monitor.bytes_read, monitor.len, 0,
                                        time.time() - stage_start, paused=True)
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
        text = render_progress("⬆️", "Загрузка на VikingFile", done, monitor.len, speed, now - stage_start)
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
        raise RuntimeError(f"Ошибка VikingFile ({resp.status_code}): {resp.text[:300]}")

    data = resp.json()
    if not data.get("url"):
        raise RuntimeError(f"Ошибка VikingFile: {data}")

    return {"name": data.get("name", file_name), "size": data.get("size", file_size), "url": data["url"]}


# ---------------------------------------------------------------------------
# Загрузка на Pixeldrain
# ---------------------------------------------------------------------------

class ControlledFileReader:
    """Файл-обёртка с поддержкой паузы/стопа и подсчётом прочитанных байт (для PUT-загрузки)."""

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


def upload_to_pixeldrain_once(file_path, chat_id, status_msg_id, task_id, control, stage_start):
    if not PIXELDRAIN_API_KEY:
        raise RuntimeError("Секрет PIXELDRAIN_API_KEY не задан в репозитории.")

    file_name = os.path.basename(file_path)
    encoded_name = quote(file_name)

    state = {"last_edit": 0, "last_done": 0, "last_time": time.time()}

    def on_progress(done, total, _speed, paused=False):
        now = time.time()
        if not paused and now - state["last_edit"] < 3:
            return
        dt = now - state["last_time"]
        speed = (done - state["last_done"]) / dt if (dt > 0 and not paused) else 0
        text = render_progress("⬆️", "Загрузка на Pixeldrain", done, total, speed, now - stage_start, paused=paused)
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
        raise RuntimeError(f"Ошибка Pixeldrain ({resp.status_code}): {resp.text[:300]}")

    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Ошибка Pixeldrain: {data}")

    file_id = data["id"]
    return {
        "name": file_name,
        "size": os.path.getsize(file_path),
        "url": f"https://pixeldrain.com/u/{file_id}",
    }


# ---------------------------------------------------------------------------
# Загрузка на FuckingFast / BuzzHeavier
# ---------------------------------------------------------------------------
# Оба сервиса работают на одинаковой платформе: анонимная загрузка - это
# простой PUT с телом файла на поддомен w.<домен>/{имя_файла}. Ответ сервера
# в некоторых случаях приходит как JSON, а в некоторых - как обычный текст
# со ссылкой (именно поэтому в официальных примерах его просто пишут в stdout
# через `| cat`), так что разбираем оба варианта.

def upload_to_wstyle_once(domain, display_name, token, file_path, chat_id, status_msg_id, task_id, control, stage_start):
    file_name = os.path.basename(file_path)
    upload_url = f"https://w.{domain}/{quote(file_name)}"

    state = {"last_edit": 0, "last_done": 0, "last_time": time.time()}

    def on_progress(done, total, _speed, paused=False):
        now = time.time()
        if not paused and now - state["last_edit"] < 3:
            return
        dt = now - state["last_time"]
        speed = (done - state["last_done"]) / dt if (dt > 0 and not paused) else 0
        text = render_progress("⬆️", f"Загрузка на {display_name}", done, total, speed,
                                now - stage_start, paused=paused)
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
        raise RuntimeError(f"Ошибка {display_name} ({resp.status_code}): {resp.text[:300]}")

    link = None
    try:
        data = resp.json()
        link = data.get("url") or data.get("link") or data.get("downloadUrl")
    except ValueError:
        pass

    if not link:
        text = resp.text.strip()
        if text.startswith("http"):
            link = text

    if not link:
        raise RuntimeError(f"Не удалось разобрать ответ {display_name}: {resp.text[:300]}")

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
        raise RuntimeError(f"Gofile не вернул список серверов: {data}")
    return servers[0]["name"]


def upload_to_gofile_once(file_path, chat_id, status_msg_id, task_id, control, stage_start):
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
                text = render_progress("⬆️", "Загрузка на Gofile", monitor.bytes_read, monitor.len, 0,
                                        time.time() - stage_start, paused=True)
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
        text = render_progress("⬆️", "Загрузка на Gofile", done, monitor.len, speed, now - stage_start)
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
        raise RuntimeError(f"Ошибка Gofile ({resp.status_code}): {resp.text[:300]}")

    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"Ошибка Gofile: {data}")

    d = data["data"]
    return {"name": d.get("fileName", file_name), "size": file_size, "url": d["downloadPage"]}


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
                    f"⚠️ Сбой сети при загрузке (попытка {attempt}/{UPLOAD_RETRIES}), "
                    f"пробую снова через {wait_s}с...\n"
                    f"<code>{html_escape(str(e))[:200]}</code>",
                    reply_markup=build_progress_keyboard(task_id, control),
                )
                time.sleep(wait_s)

    hint = ""
    if dest == "pixeldrain":
        hint = (
            "\n\n💡 Похоже, Pixeldrain систематически рвёт соединение (SSL EOF) - "
            "это часто означает, что сервис ограничивает загрузку с IP-адресов "
            "дата-центров/CI (какие использует GitHub Actions). Попробуй прислать "
            "ссылку заново и выбрать <b>VikingFile</b> - он обычно стабильнее в такой среде."
        )
    raise RuntimeError(f"Не удалось загрузить после {UPLOAD_RETRIES} попыток: {last_error}{hint}")


# ---------------------------------------------------------------------------
# Обработка задачи целиком (в отдельном потоке)
# ---------------------------------------------------------------------------

def task_worker(chat_id, url, dest, status_msg_id, task_id, control):
    task_start = time.time()
    file_path = None
    try:
        edit_message(
            chat_id, status_msg_id,
            f"🎯 <b>Хостинг:</b> {DESTINATIONS[dest]}\n"
            f"🔗 <code>{html_escape(url)}</code>\n\n⏳ Начинаю скачивание...",
            reply_markup=build_progress_keyboard(task_id, control),
        )

        file_path = download_with_progress(url, chat_id, status_msg_id, task_id, control)
        size = os.path.getsize(file_path)

        edit_message(
            chat_id, status_msg_id,
            f"✅ <b>Скачивание завершено</b>\n"
            f"📄 <code>{html_escape(os.path.basename(file_path))}</code>\n"
            f"📦 {human_size(size)}\n\n"
            f"⬆️ Начинаю загрузку на {DESTINATIONS[dest]}...",
            reply_markup=build_progress_keyboard(task_id, control),
        )

        result = upload_file(dest, file_path, chat_id, status_msg_id, task_id, control)
        total_time = time.time() - task_start

        edit_message(
            chat_id, status_msg_id,
            "🎉 <b>Готово!</b>\n"
            f"{divider()}\n"
            f"📄 <b>Файл:</b> <code>{html_escape(result['name'])}</code>\n"
            f"📦 <b>Размер:</b> {human_size(result['size'])}\n"
            f"🌐 <b>Хостинг:</b> {DESTINATIONS[dest]}\n"
            f"🕐 <b>Затрачено времени:</b> {human_time(total_time)}\n"
            f"{divider()}\n"
            f"🔗 <b>Ссылка:</b> {result['url']}",
        )

    except TaskStopped:
        edit_message(chat_id, status_msg_id, "⏹ <b>Остановлено пользователем</b>")
    except Exception as e:
        edit_message(chat_id, status_msg_id, f"❌ <b>Ошибка</b>\n<code>{html_escape(str(e))[:500]}</code>")
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass
        active_controls.pop(chat_id, None)


# ---------------------------------------------------------------------------
# Обработка сообщений и нажатий кнопок
# ---------------------------------------------------------------------------

def handle_message(message):
    global last_activity_time

    chat_id = str(message["chat"]["id"])
    text = (message.get("text") or "").strip()

    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        send_message(chat_id, "⛔ У вас нет доступа к этому боту.")
        return

    last_activity_time = time.time()

    if text in ("/start", "/help"):
        send_message(
            chat_id,
            "👋 <b>Привет!</b>\n\n"
            "Пришли мне ссылку на файл — я скачаю его и предложу перезалить "
            "на <b>VikingFile</b>, <b>Pixeldrain</b>, <b>FuckingFast</b>, "
            "<b>BuzzHeavier</b> или <b>Gofile</b> на выбор.\n\n"
            "На этапе скачивания и загрузки под сообщением с прогрессом будут "
            "кнопки ⏸ <b>Пауза</b> и ⏹ <b>Стоп</b>.\n\n"
            "⚙️ <b>Команды</b>\n"
            "/stop — остановить текущую задачу (аналог кнопки ⏹ Стоп)\n"
            "/shutdown — полностью завершить работу бота"
        )
        return

    if text == "/stop":
        control = active_controls.get(chat_id)
        if control:
            control.stopped.set()
            control.running.set()   # разбудить поток, если он ждал на паузе
            send_message(chat_id, "⏹ Останавливаю текущую задачу...")
        else:
            send_message(chat_id, "ℹ️ Сейчас нет активной задачи. Чтобы полностью завершить бота, используй /shutdown.")
        return

    if text == "/shutdown":
        send_message(chat_id, "🛑 Останавливаю бота полностью...")
        should_stop_bot.set()
        return

    if chat_id in active_controls:
        send_message(chat_id, "⏳ Уже выполняется другая задача. Дождись её завершения или отправь /stop.")
        return

    match = URL_RE.search(text)
    if not match:
        send_message(chat_id, "📎 Пришли, пожалуйста, ссылку на файл (начинается с http:// или https://).")
        return

    url = match.group(0)
    token = uuid.uuid4().hex[:8]

    size_bytes = get_remote_file_size(url)
    size_line = f"\n📦 Размер: ~{human_size(size_bytes)}" if size_bytes else ""

    status = send_message(
        chat_id,
        f"🔗 <b>Принял ссылку</b>\n<code>{html_escape(url)}</code>{size_line}\n\nКуда залить файл?",
        reply_markup=build_target_keyboard(token, size_bytes),
    )
    pending_selections[chat_id] = {"token": token, "url": url, "message_id": status["message_id"]}


def handle_callback_query(cq):
    global last_activity_time

    data = cq.get("data", "")
    chat_id = str(cq["message"]["chat"]["id"])
    message_id = cq["message"]["message_id"]

    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        answer_callback(cq["id"], "Нет доступа", show_alert=True)
        return

    last_activity_time = time.time()
    parts = data.split("|")
    action = parts[0]

    if action == "toolarge":
        dest = parts[1]
        answer_callback(
            cq["id"],
            f"⛔ Файл больше лимита {human_size(PIXELDRAIN_MAX_SIZE_BYTES)} для {DESTINATIONS.get(dest, dest)}. "
            f"Выбери VikingFile.",
            show_alert=True,
        )
        return

    if action == "target":
        dest, token = parts[1], parts[2]
        pending = pending_selections.get(chat_id)
        if not pending or pending["token"] != token:
            answer_callback(cq["id"], "Эта кнопка устарела", show_alert=True)
            return

        if dest == "pixeldrain" and not PIXELDRAIN_API_KEY:
            answer_callback(cq["id"], "Pixeldrain недоступен: не задан PIXELDRAIN_API_KEY", show_alert=True)
            return

        del pending_selections[chat_id]
        answer_callback(cq["id"], f"Выбрано: {DESTINATIONS[dest]}")

        task_id = uuid.uuid4().hex[:8]
        control = TaskControl(task_id)
        active_controls[chat_id] = control

        thread = threading.Thread(
            target=task_worker,
            args=(chat_id, pending["url"], dest, message_id, task_id, control),
            daemon=True,
        )
        thread.start()
        return

    if action in ("toggle", "stop"):
        control = active_controls.get(chat_id)
        if not control or control.task_id != parts[1]:
            answer_callback(cq["id"], "Задача уже завершена", show_alert=True)
            return

        if action == "toggle":
            if control.running.is_set():
                control.running.clear()
                answer_callback(cq["id"], "⏸ Пауза")
            else:
                control.running.set()
                answer_callback(cq["id"], "▶️ Продолжаю")
        elif action == "stop":
            control.stopped.set()
            control.running.set()   # разбудить поток, если он ждал на паузе
            answer_callback(cq["id"], "⏹ Останавливаю задачу...")
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
            "ВНИМАНИЕ: TELEGRAM_ALLOWED_CHAT_IDS не задан. "
            "Бот будет отвечать любому, кто его найдёт.",
            file=sys.stderr,
        )
    if not PIXELDRAIN_API_KEY:
        print("Секрет PIXELDRAIN_API_KEY не задан - опция Pixeldrain будет недоступна.", file=sys.stderr)

    # На случай, если для бота когда-либо был настроен webhook - он мешает
    # long polling'у (getUpdates) и вызывает конфликт. Сбрасываем его на всякий случай.
    try:
        tg_call("deleteWebhook", drop_pending_updates="false")
    except Exception as e:
        print(f"Не удалось сбросить webhook (не критично): {e}")

    print("Запускаю aria2c...")
    start_aria2c()

    def handle_sigterm(signum, frame):
        should_stop_bot.set()

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    offset = 0
    print("Бот запущен, жду сообщений в Telegram...")

    try:
        while not should_stop_bot.is_set():
            if time.time() - start_time > HARD_TIMEOUT:
                print("Достигнут общий лимит времени работы, завершаюсь.")
                break
            if time.time() - last_activity_time > IDLE_TIMEOUT:
                print("Нет активности слишком долго, завершаюсь.")
                break

            try:
                updates = get_updates(offset)
            except Exception as e:
                if "Conflict" in str(e) or "terminated by other" in str(e):
                    print(f"Конфликт с другим запущенным экземпляром бота (вероятно, предыдущий запуск ещё "
                          f"не остановился): {e}. Повтор через 5с.")
                else:
                    print(f"Ошибка при опросе Telegram: {e}, повтор через 5с")
                time.sleep(5)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                except Exception as e:
                    print(f"Ошибка обработки апдейта: {e}")

    finally:
        # останавливаем все активные задачи перед выходом
        for control in list(active_controls.values()):
            control.stopped.set()
            control.running.set()
        stop_aria2c()
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

    print("Бот остановлен.")


if __name__ == "__main__":
    main()
