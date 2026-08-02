#!/usr/bin/env python3
"""Local web UI: Telegram login, export and a report table without a terminal."""

from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import threading
import time
import uuid
from copy import copy
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from openpyxl import Workbook
from openpyxl.drawing.image import Image as ExcelImage
from openpyxl.utils import get_column_letter
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from werkzeug.serving import make_server

from telegram_posts_export import export_authenticated, parse_date


ROOT = Path(__file__).resolve().parent
APP_VERSION = "0.2.0"
DATA_DIR = Path(os.environ.get("TG_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR = DATA_DIR / "exports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
TABLE_DIR = DATA_DIR / "table_exports"
TABLE_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "settings.json"
app = Flask(__name__)
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
state_lock = threading.Lock()
telegram_client: TelegramClient | None = None
auth_phone = ""
auth_code_hash = ""
auth_needs_password = False
jobs: dict[str, dict] = {}
job_futures: dict[str, object] = {}
flask_server = None


def load_api_config() -> dict[str, str]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def write_config(config: dict) -> None:
    # DATA_DIR is created at startup, but if the data folder lives on a
    # removable/network volume it can vanish while the app keeps running;
    # recreating it here turns a raw OSError into a normal write.
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(config), encoding="utf-8")
        CONFIG_PATH.chmod(0o600)
    except OSError as exc:
        raise RuntimeError(
            f"Не удалось сохранить настройки в {DATA_DIR}. Проверьте, что эта папка существует и доступна для записи."
        ) from exc


def save_api_config(api_id: str, api_hash: str) -> None:
    config = load_api_config()
    config.update(api_id=api_id, api_hash=api_hash)
    write_config(config)


def save_last_channel(channel: str) -> None:
    config = load_api_config()
    config["last_channel"] = channel
    write_config(config)


async def ensure_saved_session() -> bool:
    """Reconnect the saved Telethon session after a web-app restart."""
    global telegram_client
    if telegram_client and telegram_client.is_connected():
        return await telegram_client.is_user_authorized()
    config = load_api_config()
    if not config.get("api_id") or not config.get("api_hash"):
        return False
    session = os.environ.get("TG_SESSION", str(DATA_DIR / "telegram_posts_export"))
    telegram_client = TelegramClient(session, int(config["api_id"]), config["api_hash"])
    await telegram_client.connect()
    return await telegram_client.is_user_authorized()


def run_async(coroutine):
    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    return future.result()


# key, Excel header, Excel column width. The screenshot sits right after the
# date so the picture is visible without scrolling past every metric column.
TABLE_COLUMNS = [
    ("date_local", "Дата публикации", 19),
    ("screenshot", "Скриншот", 33),
    ("id", "ID поста", 10),
    ("url", "Ссылка", 38),
    ("text", "Текст", 70),
    ("views", "Просмотры", 12),
    ("reactions", "Реакции", 10),
    ("reactions_detail", "Реакции подробно", 24),
    ("forwards", "Пересылки", 12),
    ("replies", "Комментарии", 13),
    ("media_type", "Медиа", 12),
    ("album_size", "Файлов в посте", 15),
    ("is_forwarded", "Репост", 10),
    ("char_count", "Символов", 11),
    ("channel_title", "Канал", 24),
]
# Telegram post screenshots are tall; keeping them narrow enough for one screen
# is what makes the report readable row by row.
MAX_IMAGE_WIDTH = 220
MAX_IMAGE_HEIGHT = 300
NUMERIC_KEYS = {"id", "views", "reactions", "forwards", "replies", "char_count", "album_size"}
# Older exports and hand-made JSON files use different names for the same data.
ALIASES = {
    "id": ("id", "messageId", "message_id"),
    "has_media": ("has_media", "hasMedia"),
    "is_forwarded": ("is_forwarded", "isForwarded"),
    "screenshot": ("screenshot", "screenshot_path"),
    "channel_title": ("channel_title", "channel", "channelTitle"),
    "date_local": ("date_local", "dateLocal", "date"),
    "reactions_detail": ("reactions_detail", "reactionsDetail"),
    "char_count": ("char_count", "charCount"),
    "media_type": ("media_type", "mediaType"),
    "album_size": ("album_size", "albumSize"),
}


def format_local_date(value: str) -> str:
    """Turn an ISO timestamp into the readable form used across the report."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value or "")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%d.%m.%Y %H:%M")


def normalize_post(item: dict, index: int) -> dict:
    """Return one table row with every column present and typed predictably."""
    if not isinstance(item, dict):
        raise ValueError(f"Пост №{index} должен быть объектом JSON")
    row = {}
    for key, _, _ in TABLE_COLUMNS:
        candidates = ALIASES.get(key, (key,))
        row[key] = next((item[name] for name in candidates if name in item and item[name] != ""), "")
    if row["id"] == "":
        raise ValueError(f"У поста №{index} отсутствует id / messageId")
    row["date_local"] = format_local_date(row["date_local"])
    # Older exports stored custom emoji reactions as a raw document id.
    row["reactions_detail"] = re.sub(r"\b\d{6,}\b", "⭐", str(row["reactions_detail"] or ""))
    row["date"] = str(item.get("date", "") or "")
    row["text"] = str(row["text"] or "")
    row["char_count"] = int(row["char_count"] or 0) or len(row["text"])
    row["album_size"] = int(row["album_size"] or 1)
    row["is_forwarded"] = "Да" if row["is_forwarded"] in (True, "Да", "true", 1) else "Нет"
    row["has_media"] = bool(next((item[name] for name in ALIASES["has_media"] if name in item), row["media_type"]))
    if not row["media_type"] and row["has_media"]:
        row["media_type"] = "Медиа"
    for key in NUMERIC_KEYS:
        try:
            row[key] = int(row[key] or 0)
        except (TypeError, ValueError):
            row[key] = 0
    return row


def parse_posts_json(raw: str) -> list[dict]:
    """Validate the exported JSON format and return predictable table rows."""
    payload = json.loads(raw)
    if isinstance(payload, dict):
        payload = payload.get("posts", payload.get("items", []))
    if not isinstance(payload, list):
        raise ValueError("JSON должен содержать массив постов")
    return [normalize_post(item, index) for index, item in enumerate(payload, start=1)]


def channel_from_url(url: str) -> str:
    match = re.match(r"https?://t\.me/(?:s/)?([^/?#]+)/", str(url or ""))
    return match.group(1) if match else ""


def find_screenshot(row: dict, export_root: Path | None) -> Path | None:
    """Locate the PNG that belongs to this post.

    Screenshots are named after the message id, so the same file name exists in
    every export. When the export folder is known the lookup is exact; for
    pasted JSON it is narrowed by the channel in the post URL and then by the
    freshest file, so a picture from an unrelated channel cannot sneak in.
    """
    filename = Path(str(row.get("screenshot") or "")).name
    if not filename:
        return None
    if export_root:
        candidate = export_root / "screenshots" / filename
        return candidate if candidate.is_file() else None
    matches = [path for path in EXPORT_DIR.rglob(filename) if path.parent.name == "screenshots"]
    if not matches:
        return None
    channel = channel_from_url(row.get("url", ""))
    scoped = [path for path in matches if channel and path.parent.parent.name.startswith(f"{channel}_")]
    return max(scoped or matches, key=lambda path: path.stat().st_mtime)


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow([label for _, label, _ in TABLE_COLUMNS])
        for row in rows:
            writer.writerow([row[key] for key, _, _ in TABLE_COLUMNS])


def write_xlsx(path: Path, rows: list[dict], export_root: Path | None) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Посты"
    sheet.append([label for _, label, _ in TABLE_COLUMNS])
    for row in rows:
        sheet.append([row[key] for key, _, _ in TABLE_COLUMNS])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for index, (_, _, width) in enumerate(TABLE_COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    for cell in sheet[1]:
        font = copy(cell.font)
        font.bold = True
        cell.font = font
        alignment = copy(cell.alignment)
        alignment.vertical = "center"
        cell.alignment = alignment
    sheet.row_dimensions[1].height = 24

    keys = [key for key, _, _ in TABLE_COLUMNS]
    url_index, text_index, shot_index = keys.index("url"), keys.index("text"), keys.index("screenshot")
    shot_column = get_column_letter(shot_index + 1)
    for row, source in zip(sheet.iter_rows(min_row=2), rows):
        link = row[url_index]
        link.hyperlink = link.value or None
        link.style = "Hyperlink"
        for cell in row:
            alignment = copy(cell.alignment)
            alignment.vertical = "top"
            alignment.wrap_text = cell is row[text_index]
            cell.alignment = alignment
        # Excel images are anchored to cells rather than being true cell values,
        # so the row must be tall enough for the picture to stay fully visible.
        screenshot_path = find_screenshot(source, export_root)
        row[shot_index].value = "" if screenshot_path else "нет скриншота"
        if screenshot_path:
            image = ExcelImage(str(screenshot_path))
            scale = min(MAX_IMAGE_WIDTH / image.width, MAX_IMAGE_HEIGHT / image.height, 1)
            image.width = max(1, round(image.width * scale))
            image.height = max(1, round(image.height * scale))
            sheet.add_image(image, f"{shot_column}{row[0].row}")
            sheet.row_dimensions[row[0].row].height = min(image.height * 0.75 + 6, 409.5)
    workbook.save(path)


def create_table_exports(rows: list[dict], export_root: Path | None = None) -> tuple[str, str, int]:
    export_id = uuid.uuid4().hex[:12]
    xlsx_path = TABLE_DIR / f"telegram-posts-{export_id}.xlsx"
    csv_path = TABLE_DIR / f"telegram-posts-{export_id}.csv"
    write_csv(csv_path, rows)
    write_xlsx(xlsx_path, rows, export_root)
    return xlsx_path.name, csv_path.name, len(rows)


def safe_export_dir(name: str) -> Path | None:
    """Resolve an export folder name coming from the browser."""
    if not name or name != Path(name).name or name.startswith("."):
        return None
    path = EXPORT_DIR / name
    return path if path.is_dir() and (path / "posts.json").is_file() else None


def read_export(name: str) -> list[dict]:
    path = safe_export_dir(name)
    if not path:
        raise FileNotFoundError("Экспорт не найден")
    return parse_posts_json((path / "posts.json").read_text(encoding="utf-8"))


def describe_export(path: Path) -> dict:
    """Summary card for the export history list."""
    match = re.match(r"^(.*)_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})$", path.name)
    channel, from_date, to_date = match.groups() if match else (path.name, "", "")
    try:
        posts = json.loads((path / "posts.json").read_text(encoding="utf-8"))
        count = len(posts) if isinstance(posts, list) else 0
    except (json.JSONDecodeError, OSError):
        count = 0
    modified = path.stat().st_mtime
    return {
        "name": path.name,
        "channel": channel,
        "from_date": from_date,
        "to_date": to_date,
        "count": count,
        "modified": modified,
        "updated_at": datetime.fromtimestamp(modified).strftime("%d.%m.%Y %H:%M"),
    }


def posts_payload(rows: list[dict], export_name: str) -> list[dict]:
    """Rows enriched with a browser-servable screenshot URL for the UI table."""
    payload = []
    for row in rows:
        item = dict(row)
        filename = Path(str(row.get("screenshot") or "")).name
        item["screenshot_url"] = f"/api/exports/{export_name}/screenshots/{filename}" if filename else ""
        payload.append(item)
    return payload


def friendly_auth_error(exc: Exception) -> str:
    """Translate the Telethon exceptions users actually hit into Russian."""
    if isinstance(exc, PhoneCodeInvalidError):
        return "Неверный код. Проверьте, что вводите последний код — каждый запрос «Получить код» делает предыдущий код недействительным."
    if isinstance(exc, PhoneCodeExpiredError):
        return "Код устарел. Нажмите «Отправить код ещё раз»."
    if isinstance(exc, PhoneNumberInvalidError):
        return "Некорректный номер телефона. Укажите его в международном формате, например +79991234567."
    if isinstance(exc, PhoneNumberBannedError):
        return "Этот номер заблокирован в Telegram."
    if isinstance(exc, FloodWaitError):
        minutes = max(1, exc.seconds // 60)
        return f"Telegram временно ограничил попытки входа для этого номера. Повторите примерно через {minutes} мин."
    return str(exc)


async def begin_login(api_id: int, api_hash: str, phone: str) -> str:
    global telegram_client, auth_phone, auth_code_hash, auth_needs_password
    if telegram_client:
        await telegram_client.disconnect()
    session = os.environ.get("TG_SESSION", str(DATA_DIR / "telegram_posts_export"))
    telegram_client = TelegramClient(session, api_id, api_hash)
    await telegram_client.connect()
    if await telegram_client.is_user_authorized():
        return "authorized"
    if not phone:
        raise ValueError("Укажите номер телефона")
    # A fresh code request invalidates whatever code was sent before, so any
    # code the user is still holding from an earlier attempt stops working.
    sent = await telegram_client.send_code_request(phone)
    auth_phone, auth_code_hash = phone, sent.phone_code_hash
    auth_needs_password = False
    return "code_required"


async def finish_login(code: str, password: str = "") -> str:
    global auth_needs_password
    if not telegram_client:
        raise RuntimeError("Сначала запросите код входа")
    if auth_needs_password:
        if not password:
            return "password_required"
        await telegram_client.sign_in(password=password)
        auth_needs_password = False
        return "authorized"
    if not code:
        raise ValueError("Введите код из Telegram")
    try:
        await telegram_client.sign_in(auth_phone, code, phone_code_hash=auth_code_hash)
    except SessionPasswordNeededError:
        if not password:
            auth_needs_password = True
            return "password_required"
        await telegram_client.sign_in(password=password)
    return "authorized"


async def export_job(job_id: str, channel: str, from_date: str, to_date: str) -> None:
    try:
        start = parse_date(from_date)
        end = parse_date(to_date, end_of_day=True)
        started = time.monotonic()
        with state_lock:
            jobs[job_id].update(status="running", log="Собираю список постов...", started_at=started)

        async def update_progress(processed: int, total: int, detail: str) -> None:
            elapsed = time.monotonic() - started
            average = elapsed / processed if processed else 0
            remaining = max(total - processed, 0)
            with state_lock:
                jobs[job_id].update(
                    processed=processed,
                    total=total,
                    progress=round(processed / total * 100) if total else 0,
                    elapsed_seconds=round(elapsed),
                    eta_seconds=round(average * remaining) if processed else None,
                    log=detail,
                )

        posts, output = await export_authenticated(
            telegram_client, channel, start, end, str(EXPORT_DIR), progress_callback=update_progress
        )
        elapsed = round(time.monotonic() - started)
        rows = parse_posts_json(json.dumps([post.__dict__ for post in posts], ensure_ascii=False))
        with state_lock:
            jobs[job_id].update(
                status="done",
                progress=100,
                total=len(rows),
                processed=len(rows),
                elapsed_seconds=elapsed,
                eta_seconds=0,
                export_name=output.name,
                log=f"Готово: {len(rows)} постов за {elapsed} сек.",
            )
    except asyncio.CancelledError:
        with state_lock:
            jobs[job_id].update(status="cancelled", log="Экспорт остановлен. Уже созданные файлы сохранены.")
        raise
    except Exception as exc:
        with state_lock:
            jobs[job_id].update(status="error", log=f"Ошибка: {exc}")


@app.get("/")
def index():
    return render_template("index.html", app_version=APP_VERSION)


@app.post("/api/auth/start")
def auth_start():
    data = request.get_json(silent=True) or {}
    try:
        api_id = str(data.get("api_id", "")).strip()
        api_hash = str(data.get("api_hash", "")).strip()
        if api_id and api_hash:
            save_api_config(api_id, api_hash)
        else:
            config = load_api_config()
            api_id, api_hash = config.get("api_id", ""), config.get("api_hash", "")
        if not api_id or not api_hash:
            return jsonify(error="Укажите API ID и API Hash с my.telegram.org"), 400
        try:
            api_id_int = int(api_id)
        except ValueError:
            return jsonify(error="API ID должен быть числом"), 400
        status = run_async(begin_login(api_id_int, api_hash, str(data.get("phone", "")).strip()))
        return jsonify(status=status)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=friendly_auth_error(exc)), 400


@app.post("/api/auth/verify")
def auth_verify():
    data = request.get_json(silent=True) or {}
    try:
        status = run_async(finish_login(str(data.get("code", "")).strip(), str(data.get("password", ""))))
        return jsonify(status=status)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=friendly_auth_error(exc)), 400


@app.get("/api/auth/status")
def auth_status():
    try:
        return jsonify(authorized=run_async(ensure_saved_session()))
    except Exception as exc:
        return jsonify(authorized=False, error=str(exc))


@app.get("/api/config")
def api_config():
    config = load_api_config()
    return jsonify(
        api_id=config.get("api_id", ""),
        has_api_hash=bool(config.get("api_hash")),
        last_channel=config.get("last_channel", ""),
    )


@app.get("/api/exports")
def list_exports():
    items = [describe_export(path) for path in EXPORT_DIR.iterdir() if path.is_dir() and (path / "posts.json").is_file()]
    items.sort(key=lambda item: item["modified"], reverse=True)
    return jsonify(exports=items)


@app.get("/api/exports/<name>/posts")
def export_posts(name: str):
    try:
        return jsonify(posts=posts_payload(read_export(name), name), export=describe_export(safe_export_dir(name)))
    except FileNotFoundError as exc:
        return jsonify(error=str(exc)), 404
    except (json.JSONDecodeError, ValueError) as exc:
        return jsonify(error=f"Не удалось прочитать посты: {exc}"), 400


@app.get("/api/exports/<name>/screenshots/<filename>")
def export_screenshot(name: str, filename: str):
    path = safe_export_dir(name)
    safe_name = Path(filename).name
    if not path or safe_name != filename:
        return jsonify(error="Скриншот не найден"), 404
    image = path / "screenshots" / safe_name
    if not image.is_file():
        return jsonify(error="Скриншот не найден"), 404
    return send_file(image, mimetype="image/png")


@app.post("/api/exports/<name>/table")
def export_table(name: str):
    """Build Excel + CSV straight from a finished export, no JSON pasting."""
    try:
        rows = read_export(name)
        selected = (request.get_json(silent=True) or {}).get("ids")
        if selected:
            wanted = {int(value) for value in selected}
            rows = [row for row in rows if row["id"] in wanted]
        if not rows:
            return jsonify(error="Нет постов для выгрузки"), 400
        xlsx_name, csv_name, count = create_table_exports(rows, safe_export_dir(name))
        return jsonify(count=count, xlsx_url=f"/api/table-exports/{xlsx_name}", csv_url=f"/api/table-exports/{csv_name}")
    except FileNotFoundError as exc:
        return jsonify(error=str(exc)), 404
    except Exception as exc:
        return jsonify(error=f"Ошибка создания таблицы: {exc}"), 500


@app.post("/api/convert-json")
def convert_json():
    """Convert pasted JSON or an uploaded JSON file into Excel and CSV."""
    try:
        if "file" in request.files:
            raw = request.files["file"].read().decode("utf-8-sig")
        else:
            raw = str((request.get_json(silent=True) or {}).get("json", ""))
        if not raw.strip():
            return jsonify(error="Загрузите JSON-файл или вставьте JSON-текст"), 400
        xlsx_name, csv_name, count = create_table_exports(parse_posts_json(raw))
        return jsonify(count=count, xlsx_url=f"/api/table-exports/{xlsx_name}", csv_url=f"/api/table-exports/{csv_name}")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return jsonify(error=f"Не удалось обработать JSON: {exc}"), 400
    except Exception as exc:
        return jsonify(error=f"Ошибка создания таблицы: {exc}"), 500


@app.get("/api/table-exports/<filename>")
def download_table(filename: str):
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.startswith("telegram-posts-"):
        return jsonify(error="Некорректное имя файла"), 404
    path = TABLE_DIR / safe_name
    if not path.is_file():
        return jsonify(error="Файл не найден"), 404
    return send_file(path, as_attachment=True, download_name=safe_name)


@app.post("/api/export")
def start_export():
    data = request.get_json(silent=True) or {}
    if not run_async(ensure_saved_session()):
        return jsonify(error="Сначала войдите в Telegram"), 401
    channel, from_date, to_date = (str(data.get(key, "")).strip() for key in ("channel", "from_date", "to_date"))
    if not all((channel, from_date, to_date)):
        return jsonify(error="Заполните канал и период"), 400
    try:
        if parse_date(to_date, end_of_day=True) <= parse_date(from_date):
            return jsonify(error="Дата окончания должна быть не раньше даты начала"), 400
    except ValueError:
        return jsonify(error="Даты должны быть в формате ГГГГ-ММ-ДД"), 400
    with state_lock:
        running = [job_id for job_id, job in jobs.items() if job.get("status") in {"queued", "running", "cancelling"}]
    if running:
        return jsonify(error="Экспорт уже выполняется. Дождитесь окончания или остановите его."), 409
    save_last_channel(channel)
    job_id = uuid.uuid4().hex[:10]
    with state_lock:
        jobs[job_id] = {"status": "queued", "log": "Задача поставлена в очередь", "processed": 0, "total": 0, "progress": 0}
    job_futures[job_id] = asyncio.run_coroutine_threadsafe(export_job(job_id, channel, from_date, to_date), loop)
    return jsonify(job_id=job_id)


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    with state_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify(error="Задача не найдена"), 404
    return jsonify(**job)


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    with state_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(error="Задача не найдена"), 404
        if job.get("status") not in {"queued", "running"}:
            return jsonify(**job)
        job.update(status="cancelling", log="Останавливаю экспорт...")
    future = job_futures.get(job_id)
    if future:
        future.cancel()
    return jsonify(status="cancelling")


@app.post("/api/app/shutdown")
def shutdown_app():
    """Stop active jobs and gracefully stop the local web server."""
    if flask_server is None:
        return jsonify(error="Сервер ещё не готов к завершению"), 503
    with state_lock:
        active_jobs = [job_id for job_id, job in jobs.items() if job.get("status") in {"queued", "running", "cancelling"}]
        for job_id in active_jobs:
            jobs[job_id].update(status="cancelled", log="Приложение закрывается. Уже созданные файлы сохранены.")
    for job_id in active_jobs:
        future = job_futures.get(job_id)
        if future:
            future.cancel()
    # Werkzeug must be shut down from another thread, otherwise the current
    # request can deadlock while the server waits for itself to finish.
    threading.Thread(target=flask_server.shutdown, daemon=True).start()
    return jsonify(status="shutting_down")


if __name__ == "__main__":
    import webbrowser

    port = int(os.environ.get("PORT", "5050"))
    flask_server = make_server("127.0.0.1", port, app, threaded=True)
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    try:
        flask_server.serve_forever()
    finally:
        if telegram_client:
            try:
                run_async(telegram_client.disconnect())
            except Exception:
                pass
        loop.call_soon_threadsafe(loop.stop)
