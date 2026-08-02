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
    date_local: str
    text: str
    url: str
    channel: str
    channel_title: str
    author: str
    views: int
    forwards: int
    replies: int
    reactions: int
    reactions_detail: str
    has_media: bool
    media_type: str
    album_size: int
    is_forwarded: bool
    forwarded_from: str
    char_count: int
    screenshot: str = ""


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


def media_kind(message: Message) -> str:
    """Human readable media type used in the report table."""
    if message.photo:
        return "Фото"
    if message.video:
        return "Видео"
    if message.voice:
        return "Голосовое"
    if message.video_note:
        return "Кружок"
    if message.audio:
        return "Аудио"
    if message.gif:
        return "GIF"
    if message.sticker:
        return "Стикер"
    if message.poll:
        return "Опрос"
    if message.document:
        return "Файл"
    if message.web_preview:
        return "Ссылка"
    return ""


def reaction_summary(message: Message) -> tuple[int, str]:
    """Return the total reaction count and a compact "👍 12 · ❤️ 4" breakdown."""
    reactions = getattr(message, "reactions", None)
    results = getattr(reactions, "results", None) or []
    total = 0
    parts = []
    for item in results:
        count = int(getattr(item, "count", 0) or 0)
        total += count
        # Custom emoji reactions carry a document_id instead of a character.
        emoji = getattr(item.reaction, "emoticon", None) or "⭐"
        parts.append(f"{emoji} {count}")
    return total, " · ".join(parts)


def forwarded_source(message: Message) -> str:
    forward = getattr(message, "fwd_from", None)
    if not forward:
        return ""
    chat = getattr(message.forward, "chat", None) if getattr(message, "forward", None) else None
    sender = getattr(message.forward, "sender", None) if getattr(message, "forward", None) else None
    name = getattr(chat, "title", None) or getattr(sender, "username", None) or getattr(forward, "from_name", "")
    return str(name or "")


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
</style></head><body><main class='page'><article class='post'><div class='header'><div class='avatar'>✈</div><div><div class='channel'>{html.escape(channel_name)}</div><div class='date'>{html.escape(post.date_local or post.date)}</div></div></div><div class='text'>{text}</div>{media}<div class='footer'>Пост #{post.id} · <span class='link'>{html.escape(post.url)}</span></div></article></main></body></html>"""


class ScreenshotRenderer:
    """Render post screenshots reusing a single browser for the whole export.

    Launching Chromium costs ~1-2 seconds, so a browser per post used to
    dominate the runtime of an export. One browser is started lazily and
    reused; if Playwright is unavailable the export continues without images.
    """

    def __init__(self, html_dir: Path, screenshot_dir: Path, channel_name: str) -> None:
        self.html_dir = html_dir
        self.screenshot_dir = screenshot_dir
        self.channel_name = channel_name
        self._playwright = None
        self._browser = None
        self.available = True
        self.last_error = ""

    async def __aenter__(self) -> "ScreenshotRenderer":
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.available = False
            self.last_error = "Playwright не установлен: pip install -r requirements.txt && playwright install chromium"
            return self
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch()
        except Exception as exc:  # missing browser binaries, sandbox issues, ...
            self.available = False
            self.last_error = f"Не удалось запустить Chromium: {exc}"
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def capture(self, post: ExportedPost) -> str:
        """Return the relative screenshot path, or "" when rendering failed."""
        if not self.available:
            return ""
        target = self.screenshot_dir / f"{post.id}.png"
        try:
            await self._capture_widget(post, target)
            return f"screenshots/{target.name}"
        except Exception as exc:
            self.last_error = f"Пост {post.id}: виджет Telegram недоступен ({exc}), использован резервный вид"
        try:
            await self._capture_fallback(post, target)
            return f"screenshots/{target.name}"
        except Exception as exc:
            self.last_error = f"Пост {post.id}: не удалось создать скриншот ({exc})"
            return ""

    async def _capture_widget(self, post: ExportedPost, target: Path) -> None:
        """Capture the official Telegram post widget with real media and reactions."""
        # Private channels use t.me/c/<id>/<msg> links, which have no public
        # widget. Asking for one returns an error card that still looks like a
        # message, so it has to be skipped before it gets screenshotted.
        if "/c/" in post.url:
            raise RuntimeError("приватный канал без публичного виджета")
        separator = "&" if "?" in post.url else "?"
        page = await self._browser.new_page(viewport={"width": 1000, "height": 1200}, device_scale_factor=1)
        try:
            await page.goto(f"{post.url}{separator}embed=1&mode=tme", wait_until="domcontentloaded", timeout=30000)
            widget = page.locator(".tgme_widget_message").first
            await widget.wait_for(state="visible", timeout=15000)
            if "err_message" in (await widget.get_attribute("class") or ""):
                raise RuntimeError("пост недоступен публично")
            await page.wait_for_timeout(1200)
            await widget.screenshot(path=str(target), animations="disabled")
        finally:
            await page.close()

    async def _capture_fallback(self, post: ExportedPost, target: Path) -> None:
        """Render posts of private channels locally from Telethon data."""
        html_path = self.html_dir / f"{post.id}.html"
        html_path.write_text(render_html(post, self.channel_name), encoding="utf-8")
        page = await self._browser.new_page(viewport={"width": 900, "height": 600}, device_scale_factor=1)
        try:
            await page.goto(html_path.as_uri())
            # Shoot the card itself so the image has no empty viewport padding.
            await page.locator(".page").screenshot(path=str(target))
        finally:
            await page.close()


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
        posts, out_dir = await export_authenticated(client, args.channel, start, end, args.output)
        return out_dir


