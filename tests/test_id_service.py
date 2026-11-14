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
        accounts.sync_ids(conn)   # как после переноса: новые номера — после перенесённых
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


@pytest.fixture
def sms(monkeypatch):
    """kazpayment без сети: код всегда 1234, номер подтверждает тот, на который отправили."""
    from zakup import kazpayment
    sent = []
    monkeypatch.setattr(kazpayment, "request_otp", lambda phone: sent.append(phone) or f"req-{phone}")
    monkeypatch.setattr(kazpayment, "verify_otp",
                        lambda request_id, code: request_id.removeprefix("req-") if code == "1234" else None)
    return sent


def _open_registration(client):
    assert _api(client, "/api/registration", {"open": True}).json() == {"open": True}


def _flow(reply):
    import re
    return re.search(r'name="flow" value="([^"]+)"', reply.text).group(1)


def _own(client, path, data):
    return client.post(path, data=data, headers={"origin": "https://id.tenderim.com"}, follow_redirects=False)


def test_registration_closed_by_default_then_full_flow(service, sms):
    assert service.get("/register").status_code == 403
    _open_registration(service)
    assert "Получить код" in service.get("/register").text
    taken = _own(service, "/register", {"phone": "+7 701 111 22 33"})
    assert taken.status_code == 422 and "уже зарегистрирован" in taken.text
    step = _own(service, "/register", {"phone": "8 702 000 11 22"})
    assert step.status_code == 200 and sms == ["+77020001122"] and "+77020001122" in step.text
    flow = _flow(step)
    short = _own(service, "/register/code", {"flow": flow, "code": "1234", "password": "коротко", "password2": "коротко"})
    assert short.status_code == 422
    wrong = _own(service, "/register/code", {"flow": flow, "code": "9999", "password": "длинный-пароль",
                                             "password2": "длинный-пароль"})
    assert "Неверный код" in wrong.text
    done = _own(service, "/register/code", {"flow": flow, "code": "1234", "password": "длинный-пароль",
                                            "password2": "длинный-пароль"})
    assert done.headers["location"] == f"{SITE}/welcome/company"
    claims = token.read(done.cookies.get(token.COOKIE), PUBLIC)
    assert claims.user_id == 8
    assert _api_get(service, "/api/accounts/8") == {"previous_id": None, "deleted": False}
    service.cookies.clear()
    assert _login(service, login="+77020001122", password="длинный-пароль").headers["location"] == f"{SITE}/my"


def _api_get(client, path):
    return client.get(path, headers={"authorization": f"Bearer {SERVICE_KEY}"}).json()


def test_deleted_number_registers_again_only_when_allowed(service, sms):
    _open_registration(service)
    _api(service, "/api/status", {"user_id": 7, "deleted": True})
    assert "error=password" in _login(service).headers["location"]
    closed = _own(service, "/register", {"phone": "+77011112233"})
    assert closed.status_code == 403
    _api(service, "/api/status", {"user_id": 7, "may_reregister": True})
    flow = _flow(_own(service, "/register", {"phone": "+77011112233"}))
    done = _own(service, "/register/code", {"flow": flow, "code": "1234", "password": "новый-пароль-1",
                                            "password2": "новый-пароль-1"})
    new_id = token.read(done.cookies.get(token.COOKIE), PUBLIC).user_id
    assert new_id != 7 and _api_get(service, f"/api/accounts/{new_id}")["previous_id"] == 7


def test_password_reset_by_sms_ends_old_sessions(service, sms):
    old = _login(service).cookies.get(token.COOKIE)
    assert _own(service, "/forgot", {"phone": "+77779998877"}).status_code == 404
    flow = _flow(_own(service, "/forgot", {"phone": "87011112233"}))
    done = _own(service, "/forgot/code", {"flow": flow, "code": "1234", "password": "сброшенный-пароль",
                                          "password2": "сброшенный-пароль"})
    assert done.headers["location"] == f"{SITE}/"
    assert _api(service, "/api/renew", {"token": old}).status_code == 401
    service.cookies.clear()
    assert _login(service, password="сброшенный-пароль").headers["location"] == f"{SITE}/my"


