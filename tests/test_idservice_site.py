"""Основной сайт с сервисом учётных записей (zakup.idservice): жетон, продление, формы, события."""

from datetime import UTC, datetime, timedelta

import pytest

from zakup import idservice, notify, telegram
from zakup_id import token

PRIVATE, PUBLIC = token.generate_keys()
ID = "https://id.tenderim.com"


class Reply:
    def __init__(self, status_code=200, data=None):
        self.status_code, self.data = status_code, data or {}

    def json(self):
        return self.data


@pytest.fixture
def service(monkeypatch, client):
    """Сервис включён; его API подменён: calls — что сайт спросил, answers — что ответить."""
    monkeypatch.setenv("ZAKUP_ID_URL", ID)
    monkeypatch.setenv("ZAKUP_ID_PUBLIC_KEY", PUBLIC)
    monkeypatch.setenv("ZAKUP_ID_SERVICE_KEY", "k")
    calls, answers = [], {}

    def api(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        answer = answers.get(path, Reply(404))
        if isinstance(answer, Exception):
            raise answer
        return answer(kwargs) if callable(answer) else answer
    monkeypatch.setattr(idservice, "_api", api)
    client.calls, client.answers = calls, answers
    return client


def _enter(client, user_id, age=timedelta(0), sid="s1"):
    value = token.issue(PRIVATE, user_id, sid, now=datetime.now(UTC) - age)
    client.cookies.set(token.COOKIE, value)
    return value


def test_fresh_token_signs_in_and_creates_user(service, conn):
    _enter(service, 500)
    page = service.get("/settings")
    assert page.status_code == 200 and f'href="{ID}/password"' in page.text
    assert service.calls == [("GET", "/api/accounts/500", None)]      # один раз — запись заведена
    service.get("/settings")
    assert len(service.calls) == 1
    row = conn.execute("SELECT email, role, trial_until > now() + interval '29 days' FROM users WHERE id = 500").fetchone()
    assert row == ("id:500", "user", True)
    assert "id:500" not in page.text and "№ 500" in page.text   # заглушку логина не показываем


def test_trial_moves_from_previous_record_of_same_number(service, conn):
    conn.execute("INSERT INTO users (id, email, trial_until) VALUES (400, 'old', now() - interval '5 days')")
    conn.commit()
    service.answers["/api/accounts/501"] = Reply(200, {"previous_id": 400, "deleted": False})
    _enter(service, 501)
    service.get("/settings")
    assert conn.execute("SELECT trial_until < now() FROM users WHERE id = 501").fetchone() == (True,)


def test_old_token_is_renewed_or_forgotten(service, conn):
    conn.execute("INSERT INTO users (id, email) VALUES (502, 'id:502')")
    conn.commit()
    old = _enter(service, 502, age=timedelta(days=2))
    fresh = token.issue(PRIVATE, 502, "s1", 1)
    service.answers["/api/renew"] = Reply(200, {"token": fresh})
    reply = service.get("/settings")
    assert reply.status_code == 200 and ("POST", "/api/renew", {"token": old}) in service.calls
    assert reply.cookies.get(token.COOKIE) == fresh
    # Сессию закрыли в Казахстане (выход, смена пароля) — сайт забывает вход.
    service.cookies.clear()
    _enter(service, 502, age=timedelta(days=2))
    service.answers["/api/renew"] = Reply(401, {"detail": "session ended"})
    reply = service.get("/settings", follow_redirects=False)
    assert 'tid=""' in reply.headers.get("set-cookie", "")
    assert f'href="{ID}/password"' not in reply.text


def test_service_down_keeps_recent_login_only(service, conn):
    conn.execute("INSERT INTO users (id, email) VALUES (503, 'id:503')")
    conn.commit()
    service.answers["/api/renew"] = idservice.ServiceError("ConnectTimeout")
    _enter(service, 503, age=timedelta(days=2))
    assert f'href="{ID}/password"' in service.get("/settings").text
    service.cookies.clear()
    _enter(service, 503, age=timedelta(days=4))
    assert f'href="{ID}/password"' not in service.get("/settings").text


def test_blocked_here_is_not_signed_in_and_forged_token_ignored(service, conn):
    conn.execute("INSERT INTO users (id, email, disabled) VALUES (504, 'id:504', true)")
    conn.commit()
    _enter(service, 504)
    assert f'href="{ID}/password"' not in service.get("/settings").text
    service.cookies.clear()
    service.cookies.set(token.COOKIE, token.issue(token.generate_keys()[0], 1, "x"))
    assert f'href="{ID}/password"' not in service.get("/settings").text
    assert conn.execute("SELECT count(*) FROM users WHERE email = 'id:1'").fetchone() == (0,)


def test_forms_lead_to_service(service):
    page = service.get("/lots").text
    assert f'action="{ID}/login"' in page and 'name="login"' in page and f'href="{ID}/register"' in page
    assert service.get("/register", follow_redirects=False).headers["location"] == f"{ID}/register"
    login = service.get("/login?error=password")
    assert login.status_code == 401 and "Неверный телефон или пароль" in login.text
    assert f'action="{ID}/login"' in service.get("/staff/login").text
    kk = service.get("/forgot?lang=kk", follow_redirects=False)
    assert kk.headers["location"] == f"{ID}/forgot?lang=kk"


def test_events_go_to_telegram_and_are_acknowledged(service, conn, monkeypatch):
    conn.execute("INSERT INTO users (id, email, telegram_chat_id, notify_lang) VALUES (505, 'id:505', '777', 'both')")
    conn.commit()
    sent, owner = [], []
    monkeypatch.setattr(telegram, "send_message", lambda chat, text, buttons=None: sent.append((chat, text)))
    monkeypatch.setattr(notify, "send", lambda text, buttons=None: owner.append(text) or True)
    service.answers["/api/events"] = Reply(200, {"events": [
        {"id": 1, "user_id": 505, "kind": "phone_changed", "detail": "…1122", "before": "…2233"},
        {"id": 2, "user_id": None, "kind": "owner_alert", "detail": "половина SMS"}]})
    service.answers["/api/events/ack"] = Reply(200, {"acked": 2})
    assert idservice.poll_events(conn) == 2
    assert sent[0][0] == "777" and "…2233 → …1122" in sent[0][1] and "өзгертілді" in sent[0][1]
    assert owner == ["id.tenderim.com: половина SMS"]
    assert ("POST", "/api/events/ack", {"ids": [1, 2]}) in service.calls


def test_disabled_service_changes_nothing(client):
    page = client.get("/lots").text
    assert 'action="/login"' in page and f"{ID}" not in page
    assert client.get("/register").status_code in (200, 403)


def test_renewal_failures_other_than_ended_session_keep_login(service, conn):
    conn.execute("INSERT INTO users (id, email) VALUES (506, 'id:506')")
    conn.commit()
    for answer in (Reply(401, {"detail": "service key required"}), Reply(403), Reply(200, {"token": None})):
        service.cookies.clear()
        service.answers["/api/renew"] = answer
        _enter(service, 506, age=timedelta(days=2))
        assert f'href="{ID}/password"' in service.get("/settings").text


def test_logout_closes_site_and_service_sessions(service, conn):
    conn.execute("INSERT INTO users (id, email) VALUES (507, 'id:507')")
    conn.commit()
    value = _enter(service, 507)
    service.answers["/api/logout"] = Reply(200, {"ok": True})
    reply = service.post("/logout", headers={"origin": "https://testserver"}, follow_redirects=False)
    assert ("POST", "/api/logout", {"token": value}) in service.calls and 'tid=""' in reply.headers["set-cookie"]


def test_no_local_users_while_service_is_on(service, conn):
    service.cookies.clear()
    service.post("/login", data={"email": "owner@mail.kz", "password": "long-enough-password"},
                 headers={"origin": "https://testserver"})
    reply = service.post("/settings/users", data={"login": "+77011112233"}, headers={"origin": "https://testserver"})
    assert reply.status_code == 409


def test_bad_event_does_not_block_others(service, conn, monkeypatch):
    owner = []
    monkeypatch.setattr(notify, "send", lambda text, buttons=None: owner.append(text) or True)
    service.answers["/api/events"] = Reply(200, {"events": [
        {"id": 3, "user_id": 1, "kind": "phone_changed"},           # без detail
        {"id": 4, "user_id": None, "kind": "owner_alert", "detail": "x"}]})
    service.answers["/api/events/ack"] = Reply(200, {"acked": 2})
    assert idservice.poll_events(conn) == 2 and owner == ["id.tenderim.com: x"]
    service.answers["/api/events"] = Reply(401, {"detail": "service key required"})
    assert idservice.poll_events(conn) == 0
