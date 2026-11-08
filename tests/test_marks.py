from datetime import UTC, datetime, timedelta

from zakup import auth, db, marks

NOW = datetime(2026, 11, 9, 6, 0, tzinfo=UTC)


def _lot(conn, end):
    db.upsert_announcement(conn, {"id": 7, "name_ru": "Столы", "end_date": end.isoformat()})
    db.upsert_lot(conn, {"id": 70, "trd_buy_id": 7, "name_ru": "Стол", "amount": 500000})
    user = auth.set_password(conn, "owner@mail.kz", "long-enough-password")
    conn.execute("UPDATE users SET telegram_chat_id = '42', paid_until = now() + interval '30 days'")
    return user


def test_mark_note_and_clear(conn):
    user = _lot(conn, NOW + timedelta(days=2))
    marks.set_mark(conn, user, 70, "interested")
    marks.set_note(conn, user, 70, "  поставщик 400 000  ")
    assert marks.of_user(conn, user, [70]) == {70: ("interested", "поставщик 400 000")}
    marks.set_mark(conn, user, 70, "")
    assert marks.of_user(conn, user, [70]) == {70: ("", "поставщик 400 000")}
    marks.set_note(conn, user, 70, "")
    assert marks.of_user(conn, user, [70]) == {}


def test_reminds_once_per_threshold(conn, monkeypatch):
    sent = []
    monkeypatch.setattr(marks.telegram, "send_message", lambda chat, text: sent.append((chat, text)))
    user = _lot(conn, NOW + timedelta(hours=30))
    marks.set_mark(conn, user, 70, "interested")
    conn.execute("UPDATE lot_marks SET status_at = %s", (NOW - timedelta(days=1),))
    assert marks.remind(conn, NOW, pause=0) == 0  # до окончания больше суток
    later = NOW + timedelta(hours=7)
    assert marks.remind(conn, later, pause=0) == 1
    assert sent[0][0] == "42" and "Осталось / Қалды: 23 ч" in sent[0][1] and "/lots/70" in sent[0][1]
    assert marks.remind(conn, later + timedelta(minutes=10), pause=0) == 0
    assert marks.remind(conn, NOW + timedelta(hours=28), pause=0) == 1
    assert "Осталось / Қалды: 2 ч" in sent[1][1]


def test_no_reminder_for_fresh_or_removed_favorites(conn, monkeypatch):
    sent = []
    monkeypatch.setattr(marks.telegram, "send_message", lambda chat, text: sent.append(text))
    user = _lot(conn, NOW + timedelta(hours=10))
    marks.set_mark(conn, user, 70, "interested")
    conn.execute("UPDATE lot_marks SET status_at = %s", (NOW,))
    # В избранное добавили, когда до конца было 10 ч: суточное напоминание лишнее, за 3 часа — придёт.
    assert marks.remind(conn, NOW + timedelta(hours=1), pause=0) == 0
    assert marks.remind(conn, NOW + timedelta(hours=8), pause=0) == 1
    marks.set_mark(conn, user, 70, "")  # убрали из избранного
    conn.execute("UPDATE lot_marks SET reminded_hours = NULL")
    assert marks.remind(conn, NOW + timedelta(hours=9), pause=0) == 0


def test_no_reminder_after_tariff_ends(conn, monkeypatch):
    sent = []
    monkeypatch.setattr(marks.telegram, "send_message", lambda chat, text: sent.append(chat))
    user = _lot(conn, NOW + timedelta(hours=23))
    conn.execute("UPDATE users SET paid_until = now() - interval '1 day', role = 'user'")
    marks.set_mark(conn, user, 70, "interested")
    conn.execute("UPDATE lot_marks SET status_at = %s", (NOW - timedelta(days=1),))
    assert marks.remind(conn, NOW, pause=0) == 0 and sent == []
