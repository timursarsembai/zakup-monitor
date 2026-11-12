"""Вход, регистрация и восстановление пароля."""


import pytest

from tests.web_helpers import ORIGIN, _fake_sms, _flow, _login
from zakup import auth, signup


def test_pages_redirect_to_login_and_back(client):
    response = client.get("/subscriptions?x=1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/subscriptions%3Fx%3D1"
    assert "Войти" in client.get(response.headers["location"]).text


def test_wrong_password_shows_error(client):
    response = _login(client, password="wrong-password")
    assert response.status_code == 401
    assert "Неверный телефон или пароль" in response.text
    assert "zakup_session" not in response.cookies


def test_login_sets_secure_cookie_and_opens_pages(client):
    response = _login(client, next="/matches")
    assert response.status_code == 303
    assert response.headers["location"] == "/matches"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    page = client.get("/")
    assert page.status_code == 200 and "owner@mail.kz" in page.text
    assert client.get("/login", follow_redirects=False).status_code == 303
    client.post("/logout", headers=ORIGIN)
    # Главная (поиск закупок) открыта всем, личные разделы — только после входа.
    guest = client.get("/", follow_redirects=False)
    assert guest.status_code == 200 and "Регистрация" in guest.text
    assert client.get("/my", follow_redirects=False).headers["location"].startswith("/login")


@pytest.mark.parametrize("target", ["https://evil.example", "//evil.example", "/\\\\evil.example"])
def test_next_never_leaves_the_site(client, target):
    # Чужой адрес отбрасывается: администратор попадает на свою главную — дашборд.
    assert _login(client, next=target).headers["location"] == "/dashboard"


def test_login_from_another_site_is_rejected(client):
    response = client.post(
        "/login",
        data={"email": "owner@mail.kz", "password": "long-enough-password"},
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_too_many_failures_lock_the_form(client):
    for _ in range(auth.MAX_FAILURES_PER_EMAIL):
        _login(client, password="wrong-password")
    response = _login(client)
    assert response.status_code == 429
    assert "Слишком много" in response.text


def test_plain_login_without_email_works(client, conn):
    auth.set_password(conn, "Friend1", "another-long-password")
    response = client.post(
        "/login",
        data={"email": " friend1 ", "password": "another-long-password", "next": "/"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303 and "zakup_session" in response.cookies


def test_register_by_phone_then_company(client, conn, monkeypatch):
    sent = _fake_sms(monkeypatch)
    assert "Регистрация скоро откроется" in client.get("/register").text
    assert client.post("/register", data={"phone": "87011112233"}, headers=ORIGIN).status_code == 403
    signup.set_registration_open(conn, True)
    conn.commit()
    assert client.post("/register", data={"phone": "+7 727 111 22 33"}, headers=ORIGIN).status_code == 422
    page = client.post("/register", data={"phone": "8 (701) 111-22-33"}, headers=ORIGIN)
    assert sent == ["+77011112233"] and "+77011112233" in page.text
    flow = _flow(page.text)
    data = {"flow": flow, "code": "1234", "password": "long-password-1", "password2": "other-password-1"}
    assert "Пароли не совпадают" in client.post("/register/code", data=data, headers=ORIGIN).text
    data["password2"] = data["password"]
    data["code"] = "0000"
    assert "Неверный код. Осталось попыток: 4" in client.post("/register/code", data=data, headers=ORIGIN).text
    data["code"] = "1234"
    done = client.post("/register/code", data=data, headers=ORIGIN, follow_redirects=False)
    assert done.headers["location"] == "/welcome/company"
    assert conn.execute("SELECT role, phone FROM users WHERE email = '+77011112233'").fetchone() == (
        "user", "+77011112233")
    # Пробный месяц: платное открыто сразу после регистрации.
    assert conn.execute("""SELECT trial_until > now() + interval '29 days', paid_until IS NULL
                           FROM users WHERE email = '+77011112233'""").fetchone() == (True, True)
    # Тот же flow второй раз не работает.
    assert client.post("/register/code", data=data, headers=ORIGIN).status_code == 410
    assert "Ваша организация" in client.get("/welcome/company").text
    saved = client.post("/welcome/company", data={"kind": "ip", "bin": "900101300123", "name": "ИП Тест"},
                        headers=ORIGIN, follow_redirects=False)
    assert saved.headers["location"] == "/"
    assert conn.execute("SELECT kind, bin, name FROM client_orgs").fetchone() == ("ip", "900101300123", "ИП Тест")
    # Повторная регистрация того же номера — отказ.
    assert "уже зарегистрирован" in client.post("/register", data={"phone": "87011112233"},
                                               headers=ORIGIN).text
    client.post("/logout", headers=ORIGIN)
    ok = client.post("/login", data={"email": "8 701 111 22 33", "password": "long-password-1"},
                     headers=ORIGIN, follow_redirects=False)
    assert ok.status_code == 303


def test_forgot_password_by_sms_and_staff_login(client, conn, monkeypatch):
    _fake_sms(monkeypatch)
    auth.set_password(conn, "+77011112233", "old-password-1")
    assert "не зарегистрирован" in client.post("/forgot", data={"phone": "87051112233"}, headers=ORIGIN).text
    page = client.post("/forgot", data={"phone": "87011112233"}, headers=ORIGIN).text
    done = client.post("/forgot/code", data={"flow": _flow(page), "code": "1234", "password": "new-password-1",
                                             "password2": "new-password-1"}, headers=ORIGIN, follow_redirects=False)
    assert done.status_code == 303
    assert auth.login(conn, "+77011112233", "new-password-1", None, None)
    assert not auth.login(conn, "+77011112233", "old-password-1", None, None)
    client.post("/logout", headers=ORIGIN)
    assert "Вход для сотрудников" in client.get("/staff/login").text
    ok = client.post("/staff/login", data={"email": "owner@mail.kz", "password": "long-enough-password"},
                     headers=ORIGIN, follow_redirects=False)
    assert ok.status_code == 303


def test_sms_limits_protect_balance(client, conn, monkeypatch):
    sent = _fake_sms(monkeypatch)
    auth.set_password(conn, "+77011112233", "old-password-1")
    for _ in range(signup.PER_IP_HOUR):
        client.post("/forgot", data={"phone": "87011112233"}, headers=ORIGIN)
    blocked = client.post("/forgot", data={"phone": "87011112233"}, headers=ORIGIN)
    assert blocked.status_code == 429 and len(sent) == signup.PER_IP_HOUR