def test_phone_checks_are_limited_per_address(service, sms):
    _open_registration(service)
    for _ in range(10):
        _own(service, "/forgot", {"phone": "+77779998877"})
    assert _own(service, "/forgot", {"phone": "+77011112233"}).status_code == 429
    assert sms == []


def test_password_change_needs_session_and_current_password(service):
    assert service.get("/password", follow_redirects=False).headers["location"].startswith(f"{SITE}/login")
    first = _login(service)
    service.cookies.set(token.COOKIE, first.cookies.get(token.COOKIE), domain="id.tenderim.com")
    assert "Текущий пароль" in service.get("/password").text
    bad = _own(service, "/password", {"current": "не-тот", "password": "другой-пароль", "password2": "другой-пароль"})
    assert bad.status_code == 422 and "неверный" in bad.text
    ok = _own(service, "/password", {"current": "верный-пароль", "password": "другой-пароль",
                                     "password2": "другой-пароль"})
    assert ok.headers["location"] == f"{SITE}/settings?notice=password"
    assert _api(service, "/api/renew", {"token": first.cookies.get(token.COOKIE)}).status_code == 401
    assert _api(service, "/api/renew", {"token": ok.cookies.get(token.COOKIE)}).status_code == 200


def test_kazakh_pages(service):
    assert 'lang="kk"' in service.get("/forgot?lang=kk").text


def test_blocked_number_gets_no_reset_sms(service, sms):
    _api(service, "/api/revoke", {"user_id": 7, "block": True})
    reply = _own(service, "/forgot", {"phone": "+77011112233"})
    assert reply.status_code == 403 and sms == []


def test_replaced_record_cannot_be_restored(service, sms):
    _open_registration(service)
    _api(service, "/api/status", {"user_id": 7, "deleted": True, "may_reregister": True})
    flow = _flow(_own(service, "/register", {"phone": "+77011112233"}))
    _own(service, "/register/code", {"flow": flow, "code": "1234", "password": "новый-пароль-1",
                                     "password2": "новый-пароль-1"})
    assert _api(service, "/api/status", {"user_id": 7, "deleted": False}).status_code == 409
    assert _api(service, "/api/status", {"user_id": 99, "deleted": True}).status_code == 404


def test_deleted_account_gets_no_messages(service, monkeypatch):
    import zakup_id.app as id_app
    monkeypatch.setenv("ID_TELEGRAM_TOKEN", "bot-token")
    monkeypatch.setattr(id_app.httpx, "post", lambda *a, **k: pytest.fail("must not send"))
    _api(service, "/api/status", {"user_id": 7, "deleted": True})
    assert _api(service, "/api/notify", {"user_id": 7, "text": "x"}).json()["reason"] == "no_chat"


def test_wrong_code_message_in_kazakh(service, sms):
    flow = _flow(_own(service, "/forgot", {"phone": "+77011112233", "lang": "kk"}))
    reply = _own(service, "/forgot/code", {"flow": flow, "code": "0000", "password": "сброшенный-пароль",
                                           "password2": "сброшенный-пароль", "lang": "kk"})
    assert "Қалған әрекеттер: 4" in reply.text


def test_replaced_token_does_not_open_password_page(service):
    old = _login(service).cookies.get(token.COOKIE)
    _api(service, "/api/renew", {"token": old})
    service.db.execute("UPDATE sessions SET renewed_at = now() - interval '1 hour'")
    service.db.commit()
    service.cookies.set(token.COOKIE, old, domain="id.tenderim.com")
    assert service.get("/password", follow_redirects=False).status_code == 303


def test_parallel_password_guesses_stay_limited(service):
    first = _login(service).cookies.get(token.COOKIE)
    service.cookies.set(token.COOKIE, first, domain="id.tenderim.com")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as pool:
        replies = list(pool.map(lambda i: _own(service, "/password", {
            "current": f"guess-{i}", "password": "другой-пароль", "password2": "другой-пароль"}), range(12)))
    assert sum("Текущий пароль неверный" in r.text for r in replies) == accounts.MAX_FAILURES_PER_LOGIN


def _signed_in_client(client):
    client.cookies.set(token.COOKIE, _login(client).cookies.get(token.COOKIE), domain="id.tenderim.com")


