import json
from datetime import UTC, datetime, timedelta

import pytest

from zakup import autofix, incidents
from zakup.goszakup import PortalUnavailable


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr(incidents.notify, "send", messages.append)
    monkeypatch.setattr(autofix.notify, "send", lambda text, buttons=None: messages.append(text))
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
    since = datetime.now(UTC) - timedelta(minutes=40)
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


PATCH = """diff --git a/src/zakup/sync.py b/src/zakup/sync.py
--- a/src/zakup/sync.py
+++ b/src/zakup/sync.py
@@ -1 +1 @@
-old
+new
"""


def test_autofix_proposes_fix_for_approval(conn, sent, monkeypatch):
    incidents.failed("синхронизация", _raise(ValueError("bad")), conn)
    buttons = []

    def send(text, buttons=None, _pressed=buttons):
        sent.append(text)
        _pressed.extend(buttons or [])
    monkeypatch.setattr(autofix.notify, "send", send)
    row = autofix._next(conn)
    autofix._finish(conn, row[0], "proposed", "Исправил разбор даты.", patch=PATCH)
    status, patch = conn.execute("SELECT status, patch FROM incidents").fetchone()
    assert status == "proposed" and patch == PATCH
    assert "выложить?" in sent[-1] and "src/zakup/sync.py: +1 −1" in sent[-1]
    assert buttons[0][0]["callback_data"] == f"autofix:approve:{row[0]}"


def test_autofix_patch_only_in_code_and_tests():
    assert autofix.allowed_patch(PATCH)
    assert not autofix.allowed_patch(PATCH.replace("src/zakup/sync.py", "scripts/deploy.sh"))
    assert not autofix.allowed_patch(PATCH.replace("src/zakup/sync.py", "src/zakup/autofix.py"))
    assert not autofix.allowed_patch(PATCH + PATCH.replace("src/zakup/sync.py", "docker-compose.yml"))
    assert not autofix.allowed_patch("")


def test_only_owner_approves_fix(conn, monkeypatch):
    from zakup import bot

    monkeypatch.setattr(bot.config, "owner_chat_id", lambda: "42")
    conn.execute("INSERT INTO incidents (fingerprint, stage, error_type, status, patch) "
                 "VALUES ('E@x', 's', 'E', 'proposed', 'p')")

    def press(chat, sender, data="autofix:approve:1"):
        return bot.handle_callback(conn, {"id": "q", "data": data, "from": {"id": sender},
                                          "message": {"chat": {"id": chat}}})
    assert press(42, 7) == "Это может только владелец."
    assert press(7, 42) == "Это может только владелец."
    assert conn.execute("SELECT status FROM incidents").fetchone()[0] == "proposed"
    assert "выкладываю" in press(42, 42)
    assert conn.execute("SELECT status FROM incidents").fetchone()[0] == "approved"
    assert press(42, 42, "autofix:reject:1") == "Уже решено раньше."


def test_autofix_gives_up_after_repeated_failures(conn, sent):
    for _ in range(2):
        conn.execute("INSERT INTO incidents (fingerprint, stage, error_type, status, started_at) "
                     "VALUES ('E@x', 's', 'E', 'failed', now())")
    conn.execute("INSERT INTO incidents (fingerprint, stage, error_type) VALUES ('E@x', 's', 'E')")
    assert autofix._next(conn) is None
    assert conn.execute("SELECT status FROM incidents ORDER BY id DESC LIMIT 1").fetchone()[0] == "owner"
    assert "нужно ваше решение" in sent[-1]


def test_portal_refusal_is_outage_not_code_error(conn, sent):
    from zakup.goszakup import GoszakupError

    incidents.failed("синхронизация", _raise(GoszakupError("/v3/trd-buy: HTTP 403", 403)), conn)
    assert sent == [] and conn.execute("SELECT count(*) FROM incidents").fetchone()[0] == 0
    incidents.failed("синхронизация", _raise(GoszakupError("/v3/trd-buy: HTTP 400", 400)), conn)
    assert conn.execute("SELECT count(*) FROM incidents").fetchone()[0] == 1
