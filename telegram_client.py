"""
telegram_client.py — single place for all Telegram messaging.

Usage:
    from telegram_client import send_message, format_bloomberg_post
"""

import os
import logging
import requests
from datetime import datetime
from config import CONFIG
from zoneinfo import ZoneInfo

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

def format_bloomberg_post(post: dict) -> str:
    """Format a single Bloomberg post for Telegram."""
    ts   = _to_local_time(post.get('timestamp_raw') or post.get('timestamp', ''))
    text = post.get('body') or post.get('title', '')
    return (
        f"📰 <b>Bloomberg</b>\n\n"
        f"• [{ts}] {text}\n\n"
        f"⏰ {datetime.now().astimezone(_TZ).strftime('%d/%m %H:%M')}"
    )