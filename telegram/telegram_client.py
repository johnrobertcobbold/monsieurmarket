"""
telegram_client.py — single place for all Telegram messaging.

Usage:
    from telegram_client import send_message, format_bloomberg_post, format_bloomberg_posts
"""

import os
import logging
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

from config import CONFIG

log = logging.getLogger('MonsieurMarket')

_TZ = ZoneInfo(CONFIG.get('display_timezone', 'UTC'))


def _to_local_time(iso: str) -> str:
    """Convert ISO UTC timestamp to local display time (from config display_timezone)."""
    try:
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
        return dt.astimezone(_TZ).strftime('%d/%m %H:%M')
    except Exception:
        return iso[:16].replace('T', ' ') + ' UTC'


def send_message(message: str, force: bool = False) -> bool:
    """Send a message to Telegram. Returns True on success.
    force is accepted for backwards compatibility but ignored —
    quiet hours logic has been removed."""
    token   = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')

    if not token or not chat_id:
        log.error("Telegram credentials not set")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id":    chat_id,
            "text":       message,
            "parse_mode": "HTML",
        }, timeout=10)
        r.raise_for_status()
        log.info("📲 Telegram message sent")
        return True
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


def _freshness(iso: str) -> str:
    """Return a freshness indicator based on post age."""
    try:
        dt      = datetime.fromisoformat(iso.replace('Z', '+00:00'))
        age_sec = (datetime.now(dt.tzinfo) - dt).total_seconds()
        if age_sec < 120:
            return f"🟢 {int(age_sec)}s ago"
        elif age_sec < 600:
            return f"🟡 {int(age_sec // 60)}min ago"
        else:
            return f"🔴 {int(age_sec // 60)}min ago"
    except Exception:
        return ""


def format_bloomberg_post(post: dict) -> str:
    """Format a single Bloomberg post for Telegram. Timestamp converted to local time."""
    ts_raw   = post.get('timestamp_raw') or post.get('timestamp', '')
    ts       = _to_local_time(ts_raw)
    fresh    = _freshness(ts_raw)
    text     = post.get('body') or post.get('title', '')
    fresh_str = f" — {fresh}" if fresh else ""
    return (
        f"📰 <b>Bloomberg</b>{fresh_str}\n\n"
        f"• [{ts}] {text}\n\n"
        f"⏰ {datetime.now().astimezone(_TZ).strftime('%d/%m %H:%M')}"
    )


def format_bloomberg_posts(posts: list[dict], trigger: str = 'poll') -> str:
    """
    Format multiple Bloomberg posts into a Telegram message.

    trigger='poll'        — background poll, just the headlines
    trigger='price_spike' — triggered by Brent move, adds context header
    """
    if trigger == 'price_spike':
        header = "📰 <b>Bloomberg — fresh context after price move</b>\n"
    else:
        header = "📰 <b>Bloomberg — new posts</b>\n"

    lines = [header]
    for p in posts[:5]:
        ts = _to_local_time(p.get('timestamp_raw') or p.get('timestamp', ''))
        lines.append(f"• [{ts}] {p.get('title', '')[:100]}")

    lines.append(f"\n⏰ {datetime.now().astimezone(_TZ).strftime('%d/%m %H:%M')}")
    return "\n".join(lines)