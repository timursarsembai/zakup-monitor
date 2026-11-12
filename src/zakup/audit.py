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
    "payment_start": "Перешёл к оплате",
    "payment_invoice": "Счёт в Kaspi на номер",
    "otp_sent": "Отправлен SMS-код",
    "register": "Регистрация",
    "password_reset_sms": "Восстановил пароль по SMS",
    "client_org": "Указал организацию",
    "seller_claim": "Заявка «Это мой магазин»",
    "shop_save": "Сохранил свой магазин",
    "shop_product": "Изменил товар своего магазина",
    "seller_moderation": "Модерация продавца",
    "view_announcement": "Открыл объявление",
    "view_lot": "Открыл лот",
    "view_page": "Открыл страницу",
    "mark": "Отметка лота",
    "note": "Заметка к лоту",
    "research": "Поиск товаров с ИИ",
    "export": "Выгрузка в Excel",
    "subscription_create": "Создал подписку",
    "subscription_delete": "Удалил подписку",
    "subscription_update": "Изменил подписку",
    "subscription_toggle": "Вкл./выкл. подписку",
    "subscription_chat": "Сменил получателя подписки",
    "password_change": "Сменил свой пароль",
    "telegram_link": "Подключение Telegram",
    "client_group_join": "Заявка в группу клиентов",
    "telegram_unlink": "Отключил Telegram",
    "group_link": "Подключение группы",
    "group_remove": "Отключил группу",
    "settings_ai": "Настройки ИИ",
    "settings_company": "Настройки организации",
    "settings_proxy": "Настройки прокси",
    "settings_tariff": "Изменил тариф",
    "history_start": "Запустил догрузку истории с даты",
    "history_stop": "Остановил догрузку истории",
    "settings_requisites": "Изменил реквизиты",
    "settings_brand": "Изменил текст под логотипом",
    "settings_logo": "Изменил логотип",
    "settings_registration": "Регистрация открыта/закрыта",
    "user_add": "Добавил пользователя",
    "user_reset": "Сбросил пароль пользователю",
    "user_role": "Сменил роль пользователя",
    "user_disable": "Вкл./выкл. пользователя",
    "user_delete": "Удалил пользователя",
    "impersonate": "Вошёл как пользователь",
    "impersonate_stop": "Вернулся в свой аккаунт",
    "user_access": "Доступ к платному вручную",
    "user_searches": "Начислил поиски",
    "user_restore": "Восстановил пользователя",
    "user_reregister": "Повторная регистрация удалённого",
    "source_add": "Добавил источник",
    "spec_summary": "Выжимка техспецификации",
    "supplier_questions": "Вопросы поставщику (ИИ)",
    "source_toggle": "Вкл./выкл. источник",
}


def log(conn: psycopg.Connection, user_id: int | None, action: str, target: str | None = None,
        details: dict[str, Any] | None = None, ip: str | None = None,
        login: str | None = None) -> None:
    if action in ("view_announcement", "view_lot", "view_page") and user_id:
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
