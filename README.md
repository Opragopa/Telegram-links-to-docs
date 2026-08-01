# Экспорт постов Telegram-канала

Скрипт собирает посты канала за заданный период, формирует ссылки на сообщения и делает PNG-скриншот каждого поста. Скриншот строится локально из данных Telethon, поэтому работает и для приватных каналов.

## Быстрый запуск

Для macOS/Linux скачайте репозиторий и выполните:

```bash
chmod +x start.sh start.command
./start.sh
```

На macOS можно также дважды открыть `start.command`. Скрипт сам создаст `.venv`, установит Python-зависимости и Chromium для создания скриншотов.

## Ручная установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

В `.env` укажите `TG_API_ID`, `TG_API_HASH` и канал. API-данные выдаются в [my.telegram.org](https://my.telegram.org).

## Запуск скрипта без веб-интерфейса

```bash
python telegram_posts_export.py \
  --channel @example_channel \
  --from 2026-07-01 \
  --to 2026-08-01
```

При первом запуске Telethon запросит номер телефона, код из Telegram и пароль 2FA. Сессия сохранится локально и повторно вводить код обычно не потребуется.

Результаты появятся в `exports/<канал>_<начало>_<конец>/`:

- `posts.csv` — таблица для Excel/Google Sheets;
- `posts.json` — те же данные в JSON;
- `screenshots/` — PNG-скриншоты;
- `html/` — промежуточные HTML-файлы скриншотов.

Дата `--to` не включается в выборку: для периода за весь июль используйте `--from 2026-07-01 --to 2026-08-01`.

## Веб-интерфейс

```bash
python web_app.py
```

Откройте http://127.0.0.1:5050, заполните канал, API ID, API Hash и даты. Задача выполняется в фоне, а журнал прогресса отображается в браузере.
