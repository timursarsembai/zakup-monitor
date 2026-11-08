"""Тариф, поиски, оплата и вебхук."""

from datetime import UTC, datetime

import pytest

from tests.web_helpers import ORIGIN, _announcement_with_lots, _login, _signed
from zakup import auth, web


def test_search_balance_charge_refund_history(client, conn):
    from zakup import credits

    _announcement_with_lots(conn)
    auth.set_password(conn, "+77011112233", "client-password-1")
    conn.execute("UPDATE users SET role = 'user', paid_until = now() + interval '30 days' WHERE email = '+77011112233'")
    conn.commit()
    client_id = conn.execute("SELECT id FROM users WHERE email = '+77011112233'").fetchone()[0]
    client.post("/login", data={"email": "87011112233", "password": "client-password-1"}, headers=ORIGIN)
    # Без поисков — на страницу покупки, ничего не ставится в очередь.
    empty = client.post("/lots/5001/research", data={"region": "Алматы", "whole_kz": "1"}, headers=ORIGIN,
                        follow_redirects=False)
    assert empty.headers["location"] == "/pricing?need=searches"
    assert conn.execute("SELECT count(*) FROM lot_research").fetchone()[0] == 0
    credits.add(conn, client_id, 20, "grant")
    conn.commit()
    page = client.get("/lots/5001").text
    assert "🔍 20" in page and "×1" in page and "Запустить поиск товаров?" in page
    client.post("/lots/5001/research", data={"region": "Алматы", "whole_kz": "1"}, headers=ORIGIN)
    job = conn.execute("SELECT id FROM lot_research").fetchone()[0]
    assert credits.balance(conn, client_id) == 19
    assert credits.refund(conn, job) and not credits.refund(conn, job)  # возврат — один раз
    conn.commit()
    assert credits.balance(conn, client_id) == 20
    history = client.get("/balance").text
    assert "Поиск товаров" in history and "Возврат" in history and "/lots/5001" in history
    assert "+20" in history and "-1" in history
    # Сотрудник ищет без баланса.
    client.post("/logout", headers=ORIGIN)
    _login(client)
    conn.execute("UPDATE lot_research SET status = 'error'")
    conn.commit()
    client.post("/lots/5002/research", data={"region": "Алматы", "whole_kz": "1"}, headers=ORIGIN)
    assert conn.execute("SELECT count(*) FROM search_ledger WHERE kind = 'search'").fetchone()[0] == 1
    assert "поисков на балансе — 40" in client.post(f"/settings/users/{client_id}/searches",
                                                  data={"count": "20"}, headers=ORIGIN).text


def test_admin_edits_tariff_shown_to_clients(client, conn):
    from zakup import tariff

    page = client.get("/pricing").text
    assert "краткая выжимка техспецификации — без ограничений" in page and "выгрузка в Excel" in page
    _login(client)
    assert "Цена в месяц" in client.get("/settings").text
    saved = client.post("/settings/tariff", data={"name_ru": "Профи", "price": "12 900",
                                                  "features_ru": "• уведомления\n- избранное;\n\n"},
                        headers=ORIGIN, follow_redirects=False)
    assert saved.headers["location"] == "/settings?saved=tariff#tariff"
    assert tariff.get(conn)["features_ru"] == ["уведомления", "избранное"]
    page = client.get("/pricing").text
    assert "«Профи»" in page and "12 900 ₸" in page and "<li>избранное</li>" in page
    assert client.post("/settings/tariff", data={"name_ru": "x", "price": "0", "features_ru": "a"},
                       headers=ORIGIN).status_code == 422


