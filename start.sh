#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "Создаю виртуальное окружение..."
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"
"$VENV_DIR/bin/playwright" install chromium
echo "Откройте http://127.0.0.1:5050"
exec "$VENV_DIR/bin/python" "$PROJECT_DIR/web_app.py"
