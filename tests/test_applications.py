"""Слежение за реестром заявок OWS (/v3/trd-app): раз в сутки, сообщение владельцу — один раз."""

from datetime import UTC, datetime, timedelta

from zakup import applications
from zakup.goszakup import GoszakupError


class Registry:
    def __init__(self, total):
        self.value, self.calls = total, 0

    def total(self, path):
        assert path == "/v3/trd-app"
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def test_daily_check_notifies_once_when_registry_starts_working(conn, monkeypatch):
    sent, ok = [], [False]
    monkeypatch.setattr(applications.notify, "send", lambda text: sent.append(text) or ok[0])
    day = timedelta(days=1, minutes=1)
    start = datetime(2026, 11, 12, 5, tzinfo=UTC)
    empty = Registry(0)
    assert applications.check(conn, empty, start) == "total 0"
    assert applications.check(conn, empty, start + timedelta(hours=5)) == "skip"  # раз в сутки
    assert empty.calls == 1 and sent == []
    # Ошибка портала или ответа — не сбой; следующая попытка тоже через сутки.
    down = Registry(GoszakupError("nope"))
    assert applications.check(conn, down, start + day) == "error"
    assert applications.check(conn, down, start + day + timedelta(minutes=15)) == "skip"
    odd = Registry(ValueError("not json"))
    assert applications.check(conn, odd, start + 2 * day) == "error"
    # Заработал: Telegram недоступен — повторим через сутки; дальше — ни слова.
    working = Registry(1234)
    assert applications.check(conn, working, start + 3 * day) == "total 1234"
    ok[0] = True
    assert applications.check(conn, working, start + 4 * day) == "total 1234"
    assert applications.check(conn, working, start + 5 * day) == "total 1234"
    assert len(sent) == 2 and "1234" in sent[0]
    # Транзакция не остаётся открытой.
    assert conn.info.transaction_status.name == "IDLE"


def test_broken_state_does_not_crash(conn):
    conn.execute("INSERT INTO app_settings (key, value) VALUES (%s, 'null')", (applications.KEY,))
    conn.commit()
    assert applications.check(conn, Registry(0), datetime(2026, 11, 12, tzinfo=UTC)) == "total 0"
