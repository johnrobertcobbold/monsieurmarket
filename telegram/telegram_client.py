"""
telegram_client.py — single place for all Telegram messaging.

Usage:
    from telegram_client import send_message, format_bloomberg_post, format_bloomberg_posts
    from telegram_client import start_telegram_receiver
"""

import os
import logging
import requests
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from config import CONFIG

log = logging.getLogger('MonsieurMarket')

_TZ = ZoneInfo(CONFIG.get('display_timezone', 'UTC'))

MM_SIGNAL_URL = os.getenv('MM_SIGNAL_URL', 'http://localhost:3456/signal')


def _to_local_time(iso: str) -> str:
    """Convert ISO UTC timestamp to local display time (from config display_timezone)."""
    try:
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
        return dt.astimezone(_TZ).strftime('%d/%m %H:%M')
    except Exception:
        return iso[:16].replace('T', ' ') + ' UTC'


def send_message(message: str, force: bool = False) -> bool:
    """Send a message to Telegram. Returns True on success."""
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
    """Format a single Bloomberg post for Telegram."""
    ts_raw    = post.get('timestamp_raw') or post.get('timestamp', '')
    ts        = _to_local_time(ts_raw)
    fresh     = _freshness(ts_raw)
    text      = post.get('body') or post.get('title', '')
    fresh_str = f" — {fresh}" if fresh else ""
    return (
        f"📰 <b>Bloomberg</b>{fresh_str}\n\n"
        f"• [{ts}] {text}\n\n"
        f"⏰ {datetime.now().astimezone(_TZ).strftime('%d/%m %H:%M')}"
    )


def format_bloomberg_posts(posts: list[dict], trigger: str = 'poll') -> str:
    """Format multiple Bloomberg posts into a Telegram message."""
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


# ─────────────────────────────────────────────
# INCOMING COMMAND RECEIVER
# ─────────────────────────────────────────────
def _handle_command(text: str, chat_id: int):
    allowed = {int(os.getenv('TELEGRAM_CHAT_ID', 0))}
    if chat_id not in allowed:
        log.warning(f"⚠️ Unauthorized command from {chat_id}: {text}")
        return

    log.info(f"📩 Command received: {text}")
    
    # parse here so MM receives clean structured data
    parts = text.strip().lower().split()
    cmd   = parts[0]  # /straddle, /status, /pause etc

    try:
        requests.post(MM_SIGNAL_URL, json={
            'source': 'telegram',
            'type':   cmd.lstrip('/'),   # 'straddle', 'status', 'pause'
            'data':   {'text': text, 'parts': parts, 'chat_id': chat_id},
            'ts':     time.time(),
        }, timeout=3)
    except Exception as e:
        log.warning(f"Command forward failed: {e}")


def start_telegram_receiver():
    """Start long-polling for incoming Telegram commands in background thread."""
    def _poll():
        token  = os.getenv('TELEGRAM_BOT_TOKEN')
        offset = 0
        log.info("📩 Telegram receiver started")

        while True:
            try:
                r = requests.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={'offset': offset, 'timeout': 30},
                    timeout=35,
                )
                updates = r.json().get('result', [])
                for update in updates:
                    offset  = update['update_id'] + 1
                    message = update.get('message', {})
                    text    = message.get('text', '').strip()
                    chat_id = message.get('chat', {}).get('id')
                    if text.startswith('/') and chat_id:
                        _handle_command(text, chat_id)

            except Exception as e:
                log.warning(f"Telegram receiver error: {e}")
                time.sleep(5)

    t = threading.Thread(target=_poll, name='TelegramReceiver', daemon=True)
    t.start()