def group_messages(messages: list[Message]) -> list[list[Message]]:
    """Merge album messages (same grouped_id) into a single logical post."""
    grouped: list[list[Message]] = []
    positions: dict[Any, int] = {}
    for message in messages:
        key = message.grouped_id or f"single:{message.id}"
        if key not in positions:
            positions[key] = len(grouped)
            grouped.append([])
        grouped[positions[key]].append(message)
    return grouped


def build_post(group: list[Message], entity: Any, channel: str, channel_name: str) -> ExportedPost:
    # An album consists of several messages with one grouped_id. Use the first
    # message in Telegram's link order and merge the album into one post.
    primary = min(group, key=lambda item: item.id)
    text_message = next((item for item in group if item.message), primary)
    published = min(item.date for item in group).astimezone(timezone.utc)
    views = max((int(getattr(item, "views", 0) or 0) for item in group), default=0)
    forwards = max((int(getattr(item, "forwards", 0) or 0) for item in group), default=0)
    replies = max((int(getattr(getattr(item, "replies", None), "replies", 0) or 0) for item in group), default=0)
    reactions, reactions_detail = reaction_summary(primary)
    text = safe_text(text_message)
    media_types = [kind for kind in (media_kind(item) for item in group) if kind]
    return ExportedPost(
        id=primary.id,
        date=published.isoformat(),
        date_local=published.astimezone().strftime("%d.%m.%Y %H:%M"),
        text=text,
        url=message_url(entity, primary.id),
        channel=getattr(entity, "username", None) or channel_slug(channel),
        channel_title=channel_name,
        author=getattr(getattr(text_message, "sender", None), "username", "") or "",
        views=views,
        forwards=forwards,
        replies=replies,
        reactions=reactions,
        reactions_detail=reactions_detail,
        has_media=bool(media_types),
        media_type=media_types[0] if media_types else "",
        album_size=len(group),
        is_forwarded=bool(getattr(primary, "fwd_from", None)),
        forwarded_from=forwarded_source(primary),
        char_count=0 if text == "[Пост без текста]" else len(text),
    )


async def export_authenticated(
    client: TelegramClient,
    channel: str,
    start: datetime,
    end: datetime,
    output: str = "exports",
    progress_callback=None,
) -> tuple[list[ExportedPost], Path]:
    """Export with an already authenticated client (used by the web UI)."""
    if end <= start:
        raise ValueError("Дата окончания должна быть позже даты начала")
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

    grouped_messages = group_messages(messages)
    posts: list[ExportedPost] = []
    total = len(grouped_messages)
    if progress_callback:
        await progress_callback(0, total, f"Найдено постов: {total}. Создаю скриншоты...")

    async with ScreenshotRenderer(html_dir, screenshot_dir, channel_name) as renderer:
        if not renderer.available and progress_callback:
            await progress_callback(0, total, f"Скриншоты отключены — {renderer.last_error}")
        for processed, group in enumerate(grouped_messages, start=1):
            post = build_post(group, entity, channel, channel_name)
            post.screenshot = await renderer.capture(post)
            posts.append(post)
            if progress_callback:
                await progress_callback(processed, total, f"{post.date_local} · {post.url}")
            print(f"[{len(posts)}] {post.id}: {post.url}")

    write_export_files(out_dir, posts)
    print(f"Готово: {len(posts)} постов → {out_dir}")
    return posts, out_dir


def write_export_files(out_dir: Path, posts: list[ExportedPost]) -> None:
    records = [asdict(post) for post in posts]
    (out_dir / "posts.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "posts.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(ExportedPost.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(records)


def build_parser() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Собрать посты Telegram-канала за период и сделать PNG-скриншоты.")
    parser.add_argument("--channel", default=os.environ.get("TG_CHANNEL"), help="@username, ссылка t.me или ID канала")
    parser.add_argument("--from", dest="from_date", required=True, help="Начало: YYYY-MM-DD или ISO datetime")
    parser.add_argument("--to", dest="to_date", required=True, help="Конец включительно: YYYY-MM-DD или ISO datetime")
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
