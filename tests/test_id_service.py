"""Сервис учётных записей (zakup_id): жетоны, вход, выход, продление, уведомления."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit, urlunsplit

import psycopg
import pytest
from fastapi.testclient import TestClient

from zakup.auth import hash_password
from zakup_id import accounts, token

SITE = "https://tenderim.com"
SERVICE_KEY = "service-key-for-tests"
PRIVATE, PUBLIC = token.generate_keys()


def test_token_roundtrip_and_tamper():
    now = datetime(2026, 11, 14, 10, tzinfo=UTC)
    value = token.issue(PRIVATE, 42, "sid-1", 3, now=now)
    claims = token.read(value, PUBLIC)
    assert (claims.user_id, claims.sid, claims.issued_at, claims.gen) == (42, "sid-1", now, 3)
    assert claims.fresh(now + timedelta(hours=23)) and not claims.fresh(now + timedelta(hours=25))
    assert claims.usable(now + timedelta(days=2)) and not claims.usable(now + timedelta(days=4))
    body, signature = value.split(".")
    forged = token.issue(PRIVATE, 43, "sid-1", 3, now=now).split(".")[0]
    assert token.read(f"{forged}.{signature}", PUBLIC) is None       # чужое тело
    assert token.read(value, token.generate_keys()[1]) is None       # чужой ключ
    for junk in (None, "", "abc", "a.b.c", "!!.??", "x" * 600):
        assert token.read(junk, PUBLIC) is None


@pytest.fixture
def id_db(_schema):
    """Своя база сервиса рядом с тестовой базой сайта — как на сервере в Казахстане."""
    url, _ = _schema
    parts = urlsplit(url)
    id_url = urlunsplit(parts._replace(path="/zakup_id_test"))
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS zakup_id_test WITH (FORCE)")
        admin.execute("CREATE DATABASE zakup_id_test")
    return id_url


@pytest.fixture
def service(id_db, monkeypatch):
    monkeypatch.setenv("ID_DATABASE_URL", id_db)
    monkeypatch.setenv("ID_SIGNING_KEY", PRIVATE)
    monkeypatch.setenv("ID_SERVICE_KEY", SERVICE_KEY)
    monkeypatch.setenv("ID_DEVICE_KEY", "device-key-for-tests")
    monkeypatch.setenv("ID_SITE_URL", SITE)
    monkeypatch.setenv("ID_COOKIE_DOMAIN", "")
    from zakup_id.app import app
    with TestClient(app, base_url="https://id.tenderim.com") as client, psycopg.connect(id_db) as conn:
        conn.execute("INSERT INTO accounts (id, login, password_hash, telegram_chat_id) "
                     "VALUES (7, '+77011112233', %s, '555')", (hash_password("верный-пароль"),))
        conn.commit()
        client.db = conn
        yield client


def _login(client, password="верный-пароль", login="8 701 111-22-33", origin=SITE, next="/my"):
    return client.post("/login", data={"login": login, "password": password, "next": next},
                       headers={"origin": origin}, follow_redirects=False)


def test_login_issues_token_and_returns_to_site(service):
    reply = _login(service)
    assert reply.status_code == 303 and reply.headers["location"] == f"{SITE}/my"
    claims = token.read(reply.cookies.get(token.COOKIE), PUBLIC)
    assert claims.user_id == 7 and claims.gen == 0
    assert accounts.renew(service.db, 7, claims.sid, 0) == 1
    attempt = service.db.execute("SELECT login, ok FROM login_attempts").fetchall()
    assert attempt == [("+77011112233", True)]


def test_login_refuses_foreign_pages_and_unsafe_next(service):
    assert _login(service, origin="https://evil.example").status_code == 403
    assert _login(service, next="//evil.example").headers["location"] == f"{SITE}/"


def test_wrong_password_goes_back_without_login_in_address(service):
    reply = _login(service, password="не-тот")
    where = urlsplit(reply.headers["location"])
    assert where.path == "/login" and parse_qs(where.query) == {"error": ["password"], "next": ["/my"]}
    assert "701" not in reply.headers["location"] and token.COOKIE not in reply.cookies


def test_lock_after_failures_but_not_for_known_browser(service):
    first = _login(service)                     # этот браузер уже входил — получил метку
    device = first.cookies.get("tdev")
    service.cookies.clear()
    for _ in range(accounts.MAX_FAILURES_PER_LOGIN):
        _login(service, password="не-тот")
    assert "error=lock" in _login(service).headers["location"]
    service.cookies.set("tdev", device, domain="id.tenderim.com")
    assert _login(service).headers["location"] == f"{SITE}/my"


def test_disabled_account_cannot_log_in(service):
    service.db.execute("UPDATE accounts SET disabled = true")
    service.db.commit()
    assert "error=password" in _login(service).headers["location"]


def _api(client, path, payload, key=SERVICE_KEY):
    return client.post(path, json=payload, headers={"authorization": f"Bearer {key}"})


def test_renew_and_logout(service):
    old = _login(service).cookies.get(token.COOKIE)
    assert _api(service, "/api/renew", {"token": old}, key="wrong").status_code == 401
    fresh = _api(service, "/api/renew", {"token": old}).json()["token"]
    assert token.read(fresh, PUBLIC).user_id == 7
    service.cookies.set(token.COOKIE, old, domain="id.tenderim.com")
    reply = service.post("/logout", headers={"origin": SITE}, follow_redirects=False)
    assert reply.headers["location"] == f"{SITE}/"
    assert _api(service, "/api/renew", {"token": fresh}).status_code == 401


def test_renewed_token_retires_the_old_one(service):
    old = _login(service).cookies.get(token.COOKIE)
    fresh = _api(service, "/api/renew", {"token": old}).json()["token"]
    # Вторая копия сайта продлевает тот же жетон одновременно — не выкидываем.
    again = _api(service, "/api/renew", {"token": old}).json()["token"]
    assert token.read(again, PUBLIC).gen == token.read(fresh, PUBLIC).gen == 1
    service.db.execute("UPDATE sessions SET renewed_at = now() - interval '1 hour'")
    service.db.commit()
    assert _api(service, "/api/renew", {"token": old}).status_code == 401   # утёкший старый
    assert token.read(_api(service, "/api/renew", {"token": fresh}).json()["token"], PUBLIC).gen == 2


def test_revoke_ends_every_session_and_block_holds(service):
    first = _login(service)
    device = first.cookies.get("tdev")
    _login(service)
    assert _api(service, "/api/revoke", {"user_id": 7}).json() == {"ended": 2}
    assert _api(service, "/api/renew", {"token": first.cookies.get(token.COOKIE)}).status_code == 401
    # После «выйти везде» старая метка браузера не спасает от блокировки подбора.
    service.cookies.clear()
    for _ in range(accounts.MAX_FAILURES_PER_LOGIN):
        _login(service, password="не-тот")
    service.cookies.set("tdev", device, domain="id.tenderim.com")
    assert "error=lock" in _login(service).headers["location"]
    service.db.execute("DELETE FROM login_attempts")
    service.db.commit()
    _api(service, "/api/revoke", {"user_id": 7, "block": True})
    assert "error=password" in _login(service).headers["location"]
    _api(service, "/api/revoke", {"user_id": 7, "block": False})
    assert _login(service).headers["location"] == f"{SITE}/my"


def test_sync_ids_after_import(service):
    accounts.sync_ids(service.db)
    new = service.db.execute("INSERT INTO accounts (login) VALUES ('new@example.com') RETURNING id").fetchone()
    assert new == (8,)


def test_notify_sends_to_chat_and_forgets_gone_chat(service, monkeypatch):
    import zakup_id.app as id_app
    sent = []

    class Reply:
        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    monkeypatch.setenv("ID_TELEGRAM_TOKEN", "bot-token")
    monkeypatch.setattr(id_app.httpx, "post",
                        lambda url, json, timeout: sent.append(json) or Reply({"ok": True}))
    assert _api(service, "/api/notify", {"user_id": 7, "text": "Новый лот"}).json() == {"sent": True}
    assert sent[0]["chat_id"] == "555" and sent[0]["text"] == "Новый лот"
    assert _api(service, "/api/notify", {"user_id": 8, "text": "x"}).json()["reason"] == "no_chat"
    monkeypatch.setattr(id_app.httpx, "post", lambda url, json, timeout: Reply(
        {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}))
    assert _api(service, "/api/notify", {"user_id": 7, "text": "x"}).json()["reason"] == "chat_gone"
    assert service.db.execute("SELECT telegram_chat_id FROM accounts").fetchone() == (None,)
