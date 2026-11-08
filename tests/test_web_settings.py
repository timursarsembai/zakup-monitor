"""Настройки и управление пользователями."""



from tests.web_helpers import ORIGIN, _login
from zakup import auth, web


def test_settings_link_redirects_to_bot(client, monkeypatch):
    monkeypatch.setenv("ZAKUP_TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ZAKUP_TELEGRAM_BOT_USERNAME", "test_bot")
    _login(client)
    assert "Подключить Telegram" in client.get("/settings").text
    response = client.post("/settings/telegram/link", headers=ORIGIN, follow_redirects=False)
    assert response.headers["location"].startswith("https://t.me/test_bot?start=")


def test_roles_users_and_passwords(client, conn):
    _login(client)
    page = client.post("/settings/users", data={"login": "Amir"}, headers=ORIGIN).text
    password = page.split("Пароль: ", 1)[1].split(" ", 1)[0]
    assert len(password) == 12 and "Telegram-группы" in page and "Сбросить пароль" in page
    client.post("/logout", headers=ORIGIN)
    friend = client.post("/login", data={"email": "amir", "password": password, "next": "/settings"},
                         headers=ORIGIN)
    assert "Мой пароль" in friend.text
    assert "Telegram-группы" not in friend.text and "Искусственный интеллект" not in friend.text
    assert "Пользователи" not in friend.text
    assert client.post("/settings/ai", data={"provider": "claude_code"}, headers=ORIGIN).status_code == 403
    assert client.post("/settings/users", data={"login": "x"}, headers=ORIGIN).status_code == 403
    bad = client.post("/settings/password", data={"current": "wrong-one", "password": "new-password-1",
                                                  "repeat": "new-password-1"}, headers=ORIGIN)
    assert bad.status_code == 422 and "Текущий пароль неверный" in bad.text
    ok = client.post("/settings/password", data={"current": password, "password": "new-password-1",
                                                 "repeat": "new-password-1"},
                     headers=ORIGIN, follow_redirects=False)
    assert ok.headers["location"] == "/login"
    assert "Мой пароль" not in client.get("/settings").text  # гостю — только «Интерфейс»  # сессия сброшена
    amir = conn.execute("SELECT id FROM users WHERE email = 'amir'").fetchone()[0]
    _login(client)
    reset = client.post(f"/settings/users/{amir}/reset", headers=ORIGIN).text
    assert "Новый пароль для amir" in reset


def test_settings_tabs_and_ai_models(client, conn, monkeypatch):
    from cryptography.fernet import Fernet

    _login(client)
    page = client.get("/settings").text
    assert "<title>Настройки · Zakup</title>" in page
    for tab in ("Уведомления", "Аккаунт", "Искусственный интеллект", "Организация", "Пользователи"):
        assert 'role="tab" data-tab' in page and tab in page
    assert 'value="claude-sonnet-5-5"' in page
    monkeypatch.setenv("ZAKUP_SECRET_KEY", Fernet.generate_key().decode())
    assert "Модель для выжимки и вопросов поставщику" in page
    saved = client.post("/settings/ai", data={"provider": "anthropic", "api_key": "sk-ant-x",
                                               "model": "claude-opus-5-5",
                                               "model_light": "claude-sonnet-5-5"},
                        headers=ORIGIN, follow_redirects=False)
    assert saved.headers["location"] == "/settings?saved=ai#ai"
    assert conn.execute("SELECT ai_provider, ai_model, ai_model_light FROM users WHERE role = 'admin'"
                        ).fetchone() == ("anthropic", "claude-opus-5-5", "claude-sonnet-5-5")
    # Поиск — своя модель, выжимка и вопросы поставщику — своя.
    assert web.research.light_model(conn) == ("anthropic", "claude-sonnet-5-5")
    monkeypatch.setattr(web.research, "provider_models",
                        lambda provider, key: [("claude-x", "Claude X")] if key == "sk-ant-x" else [])
    assert client.get("/settings/ai/models?provider=anthropic").json() == [["claude-x", "Claude X"]]
    client.post("/settings/ai", data={"provider": "claude_code", "model": "ignored"}, headers=ORIGIN)
    assert conn.execute("SELECT ai_model FROM users WHERE role = 'admin'").fetchone()[0] is None
    assert web.research.light_model(conn) == ("claude_code", "haiku")


