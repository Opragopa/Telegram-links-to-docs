#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VERSION="${1:-0.0.3}"
DIST="$ROOT/dist"
STAGE="$(mktemp -d)"
APP_NAME="Telegram Posts Exporter.app"
APP="$STAGE/$APP_NAME"

trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$DIST" "$STAGE"
ditto "$ROOT/$APP_NAME" "$APP"
PAYLOAD="$APP/Contents/Resources/app"
mkdir -p "$PAYLOAD/templates"
cp "$ROOT/start.sh" "$ROOT/web_app.py" "$ROOT/telegram_posts_export.py" "$ROOT/requirements.txt" "$PAYLOAD/"
cp "$ROOT/templates/index.html" "$PAYLOAD/templates/index.html"
chmod +x "$PAYLOAD/start.sh" "$APP/Contents/MacOS/Telegram Posts Exporter"

OUTPUT="$DIST/Telegram-Posts-Exporter-$VERSION.dmg"
rm -f "$OUTPUT"
hdiutil create -volname "Telegram Posts Exporter" -srcfolder "$STAGE" -ov -format UDZO "$OUTPUT"
echo "Created: $OUTPUT"
