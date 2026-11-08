"""Служебные оповещения владельцу (упал сборщик и т.п.)."""

import html
import logging

from zakup import config, telegram

logger = logging.getLogger(__name__)


def send(text: str, buttons: list[list[dict]] | None = None) -> bool:
    """buttons — строки кнопок под сообщением (inline_keyboard Telegram)."""
    chat_id = config.owner_chat_id()
    if not telegram.token() or not chat_id:
        logger.warning("message not sent, Telegram is not configured: %s", text)
        return False
    try:
        telegram.send_message(chat_id, html.escape(text), buttons=buttons)
        return True
    except telegram.TelegramError as error:
        logger.warning("message not sent: %s", error)
        return False