def test_admin_manages_roles_disable_delete(client, conn):
    _login(client)
    auth.set_password(conn, "serik", "serik-password-1")
    serik = conn.execute("SELECT id FROM users WHERE email = 'serik'").fetchone()[0]
    me = conn.execute("SELECT id FROM users WHERE email = 'owner@mail.kz'").fetchone()[0]
    assert "роль — администратор" in client.post(
        f"/settings/users/{serik}/role", data={"role": "admin"}, headers=ORIGIN).text
    assert client.post(f"/settings/users/{me}/role", data={"role": "user"}, headers=ORIGIN).status_code == 422
    assert "отключён" in client.post(f"/settings/users/{serik}/disable", headers=ORIGIN).text
    assert auth.login(conn, "serik", "serik-password-1", None, None) is None
    client.post(f"/settings/users/{serik}/disable", headers=ORIGIN)
    assert auth.login(conn, "serik", "serik-password-1", None, None) is not None
    conn.execute("INSERT INTO subscriptions (user_id, name, query) VALUES (%s, 'Столы', 'стол')", (serik,))
    conn.commit()
    assert "удалён" in client.post(f"/settings/users/{serik}/delete", headers=ORIGIN).text
    # Мягкое удаление: запись есть, войти нельзя, подписки выключены.
    assert conn.execute("SELECT deleted_at IS NOT NULL, deleted_by FROM users WHERE id = %s",
                        (serik,)).fetchone() == (True, me)
    assert auth.login(conn, "serik", "serik-password-1", None, None) is None
    assert conn.execute("SELECT bool_or(active) FROM subscriptions WHERE user_id = %s",
                        (serik,)).fetchone()[0] is False
    page = client.get("/settings").text
    assert "Удалённые" in page and f"/settings/users/{serik}/restore" in page
    assert "разрешена" in client.post(f"/settings/users/{serik}/reregister", headers=ORIGIN).text
    assert "восстановлен" in client.post(f"/settings/users/{serik}/restore", headers=ORIGIN).text
    assert auth.login(conn, "serik", "serik-password-1", None, None) is not None


def test_moderator_manages_only_users_and_sees_log_without_admin(client, conn):
    auth.set_password(conn, "amir", "amir-password-1")
    auth.set_password(conn, "client", "client-password-1")
    conn.execute("UPDATE users SET role = 'moderator' WHERE email = 'amir'")
    conn.commit()
    ids = dict(conn.execute("SELECT email, id FROM users").fetchall())
    _login(client)  # администратор оставляет след в журнале
    client.post("/logout", headers=ORIGIN)
    client.post("/login", data={"email": "owner@mail.kz", "password": "wrong-password"}, headers=ORIGIN)
    client.post("/login", data={"email": "client", "password": "client-password-1"}, headers=ORIGIN)
    client.get("/my")
    client.post("/logout", headers=ORIGIN)
    client.post("/login", data={"email": "amir", "password": "amir-password-1"}, headers=ORIGIN)
    page = client.get("/settings").text
    assert "Пользователи" in page and "Искусственный интеллект" not in page
    assert "owner@mail.kz" not in page and "client" in page
    log = client.get("/log").text
    assert "client" in log and "owner@mail.kz" not in log  # и неудачный вход админа не виден
    assert client.post(f"/settings/users/{ids['owner@mail.kz']}/reset", headers=ORIGIN).status_code == 403
    assert client.post(f"/settings/users/{ids['client']}/role", data={"role": "admin"},
                       headers=ORIGIN).status_code == 403
    # Сброс пароля клиенту — только администратор (иначе модератор входит под клиентом).
    assert client.post(f"/settings/users/{ids['client']}/reset", headers=ORIGIN).status_code == 403
    assert "удалён" in client.post(f"/settings/users/{ids['client']}/delete", headers=ORIGIN).text
    assert client.post(f"/settings/users/{ids['client']}/restore", headers=ORIGIN).status_code == 403
    assert client.post("/settings/ai", data={"provider": "claude_code"}, headers=ORIGIN).status_code == 403


