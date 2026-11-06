import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)


def send(text: str, chat_id: str | None = None) -> bool:
    """Шлёт сообщение в Telegram. Без chat_id — владельцу (служебные оповещения)."""
    token = os.environ.get("ZAKUP_TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("ZAKUP_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("message not sent, Telegram is not configured: %s", text)
        return False
    # urllib, not httpx: httpx logs request URLs, and this one contains the token.
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(
            {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
             "disable_web_page_preview": True}
        ).encode(),
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            return True
    except OSError as error:
        logger.warning("message not sent: %s", type(error).__name__)
        return False
