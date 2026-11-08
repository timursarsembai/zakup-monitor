"""Оплата через kazpayment: тариф (30 дней доступа) и пакеты поисков с ИИ.

Заказ заводится у kazpayment (external_ref = наш payments.ref), клиент платит по
payUrl. Оплату узнаём из вебхука payment.received (подпись HMAC-SHA256, event_id
обрабатывается один раз) и запасным опросом статуса (zakup payments). Выдача —
одна на платёж: строка платежа блокируется и переводится в paid.
"""

import hashlib
import hmac
import logging
import os
import secrets
from typing import Any

import psycopg

from zakup import credits, kazpayment, notify

logger = logging.getLogger(__name__)

TARIFF_DAYS = 30
SEARCH_PRICE = 250
SEARCH_PACKS = [(20, 4900), (50, 11900), (100, 22900)]
MAX_SEARCHES = 10_000


def searches_price(count: int) -> int:
    """Цена N поисков: по цене за поиск самого выгодного пакета, который не больше N."""
    per = SEARCH_PRICE
    for size, price in SEARCH_PACKS:
        if count >= size:
            per = min(per, price / size)
    return round(count * per)


def create(conn: psycopg.Connection, user_id: int, kind: str, quantity: int, amount: int,
           phone: str | None, title: str) -> str:
    """Заводит платёж у себя и заказ у kazpayment. Возвращает наш номер платежа."""
    ref = f"zk-{user_id}-{secrets.token_hex(5)}"
    conn.execute(
        """INSERT INTO payments (ref, user_id, kind, quantity, amount) VALUES (%s, %s, %s, %s, %s)""",
        (ref, user_id, kind, quantity, amount),
    )
    conn.commit()
    pay_url = kazpayment.create_order(ref, amount, title, phone)
    conn.execute("UPDATE payments SET pay_url = %s WHERE ref = %s", (pay_url, ref))
    conn.commit()
    return ref


def fulfill(conn: psycopg.Connection, ref: str, paid_amount: int, late: bool = False) -> bool:
    """Выдаёт оплаченное (один раз). False — платёж не наш, уже выдан или недоплачен."""
    row = conn.execute(
        "SELECT id, user_id, kind, quantity, amount, status FROM payments WHERE ref = %s FOR UPDATE",
        (ref,),
    ).fetchone()
    if row is None or row[5] == "paid":
        conn.commit()
        return False
    payment_id, user_id, kind, quantity, amount, _ = row
    if paid_amount < amount:
        conn.commit()
        logger.warning("payment %s: paid %s of %s", ref, paid_amount, amount)
        return False
    if kind == "tariff":
        conn.execute(
            """UPDATE users SET paid_until = greatest(coalesce(paid_until, now()), now())
                   + make_interval(days => %s) WHERE id = %s""",
            (quantity, user_id),
        )
    else:
        credits.add(conn, user_id, quantity, "purchase", note=f"оплата {amount} ₸")
    conn.execute("UPDATE payments SET status = 'paid', paid_at = now() WHERE id = %s", (payment_id,))
    conn.commit()
    if late:
        notify.send(f"zakup: оплата {ref} пришла после отмены заказа — выдано, проверьте.")
    return True


def verify_signature(body: bytes, header: str | None, secret: str | None = None) -> bool:
    secret = secret if secret is not None else os.environ.get("KAZPAYMENT_WEBHOOK_SECRET", "").strip()
    if not secret or not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


def handle_event(conn: psycopg.Connection, event: dict[str, Any]) -> str:
    """Обрабатывает событие вебхука один раз (по event_id). Возвращает что сделано."""
    event_id = str(event.get("event_id") or "")
    if not event_id:
        return "no id"
    fresh = conn.execute(
        "INSERT INTO payment_events (event_id, event) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING 1",
        (event_id, str(event.get("event") or "")[:40]),
    ).fetchone()
    conn.commit()
    if not fresh:
        return "duplicate"
    kind, ref = event.get("event"), event.get("order_ref")
    if not ref:
        return "not ours"
    if kind == "payment.received":
        done = fulfill(conn, str(ref), int(event.get("paid_amount") or 0), bool(event.get("late")))
        return "fulfilled" if done else "skipped"
    if kind == "order.cancelled":
        conn.execute("UPDATE payments SET status = 'cancelled' WHERE ref = %s AND status = 'pending'", (ref,))
        conn.commit()
        return "cancelled"
    if kind == "order.refunded":
        updated = conn.execute(
            "UPDATE payments SET status = 'refunded' WHERE ref = %s RETURNING kind, user_id, quantity",
            (ref,),
        ).fetchone()
        conn.commit()
        if updated:
            notify.send(f"zakup: возврат по {ref} ({updated[0]}). Доступ/поиски проверьте вручную.")
        return "refunded"
    return "ignored"


def poll(conn: psycopg.Connection) -> int:
    """Запасной путь: спрашивает статус неоплаченных заказов за последние 2 суток."""
    done = 0
    for (ref,) in conn.execute(
        """SELECT ref FROM payments WHERE status = 'pending' AND pay_url IS NOT NULL
           AND created_at > now() - interval '2 days' ORDER BY created_at LIMIT 50"""
    ).fetchall():
        try:
            order = kazpayment.order_status(ref)
        except kazpayment.KazpaymentError as error:
            logger.info("poll %s: %s", ref, error.code)
            continue
        if order.get("status") == "paid" and fulfill(conn, ref, int(order.get("paidAmount") or 0)):
            done += 1
        elif order.get("status") == "cancelled":
            conn.execute("UPDATE payments SET status = 'cancelled' WHERE ref = %s AND status = 'pending'",
                         (ref,))
            conn.commit()
    return done


def history(conn: psycopg.Connection, user_id: int) -> list[tuple]:
    return conn.execute(
        """SELECT created_at, kind, quantity, amount, status, paid_at, pay_url FROM payments
           WHERE user_id = %s ORDER BY created_at DESC LIMIT 50""",
        (user_id,),
    ).fetchall()


def get(conn: psycopg.Connection, ref: str, user_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT ref, kind, quantity, amount, status, pay_url, created_at, paid_at, checked_at
           FROM payments WHERE ref = %s AND user_id = %s""",
        (ref, user_id),
    ).fetchone()
    keys = ("ref", "kind", "quantity", "amount", "status", "pay_url", "created_at", "paid_at", "checked_at")
    return dict(zip(keys, row)) if row else None


def check(conn: psycopg.Connection, ref: str) -> str:
    """Статус для страницы оплаты: спрашивает kazpayment не чаще раза в 10 секунд."""
    row = conn.execute(
        """UPDATE payments SET checked_at = now() WHERE ref = %s AND status = 'pending'
             AND (checked_at IS NULL OR checked_at < now() - interval '10 seconds')
           RETURNING ref""",
        (ref,),
    ).fetchone()
    conn.commit()
    if row:
        try:
            order = kazpayment.order_status(ref)
            if order.get("status") == "paid":
                fulfill(conn, ref, int(order.get("paidAmount") or 0))
            elif order.get("status") == "cancelled":
                conn.execute("UPDATE payments SET status = 'cancelled' WHERE ref = %s AND status = 'pending'",
                             (ref,))
                conn.commit()
        except kazpayment.KazpaymentError as error:
            logger.info("check %s: %s", ref, error.code)
    return conn.execute("SELECT status FROM payments WHERE ref = %s", (ref,)).fetchone()[0]