def test_pay_tariff_and_searches_via_webhook_and_poll(client, conn, monkeypatch):
    from zakup import credits, payments

    monkeypatch.setenv("KAZPAYMENT_WEBHOOK_SECRET", "whsec")
    orders = []
    monkeypatch.setattr(web.kazpayment, "create_order",
                        lambda ref, amount, comment, phone=None: orders.append((ref, amount, phone)) or f"https://kazpayment.kz/pay/{ref}")
    auth.set_password(conn, "+77011112233", "client-password-1")
    conn.execute("UPDATE users SET role = 'user', phone = email WHERE email = '+77011112233'")
    conn.commit()
    uid = conn.execute("SELECT id FROM users WHERE email = '+77011112233'").fetchone()[0]
    client.post("/login", data={"email": "87011112233", "password": "client-password-1"}, headers=ORIGIN)
    go = client.post("/pay/tariff", headers=ORIGIN, follow_redirects=False)
    ref, amount, phone = orders[0]
    assert go.headers["location"] == f"/pay/{ref}"
    assert amount == 9900 and phone == "+77011112233"
    page = client.get(f"/pay/{ref}").text
    assert "9 900 ₸" in page and "Выставить счёт в Kaspi" in page and f"https://kazpayment.kz/pay/{ref}" in page
    sent = []
    monkeypatch.setattr(web.kazpayment, "remote_invoice", lambda r, p: sent.append((r, p)) or True)
    assert client.post(f"/pay/{ref}/kaspi", headers=ORIGIN, follow_redirects=False).headers["location"].endswith("sent=1")
    assert sent == [(ref, "+77011112233")]
    monkeypatch.setattr(web.payments.kazpayment, "order_status", lambda r: {"status": "pending"})
    assert client.get(f"/pay/{ref}/status").json() == {"status": "pending"}
    raw, headers = _signed({"event_id": "e1", "event": "payment.received", "order_ref": ref,
                            "amount": 9900, "paid_amount": 9900, "late": False})
    assert client.post("/webhooks/kazpayment", content=raw, headers={**headers, "x-kazpayment-signature": "sha256=bad"}).status_code == 401
    assert client.post("/webhooks/kazpayment", content=raw, headers=headers).status_code == 200
    assert client.post("/webhooks/kazpayment", content=raw, headers=headers).status_code == 200  # повтор
    until = conn.execute("SELECT paid_until FROM users WHERE id = %s", (uid,)).fetchone()[0]
    assert until is not None and (until - datetime.now(UTC)).days in (29, 30)
    # Повтор события не продлевает второй раз.
    assert conn.execute("SELECT paid_until FROM users WHERE id = %s", (uid,)).fetchone()[0] == until
    # Пакет поисков: недоплата не выдаёт, полная оплата через запасной опрос — выдаёт.
    client.post("/pay/searches", data={"count": "20"}, headers=ORIGIN)
    ref2, amount2, _ = orders[1]
    assert amount2 == 4900 and payments.searches_price(30) == 7350 and payments.searches_price(5) == 1250
    raw, headers = _signed({"event_id": "e2", "event": "payment.received", "order_ref": ref2,
                            "amount": 4900, "paid_amount": 1000})
    client.post("/webhooks/kazpayment", content=raw, headers=headers)
    assert credits.balance(conn, uid) == 0
    monkeypatch.setattr(payments.kazpayment, "order_status",
                        lambda ref: {"status": "paid", "paidAmount": 4900} if ref == ref2 else {"status": "pending"})
    assert payments.poll(conn) == 1 and credits.balance(conn, uid) == 20
    assert payments.poll(conn) == 0  # уже выдано
    page = client.get("/pricing").text
    assert "оплачено" in page and "Мои оплаты" in page
    assert "✓ Оплачено" in client.get(f"/pay/{ref}").text


def test_webhook_failure_is_retried_and_refund_not_recredited(conn, monkeypatch):
    from zakup import payments

    auth.set_password(conn, "+77011112233", "client-password-1")
    uid = conn.execute("SELECT id FROM users").fetchone()[0]
    conn.execute("INSERT INTO payments (ref, user_id, kind, quantity, amount) VALUES ('r1', %s, 'searches', 20, 4900)",
                 (uid,))
    conn.commit()
    event = {"event_id": "e1", "event": "payment.received", "order_ref": "r1", "paid_amount": "4900.00"}
    real = payments.fulfill
    monkeypatch.setattr(payments, "fulfill", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    with pytest.raises(RuntimeError):
        payments.handle_event(conn, event)
    conn.rollback()
    monkeypatch.setattr(payments, "fulfill", real)
    assert payments.handle_event(conn, event) == "fulfilled"  # повтор kazpayment прошёл
    assert payments.handle_event(conn, {"event_id": "e2", "event": "order.refunded", "order_ref": "r1"}) == "refunded"
    again = {"event_id": "e3", "event": "payment.received", "order_ref": "r1", "paid_amount": 4900}
    assert payments.handle_event(conn, again) == "skipped"
    assert conn.execute("SELECT sum(delta) FROM search_ledger").fetchone()[0] == 20


def test_search_price_never_above_bigger_pack():
    from zakup import payments

    prices = [payments.searches_price(n) for n in range(1, 130)]
    assert prices == sorted(prices)
    assert payments.searches_price(49) <= 11900 and payments.searches_price(99) <= 22900
    assert payments.searches_price(5) == 1250 and payments.searches_price(30) == 7350


def test_searches_cannot_be_bought_without_tariff(client, conn):
    auth.set_password(conn, "+77011112233", "client-password-1")
    conn.execute("UPDATE users SET role = 'user', phone = email")
    conn.commit()
    client.post("/login", data={"email": "87011112233", "password": "client-password-1"}, headers=ORIGIN)
    response = client.post("/pay/searches", data={"count": "20"}, headers=ORIGIN)
    assert response.status_code == 402
