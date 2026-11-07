import pytest

from zakup import db, matcher, telegram


def _lot(conn, lot_id, name, amount):
    db.upsert_lot(conn, {"id": lot_id, "trd_buy_id": 1, "name_ru": name, "amount": amount})


def _subscription(conn, query, min_amount=None, matched_up_to=0, chat_id=None):
    user = conn.execute(
        "INSERT INTO users (email, telegram_chat_id) VALUES ('a@b.kz', %s) "
        "ON CONFLICT (email) DO UPDATE SET telegram_chat_id = excluded.telegram_chat_id "
        "RETURNING id",
        (chat_id,),
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
    assert matcher.deliver(conn) == 0  # Telegram не подключён: только помечаем
    assert matcher.pending(conn) == []
    assert conn.execute(
        "SELECT matched_up_to FROM subscriptions WHERE id = %s", (sub,)
    ).fetchone()[0] == 4  # seen_seq: четвёртый вставленный лот


def test_late_published_lot_with_low_id_still_matches(conn):
    _lot(conn, 100, "Бумага", 5000)
    _subscription(conn, "картридж", matched_up_to=1)
    _lot(conn, 7, "Картриджи", 5000)
    assert matcher.run(conn) == 1
    assert [m.lot_id for m in matcher.pending(conn)] == [7]


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


@pytest.fixture
def outbox(monkeypatch):
    sent = []

    def fake_send(chat_id, text):
        if outbox_error:
            raise outbox_error[0]
        sent.append((chat_id, text))

    outbox_error = []
    monkeypatch.setattr(telegram, "send_message", fake_send)
    return sent, outbox_error


def test_deliver_sends_digests_and_respects_message_limit(conn, outbox, monkeypatch):
    sent, _ = outbox
    monkeypatch.setattr(telegram, "MESSAGE_LIMIT", 300)
    _subscription(conn, "картридж", chat_id="42")
    for i in range(1, 8):
        _lot(conn, i, f"Картриджи партия {i}", 1000 + i)
    assert matcher.run(conn) == 7
    assert matcher.deliver(conn, pause=0) == 7
    assert len(sent) > 1 and all(len(text) <= 300 for _, text in sent)
    assert sum(text.count("Картриджи партия") for _, text in sent) == 7
    assert matcher.pending(conn) == []


def test_blocked_chat_is_unlinked_and_temporary_failure_retries(conn, outbox):
    sent, error = outbox
    _subscription(conn, "картридж", chat_id="42")
    _lot(conn, 1, "Картриджи", 1000)
    matcher.run(conn)
    error.append(telegram.TelegramError(500, "Internal Server Error"))
    assert matcher.deliver(conn, pause=0) == 0
    assert len(matcher.pending(conn)) == 1
    error[0] = telegram.TelegramError(403, "Forbidden: bot was blocked by the user")
    assert matcher.deliver(conn, pause=0) == 0
    assert matcher.pending(conn) == []
    assert conn.execute("SELECT telegram_chat_id FROM users").fetchone()[0] is None
