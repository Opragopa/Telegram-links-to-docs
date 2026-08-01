#!/usr/bin/env python3
"""Export Telegram channel posts and render each post as a PNG screenshot."""

from __future__ import annotations

import argparse
import asyncio
import csv
import html
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.custom.message import Message


@dataclass
class ExportedPost:
    id: int
    date: str
    text: str
    url: str
    author: str
    has_media: bool
    screenshot: str


def parse_date(value: str, *, end_of_day: bool = False) -> datetime:
    """Parse YYYY-MM-DD or ISO datetime and return an aware UTC datetime."""
    if len(value) == 10:
        parsed = datetime.combine(
            date.fromisoformat(value),
            time.max if end_of_day else time.min,
        )
    else:
        parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def channel_slug(value: str) -> str:
    value = value.strip().rstrip("/").split("/")[-1].lstrip("@")
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value)
    return value or "channel"


def message_url(entity: Any, message_id: int) -> str:
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    # Telegram's internal ID for private/supergroup channels is -100XXXXXXXXXX.
    raw_id = str(getattr(entity, "id", ""))
    internal_id = raw_id[4:] if raw_id.startswith("-100") else raw_id
    return f"https://t.me/c/{internal_id}/{message_id}"


def safe_text(message: Message) -> str:
    text = message.message or ""
    return text.strip() or "[Пост без текста]"


def render_html(post: ExportedPost, channel_name: str) -> str:
    text = html.escape(post.text).replace("\n", "<br>")
    media = "<div class='media'>МЕДИА В ПОСТЕ</div>" if post.has_media else ""
    return f"""<!doctype html>
<html lang='ru'><head><meta charset='utf-8'><style>
* {{ box-sizing: border-box; }} body {{ margin:0; background:#dce8f2; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; color:#17212b; }}
.page {{ width:900px; padding:42px; }} .post {{ background:#fff; border-radius:18px; padding:30px 34px 24px; box-shadow:0 8px 28px #8aa0b533; }}
.header {{ display:flex; align-items:center; gap:14px; margin-bottom:22px; }} .avatar {{ width:52px; height:52px; border-radius:50%; background:#229ed9; color:white; display:grid; place-items:center; font-size:25px; font-weight:700; }}
.channel {{ font-size:22px; font-weight:700; }} .date {{ color:#7b8a97; font-size:16px; margin-top:3px; }} .text {{ font-size:25px; line-height:1.42; white-space:normal; overflow-wrap:anywhere; }}
.media {{ margin-top:22px; border-radius:12px; background:#eef4f7; color:#78909c; height:130px; display:grid; place-items:center; font-size:18px; letter-spacing:1px; }}
.footer {{ border-top:1px solid #e8edf0; margin-top:26px; padding-top:15px; color:#78909c; font-size:15px; }} .link {{ color:#168acd; }}
</style></head><body><main class='page'><article class='post'><div class='header'><div class='avatar'>✈</div><div><div class='channel'>{html.escape(channel_name)}</div><div class='date'>{html.escape(post.date)}</div></div></div><div class='text'>{text}</div>{media}<div class='footer'>Пост #{post.id} · <span class='link'>{html.escape(post.url)}</span></div></article></main></body></html>"""


async def take_screenshot(post: ExportedPost, channel_name: str, html_dir: Path, screenshot_dir: Path) -> None:
    try:
        await take_telegram_widget_screenshot(post, screenshot_dir)
        return
    except Exception as exc:
        print(f"Telegram Widget недоступен для поста {post.id}, использую резервный вид: {exc}")

    html_path = html_dir / f"{post.id}.html"
    html_path.write_text(render_html(post, channel_name), encoding="utf-8")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Для скриншотов установите Playwright: pip install -r requirements.txt && playwright install chromium") from exc

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(device_scale_factor=1)
        await page.goto(html_path.as_uri())
        await page.screenshot(path=str(screenshot_dir / f"{post.id}.png"), full_page=True)
        await browser.close()


async def take_telegram_widget_screenshot(post: ExportedPost, screenshot_dir: Path) -> None:
    """Capture the official Telegram post widget with real media and reactions."""
    from playwright.async_api import async_playwright

    separator = "&" if "?" in post.url else "?"
    widget_url = f"{post.url}{separator}embed=1&mode=tme"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1000, "height": 1200}, device_scale_factor=1)
        try:
            await page.goto(widget_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500)
            widget = page.locator(".tgme_widget_message")
            await widget.wait_for(state="visible", timeout=15000)
            await widget.screenshot(path=str(screenshot_dir / f"{post.id}.png"), animations="disabled")
        finally:
            await browser.close()


