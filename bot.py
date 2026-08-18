#!/usr/bin/env python3
"""
Telegram-бот для перезалива файлов на VikingFile.

Логика:
  1. Бот поднимает aria2c в режиме RPC (для получения реального прогресса скачивания).
  2. Бот слушает Telegram через long polling (getUpdates).
  3. Пользователь присылает боту ссылку на файл.
  4. Бот скачивает файл через aria2c, редактируя сообщение с прогрессом.
  5. Бот заливает скачанный файл на vikingfile.com (multipart upload), тоже с прогрессом.
  6. Бот присылает финальную ссылку на файл.

Если новых сообщений нет дольше IDLE_TIMEOUT секунд — бот завершает работу
(чтобы не держать job GitHub Actions запущенным вечно).
Команда /stop в чате тоже завершает работу немедленно.
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
from html import escape as html_escape

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

# ---------------------------------------------------------------------------
# Конфигурация из переменных окружения
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
VIKINGFILE_USER_HASH = os.environ.get("VIKINGFILE_USER_HASH", "")  # пусто = анонимная загрузка

# Список разрешённых chat_id через запятую, например "123456789,987654321"
ALLOWED_CHAT_IDS = {
    c.strip() for c in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if c.strip()
}

IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT_MINUTES", "10")) * 60    # минуты без сообщений -> выход
HARD_TIMEOUT = int(os.environ.get("HARD_TIMEOUT_SECONDS", "20400"))     # общий потолок на весь job (~340 мин)

DOWNLOAD_DIR = os.path.abspath("downloads")
ARIA2_RPC_PORT = 6800
ARIA2_RPC_SECRET = "pdbotsecret"
ARIA2_RPC_URL = f"http://127.0.0.1:{ARIA2_RPC_PORT}/jsonrpc"

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

URL_RE = re.compile(r"https?://\S+")
UPLOAD_RETRIES = 3

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

start_time = time.time()
last_activity_time = time.time()
should_stop = threading.Event()


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


def send_message(chat_id, text, **kwargs):
    kwargs.setdefault("parse_mode", "HTML")
    kwargs.setdefault("disable_web_page_preview", True)
    return tg_call("sendMessage", chat_id=chat_id, text=text, **kwargs)


def edit_message(chat_id, message_id, text, **kwargs):
    kwargs.setdefault("parse_mode", "HTML")
    kwargs.setdefault("disable_web_page_preview", True)
    try:
        return tg_call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, **kwargs)
    except RuntimeError as e:
        if "message is not modified" in str(e):
            return None
        raise


def get_updates(offset):
    resp = requests.post(
        f"{TELEGRAM_API}/getUpdates",
        data={"offset": offset, "timeout": 25, "allowed_updates": json.dumps(["message"])},
        timeout=35,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error in getUpdates: {data}")
    return data["result"]


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


def render_progress(icon, title, done, total, speed_bps, elapsed, extra_line=None):
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

    lines = [
        f"{icon} <b>{title}</b>",
        "",
        f"<code>[{bar}] {pct:5.1f}%</code>",
        f"📦 {human_size(done)} / {human_size(total) if total else '?'}",
        f"⚡ {speed_str}   ⏳ ETA: {eta_str}",
        f"🕐 Прошло: {human_time(elapsed)}",
    ]
    if extra_line:
        lines.append(extra_line)
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
    payload = {
        "jsonrpc": "2.0",
        "id": str(_rpc_id_counter),
        "method": method,
        "params": full_params,
    }
    resp = requests.post(ARIA2_RPC_URL, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"aria2 RPC error: {data['error']}")
    return data["result"]


def download_with_progress(url, chat_id, status_msg_id, custom_name=None):
    """Скачивает файл через aria2c, обновляя телеграм-сообщение с прогрессом.
    Возвращает путь к скачанному файлу."""
    options = {}
    if custom_name:
        options["out"] = custom_name

    gid = aria2_rpc("aria2.addUri", [[url], options])

    last_edit_text = None
    last_edit_time = 0
    stage_start = time.time()

    while True:
        if should_stop.is_set():
            raise RuntimeError("Остановлено пользователем")

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
                edit_message(chat_id, status_msg_id, text)
                last_edit_text = text
            last_edit_time = now

        if state == "complete":
            file_path = status["files"][0]["path"]
            return file_path

        if state not in ("active", "waiting", "paused"):
            raise RuntimeError(f"Неожиданный статус aria2c: {state}")

        time.sleep(1)


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


def upload_to_vikingfile_once(file_path, chat_id, status_msg_id, stage_start):
    server_url = get_vikingfile_upload_server()
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)

    fh = open(file_path, "rb")
    encoder = MultipartEncoder(fields={
        "file": (file_name, fh, "application/octet-stream"),
        "user": VIKINGFILE_USER_HASH,
    })

    state = {"last_edit": 0, "last_done": 0, "last_time": time.time()}

    def progress_callback(monitor):
        now = time.time()
        if now - state["last_edit"] < 3:
            return
        done = monitor.bytes_read
        dt = now - state["last_time"]
        speed = (done - state["last_done"]) / dt if dt > 0 else 0
        text = render_progress("⬆️", "Загрузка на VikingFile", done, monitor.len, speed, now - stage_start)
        edit_message(chat_id, status_msg_id, text)
        state["last_edit"] = now
        state["last_done"] = done
        state["last_time"] = now

    monitor = MultipartEncoderMonitor(encoder, progress_callback)

    try:
        resp = requests.post(
            server_url,
            data=monitor,
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

    return {
        "name": data.get("name", file_name),
        "size": data.get("size", file_size),
        "hash": data.get("hash"),
        "url": data["url"],
    }


def upload_to_vikingfile(file_path, chat_id, status_msg_id):
    stage_start = time.time()
    last_error = None
    for attempt in range(1, UPLOAD_RETRIES + 1):
        try:
            return upload_to_vikingfile_once(file_path, chat_id, status_msg_id, stage_start)
        except (requests.RequestException, RuntimeError) as e:
            last_error = e
            if attempt < UPLOAD_RETRIES:
                edit_message(
                    chat_id, status_msg_id,
                    f"⚠️ Сбой сети при загрузке (попытка {attempt}/{UPLOAD_RETRIES}), пробую снова...\n"
                    f"<code>{html_escape(str(e))[:200]}</code>"
                )
                time.sleep(5 * attempt)
    raise RuntimeError(f"Не удалось загрузить после {UPLOAD_RETRIES} попыток: {last_error}")


# ---------------------------------------------------------------------------
# Обработка одного сообщения-ссылки
# ---------------------------------------------------------------------------

def process_link(chat_id, url):
    task_start = time.time()
    status = send_message(
        chat_id,
        f"🔗 <b>Принял ссылку</b>\n<code>{html_escape(url)}</code>\n\n⏳ Начинаю скачивание..."
    )
    status_msg_id = status["message_id"]

    file_path = None
    try:
        file_path = download_with_progress(url, chat_id, status_msg_id)
        size = os.path.getsize(file_path)

        edit_message(
            chat_id, status_msg_id,
            f"✅ <b>Скачивание завершено</b>\n"
            f"📄 <code>{html_escape(os.path.basename(file_path))}</code>\n"
            f"📦 {human_size(size)}\n\n"
            f"⬆️ Начинаю загрузку на VikingFile..."
        )

        result = upload_to_vikingfile(file_path, chat_id, status_msg_id)
        total_time = time.time() - task_start

        edit_message(
            chat_id, status_msg_id,
            "🎉 <b>Готово!</b>\n"
            f"{divider()}\n"
            f"📄 <b>Файл:</b> <code>{html_escape(result['name'])}</code>\n"
            f"📦 <b>Размер:</b> {human_size(result['size'])}\n"
            f"🕐 <b>Затрачено времени:</b> {human_time(total_time)}\n"
            f"{divider()}\n"
            f"🔗 <b>Ссылка:</b> {result['url']}"
        )

    except Exception as e:
        edit_message(
            chat_id, status_msg_id,
            f"❌ <b>Ошибка</b>\n<code>{html_escape(str(e))[:500]}</code>"
        )
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Главный цикл
# ---------------------------------------------------------------------------

def handle_update(update):
    global last_activity_time

    message = update.get("message")
    if not message:
        return

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
            "Пришли мне ссылку на файл — я скачаю его и перезалью на "
            "<a href=\"https://vikingfile.com\">VikingFile</a>.\n\n"
            "⚙️ <b>Команды</b>\n"
            "/stop — завершить работу бота"
        )
        return

    if text == "/stop":
        send_message(chat_id, "🛑 Останавливаюсь...")
        should_stop.set()
        return

    match = URL_RE.search(text)
    if not match:
        send_message(chat_id, "📎 Пришли, пожалуйста, ссылку на файл (начинается с http:// или https://).")
        return

    url = match.group(0)
    process_link(chat_id, url)


def main():
    if not ALLOWED_CHAT_IDS:
        print(
            "ВНИМАНИЕ: TELEGRAM_ALLOWED_CHAT_IDS не задан. "
            "Бот будет отвечать любому, кто его найдёт. "
            "Рекомендуется задать секрет со своим chat_id.",
            file=sys.stderr,
        )

    print("Запускаю aria2c...")
    start_aria2c()

    def handle_sigterm(signum, frame):
        should_stop.set()

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    offset = 0
    print("Бот запущен, жду сообщений в Telegram...")

    try:
        while not should_stop.is_set():
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
        stop_aria2c()
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

    print("Бот остановлен.")


if __name__ == "__main__":
    main()
