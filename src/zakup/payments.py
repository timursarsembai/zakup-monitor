"""Оплата через kazpayment: тариф (30 дней доступа) и пакеты поисков с ИИ.

Заказ заводится у kazpayment (external_ref = наш payments.ref), клиент платит по
payUrl. Оплату узнаём из вебхука payment.received (подпись HMAC-SHA256, event_id
обрабатывается один раз) и запасным опросом статуса (zakup payments). Выдача —
одна на платёж: строка платежа блокируется и переводится в paid.
"""

import hashlib
import hmac
import logging
import secrets
from typing import Any

import psycopg

from zakup import config, credits, kazpayment, notify

logger = logging.getLogger(__name__)

TARIFF_DAYS = 30
SEARCH_PRICE = 250
SEARCH_PACKS = [(20, 4900), (50, 11900), (100, 22900)]
MAX_SEARCHES = 10_000


def searches_price(count: int) -> int:
    """Цена N поисков: по цене за поиск самого выгодного пакета, который не больше N,
    но не дороже ближайшего пакета побольше (49 поисков не дороже пакета на 50)."""
    per = SEARCH_PRICE
    for size, price in SEARCH_PACKS:
        if count >= size:
            per = min(per, price / size)
    bigger = [price for size, price in SEARCH_PACKS if size >= count]
    return min([round(count * per), *bigger])


# Неоплаченных заказов у одного человека за сутки — не больше стольких: каждый — заказ
# у kazpayment, а запасная проверка оплат смотрит ограниченное их число.
OPEN_ORDERS = 3


class TooManyOrders(Exception):
    pass


def start(conn: psycopg.Connection, user_id: int, kind: str, quantity: int, amount: int) -> tuple[str, bool]:
    """Заводит платёж у себя: (номер, новый ли). Такой же неоплаченный заказ за сутки —
    тот же номер, без нового заказа у kazpayment."""
    conn.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (user_id,))
    same = conn.execute(
        """SELECT ref FROM payments WHERE user_id = %s AND kind = %s AND quantity = %s AND amount = %s
           AND status = 'pending' AND pay_url IS NOT NULL AND created_at > now() - interval '1 day'
           ORDER BY id DESC LIMIT 1""", (user_id, kind, quantity, amount)).fetchone()
    if same:
        conn.commit()
        return same[0], False
    open_orders = conn.execute(
        """SELECT count(*) FROM payments WHERE user_id = %s AND status = 'pending'
           AND created_at > now() - interval '1 day'""", (user_id,)).fetchone()[0]
    if open_orders >= OPEN_ORDERS:
        conn.commit()
        raise TooManyOrders()
    ref = f"zk-{user_id}-{secrets.token_hex(5)}"
    conn.execute(
        """INSERT INTO payments (ref, user_id, kind, quantity, amount) VALUES (%s, %s, %s, %s, %s)""",
        (ref, user_id, kind, quantity, amount),
    )
    conn.commit()
    return ref, True


def attach(conn: psycopg.Connection, ref: str, pay_url: str) -> None:
    conn.execute("UPDATE payments SET pay_url = %s WHERE ref = %s", (pay_url, ref))
    conn.commit()


def drop(conn: psycopg.Connection, ref: str) -> None:
    """Заказ у kazpayment не создался — наш черновик не считается открытым заказом."""
    conn.execute("UPDATE payments SET status = 'cancelled' WHERE ref = %s AND pay_url IS NULL", (ref,))
    conn.commit()


def create(conn: psycopg.Connection, user_id: int, kind: str, quantity: int, amount: int,
           phone: str | None, title: str) -> str:
    """Заводит платёж у себя и заказ у kazpayment. Возвращает наш номер платежа.
    Сайт держит соединение с базой на время запроса — там вместо этого start/attach."""
    ref, new = start(conn, user_id, kind, quantity, amount)
    if new:
        try:
            pay_url = kazpayment.create_order(ref, amount, title, phone)
        except kazpayment.KazpaymentError:
            drop(conn, ref)
            raise
        attach(conn, ref, pay_url)
    return ref


