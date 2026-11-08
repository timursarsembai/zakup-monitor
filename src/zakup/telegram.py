"""Минимальный клиент Telegram Bot API на urllib.

urllib, а не httpx: httpx пишет URL запросов в лог, а в URL лежит токен бота.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# Сообщение в Telegram — до 4096 символов, оставляем запас на разметку.
MESSAGE_LIMIT = 3800


class TelegramError(Exception):
    def __init__(self, code: int | None, description: str):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description

    @property
    def chat_gone(self) -> bool:
        """Пользователь заблокировал бота или чата больше нет: слать туда бессмысленно."""
        return self.code == 403 or (
            self.code == 400 and "chat not found" in self.description.lower()
        )


# Два бота: «main» — для владельца и сотрудников (@zakup_sarsembai_bot), «client» — для
# клиентов (@tenderim_kz_bot). Чаты клиентского бота хранятся с префиксом «c:»
# (users.telegram_chat_id = "c:123"): так любая рассылка сама уходит нужным ботом.
CLIENT_PREFIX = "c:"
_TOKENS = {"main": "ZAKUP_TELEGRAM_BOT_TOKEN", "client": "ZAKUP_CLIENT_BOT_TOKEN"}
_usernames: dict[str, str] = {}


def token(bot: str = "main") -> str | None:
    return os.environ.get(_TOKENS[bot]) or None


def bot_username(bot: str = "main") -> str | None:
    if bot == "main":
        return os.environ.get("ZAKUP_TELEGRAM_BOT_USERNAME") or None
    if bot not in _usernames and token(bot):
        try:
            _usernames[bot] = call("getMe", bot=bot)["username"]
        except (TelegramError, KeyError):
            return None
    return _usernames.get(bot)


def route(chat_id: str | int) -> tuple[str, str]:
    """(бот, настоящий id чата) по сохранённому id: «c:123» — клиентский бот."""
    text = str(chat_id)
    if text.startswith(CLIENT_PREFIX):
        return "client", text[len(CLIENT_PREFIX):]
    return "main", text


def call(method: str, http_timeout: float = 15, bot: str = "main", **params: Any) -> Any:
    bot_token = token(bot)
    if not bot_token:
        raise TelegramError(None, "bot token is not configured")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/{method}",
        data=json.dumps(params).encode(),
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=http_timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            payload = json.load(error)
        except ValueError:
            raise TelegramError(error.code, "non-JSON error response") from None
    except (OSError, ValueError) as error:
        raise TelegramError(None, type(error).__name__) from None
    if not payload.get("ok"):
        raise TelegramError(payload.get("error_code"), payload.get("description", ""))
    return payload["result"]


def send_message(chat_id: str | int, text: str) -> None:
    bot, real_id = route(chat_id)
    call(
        "sendMessage",
        bot=bot,
        chat_id=real_id,
        text=text,
        parse_mode="HTML",
        link_preview_options={"is_disabled": True},
    )
