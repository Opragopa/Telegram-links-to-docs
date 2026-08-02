#!/usr/bin/env bash
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Inside a .app the payload may sit on a read-only DMG, and anything written
# into the bundle is lost on the next update. Keep the environment and the
# user data in the home directory; a plain repo checkout keeps using .venv.
case "$PROJECT_DIR" in
  *.app/Contents/Resources/app)
    HOME_DIR="${TG_HOME:-$HOME/.telegram-posts-exporter}"
    VENV_DIR="$HOME_DIR/venv"
    DATA_DIR="${TG_DATA_DIR:-$HOME_DIR/data}"
    ;;
  *)
    HOME_DIR="$PROJECT_DIR"
    VENV_DIR="$PROJECT_DIR/.venv"
    DATA_DIR="${TG_DATA_DIR:-$PROJECT_DIR/data}"
    ;;
esac

LOG_FILE="$HOME_DIR/setup.log"
SETUP_MARKER="$VENV_DIR/.setup-complete-v4"
export TG_DATA_DIR="$DATA_DIR"
export TG_SESSION="${TG_SESSION:-$DATA_DIR/telegram_posts_export}"
export PYTHONDONTWRITEBYTECODE=1

notify() {
  osascript -e "display notification \"$1\" with title \"Telegram Posts Exporter\"" >/dev/null 2>&1 || true
  echo "$1"
}

fail() {
  local message="$1"
  echo "$message" >&2
  osascript -e "display dialog \"$message\" with title \"Telegram Posts Exporter\" buttons {\"OK\"} default button 1 with icon stop" >/dev/null 2>&1 || true
  exit 1
}

if ! mkdir -p "$HOME_DIR" "$DATA_DIR" 2>/dev/null; then
  fail "Нет доступа на запись в $HOME_DIR. Скопируйте приложение в папку «Программы» и запустите оттуда."
fi

if ! command -v python3 >/dev/null 2>&1; then
  fail "Не найден python3. Установите инструменты разработчика командой xcode-select --install и запустите приложение снова."
fi

if [ ! -f "$SETUP_MARKER" ]; then
  notify "Первый запуск: устанавливаю компоненты. Это займёт несколько минут, браузер откроется сам."
  {
    echo "=== setup $(date) ==="
    python3 -m venv "$VENV_DIR" &&
    "$VENV_DIR/bin/python" -m pip install --upgrade pip &&
    "$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt" &&
    "$VENV_DIR/bin/python" -m playwright install chromium
  } >>"$LOG_FILE" 2>&1 || fail "Не удалось установить компоненты. Подробности: $LOG_FILE"
  touch "$SETUP_MARKER"
  notify "Готово, открываю интерфейс."
fi

echo "Откройте http://127.0.0.1:${PORT:-5050}"
exec "$VENV_DIR/bin/python" "$PROJECT_DIR/web_app.py"