def fulfill(conn: psycopg.Connection, ref: str, paid_amount: int, late: bool = False) -> bool:
    """Выдаёт оплаченное (один раз). False — платёж не наш, уже выдан или недоплачен."""
    row = conn.execute(
        "SELECT id, user_id, kind, quantity, amount, status FROM payments WHERE ref = %s FOR UPDATE",
        (ref,),
    ).fetchone()
    # Выдаём только за неоплаченный или отменённый (поздняя оплата) заказ: возвращённый
    # повторно не выдаём.
    if row is None or row[5] not in ("pending", "cancelled"):
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
    secret = secret if secret is not None else config.kazpayment_webhook_secret()
    if not secret or not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


def handle_event(conn: psycopg.Connection, event: dict[str, Any]) -> str:
    """Обрабатывает событие вебхука один раз (по event_id). Возвращает что сделано."""
    event_id = str(event.get("event_id") or "")
    if not event_id:
        return "no id"
    # Отметка «событие обработано» и само действие — одной транзакцией: если действие
    # упадёт, отметка откатится и повтор kazpayment обработается заново.
    fresh = conn.execute(
        "INSERT INTO payment_events (event_id, event) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING 1",
        (event_id, str(event.get("event") or "")[:40]),
    ).fetchone()
    if not fresh:
        conn.commit()
        return "duplicate"
    kind, ref = event.get("event"), event.get("order_ref")
    if not ref:
        conn.commit()
        return "not ours"
    if kind == "payment.received":
        paid = int(float(event.get("paid_amount") or 0))
        done = fulfill(conn, str(ref), paid, bool(event.get("late")))
        return "fulfilled" if done else "skipped"
    if kind == "order.cancelled":
        conn.execute("UPDATE payments SET status = 'cancelled' WHERE ref = %s AND status = 'pending'", (ref,))
        conn.commit()
        return "cancelled"
    if kind == "order.refunded":
        return _refunded(conn, str(ref))
    conn.commit()
    return "ignored"


def _refunded(conn: psycopg.Connection, ref: str) -> str:
    """Деньги вернули: забираем выданное — дни тарифа и неизрасходованные поиски.
    Возврат раньше оплаты (события пришли не по порядку) — заказ сразу «возвращён»,
    и поздняя оплата по нему уже ничего не выдаст."""
    row = conn.execute("SELECT user_id, kind, quantity, status FROM payments WHERE ref = %s FOR UPDATE",
                       (ref,)).fetchone()
    if row is None or row[3] == "refunded":
        conn.commit()
        return "skipped"
    user_id, kind, quantity, status = row
    conn.execute("UPDATE payments SET status = 'refunded' WHERE ref = %s", (ref,))
    taken = ""
    if status == "paid" and kind == "tariff":
        conn.execute(
            """UPDATE users SET paid_until = greatest(now(), paid_until - make_interval(days => %s))
               WHERE id = %s AND paid_until IS NOT NULL""", (quantity, user_id))
        taken = f"тариф сокращён на {quantity} дн."
    elif status == "paid":
        conn.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (user_id,))
        back = min(quantity, max(credits.balance(conn, user_id), 0))
        if back:
            credits.add(conn, user_id, -back, "chargeback", note=f"возврат оплаты {ref}")
        taken = f"списано {back} из {quantity} поисков" + (" (остальные уже израсходованы)" if back < quantity else "")
    conn.commit()
    notify.send(f"zakup: возврат по {ref} ({kind}): "
                + (taken if status == "paid" else f"заказ был «{status}», ничего не выдавалось") + ".")
    return "refunded"


def poll(conn: psycopg.Connection) -> int:
    """Запасной путь: спрашивает статус неоплаченных заказов за последние 2 суток."""
    done = 0
    for (ref,) in conn.execute(
        """SELECT ref FROM payments WHERE status = 'pending' AND pay_url IS NOT NULL
           AND created_at > now() - interval '2 days' ORDER BY created_at DESC LIMIT 50"""
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
    return dict(zip(keys, row, strict=True)) if row else None


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
