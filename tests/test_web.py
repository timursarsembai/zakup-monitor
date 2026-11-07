import os

import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from zakup import auth, web

ORIGIN = {"origin": "https://testserver"}


@pytest.fixture
def client(conn, monkeypatch):
    # Не conn.info.dsn: в нём нет пароля.
    test_pool = ConnectionPool(
        os.environ["TEST_DATABASE_URL"], min_size=1, max_size=2, open=False, timeout=5
    )
    monkeypatch.setattr(web, "pool", test_pool)
    auth.set_password(conn, "owner@mail.kz", "long-enough-password")
    with TestClient(web.app, base_url="https://testserver") as test_client:
        yield test_client


def _login(client, password="long-enough-password", next="/"):
    return client.post(
        "/login",
        data={"email": "owner@mail.kz", "password": password, "next": next},
        headers=ORIGIN,
        follow_redirects=False,
    )


def test_pages_redirect_to_login_and_back(client):
    response = client.get("/subscriptions?x=1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/subscriptions%3Fx%3D1"
    assert "Войти" in client.get(response.headers["location"]).text


def test_wrong_password_shows_error(client):
    response = _login(client, password="wrong-password")
    assert response.status_code == 401
    assert "Неверный e-mail или пароль" in response.text
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
    assert client.get("/", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("target", ["https://evil.example", "//evil.example", "/\\\\evil.example"])
def test_next_never_leaves_the_site(client, target):
    assert _login(client, next=target).headers["location"] == "/"


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


def test_settings_link_redirects_to_bot(client, monkeypatch):
    monkeypatch.setenv("ZAKUP_TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ZAKUP_TELEGRAM_BOT_USERNAME", "test_bot")
    _login(client)
    assert "Подключить Telegram" in client.get("/settings").text
    response = client.post("/settings/telegram/link", headers=ORIGIN, follow_redirects=False)
    assert response.headers["location"].startswith("https://t.me/test_bot?start=")
