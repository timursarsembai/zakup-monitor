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


def _as_admin(service, conn):
    conn.execute("INSERT INTO users (id, email) VALUES (700, 'id:700')")
    conn.commit()
    service.cookies.clear()
    service.post("/login", data={"email": "owner@mail.kz", "password": "long-enough-password"},
                 headers={"origin": "https://testserver"})


def _staff(service, path):
    return service.post(f"/settings/users/700/{path}", headers={"origin": "https://testserver"})


def test_staff_actions_go_to_service_first(service, conn):
    """Отключить, удалить, вернуть, разрешить заново, регистрация — сначала в Казахстане."""
    _as_admin(service, conn)
    for path in ("/api/revoke", "/api/status", "/api/registration"):
        service.answers[path] = Reply(200, {"ok": True, "open": False})
    assert _staff(service, "disable").status_code == 200
    assert _staff(service, "delete").status_code == 200
    assert _staff(service, "reregister").status_code == 200
    assert _staff(service, "restore").status_code == 200
    service.post("/settings/registration", headers={"origin": "https://testserver"})
    # Регистрация закрыта в Казахстане (копия здесь могла разойтись) — открываем.
    assert [c for c in service.calls if c[0] == "POST" or c[1] == "/api/registration"] == [
        ("POST", "/api/revoke", {"user_id": 700, "block": True}),
        ("POST", "/api/status", {"user_id": 700, "deleted": True, "may_reregister": None}),
        ("POST", "/api/status", {"user_id": 700, "deleted": None, "may_reregister": True}),
        ("POST", "/api/status", {"user_id": 700, "deleted": False, "may_reregister": None}),
        ("GET", "/api/registration", None), ("POST", "/api/registration", {"open": True})]
    row = conn.execute("SELECT disabled, deleted_at IS NULL, may_reregister FROM users WHERE id = 700").fetchone()
    assert row == (True, True, True)


def test_staff_action_changes_nothing_when_service_fails(service, conn):
    _as_admin(service, conn)
    service.answers["/api/status"] = idservice.ServiceError("ConnectTimeout")
    reply = _staff(service, "delete")
    assert reply.status_code == 503 and "ничего не изменено" in reply.text
    assert conn.execute("SELECT deleted_at FROM users WHERE id = 700").fetchone() == (None,)
    # Номер уже занят новой регистрацией — прежнюю запись не вернуть.
    conn.execute("UPDATE users SET deleted_at = now() WHERE id = 700")
    conn.commit()
    service.answers["/api/status"] = Reply(409)
    reply = _staff(service, "restore")
    assert reply.status_code == 409
    assert conn.execute("SELECT deleted_at IS NOT NULL FROM users WHERE id = 700").fetchone() == (True,)
    # Записи в Казахстане нет (заведён до переезда) — меняем только здесь.
    service.answers["/api/status"] = Reply(404, {"detail": "no such account"})
    assert _staff(service, "reregister").status_code == 200
    assert conn.execute("SELECT may_reregister FROM users WHERE id = 700").fetchone() == (True,)
    # Другой 404 (неверный адрес, старая сборка) — ошибка, здесь ничего не меняем.
    service.answers["/api/status"] = Reply(404, {"detail": "Not Found"})
    assert _staff(service, "reregister").status_code == 503
    assert conn.execute("SELECT may_reregister FROM users WHERE id = 700").fetchone() == (True,)
    # Отключение не дошло — вход здесь не трогаем; регистрация — тоже.
    conn.execute("UPDATE users SET deleted_at = NULL WHERE id = 700")
    conn.commit()
    service.answers["/api/revoke"] = idservice.ServiceError("ConnectTimeout")
    assert _staff(service, "disable").status_code == 503
    assert conn.execute("SELECT disabled FROM users WHERE id = 700").fetchone() == (False,)
    service.answers["/api/registration"] = Reply(404)
    reply = service.post("/settings/registration", headers={"origin": "https://testserver"})
    assert reply.status_code == 503 and "отказал" in reply.text
    failed = [r[0] for r in conn.execute("SELECT action FROM user_actions WHERE action LIKE '%%_failed' ORDER BY id")]
    assert failed == ["user_delete_failed", "user_reregister_failed", "user_disable_failed",
                      "settings_registration_failed"]


def test_migrate_then_scrub_personal_data(service, conn):
    conn.execute("INSERT INTO users (id, email, password_hash, phone) VALUES (13, '+77015556677', 'scrypt$x', "
                 "'+77015556677'), (14, 'amir', 'scrypt$y', NULL)")
    conn.execute("INSERT INTO user_actions (user_id, login, action, target) VALUES (1, 'owner@mail.kz', "
                 "'user_delete', '+77015556677'), (NULL, '+77019998877', 'otp_sent', NULL)")
    conn.execute("INSERT INTO login_attempts (email, ip, ok) VALUES ('+77015556677', '10.0.0.1', false)")
    conn.commit()
    sent = {}

    def imported(kwargs):
        sent["rows"] = kwargs["json"]["accounts"]
        return Reply(200, {"imported": [r["id"] for r in sent["rows"]], "present": [], "conflicts": [],
                           "errors": []})
    service.answers["/api/import"] = imported
    result = idservice.migrate(conn)
    logins = {r["id"]: r["login"] for r in sent["rows"]}
    assert logins[13] == "+77015556677" and logins[14] == "amir" and result["conflicts"] == []
    assert next(r for r in sent["rows"] if r["id"] == 13)["password_hash"] == "scrypt$x"
    verdict = {"same": [i for i in logins if i != 14], "missing": [], "different": [14]}
    service.answers["/api/verify"] = lambda kwargs: Reply(200, verdict)
    # Проверка — ничего не стирает.
    assert idservice.scrub(conn, dry_run=True)["different"] == [14]
    assert conn.execute("SELECT email FROM users WHERE id = 13").fetchone() == ("+77015556677",)
    # Под номером 14 в Казахстане чужая запись — его не трогаем, записи о входах храним.
    stats = idservice.scrub(conn)
    assert stats["scrubbed"] == len(logins) - 1 and stats["different"] == [14]
    assert conn.execute("SELECT email, password_hash, phone FROM users WHERE id = 13").fetchone() == ("id:13", None, None)
    assert conn.execute("SELECT email, password_hash FROM users WHERE id = 14").fetchone() == ("amir", "scrypt$y")
    assert conn.execute("SELECT login, target FROM user_actions WHERE user_id = 1").fetchone() == ("id:1", "id:13")
    assert conn.execute("SELECT count(*) FROM login_attempts").fetchone() == (1,)
    # Разобрались — переехали все: стираем 14, телефоны в журнале и записи о входах.
    verdict.update(same=[14], different=[])
    stats = idservice.scrub(conn)
    assert stats["scrubbed"] == 1 and stats["login_attempts"] == 1
    assert conn.execute("SELECT login FROM user_actions WHERE action = 'otp_sent'").fetchone() == (None,)
    # Сервис не ответил — ничего не стираем.
    conn.execute("UPDATE users SET email = 'serik' WHERE id = 14")
    conn.commit()
    service.answers["/api/verify"] = idservice.ServiceError("ConnectTimeout")
    with pytest.raises(idservice.ServiceError):
        idservice.scrub(conn)
    assert conn.execute("SELECT email FROM users WHERE id = 14").fetchone() == ("serik",)
