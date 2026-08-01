#!/usr/bin/env python3
"""Local web UI: Telegram login and export without terminal interaction."""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from telegram_posts_export import export_authenticated, parse_date


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("TG_DATA_DIR", Path.home() / "Library/Application Support/Telegram Posts Exporter"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
state_lock = threading.Lock()
telegram_client: TelegramClient | None = None
auth_phone = ""
auth_code_hash = ""
auth_needs_password = False
jobs: dict[str, dict[str, str]] = {}


def run_async(coroutine):
    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    return future.result()


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
        status = run_async(begin_login(int(data.get("api_id", "")), str(data.get("api_hash", "")), str(data.get("phone", "")).strip()))
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
