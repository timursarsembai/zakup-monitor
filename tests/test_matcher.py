from zakup import db, matcher


def _lot(conn, lot_id, name, amount):
    db.upsert_lot(conn, {"id": lot_id, "trd_buy_id": 1, "name_ru": name, "amount": amount})


def _subscription(conn, query, min_amount=None, matched_up_to=0):
    user = conn.execute(
        "INSERT INTO users (email, telegram_chat_id) VALUES ('a@b.kz', NULL) "
        "ON CONFLICT (email) DO UPDATE SET email = excluded.email RETURNING id"
    ).fetchone()[0]
    return conn.execute(
        """INSERT INTO subscriptions (user_id, name, query, min_amount, matched_up_to)
           VALUES (%s, 'тест', %s, %s, %s) RETURNING id""",
        (user, query, min_amount, matched_up_to),
    ).fetchone()[0]


def test_matches_new_lots_by_words_and_amount_once(conn):
    _lot(conn, 1, "Картриджи для принтера", 50000)
    _lot(conn, 2, "Картридж лазерный", 500)
    _lot(conn, 3, "Аренда помещения", 90000)
    sub = _subscription(conn, "картридж", min_amount=1000)
    assert matcher.run(conn) == 1
    assert [m.lot_id for m in matcher.pending(conn)] == [1]
    _lot(conn, 4, "Поставка картриджей", 2000)
    assert matcher.run(conn) == 1
    assert matcher.run(conn) == 0
    assert matcher.deliver(conn) == 2
    assert matcher.pending(conn) == []
    assert conn.execute(
        "SELECT matched_up_to FROM subscriptions WHERE id = %s", (sub,)
    ).fetchone()[0] == 4


def test_subscription_skips_lots_before_its_watermark(conn):
    _lot(conn, 1, "Картриджи", 5000)
    _lot(conn, 2, "Картриджи", 5000)
    _subscription(conn, "картридж", matched_up_to=1)
    assert matcher.run(conn) == 1
    assert [m.lot_id for m in matcher.pending(conn)] == [2]


def test_format_match_escapes_html():
    text = matcher.format_match(
        matcher.Match(1, "<b>", None, 5, 77, "Бумага A4 <офисная>", 1234567.5, "ТОО «Ромашка»")
    )
    assert "&lt;офисная&gt;" in text
    assert "1 234 567.50 ₸" in text
    assert text.endswith("/announce/index/77")
