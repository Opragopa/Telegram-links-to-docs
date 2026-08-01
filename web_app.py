#!/usr/bin/env python3
"""Local web UI: Telegram login and export without terminal interaction."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from openpyxl import Workbook
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from telegram_posts_export import export_authenticated, parse_date


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("TG_DATA_DIR", Path.home() / "Library/Application Support/Telegram Posts Exporter"))
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


def load_api_config() -> dict[str, str]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_api_config(api_id: str, api_hash: str) -> None:
    CONFIG_PATH.write_text(json.dumps({"api_id": api_id, "api_hash": api_hash}), encoding="utf-8")
    CONFIG_PATH.chmod(0o600)


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
        row = {key: item.get(key, "") for key, _ in TABLE_COLUMNS}
        if row["messageId"] == "":
            raise ValueError(f"У поста №{index} отсутствует messageId")
        row["text"] = str(row["text"] or "")
        row["hasMedia"] = "Да" if bool(row["hasMedia"]) else "Нет"
        row["isForwarded"] = "Да" if bool(row["isForwarded"]) else "Нет"
        rows.append(row)
    return rows


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
    widths = {"A": 22, "B": 12, "C": 42, "D": 24, "E": 12, "F": 80, "G": 14, "H": 20}
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width
    for cell in sheet[1]:
        cell.font = cell.font.copy(bold=True)
    for row in sheet.iter_rows(min_row=2):
        row[2].hyperlink = row[2].value or None
        row[2].style = "Hyperlink"
        row[5].alignment = row[5].alignment.copy(wrap_text=True, vertical="top")
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
        with state_lock:
            jobs[job_id].update(status="running", log="Собираю посты и создаю скриншоты...")
        output = await export_authenticated(telegram_client, channel, start, end, str(DATA_DIR / "exports"))
        with state_lock:
            jobs[job_id].update(status="done", log=f"Готово. Результаты: {output}")
    except Exception as exc:
        with state_lock:
            jobs[job_id].update(status="error", log=f"Ошибка: {exc}")


@app.get("/")
def index():
    return render_template("index.html")


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
    return jsonify(authorized=bool(telegram_client and run_async(telegram_client.is_user_authorized())))


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
    if not telegram_client or not run_async(telegram_client.is_user_authorized()):
        return jsonify(error="Сначала войдите в Telegram"), 401
    channel, from_date, to_date = (str(data.get(key, "")).strip() for key in ("channel", "from_date", "to_date"))
    if not all((channel, from_date, to_date)):
        return jsonify(error="Заполните канал и период"), 400
    job_id = uuid.uuid4().hex[:10]
    with state_lock:
        jobs[job_id] = {"status": "queued", "log": "Задача поставлена в очередь"}
    asyncio.run_coroutine_threadsafe(export_job(job_id, channel, from_date, to_date), loop)
    return jsonify(job_id=job_id)


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    with state_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify(error="Задача не найдена"), 404
    return jsonify(**job)


if __name__ == "__main__":
    import webbrowser
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5050")).start()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5050")), debug=False)
