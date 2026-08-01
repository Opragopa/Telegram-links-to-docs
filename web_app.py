#!/usr/bin/env python3
"""Small local web UI for telegram_posts_export.py."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request


ROOT = Path(__file__).resolve().parent
app = Flask(__name__)
jobs: dict[str, dict[str, str]] = {}
jobs_lock = threading.Lock()


def run_job(job_id: str, command: list[str], env: dict[str, str]) -> None:
    log_path = ROOT / "exports" / f"job_{job_id}.log"
    log_path.parent.mkdir(exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, text=True)
        with jobs_lock:
            jobs[job_id]["status"] = "running"
        return_code = process.wait()
    with jobs_lock:
        jobs[job_id]["status"] = "done" if return_code == 0 else "error"
        jobs[job_id]["log"] = str(log_path.relative_to(ROOT))


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/export")
def start_export():
    data = request.get_json(silent=True) or request.form
    channel = str(data.get("channel", "")).strip()
    from_date = str(data.get("from_date", "")).strip()
    to_date = str(data.get("to_date", "")).strip()
    api_id = str(data.get("api_id", "")).strip()
    api_hash = str(data.get("api_hash", "")).strip()
    if not all((channel, from_date, to_date, api_id, api_hash)):
        return jsonify(error="Заполните все поля"), 400

    job_id = uuid.uuid4().hex[:10]
    env = os.environ.copy()
    env.update({"TG_API_ID": api_id, "TG_API_HASH": api_hash, "TG_CHANNEL": channel})
    command = [sys.executable, str(ROOT / "telegram_posts_export.py"), "--channel", channel, "--from", from_date, "--to", to_date]
    with jobs_lock:
        jobs[job_id] = {"status": "queued", "log": ""}
    threading.Thread(target=run_job, args=(job_id, command, env), daemon=True).start()
    return jsonify(job_id=job_id)


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify(error="Задача не найдена"), 404
    log_text = ""
    log_path = ROOT / job.get("log", f"exports/job_{job_id}.log")
    if log_path.exists():
        log_text = log_path.read_text(encoding="utf-8", errors="replace")[-5000:]
    return jsonify(**job, log_text=log_text)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5050")), debug=False)
