# Reuploader Bot

Telegram-бот для перезалива файлов на файлообменники: **VikingFile**, **Pixeldrain**, **FuckingFast**, **BuzzHeavier**, **Gofile**.

Можно выбрать несколько хостингов сразу — файл скачается один раз, а зальётся по очереди на все выбранные.

---

## Возможности

- Скачивание по ссылке через aria2c с прогрессом
- Перезалив файла, присланного напрямую в Telegram (документом)
- Мультизалив на несколько хостингов одновременно
- Кнопки **Пауза** / **Стоп** во время скачивания и загрузки
- Команды: `/speedtest`, `/language`, `/stop`, `/shutdown`
- Смена языка интерфейса (русский / английский)

---

## Секреты репозитория

Настройки → **Secrets and variables → Actions → New repository secret**

| Секрет | Обязателен | Описание |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Да | Токен бота от [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Нет | Telegram ID через запятую (пусто = доступен всем) |
| `VIKINGFILE_USER_HASH` | Нет | Для привязки файлов к аккаунту VikingFile |
| `PIXELDRAIN_API_KEY` | Нет | API-ключ pixeldrain [здесь](https://pixeldrain.com/user/api_keys) |
| `FUCKINGFAST_TOKEN` | Нет | Токен FuckingFast |
| `BUZZHEAVIER_TOKEN` | Нет | Токен BuzzHeavier |
| `GOFILE_API_TOKEN` | Нет | Токен Gofile |

---

## Запуск

1. **Actions → Telegram Reuploader Bot → Run workflow**
2. Открываешь бота в Telegram
3. Присылай ссылку на файл или сам файл (документом)
4. Выбирай хостинги, нажимай «Начать»
5. Смотри прогресс скачивания и загрузки
6. Получай финальные ссылки

Бот живёт до **350 минут** (timeout в workflow). Если нет сообщений дольше `idle_timeout_minutes` (по умолчанию 10) — бот завершается сам.

Один запрос за раз — остальные отклоняются с уведомлением.

---

## Структура

```
.
├── .github/workflows/
│   └── telegram-reuploader-bot.yml  # запуск Telegram-бота
├── scripts/
│   └── reuploader_bot.py            # исходник бота
└── requirements.txt                 # зависимости Python
```
