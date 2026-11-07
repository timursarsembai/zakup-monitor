"""Служебные оповещения владельцу (упал сборщик и т.п.)."""

import html
import logging
import os

from zakup import telegram

logger = logging.getLogger(__name__)


def send(text: str) -> bool:
    chat_id = os.environ.get("ZAKUP_TELEGRAM_CHAT_ID")
    if not telegram.token() or not chat_id:
        logger.warning("message not sent, Telegram is not configured: %s", text)
        return False
    try:
        telegram.send_message(chat_id, html.escape(text))
        return True
    except telegram.TelegramError as error:
        logger.warning("message not sent: %s", error)
        return False