def test_phone_change_needs_password_and_code_on_new_number(service, sms):
    _signed_in_client(service)
    assert "Новый номер телефона" in service.get("/phone").text
    bad = _own(service, "/phone", {"current": "не-тот", "phone": "+77050001122"})
    assert bad.status_code == 422 and sms == []
    service.db.execute("INSERT INTO accounts (login, phone) VALUES ('+77050009999', '+77050009999')")
    service.db.commit()
    busy = _own(service, "/phone", {"current": "верный-пароль", "phone": "+77050009999"})
    assert "уже используется" in busy.text and sms == []
    step = _own(service, "/phone", {"current": "верный-пароль", "phone": "8 705 000 11 22"})
    assert sms == ["+77050001122"] and "Подтвердить номер" in step.text
    done = _own(service, "/phone/code", {"flow": _flow(step), "code": "1234"})
    assert done.headers["location"] == f"{SITE}/settings?notice=phone"
    assert service.db.execute("SELECT login, phone FROM accounts WHERE id = 7").fetchone() == (
        "+77050001122", "+77050001122")
    service.cookies.clear()
    assert _login(service, login="+77050001122").headers["location"] == f"{SITE}/my"


def test_phone_change_flow_belongs_to_its_account(service, sms):
    _signed_in_client(service)
    flow = _flow(_own(service, "/phone", {"current": "верный-пароль", "phone": "+77050001122"}))
    service.db.execute("INSERT INTO accounts (id, login, password_hash) VALUES (50, 'other@example.com', %s)",
                       (hash_password("другой-человек"),))
    service.db.commit()
    service.cookies.clear()
    other = _login(service, login="other@example.com", password="другой-человек")
    service.cookies.set(token.COOKIE, other.cookies.get(token.COOKIE), domain="id.tenderim.com")
    assert _own(service, "/phone/code", {"flow": flow, "code": "1234"}).status_code == 410
    # Сотрудник (логин — e-mail) меняет только телефон, логин остаётся.
    flow = _flow(_own(service, "/phone", {"current": "другой-человек", "phone": "+77060001122"}))
    _own(service, "/phone/code", {"flow": flow, "code": "1234"})
    assert service.db.execute("SELECT login, phone FROM accounts WHERE id = 50").fetchone() == (
        "other@example.com", "+77060001122")


def test_phone_change_takes_released_number_and_warns_owner(service, sms, monkeypatch):
    import zakup_id.app as id_app
    sent = []

    class Reply:
        def json(self):
            return {"ok": True}
    monkeypatch.setenv("ID_TELEGRAM_TOKEN", "bot-token")
    monkeypatch.setattr(id_app.httpx, "post", lambda url, json, timeout: sent.append(json) or Reply())
    # Номер удалённой записи, которой разрешили заново, — можно занять.
    service.db.execute("INSERT INTO accounts (login, phone, deleted_at, may_reregister) "
                       "VALUES ('+77050001122', '+77050001122', now(), true)")
    service.db.commit()
    other_browser = _login(service).cookies.get(token.COOKIE)
    service.cookies.clear()
    _signed_in_client(service)
    own = _own(service, "/phone", {"current": "верный-пароль", "phone": "+77011112233"})
    assert "уже ваш номер" in own.text and sms == []
    step = _own(service, "/phone", {"current": "верный-пароль", "phone": "+77050001122"})
    done = _own(service, "/phone/code", {"flow": _flow(step), "code": "1234"})
    assert done.headers["location"] == f"{SITE}/settings?notice=phone"
    assert service.db.execute("SELECT login FROM accounts WHERE phone = '+77050001122'").fetchone() == (
        "+77050001122",)
    assert sent and "…1122" in sent[0]["text"] and "…2233" in sent[0]["text"]
    # Другие входы закрыты, этот — нет.
    assert _api(service, "/api/renew", {"token": other_browser}).status_code == 401
    assert service.get("/phone").status_code == 200


def test_password_lockout_answers_429(service):
    _signed_in_client(service)
    for _ in range(accounts.MAX_FAILURES_PER_LOGIN):
        _own(service, "/phone", {"current": "не-тот", "phone": "+77050001122"})
    assert _own(service, "/phone", {"current": "верный-пароль", "phone": "+77050001122"}).status_code == 429
