# Pixeldrain Reuploader

Скачивание файлов через aria2c с последующей загрузкой на pixeldrain.

Два режима работы:
1. **Ручной** (`reupload.yml`) — через GitHub Actions UI, вводишь ссылку — получаешь ссылку.
2. **Telegram-бот** (`bot.yml`) — запускаешь бота, общаешься через Telegram с прогрессом в реальном времени.

---

## Секреты репозитория

Настройки → **Secrets and variables → Actions → New repository secret**

| Секрет | Обязателен | Описание |
|---|---|---|
| `PIXELDRAIN_API_KEY` | Да (для обоих) | API-ключ pixeldrain [здесь](https://pixeldrain.com/user/api_keys) |
| `TELEGRAM_BOT_TOKEN` | Только для бота | Токен бота от [@BotFather](https://t.me/BotFather) |
| `ALLOWED_USERS` | Нет | Telegram ID через запятую (пусто = доступен всем) |

---

## Режим 1 — Ручной workflow

**Actions → Reupload to Pixeldrain → Run workflow**

Вводишь `file_url`, ждёшь, в summary появятся ссылки на файл.

---

## Режим 2 — Telegram-бот

1. **Actions → Telegram Reupload Bot → Run workflow**
2. Открываешь бота в Telegram, отправляешь ссылку
3. Смотришь прогресс скачивания (aria2c) и загрузки (pixeldrain)
4. Получаешь ссылку на перезалитый файл

Бот живёт **60 минут** (timeout в workflow). Если нужно дольше — поменяй `timeout-minutes` в `bot.yml`.

Один запрос за раз — остальные ставятся в очередь с уведомлением.

---

## Структура

```
.
├── .github/workflows/
│   ├── reupload.yml      # ручной перезалив через UI
│   └── bot.yml           # запуск Telegram-бота
├── bot.py                # исходник бота
└── requirements.txt      # зависимости Python
```
