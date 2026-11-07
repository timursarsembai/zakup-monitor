from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from zakup import db, matcher, telegram


def _lot(conn, lot_id, name, amount, trd_buy_id=1):
    db.upsert_lot(conn, {"id": lot_id, "trd_buy_id": trd_buy_id, "name_ru": name, "amount": amount})


def _subscription(conn, query, min_amount=None, matched_up_to=0, chat_id=None):
    user = conn.execute(
        "INSERT INTO users (email, telegram_chat_id) VALUES ('a@b.kz', %s) "
        "ON CONFLICT (email) DO UPDATE SET telegram_chat_id = excluded.telegram_chat_id "
        "RETURNING id",
        (chat_id,),
    ).fetchone()[0]
    return conn.execute(
        """INSERT INTO subscriptions (user_id, name, query, min_amount, matched_up_to)
           VALUES (%s, 'Картриджи', %s, %s, %s) RETURNING id""",
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


def _pending(**overrides):
    base = dict(
        subscription_id=1, subscription_name="Фильтр <1>", chat_id="42", lot_id=5,
        lot_name="Стол", lot_description="Журнальный столик <дуб>", amount=Decimal("689655.16"),
        count=Decimal("4.000"), unit_name="Штука", prepayment=Decimal("30.00"),
        lot_kato=["191010000"], announcement_id=77, number_anno="77-1",
        announcement_name="Приобретение столика", end_date=datetime(2026, 11, 9, 4, tzinfo=UTC),
        customer="Школа №5", announcement_kato=["191010000"],
    )
    return matcher.PendingLot(**{**base, **overrides})


def test_block_lists_lots_and_links_to_our_site():
    text = matcher.format_block([
        _pending(lot_name="Стол <дуб>", lot_name_kz="Үстел", price=Decimal("172413.79"),
                 announcement_name_kz="Үстел сатып алу", disable_person=True),
        _pending(lot_id=6, prepayment=None, lot_kato=[]),
    ])
    # Сначала целиком по-казахски, потом целиком по-русски, ссылка — одна, в конце.
    kazakh, russian = text.split("🇷🇺")
    assert kazakh.startswith("🇰🇿") and "<b>Үстел сатып алу</b>" in kazakh and "<b>Үстел</b>" in kazakh
    assert "♿ Мүгедектер ұйымдары арасында" in kazakh and "Стол" not in kazakh.split("• ")[1]
    assert "<b>Приобретение столика</b>" in russian and "<b>Стол &lt;дуб&gt;</b>" in russian
    assert "♿ Среди организаций инвалидов" in russian
    assert "172 413.79 ₸ × 4 = 689 655.16 ₸ · <b>аванс 30%</b>" in russian
    assert "⏰ 09.11 09:00" in kazakh and "⏰ 09.11 09:00" in russian
    assert text.endswith("https://zakup.sarsembai.com/a/77") and text.count("https://") == 1
    assert "goszakup.gov.kz" not in text
    assert text.count("• ") == 4


def test_lots_of_one_announcement_become_one_block():
    found = matcher.notices([_pending(lot_id=1), _pending(lot_id=2), _pending(lot_id=3, announcement_id=78)])
    assert [len(n.keys) for n in found] == [2, 1]


@pytest.fixture
def outbox(monkeypatch):
    sent, errors = [], []

    def fake_send(chat_id, text):
        if errors:
            raise errors[0]
        sent.append((chat_id, text))

    monkeypatch.setattr(telegram, "send_message", fake_send)
    return sent, errors


def test_deliver_respects_message_limit_and_names_subscription(conn, outbox, monkeypatch):
    sent, _ = outbox
    monkeypatch.setattr(telegram, "MESSAGE_LIMIT", 400)
    _subscription(conn, "картридж", chat_id="42")
    for i in range(1, 8):
        _lot(conn, i, f"Картриджи партия {i}", 1000 + i, trd_buy_id=i)
    assert matcher.run(conn) == 7
    assert matcher.deliver(conn, pause=0) == 7
    assert len(sent) > 1
    assert all(text.startswith("🔔 <b>Картриджи</b>") for _, text in sent)
    # Каждый лот — дважды: в казахской и русской частях (казахского названия нет — русское).
    assert sum(text.count("• <b>Картриджи партия") for _, text in sent) == 14
    assert matcher.pending(conn) == []


def test_blocked_chat_is_unlinked_and_temporary_failure_retries(conn, outbox):
    _, errors = outbox
    _subscription(conn, "картридж", chat_id="42")
    _lot(conn, 1, "Картриджи", 1000)
    matcher.run(conn)
    errors.append(telegram.TelegramError(500, "Internal Server Error"))
    assert matcher.deliver(conn, pause=0) == 0
    assert len(matcher.pending(conn)) == 1
    errors[0] = telegram.TelegramError(403, "Forbidden: bot was blocked by the user")
    assert matcher.deliver(conn, pause=0) == 0
    assert matcher.pending(conn) == []
    assert conn.execute("SELECT telegram_chat_id FROM users").fetchone()[0] is None
