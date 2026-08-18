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

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

# ---------------------------------------------------------------------------
# Конфигурация из переменных окружения
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
VIKINGFILE_USER_HASH = os.environ.get("VIKINGFILE_USER_HASH", "")   # пусто = анонимная загрузка
PIXELDRAIN_API_KEY = os.environ.get("PIXELDRAIN_API_KEY", "")       # нужен только если выбран Pixeldrain

ALLOWED_CHAT_IDS = {
    c.strip() for c in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if c.strip()
}

IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT_MINUTES", "10")) * 60
HARD_TIMEOUT = int(os.environ.get("HARD_TIMEOUT_SECONDS", "20400"))

DOWNLOAD_DIR = os.path.abspath("downloads")
ARIA2_RPC_PORT = 6800
ARIA2_RPC_SECRET = "pdbotsecret"
ARIA2_RPC_URL = f"http://127.0.0.1:{ARIA2_RPC_PORT}/jsonrpc"

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

URL_RE = re.compile(r"https?://\S+")
UPLOAD_RETRIES = 3

DESTINATIONS = {
    "vikingfile": "VikingFile",
    "pixeldrain": "Pixeldrain",
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

def build_target_keyboard(token):
    return {
        "inline_keyboard": [[
            {"text": "🦁 VikingFile", "callback_data": f"target|vikingfile|{token}"},
            {"text": "🟢 Pixeldrain", "callback_data": f"target|pixeldrain|{token}"},
        ]]
    }


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

    from urllib.parse import quote

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
# Общий диспетчер загрузки с ретраями
# ---------------------------------------------------------------------------

UPLOAD_FUNCS = {
    "vikingfile": upload_to_vikingfile_once,
    "pixeldrain": upload_to_pixeldrain_once,
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
                edit_message(
                    chat_id, status_msg_id,
                    f"⚠️ Сбой сети при загрузке (попытка {attempt}/{UPLOAD_RETRIES}), пробую снова...\n"
                    f"<code>{html_escape(str(e))[:200]}</code>",
                    reply_markup=build_progress_keyboard(task_id, control),
                )
                time.sleep(5 * attempt)
    raise RuntimeError(f"Не удалось загрузить после {UPLOAD_RETRIES} попыток: {last_error}")


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
            "на <b>VikingFile</b> или <b>Pixeldrain</b> на выбор.\n\n"
            "На этапе скачивания и загрузки под сообщением с прогрессом будут "
            "кнопки ⏸ <b>Пауза</b> и ⏹ <b>Стоп</b>.\n\n"
            "⚙️ <b>Команды</b>\n"
            "/stop — полностью завершить работу бота"
        )
        return

    if text == "/stop":
        send_message(chat_id, "🛑 Останавливаюсь...")
        should_stop_bot.set()
        return

    if chat_id in active_controls:
        send_message(chat_id, "⏳ Уже выполняется другая задача. Дождись её завершения или нажми ⏹ Стоп под её сообщением.")
        return

    match = URL_RE.search(text)
    if not match:
        send_message(chat_id, "📎 Пришли, пожалуйста, ссылку на файл (начинается с http:// или https://).")
        return

    url = match.group(0)
    token = uuid.uuid4().hex[:8]
    status = send_message(
        chat_id,
        f"🔗 <b>Принял ссылку</b>\n<code>{html_escape(url)}</code>\n\nКуда залить файл?",
        reply_markup=build_target_keyboard(token),
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
            except requests.RequestException as e:
                print(f"Ошибка сети при опросе Telegram: {e}, повтор через 5с")
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
