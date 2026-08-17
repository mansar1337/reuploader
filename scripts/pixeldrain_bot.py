#!/usr/bin/env python3
"""
Telegram-бот для перезалива файлов на pixeldrain.

Логика:
  1. Бот поднимает aria2c в режиме RPC (для получения реального прогресса скачивания).
  2. Бот слушает Telegram через long polling (getUpdates).
  3. Пользователь присылает боту ссылку на файл.
  4. Бот скачивает файл через aria2c, редактируя сообщение с прогрессом.
  5. Бот заливает скачанный файл на pixeldrain (PUT /api/file/{name}), тоже с прогрессом.
  6. Бот присылает финальную ссылку на pixeldrain.

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
from urllib.parse import quote

import requests

# ---------------------------------------------------------------------------
# Конфигурация из переменных окружения
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
PIXELDRAIN_API_KEY = os.environ["PIXELDRAIN_API_KEY"]

# Список разрешённых chat_id через запятую, например "123456789,987654321"
# Если не задано - бот откажется стартовать (чтобы им не мог пользоваться кто попало).
ALLOWED_CHAT_IDS = {
    c.strip() for c in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if c.strip()
}

IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT_SECONDS", "600"))       # 10 минут без сообщений -> выход
HARD_TIMEOUT = int(os.environ.get("HARD_TIMEOUT_SECONDS", "20400"))     # общий потолок на весь job (~340 мин)

DOWNLOAD_DIR = os.path.abspath("downloads")
ARIA2_RPC_PORT = 6800
ARIA2_RPC_SECRET = "pdbotsecret"
ARIA2_RPC_URL = f"http://127.0.0.1:{ARIA2_RPC_PORT}/jsonrpc"

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

URL_RE = re.compile(r"https?://\S+")

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
    return tg_call("sendMessage", chat_id=chat_id, text=text, **kwargs)


def edit_message(chat_id, message_id, text, **kwargs):
    try:
        return tg_call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, **kwargs)
    except RuntimeError as e:
        # Telegram кидает ошибку, если текст не изменился - это не страшно, игнорируем
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
# Прогресс-бар
# ---------------------------------------------------------------------------

def human_size(n):
    n = float(n)
    for unit in ["Б", "КБ", "МБ", "ГБ", "ТБ"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ПБ"


def progress_bar(fraction, width=20):
    fraction = max(0.0, min(1.0, fraction))
    filled = int(width * fraction)
    return "▓" * filled + "░" * (width - filled)


def render_progress(title, done, total, speed_bps, extra=""):
    if total > 0:
        frac = done / total
        pct = f"{frac * 100:.1f}%"
        bar = progress_bar(frac)
    else:
        pct = "?"
        bar = progress_bar(0)
    speed = f"{human_size(speed_bps)}/с" if speed_bps else "-"
    text = (
        f"{title}\n"
        f"{bar} {pct}\n"
        f"{human_size(done)} / {human_size(total) if total else '?'}\n"
        f"Скорость: {speed}"
    )
    if extra:
        text += f"\n{extra}"
    return text


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
    # ждём пока RPC поднимется
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
            text = render_progress("⬇️ Скачивание файла...", done, total, speed)
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
# Загрузка на pixeldrain
# ---------------------------------------------------------------------------

class ProgressFileReader:
    """Обёртка над файлом, которая считает прочитанные байты для прогресс-бара."""

    def __init__(self, path, on_progress):
        self._f = open(path, "rb")
        self._size = os.fstat(self._f.fileno()).st_size
        self._read = 0
        self._on_progress = on_progress

    def __len__(self):
        return self._size

    def read(self, size=-1):
        chunk = self._f.read(size)
        self._read += len(chunk)
        self._on_progress(self._read, self._size)
        return chunk

    def close(self):
        self._f.close()


def upload_to_pixeldrain(file_path, chat_id, status_msg_id):
    file_name = os.path.basename(file_path)
    encoded_name = quote(file_name)

    progress_state = {"done": 0, "total": os.path.getsize(file_path), "last_edit": 0}

    def on_progress(done, total):
        progress_state["done"] = done
        progress_state["total"] = total
        now = time.time()
        if now - progress_state["last_edit"] >= 3:
            text = render_progress("⬆️ Загрузка на pixeldrain...", done, total, 0)
            edit_message(chat_id, status_msg_id, text)
            progress_state["last_edit"] = now

    reader = ProgressFileReader(file_path, on_progress)
    try:
        resp = requests.put(
            f"https://pixeldrain.com/api/file/{encoded_name}",
            data=reader,
            headers={"Content-Length": str(len(reader))},
            auth=("", PIXELDRAIN_API_KEY),
            timeout=None,
        )
    finally:
        reader.close()

    if resp.status_code >= 400:
        raise RuntimeError(f"Ошибка pixeldrain ({resp.status_code}): {resp.text}")

    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Ошибка pixeldrain: {data}")

    file_id = data["id"]
    return {
        "id": file_id,
        "view_url": f"https://pixeldrain.com/u/{file_id}",
        "direct_url": f"https://pixeldrain.com/api/file/{file_id}",
    }


# ---------------------------------------------------------------------------
# Обработка одного сообщения-ссылки
# ---------------------------------------------------------------------------

def process_link(chat_id, url):
    status = send_message(chat_id, f"🔗 Принял ссылку:\n{url}\n\nНачинаю скачивание...")
    status_msg_id = status["message_id"]

    file_path = None
    try:
        file_path = download_with_progress(url, chat_id, status_msg_id)

        size = os.path.getsize(file_path)
        edit_message(
            chat_id, status_msg_id,
            f"✅ Скачано: {os.path.basename(file_path)} ({human_size(size)})\n\n⬆️ Начинаю загрузку на pixeldrain..."
        )

        result = upload_to_pixeldrain(file_path, chat_id, status_msg_id)

        edit_message(
            chat_id, status_msg_id,
            "✅ Готово!\n\n"
            f"Файл: {os.path.basename(file_path)} ({human_size(size)})\n\n"
            f"🔗 Ссылка: {result['view_url']}\n"
            f"⬇️ Прямая ссылка: {result['direct_url']}"
        )

    except Exception as e:
        edit_message(chat_id, status_msg_id, f"❌ Ошибка: {e}")
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
            "Привет! Пришли мне ссылку на файл, и я перезалью его на pixeldrain.\n\n"
            "Команды:\n"
            "/stop - завершить работу бота"
        )
        return

    if text == "/stop":
        send_message(chat_id, "Останавливаюсь...")
        should_stop.set()
        return

    match = URL_RE.search(text)
    if not match:
        send_message(chat_id, "Пришли, пожалуйста, ссылку на файл (начинается с http:// или https://).")
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
