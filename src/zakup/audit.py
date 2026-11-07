"""Журнал действий пользователей: кто, что и когда сделал на сайте.

Пишется из маршрутов сайта (zakup.web), смотрит администратор (/log).
Записи старше RETENTION удаляет медленный круг сборщика (purge).
"""

from datetime import timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

RETENTION = timedelta(days=30)
# Повторный просмотр той же страницы тем же человеком в течение этого времени не пишем.
VIEW_DEDUP = timedelta(minutes=10)

ACTIONS = {
    "login": "Вход",
    "login_failed": "Неудачный вход",
    "logout": "Выход",
    "view_announcement": "Открыл объявление",
    "view_lot": "Открыл лот",
    "mark": "Отметка лота",
    "note": "Заметка к лоту",
    "research": "Поиск товаров с ИИ",
    "export": "Выгрузка в Excel",
    "subscription_create": "Создал подписку",
    "subscription_delete": "Удалил подписку",
    "subscription_toggle": "Вкл./выкл. подписку",
    "subscription_chat": "Сменил получателя подписки",
    "password_change": "Сменил свой пароль",
    "telegram_link": "Подключение Telegram",
    "telegram_unlink": "Отключил Telegram",
    "group_link": "Подключение группы",
    "group_remove": "Отключил группу",
    "settings_ai": "Настройки ИИ",
    "settings_company": "Настройки организации",
    "user_add": "Добавил пользователя",
    "user_reset": "Сбросил пароль пользователю",
    "user_role": "Сменил роль пользователя",
    "user_disable": "Вкл./выкл. пользователя",
    "user_delete": "Удалил пользователя",
    "source_add": "Добавил источник",
    "source_toggle": "Вкл./выкл. источник",
}


def log(conn: psycopg.Connection, user_id: int | None, action: str, target: str | None = None,
        details: dict[str, Any] | None = None, ip: str | None = None,
        login: str | None = None) -> None:
    if action in ("view_announcement", "view_lot") and user_id:
        recent = conn.execute(
            """SELECT 1 FROM user_actions WHERE user_id = %s AND action = %s AND target = %s
               AND at > now() - %s LIMIT 1""",
            (user_id, action, target, VIEW_DEDUP),
        ).fetchone()
        if recent:
            return
    conn.execute(
        """INSERT INTO user_actions (user_id, login, action, target, details, ip)
           VALUES (%s, coalesce(%s, (SELECT email FROM users WHERE id = %s)), %s, %s, %s, %s)""",
        (user_id, login, user_id, action, target, Jsonb(details) if details else None, ip),
    )
    conn.commit()


def purge(conn: psycopg.Connection) -> int:
    deleted = conn.execute(
        "DELETE FROM user_actions WHERE at < now() - %s", (RETENTION,)
    ).rowcount
    conn.commit()
    return deleted
