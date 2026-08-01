#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
SETUP_MARKER="$VENV_DIR/.setup-complete-v2"
DATA_DIR="${TG_DATA_DIR:-$HOME/Library/Application Support/Telegram Posts Exporter}"
mkdir -p "$DATA_DIR"
export TG_DATA_DIR="$DATA_DIR"
export TG_SESSION="${TG_SESSION:-$DATA_DIR/telegram_posts_export}"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "Создаю виртуальное окружение..."
  python3 -m venv "$VENV_DIR"
fi

if [ ! -f "$SETUP_MARKER" ]; then
  "$VENV_DIR/bin/python" -m pip install --upgrade pip
  "$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"
  "$VENV_DIR/bin/playwright" install chromium
  touch "$SETUP_MARKER"
fi
echo "Откройте http://127.0.0.1:5050"
exec "$VENV_DIR/bin/python" "$PROJECT_DIR/web_app.py"
