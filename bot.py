#!/usr/bin/env python3
"""Telegram bot — reuploads files to pixeldrain via aria2c."""

import asyncio
import base64
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from aiogram.exceptions import TelegramAPIError

# ── env ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
PIXELDRAIN_API_KEY = os.environ.get("PIXELDRAIN_API_KEY", "")
ALLOWED_USERS_STR = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS = (
    {int(u.strip()) for u in ALLOWED_USERS_STR.split(",") if u.strip()}
    if ALLOWED_USERS_STR
    else None
)

DOWNLOAD_DIR = Path("/tmp/reuploads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ── regex ───────────────────────────────────────────────────────────
URL_RE = re.compile(r"https?://\S+")
# aria2c summary line:  500.0MiB/1.00GiB(50%) CN:16 DL:50.0MiB/s ETA:10s
ARIA2_RE = re.compile(
    r"([\d.]+\s*[KMGTP]?i?B)\s*/\s*([\d.]+\s*[KMGTP]?i?B)"
    r"\s*\((\d+)%\)\s+CN:(\d+)\s+DL:([\d.]+\s*[KMGTP]?i?B/s?)"
    r"(?:\s+ETA:(\S+))?"
)

# ── bot setup ────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

busy = asyncio.Lock()


# ── helpers ──────────────────────────────────────────────────────────
def _allowed(uid: int) -> bool:
    return ALLOWED_USERS is None or uid in ALLOWED_USERS


def _bar(pct: int, w: int = 14) -> str:
    filled = round(pct / 100 * w)
    return "█" * filled + "░" * (w - filled)


def _fmt_size(n: int) -> str:
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PiB"


# ── download ─────────────────────────────────────────────────────────
async def _download(url: str, status: Message) -> Path:
    proc = await asyncio.create_subprocess_exec(
        "aria2c",
        "--max-connection-per-server=16",
        "--split=16",
        f"--dir={DOWNLOAD_DIR}",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--max-tries=5",
        "--retry-wait=3",
        "--connect-timeout=30",
        "--timeout=120",
        "--summary-interval=5",
        "--console-log-level=notice",
        "--download-result=hide",
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    last_t = 0.0
    while True:
        line = await proc.stderr.readline()
        if not line:
            break
        text = line.decode().strip()
        if not text:
            continue

        m = ARIA2_RE.search(text)
        if not m:
            continue

        now = asyncio.get_running_loop().time()
        if now - last_t < 5:
            continue

        pct = int(m.group(3))
        msg_text = (
            f"⬇️ Скачивание...\n"
            f"{_bar(pct)} {pct}%\n"
            f"📦 {m.group(1)} / {m.group(2)}\n"
            f"🚀 {m.group(5)} | ⏱ {m.group(6) or '...'}\n"
            f"🔗 {m.group(4)} conn"
        )
        try:
            await status.edit_text(msg_text)
            last_t = now
        except TelegramAPIError:
            pass

    await proc.wait()
    if proc.returncode != 0:
        err = (await proc.stderr.read()).decode()
        raise RuntimeError(f"aria2c exit {proc.returncode}: {err[-500:]}")

    files = sorted(
        DOWNLOAD_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not files:
        raise RuntimeError("Downloaded file not found")
    return files[0]


# ── upload ───────────────────────────────────────────────────────────
async def _upload(filepath: Path, status: Message) -> str:
    encoded = quote(filepath.name)
    auth = "Basic " + base64.b64encode(f":{PIXELDRAIN_API_KEY}".encode()).decode()
    url = f"https://pixeldrain.com/api/file/{encoded}"
    total = filepath.stat().st_size
    uploaded = 0

    async def _chunked():
        nonlocal uploaded
        with open(filepath, "rb") as fh:
            while True:
                block = fh.read(1 << 20)  # 1 MiB
                if not block:
                    break
                uploaded += len(block)
                yield block

    progress_task = asyncio.create_task(_upload_progress(status, total, lambda: uploaded))

    try:
        async with aiohttp.ClientSession() as session:
            async with session.put(
                url,
                data=_chunked(),
                headers={"Authorization": auth},
            ) as resp:
                body = await resp.text()
                if resp.status != 201:
                    raise RuntimeError(f"Pixeldrain HTTP {resp.status}: {body}")
                data = await resp.json()
                return data["id"]
    finally:
        progress_task.cancel()


async def _upload_progress(status: Message, total: int, get_uploaded):
    while True:
        try:
            pct = get_uploaded() / total * 100 if total else 0
            await status.edit_text(
                f"⬆️ Загрузка на pixeldrain...\n"
                f"{_bar(int(pct))} {pct:.1f}%\n"
                f"📦 {_fmt_size(get_uploaded())} / {_fmt_size(total)}"
            )
        except TelegramAPIError:
            pass
        await asyncio.sleep(5)


# ── handlers ─────────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message):
    if not _allowed(message.from_user.id):
        return
    await message.answer(
        "👋 Пришли ссылку на файл — скачаю через aria2c и перезалью на pixeldrain."
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not _allowed(message.from_user.id):
        return
    await message.answer(
        "Просто отправь ссылку на файл.\n"
        "Бот скачает его (aria2c, 16 conn) и загрузит на pixeldrain."
    )


@router.message()
async def handle_message(message: Message):
    if not _allowed(message.from_user.id):
        return

    match = URL_RE.search(message.text or "")
    if not match:
        await message.answer("❌ Пришли ссылку на файл.")
        return

    if busy.locked():
        await message.answer("⏳ Уже обрабатываю запрос, подожди...")
        return

    async with busy:
        url = match.group(0)
        status = await message.answer("⬇️ Начинаю скачивание...")
        try:
            filepath = await _download(url, status)

            filename = filepath.name
            size = filepath.stat().st_size

            file_id = await _upload(filepath, status)

            view = f"https://pixeldrain.com/u/{file_id}"
            dl = f"https://pixeldrain.com/api/file/{file_id}?download"

            await status.edit_text("✅ Готово!")
            await message.answer(
                f"📁 Файл: `{filename}` ({_fmt_size(size)})\n\n"
                f"🔗 [Открыть]({view})\n"
                f"⬇️ [Скачать]({dl})",
                parse_mode="Markdown",
            )
            filepath.unlink(missing_ok=True)

        except Exception as exc:
            try:
                await status.edit_text(f"❌ Ошибка:\n`{exc}`", parse_mode="Markdown")
            except TelegramAPIError:
                pass


# ── main ──────────────────────────────────────────────────────────────
async def main():
    me = await bot.get_me()
    print(f"Bot @{me.username} is running. Press Ctrl+C to stop.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