async def export(args: argparse.Namespace) -> Path:
    load_dotenv(args.env_file)
    api_id = int(os.environ.get("TG_API_ID", "0"))
    api_hash = os.environ.get("TG_API_HASH", "")
    if not api_id or not api_hash:
        raise RuntimeError("Укажите TG_API_ID и TG_API_HASH в .env. Их можно получить на https://my.telegram.org")

    start = parse_date(args.from_date)
    end = parse_date(args.to_date, end_of_day=True)
    if end <= start:
        raise ValueError("Дата окончания должна быть позже даты начала")

    session = os.environ.get("TG_SESSION", "telegram_posts_export")
    async with TelegramClient(session, api_id, api_hash) as client:
        return await export_authenticated(client, args.channel, start, end, args.output)


async def export_authenticated(client: TelegramClient, channel: str, start: datetime, end: datetime, output: str = "exports", progress_callback=None) -> Path:
    """Export with an already authenticated client (used by the web UI)."""
    entity = await client.get_entity(channel)
    out_dir = Path(output) / f"{channel_slug(channel)}_{start.date()}_{(end - timedelta(microseconds=1)).date()}"
    screenshot_dir = out_dir / "screenshots"
    html_dir = out_dir / "html"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    html_dir.mkdir(parents=True, exist_ok=True)

    channel_name = getattr(entity, "title", None) or getattr(entity, "username", None) or str(channel)
    messages = []
    async for message in client.iter_messages(entity, offset_date=end):
        if not message.date:
            continue
        message_date = message.date.astimezone(timezone.utc)
        if message_date < start:
            break
        if message_date >= end:
            continue
        messages.append(message)

    grouped_messages = []
    group_positions = {}
    for message in messages:
        group_key = message.grouped_id or f"single:{message.id}"
        if group_key not in group_positions:
            group_positions[group_key] = len(grouped_messages)
            grouped_messages.append([])
        grouped_messages[group_positions[group_key]].append(message)

    posts: list[ExportedPost] = []
    total = len(grouped_messages)
    if progress_callback:
        await progress_callback(0, total, "Посты найдены. Создаю скриншоты...")
    for processed, group in enumerate(grouped_messages, start=1):
        # An album consists of several messages with one grouped_id. Use the
        # first message in Telegram's link order and merge the album into one post.
        primary_message = min(group, key=lambda item: item.id)
        text_message = next((item for item in group if item.message), primary_message)
        message_date = min(item.date for item in group).astimezone(timezone.utc)
        post = ExportedPost(
            id=primary_message.id,
            date=message_date.isoformat(),
            text=safe_text(text_message),
            url=message_url(entity, primary_message.id),
            author=getattr(getattr(text_message, "sender", None), "username", "") or "",
            has_media=any(bool(item.media) for item in group),
            screenshot=f"screenshots/{primary_message.id}.png",
        )
        posts.append(post)
        await take_screenshot(post, channel_name, html_dir, screenshot_dir)
        if progress_callback:
            await progress_callback(processed, total, post.url)
        print(f"[{len(posts)}] {post.id}: {post.url}")

    records = [asdict(post) for post in posts]
    (out_dir / "posts.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "posts.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(ExportedPost.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(records)
    print(f"Готово: {len(posts)} постов → {out_dir}")
    return out_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Собрать посты Telegram-канала за период и сделать PNG-скриншоты.")
    parser.add_argument("--channel", default=os.environ.get("TG_CHANNEL"), help="@username, ссылка t.me или ID канала")
    parser.add_argument("--from", dest="from_date", required=True, help="Начало: YYYY-MM-DD или ISO datetime")
    parser.add_argument("--to", dest="to_date", required=True, help="Конец: YYYY-MM-DD или ISO datetime")
    parser.add_argument("--output", default="exports", help="Каталог для результатов")
    parser.add_argument("--env-file", default=".env", help="Путь к .env")
    args = parser.parse_args()
    if not args.channel:
        parser.error("Укажите --channel или TG_CHANNEL в .env")
    return args


if __name__ == "__main__":
    try:
        asyncio.run(export(build_parser()))
    except KeyboardInterrupt:
        print("Остановлено")
    except Exception as exc:
        raise SystemExit(f"Ошибка: {exc}")