def test_proxy_settings_hide_password(client, conn, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("ZAKUP_SECRET_KEY", Fernet.generate_key().decode())
    _login(client)
    assert client.post("/settings/proxy", data={"proxy": "81.1.2.3:12323"}, headers=ORIGIN).status_code == 422
    saved = client.post("/settings/proxy", data={"proxy": "http://user:topsecret@81.1.2.3:12323"},
                        headers=ORIGIN, follow_redirects=False)
    assert saved.headers["location"] == "/settings?saved=proxy#ai"
    page = client.get("/settings").text
    assert "81.1.2.3:12323" in page and "topsecret" not in page
    monkeypatch.setattr(web.kaspi, "search", lambda query, proxy, *a, **k: [{"name": "x"}])
    assert "Прокси работает" in client.post("/settings/proxy", data={"action": "test"}, headers=ORIGIN).text
    client.post("/settings/proxy", data={"action": "delete"}, headers=ORIGIN)
    assert "Прокси не задан" in client.get("/settings").text


def test_admin_logs_in_as_user_and_returns(client, conn):
    auth.set_password(conn, "client", "client-password-1")
    auth.set_password(conn, "amir", "amir-password-1")
    conn.execute("UPDATE users SET role = 'moderator' WHERE email = 'amir'")
    conn.commit()
    ids = dict(conn.execute("SELECT email, id FROM users").fetchall())
    _login(client)
    assert "Войти как" in client.get("/settings").text
    go = client.post(f"/settings/users/{ids['client']}/impersonate", headers=ORIGIN, follow_redirects=False)
    assert go.status_code == 303 and "zakup_return" in go.headers.get("set-cookie", "")
    page = client.get("/pricing").text
    assert "Вы вошли как" in page and "<b>client</b>" in page
    assert client.get("/settings").status_code == 200 and "Пользователи" not in client.get("/settings").text
    back = client.post("/impersonate/stop", headers=ORIGIN, follow_redirects=False)
    assert back.headers["location"] == "/settings#users"
    page = client.get("/settings").text
    assert "Пользователи" in page and "Вы вошли как" not in page
    # Модератор так не может, а администратор не входит под другим администратором.
    client.post("/logout", headers=ORIGIN)
    client.post("/login", data={"email": "amir", "password": "amir-password-1"}, headers=ORIGIN)
    assert client.post(f"/settings/users/{ids['client']}/impersonate", headers=ORIGIN).status_code == 403


def test_guest_settings_only_interface_and_header_order(client):
    page = client.get("/settings")
    assert page.status_code == 200 and "Интерфейс" in page.text and "Высокий контраст" in page.text
    assert "Уведомления в Telegram" not in page.text and "Мой пароль" not in page.text
    header = page.text.split("<header>", 1)[1].split("</header>", 1)[0]
    assert header.index(">Регистрация<") < header.index(">Вход<") < header.index("data-burger")
    assert 'href="/settings"' in header


def test_client_settings_and_group_button(client, conn, monkeypatch):
    from tests.test_client_bot import _calls
    from zakup import telegram

    _calls(monkeypatch)
    monkeypatch.setattr(telegram, "bot_username", lambda which="main": "tenderim_kz_bot" if which == "client" else "zakup_bot")
    monkeypatch.setattr(telegram, "token", lambda which="main": "t")
    uid = auth.set_password(conn, "+77011112233", "client-password-1")
    conn.execute("UPDATE users SET role = 'user', paid_until = now() + interval '30 days', telegram_chat_id = 'c:555' WHERE id = %s", (uid,))
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('client_group', '-1009')")
    conn.commit()
    origin = {"origin": "https://testserver"}
    client.post("/login", data={"email": "87011112233", "password": "client-password-1"}, headers=origin)
    page = client.get("/settings").text
    assert "Вступить в группу клиентов" in page and "@tenderim_kz_bot" in page
    go = client.post("/settings/client-group", headers=origin, follow_redirects=False)
    assert go.headers["location"] == "https://t.me/+join"
    link = client.post("/settings/telegram/link", headers=origin, follow_redirects=False)
    assert link.headers["location"].startswith("https://t.me/tenderim_kz_bot?start=")
    assert web._user_groups(conn, uid) == []


def test_moderator_cannot_reset_client_password(client, conn):
    auth.set_password(conn, "client", "client-password-1")
    auth.set_password(conn, "amir", "amir-password-1")
    conn.execute("UPDATE users SET role = 'moderator' WHERE email = 'amir'")
    conn.commit()
    uid = conn.execute("SELECT id FROM users WHERE email = 'client'").fetchone()[0]
    client.post("/login", data={"email": "amir", "password": "amir-password-1"}, headers=ORIGIN)
    assert client.post(f"/settings/users/{uid}/reset", headers=ORIGIN).status_code == 403
