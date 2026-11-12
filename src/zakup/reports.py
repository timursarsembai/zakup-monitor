"""Жалобы на товары и продавцов каталога (docs/CATALOG_PLAN.md, этап 6).

Пожаловаться может вошедший пользователь (регистрация — по SMS, это и защита от спама):
не больше DAILY жалоб в сутки и одна открытая на один товар или продавца. Сотрудник на
/staff/sellers скрывает товар (product_catalog.blocked — владелец сам не вернёт), скрывает
продавца (sellers.status = 'hidden') или отклоняет жалобу.
"""

from typing import Any

import psycopg
from psycopg.rows import dict_row

from zakup import shop

REASONS = {"wrong": "Неверная цена или описание", "fraud": "Мошенничество, продавца не существует",
           "forbidden": "Запрещённый товар", "spam": "Спам, повторы", "other": "Другое"}
DAILY = 20


# Итог жалобы → сообщение на странице (?report=…).
RESULTS = {"sent": "Спасибо, жалоба отправлена — сотрудник проверит.",
           "dup": "Вы уже пожаловались — сотрудник проверит.",
           "limit": "Слишком много жалоб за сутки — попробуйте завтра.",
           "reason": "Выберите причину жалобы.", "comment": "Напишите, что не так."}


def create(conn: psycopg.Connection, user: int, reason: str, comment: str, *,
           product_id: int | None = None, seller_id: int | None = None) -> str:
    """Новая жалоба; итог — ключ RESULTS (sent — отправлена)."""
    if reason not in REASONS:
        return "reason"
    comment = (comment or "").strip()[:1000]
    if reason == "other" and not comment:
        return "comment"
    today = conn.execute("SELECT count(*) FROM catalog_reports WHERE user_id = %s AND created_at > now() - interval '1 day'",
                         (user,)).fetchone()[0]
    if today >= DAILY:
        return "limit"
    # Повтор (и двойное нажатие) отсекает уникальный индекс catalog_reports_one_open.
    added = conn.execute("""INSERT INTO catalog_reports (product_id, seller_id, user_id, reason, comment)
                            VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                         (product_id, seller_id, user, reason, comment or None)).rowcount
    return "sent" if added else "dup"


def open_reports(conn: psycopg.Connection, limit: int = 200) -> list[dict[str, Any]]:
    """Открытые жалобы — по товару или продавцу вместе (сколько их и последние слова)."""
    return conn.cursor(row_factory=dict_row).execute(
        """SELECT r.product_id, r.seller_id, count(*) AS n, max(r.created_at) AS last_at,
                  array_agg(DISTINCT r.reason) AS reasons,
                  (array_agg(r.comment ORDER BY r.created_at DESC) FILTER (WHERE r.comment IS NOT NULL))[1:3] AS comments,
                  p.name AS product_name, p.blocked, coalesce(s.name, ps.name) AS seller_name,
                  coalesce(s.id, ps.id) AS owner_id, coalesce(s.status, ps.status) AS seller_status
           FROM catalog_reports r
           LEFT JOIN product_catalog p ON p.id = r.product_id
           LEFT JOIN sellers ps ON ps.id = p.seller_id
           LEFT JOIN sellers s ON s.id = r.seller_id
           WHERE r.status = 'open'
           GROUP BY r.product_id, r.seller_id, p.name, p.blocked, s.name, ps.name, s.id, ps.id, s.status, ps.status
           ORDER BY count(*) DESC, max(r.created_at) DESC LIMIT %s""", (limit,)).fetchall()


def blocked_products(conn: psycopg.Connection, limit: int = 100) -> list[dict[str, Any]]:
    return conn.cursor(row_factory=dict_row).execute(
        """SELECT id, name, updated_at FROM product_catalog WHERE blocked ORDER BY updated_at DESC LIMIT %s""",
        (limit,)).fetchall()


def decide(conn: psycopg.Connection, staff: int, action: str, *, product_id: int | None = None,
           seller_id: int | None = None) -> bool:
    """block_product / unblock_product / hide_seller / dismiss: действие и закрытие открытых
    жалоб на этот товар или продавца."""
    if action in ("block_product", "unblock_product") and product_id:
        changed = conn.execute("UPDATE product_catalog SET blocked = %s, updated_at = now() WHERE id = %s",
                               (action == "block_product", product_id)).rowcount
        if not changed or action == "unblock_product":
            return bool(changed)
    elif action == "hide_seller" and seller_id:
        if not shop.set_status(conn, seller_id, "hidden"):
            return False
    elif action != "dismiss" or not (product_id or seller_id):
        return False
    status = "dismissed" if action == "dismiss" else "resolved"
    if action == "hide_seller":
        # Скрыли продавца — закрываем и жалобы на его товары.
        conn.execute("""UPDATE catalog_reports SET status = %s, decided_at = now(), decided_by = %s
                        WHERE status = 'open' AND (seller_id = %s OR product_id IN (
                            SELECT id FROM product_catalog WHERE seller_id = %s))""",
                     (status, staff, seller_id, seller_id))
    else:
        conn.execute("""UPDATE catalog_reports SET status = %s, decided_at = now(), decided_by = %s
                        WHERE status = 'open' AND product_id IS NOT DISTINCT FROM %s
                          AND seller_id IS NOT DISTINCT FROM %s""", (status, staff, product_id, seller_id))
    return True
