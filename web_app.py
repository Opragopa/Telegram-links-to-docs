#!/usr/bin/env python3
"""Local web UI: Telegram login and export without terminal interaction."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import threading
import time
import uuid
from copy import copy
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from openpyxl import Workbook
from openpyxl.drawing.image import Image as ExcelImage
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from telegram_posts_export import export_authenticated, parse_date


ROOT = Path(__file__).resolve().parent
APP_VERSION = "0.0.4"
DATA_DIR = Path(os.environ.get("TG_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
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
jobs: dict[str, dict[str, str]] = {}
job_futures = {}


def load_api_config() -> dict[str, str]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_api_config(api_id: str, api_hash: str) -> None:
    CONFIG_PATH.write_text(json.dumps({"api_id": api_id, "api_hash": api_hash}), encoding="utf-8")
    CONFIG_PATH.chmod(0o600)


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


TABLE_COLUMNS = [
    ("channel", "Канал"),
    ("messageId", "ID поста"),
    ("url", "Ссылка"),
    ("date", "Дата"),
    ("views", "Просмотры"),
    ("text", "Текст"),
    ("hasMedia", "Есть медиа"),
    ("isForwarded", "Пересланный пост"),
    ("screenshot", "Скриншот"),
]


def parse_posts_json(raw: str) -> list[dict]:
    """Validate the exported JSON format and return predictable table rows."""
    payload = json.loads(raw)
    if isinstance(payload, dict):
        payload = payload.get("posts", payload.get("items", []))
    if not isinstance(payload, list):
        raise ValueError("JSON должен содержать массив постов")

    rows = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Пост №{index} должен быть объектом JSON")
        # Accept both the source export format (messageId/hasMedia) and the
        # app's own posts.json format (id/has_media).
        aliases = {
            "messageId": ("messageId", "id"),
            "hasMedia": ("hasMedia", "has_media"),
            "isForwarded": ("isForwarded", "is_forwarded"),
            "screenshot": ("screenshot", "screenshot_path"),
        }
        row = {}
        for key, _ in TABLE_COLUMNS:
            candidates = aliases.get(key, (key,))
            row[key] = next((item[name] for name in candidates if name in item), "")
        if row["messageId"] == "":
            raise ValueError(f"У поста №{index} отсутствует messageId")
        row["text"] = str(row["text"] or "")
        row["hasMedia"] = "Да" if bool(row["hasMedia"]) else "Нет"
        row["isForwarded"] = "Да" if bool(row["isForwarded"]) else "Нет"
        rows.append(row)
    return rows


def find_screenshot(relative_path: str) -> Path | None:
    if not relative_path:
        return None
    filename = Path(relative_path).name
    candidates = list((DATA_DIR / "exports").rglob(filename))
    return next((path for path in candidates if path.parent.name == "screenshots"), None)


def create_table_exports(raw: str) -> tuple[str, str, int]:
    rows = parse_posts_json(raw)
    export_id = uuid.uuid4().hex[:12]
    xlsx_path = TABLE_DIR / f"telegram-posts-{export_id}.xlsx"
    csv_path = TABLE_DIR / f"telegram-posts-{export_id}.csv"

    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow([label for _, label in TABLE_COLUMNS])
        for row in rows:
            writer.writerow([row[key] for key, _ in TABLE_COLUMNS])

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Посты"
    sheet.append([label for _, label in TABLE_COLUMNS])
    for row in rows:
        sheet.append([row[key] for key, _ in TABLE_COLUMNS])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    # Excel images are anchored to cells rather than being true cell values.
    # Keep the image column and row dimensions within Excel's limits so the
    # complete screenshot remains visible without cropping or overlap.
    widths = {"A": 22, "B": 12, "C": 42, "D": 24, "E": 12, "F": 80, "G": 14, "H": 20, "I": 72}
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width
    for cell in sheet[1]:
        font = copy(cell.font)
        font.bold = True
        cell.font = font
    for row in sheet.iter_rows(min_row=2):
        row[2].hyperlink = row[2].value or None
        row[2].style = "Hyperlink"
        alignment = copy(row[5].alignment)
        alignment.wrap_text = True
        alignment.vertical = "top"
        row[5].alignment = alignment
        screenshot_path = find_screenshot(row[8].value)
        if screenshot_path:
            image = ExcelImage(str(screenshot_path))
            max_width = 500
            max_height = 500
            scale = min(max_width / image.width, max_height / image.height, 1)
            image.width = max(1, round(image.width * scale))
            image.height = max(1, round(image.height * scale))
            sheet.add_image(image, f"I{row[0].row}")
            sheet.row_dimensions[row[0].row].height = min(image.height * 0.75 + 8, 409.5)
    workbook.save(xlsx_path)
    return xlsx_path.name, csv_path.name, len(rows)


async def begin_login(api_id: int, api_hash: str, phone: str) -> str:
    global telegram_client, auth_phone, auth_code_hash, auth_needs_password
    if telegram_client:
        await telegram_client.disconnect()
    session = os.environ.get("TG_SESSION", str(DATA_DIR / "telegram_posts_export"))
    telegram_client = TelegramClient(session, api_id, api_hash)
    await telegram_client.connect()
    if await telegram_client.is_user_authorized():
        return "authorized"
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

        output = await export_authenticated(
            telegram_client, channel, start, end, str(DATA_DIR / "exports"), progress_callback=update_progress
        )
        elapsed = round(time.monotonic() - started)
        with state_lock:
            jobs[job_id].update(status="done", progress=100, elapsed_seconds=elapsed, eta_seconds=0, log=f"Готово за {elapsed} сек. Результаты: {output}")
    except Exception as exc:
        with state_lock:
            jobs[job_id].update(status="error", log=f"Ошибка: {exc}")
    except asyncio.CancelledError:
        with state_lock:
            jobs[job_id].update(status="cancelled", log="Экспорт остановлен. Уже созданные файлы сохранены.")
        raise


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
        status = run_async(begin_login(int(api_id), api_hash, str(data.get("phone", "")).strip()))
        return jsonify(status=status)
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/auth/verify")
def auth_verify():
    data = request.get_json(silent=True) or {}
    try:
        status = run_async(finish_login(str(data.get("code", "")).strip(), str(data.get("password", ""))))
        return jsonify(status=status)
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/auth/status")
def auth_status():
    return jsonify(authorized=run_async(ensure_saved_session()))


@app.get("/api/config")
def api_config():
    config = load_api_config()
    return jsonify(api_id=config.get("api_id", ""), has_api_hash=bool(config.get("api_hash")))


@app.post("/api/convert-json")
def convert_json():
    """Convert pasted JSON or an uploaded JSON file into Excel and CSV."""
    try:
        if "file" in request.files:
            uploaded = request.files["file"]
            raw = uploaded.read().decode("utf-8-sig")
        else:
            data = request.get_json(silent=True) or {}
            raw = str(data.get("json", ""))
        if not raw.strip():
            return jsonify(error="Загрузите JSON-файл или вставьте JSON-текст"), 400
        xlsx_name, csv_name, count = create_table_exports(raw)
        return jsonify(
            count=count,
            xlsx_url=f"/api/table-exports/{xlsx_name}",
            csv_url=f"/api/table-exports/{csv_name}",
        )
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


if __name__ == "__main__":
    import webbrowser
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5050")).start()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5050")), debug=False)
