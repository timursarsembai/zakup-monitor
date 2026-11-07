"""Telegram-бот: привязка чата к аккаунту сайта. Работает long polling'ом.

Совпадения рассылает не он, а worker (matcher.deliver); бот только отвечает
на команды /start <код>, /start, /stop.
"""

import hashlib
import logging
import secrets
import time
from datetime import timedelta

import psycopg

from zakup import SITE_URL, db, telegram

logger = logging.getLogger(__name__)

LINK_TTL = timedelta(minutes=30)
OFFSET_KEY = "telegram:offset"


HELP = (
    "Это бот сервиса zakup — мониторинга госзакупок.\n\n"
    f"Чтобы получать уведомления, откройте {SITE_URL}/settings и нажмите "
    "«Подключить Telegram».\n/stop — отключить уведомления."
)


def _code_hash(code: str) -> bytes:
    return hashlib.sha256(code.encode()).digest()


def create_link_code(conn: psycopg.Connection, user_id: int) -> str:
    # Telegram пропускает в параметре start только [A-Za-z0-9_-] и до 64 символов.
    code = secrets.token_urlsafe(24)
    conn.execute(
        "DELETE FROM telegram_link_codes WHERE user_id = %s OR expires_at < now()", (user_id,)
    )
    conn.execute(
        """INSERT INTO telegram_link_codes (code_hash, user_id, expires_at)
           VALUES (%s, %s, now() + %s)""",
        (_code_hash(code), user_id, LINK_TTL),
    )
    conn.commit()
    return code


def link_url(code: str) -> str:
    return f"https://t.me/{telegram.bot_username()}?start={code}"


def handle(conn: psycopg.Connection, message: dict) -> str | None:
    """Обрабатывает входящее сообщение, возвращает ответ (или None — молчать)."""
    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return None
    chat_id = str(chat["id"])
    text = (message.get("text") or "").strip()
    command, _, argument = text.partition(" ")
    command = command.split("@")[0]

    if command == "/start" and argument:
        row = conn.execute(
            """DELETE FROM telegram_link_codes WHERE code_hash = %s AND expires_at > now()
               RETURNING user_id""",
            (_code_hash(argument.strip()),),
        ).fetchone()
        if row is None:
            conn.commit()
            return (
                f"Ссылка устарела или уже использована. Возьмите новую на сайте: {SITE_URL}/settings"
            )
        # Один чат — один аккаунт: отвязываем его от прежнего, если был.
        conn.execute(
            "UPDATE users SET telegram_chat_id = NULL WHERE telegram_chat_id = %s AND id <> %s",
            (chat_id, row[0]),
        )
        email = conn.execute(
            "UPDATE users SET telegram_chat_id = %s WHERE id = %s RETURNING email",
            (chat_id, row[0]),
        ).fetchone()[0]
        conn.commit()
        return (
            f"Готово, уведомления для {email} будут приходить сюда.\n"
            "Подписки настраиваются на сайте. /stop — отключить."
        )

    if command == "/stop":
        unlinked = conn.execute(
            "UPDATE users SET telegram_chat_id = NULL WHERE telegram_chat_id = %s", (chat_id,)
        ).rowcount
        conn.commit()
        return "Уведомления отключены." if unlinked else "Этот чат и так не подключён."

    return HELP


def run(database_url: str, poll_timeout: int = 50) -> None:
    telegram.call("deleteWebhook")
    logger.info("bot @%s is polling", telegram.bot_username())
    while True:
        try:
            with db.connect(database_url) as conn:
                state = db.get_state(conn, OFFSET_KEY)
                updates = telegram.call(
                    "getUpdates",
                    http_timeout=poll_timeout + 10,
                    offset=state.last_id if state else 0,
                    timeout=poll_timeout,
                    allowed_updates=["message"],
                )
                for update in updates:
                    message = update.get("message")
                    if message and (reply := handle(conn, message)):
                        try:
                            telegram.send_message(message["chat"]["id"], reply)
                        except telegram.TelegramError as error:
                            logger.warning("reply not sent: %s", error)
                    # Сдвигаем offset после каждого апдейта: упадём — не ответим дважды.
                    db.save_state(
                        conn, OFFSET_KEY, db.SyncState(update["update_id"] + 1, None, None)
                    )
                    conn.commit()
        except telegram.TelegramError as error:
            logger.warning("getUpdates failed: %s", error)
            time.sleep(10)
        except psycopg.Error as error:
            # Only the type: database errors can quote the connection string.
            logger.warning("database error: %s", type(error).__name__)
            time.sleep(10)
