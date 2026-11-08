import json
from datetime import datetime, timedelta, timezone

import pytest

from zakup import autofix, incidents
from zakup.goszakup import PortalUnavailable


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr(incidents.notify, "send", messages.append)
    monkeypatch.setattr(autofix.notify, "send", messages.append)
    return messages


def _raise(error):
    try:
        raise error
    except Exception as caught:
        return caught


def test_code_error_is_recorded_once(conn, sent):
    error = _raise(KeyError("token=abc123 postgresql://u:p@h/db"))
    incidents.failed("синхронизация", error, conn)
    incidents.failed("синхронизация", _raise(KeyError("x")), conn)
    rows = conn.execute("SELECT status, count, trace FROM incidents").fetchall()
    assert len(rows) == 1 and rows[0][0] == "new" and rows[0][1] == 2
    assert "abc123" not in rows[0][2] and "u:p@h" not in rows[0][2]
    assert len(sent) == 1 and "Разбираю автоматически" in sent[0]


def test_portal_outage_notifies_once_after_delay(conn, sent):
    incidents.failed("синхронизация", _raise(PortalUnavailable("down")), conn)
    incidents.failed("синхронизация", _raise(PortalUnavailable("down")), conn)
    assert sent == [] and conn.execute("SELECT count(*) FROM incidents").fetchone()[0] == 0
    since = datetime.now(timezone.utc) - timedelta(minutes=40)
    conn.execute("UPDATE app_settings SET value = %s WHERE key = 'portal_outage'",
                 (json.dumps({"since": since.isoformat(), "notified": False}),))
    incidents.failed("синхронизация", _raise(PortalUnavailable("down")), conn)
    incidents.failed("синхронизация", _raise(PortalUnavailable("down")), conn)
    assert len(sent) == 1 and "не отвечает" in sent[0]
    incidents.portal_ok(conn)
    assert len(sent) == 2 and "снова отвечает" in sent[1]
    incidents.portal_ok(conn)
    assert len(sent) == 2


def test_short_outage_is_silent(conn, sent):
    incidents.failed("синхронизация", _raise(PortalUnavailable("down")), conn)
    incidents.portal_ok(conn)
    assert sent == []


def test_autofix_reports_result(conn, sent, monkeypatch):
    incidents.failed("синхронизация", _raise(ValueError("bad")), conn)
    monkeypatch.setattr(autofix, "_claude", lambda prompt: {
        "verdict": "fixed", "summary": "Исправил разбор даты.", "commit": "abcdef1234567"})
    row = autofix._next(conn)
    autofix._finish(conn, row[0], *autofix.handle(row))
    status, commit = conn.execute("SELECT status, commit FROM incidents").fetchone()
    assert status == "fixed" and commit == "abcdef1234567"
    assert "исправлено и выложено" in sent[-1] and "abcdef1234" in sent[-1]


def test_autofix_gives_up_after_repeated_failures(conn, sent):
    for _ in range(2):
        conn.execute("INSERT INTO incidents (fingerprint, stage, error_type, status, started_at) "
                     "VALUES ('E@x', 's', 'E', 'failed', now())")
    conn.execute("INSERT INTO incidents (fingerprint, stage, error_type) VALUES ('E@x', 's', 'E')")
    assert autofix._next(conn) is None
    assert conn.execute("SELECT status FROM incidents ORDER BY id DESC LIMIT 1").fetchone()[0] == "owner"
    assert "нужно ваше решение" in sent[-1]
