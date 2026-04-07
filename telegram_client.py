"""
telegram_client.py — single place for all Telegram messaging.

Usage:
    from telegram_client import send_message, format_bloomberg_posts
"""

import os
import logging
import requests
from datetime import datetime

log = logging.getLogger('MonsieurMarket')


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


def format_bloomberg_posts(posts: list[dict], trigger: str = 'poll') -> str:
    """
    Format Bloomberg posts into a Telegram message.

    trigger='poll'        — background 15-min cycle, just the headlines
    trigger='price_spike' — triggered by Brent move, adds context header
    """
    if trigger == 'price_spike':
        header = "📰 <b>Bloomberg — fresh context after price move</b>\n"
    else:
        header = "📰 <b>Bloomberg — new posts</b>\n"

    lines = [header]
    for p in posts[:5]:
        ts = p.get('timestamp_raw', '')[:16].replace('T', ' ')
        lines.append(f"• [{ts}] {p.get('title', '')[:100]}")

    lines.append(f"\n⏰ {datetime.now().strftime('%d/%m %H:%M')} Paris")
    return "\n".join(lines)