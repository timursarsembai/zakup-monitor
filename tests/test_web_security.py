"""Находки аудита безопасности: вход, вход «как пользователь», перебор номеров."""

from tests.web_helpers import ORIGIN, _fake_sms, _login
from zakup import auth


def test_others_wrong_passwords_do_not_lock_owner_on_known_device(client, conn, monkeypatch):
    monkeypatch.setenv("ZAKUP_SECRET_KEY", "test-key")
    assert _login(client).status_code == 303
    device = client.cookies.get("zakup_dev")
    assert device
    client.post("/logout", headers=ORIGIN)
    # Кто-то перебирает пароль: с нового браузера вход закрыт…
    client.cookies.delete("zakup_dev")
    for _ in range(auth.MAX_FAILURES_PER_EMAIL):
        _login(client, password="wrong-password")
    assert _login(client).status_code == 429
    # …а из браузера, где владелец уже входил, — открыт.
    client.cookies.set("zakup_dev", device)
    assert _login(client).status_code == 303
    # Чужая метка (другого пользователя) не помогает.
    client.post("/logout", headers=ORIGIN)
    client.cookies.set("zakup_dev", auth.device_mark(999))
    assert _login(client).status_code == 429


def test_failed_login_of_unknown_account_does_not_store_typed_login(client, conn):
    client.post("/login", data={"email": "my-secret-password", "password": "x"}, headers=ORIGIN)
    row = conn.execute("SELECT login FROM user_actions WHERE action = 'login_failed'").fetchone()
    assert row == (None,)


def test_impersonation_is_marked_and_cannot_touch_password_telegram_payments(client, conn):
    auth.set_password(conn, "client", "client-password-1")
    conn.execute("UPDATE users SET paid_until = now() + interval '30 days' WHERE email = 'client'")
    conn.commit()
    ids = dict(conn.execute("SELECT email, id FROM users").fetchall())
    _login(client)
    admin_token = client.cookies.get("zakup_session")
    client.post(f"/settings/users/{ids['client']}/impersonate", headers=ORIGIN)
    # Пометка — в самой сессии: без cookie возврата администратор всё равно виден.
    client.cookies.delete("zakup_return")
    assert "Вы вошли как" in client.get("/pricing").text
    for path, data in (("/settings/telegram/link", {}), ("/pay/tariff", {}),
                       ("/settings/password", {"current": "x", "password": "y" * 12, "repeat": "y" * 12})):
        assert client.post(path, data=data, headers=ORIGIN).status_code == 403, path
    assert client.get("/export.xlsx").status_code == 200
    marked = conn.execute("SELECT details->>'by_admin' FROM user_actions WHERE user_id = %s AND action <> 'impersonate'",
                          (ids["client"],)).fetchall()
    assert marked and all(m == (str(ids["owner@mail.kz"]),) for m in marked)
    # Выход «из-под пользователя» закрывает и сессию администратора.
    client.cookies.set("zakup_return", admin_token)
    client.post("/logout", headers=ORIGIN)
    assert auth.user_by_session(conn, admin_token) is None


def test_phone_checks_are_limited_per_address(client, conn, monkeypatch):
    _fake_sms(monkeypatch)
    for number in range(10):
        client.post("/forgot", data={"phone": f"8701111{number:04d}"}, headers=ORIGIN)
    blocked = client.post("/forgot", data={"phone": "87011119999"}, headers=ORIGIN)
    assert blocked.status_code == 429 and "Слишком много попыток" in blocked.text


def test_reset_rules_for_admins_and_moderator_phone_accounts(client, conn):
    auth.set_password(conn, "second-admin", "admin-password-1")
    auth.set_password(conn, "amir", "amir-password-1")
    conn.execute("UPDATE users SET role = 'admin' WHERE email = 'second-admin'")
    conn.execute("UPDATE users SET role = 'moderator' WHERE email = 'amir'")
    conn.commit()
    ids = dict(conn.execute("SELECT email, id FROM users").fetchall())
    _login(client)
    assert client.post(f"/settings/users/{ids['second-admin']}/reset", headers=ORIGIN).status_code == 403
    assert client.post(f"/settings/users/{ids['owner@mail.kz']}/reset", headers=ORIGIN).status_code == 422
    # Телефонный логин — в виде «+7…», как при регистрации.
    assert "Создан пользователь +77015554433" in client.post(
        "/settings/users", data={"login": "8 701 555 44 33"}, headers=ORIGIN).text
    client.post("/logout", headers=ORIGIN)
    client.post("/login", data={"email": "amir", "password": "amir-password-1"}, headers=ORIGIN)
    assert client.post("/settings/users", data={"login": "87017776655"}, headers=ORIGIN).status_code == 403
    assert "Создан пользователь helper" in client.post(
        "/settings/users", data={"login": "helper"}, headers=ORIGIN).text


def test_current_password_guessing_is_limited(client, conn):
    _login(client)
    wrong = {"current": "wrong-one", "password": "new-password-1", "repeat": "new-password-1"}
    for _ in range(auth.MAX_FAILURES_PER_EMAIL):
        client.post("/settings/password", data=wrong, headers=ORIGIN)
    right = {**wrong, "current": "long-enough-password"}
    assert "Слишком много неудачных попыток" in client.post("/settings/password", data=right, headers=ORIGIN).text
