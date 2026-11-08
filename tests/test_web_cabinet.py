"""Кабинет клиента: избранное, подписки, лента, доступ к платному."""



from tests.web_helpers import ORIGIN, _announcement_with_lots, _login
from zakup import auth, db


def test_favorite_heart_on_search_and_my_lots(client, conn):
    db.upsert_announcement(conn, {"id": 78, "name_ru": "Стулья", "end_date": "2099-01-01 09:00:00"})
    db.upsert_lot(conn, {"id": 6, "trd_buy_id": 78, "name_ru": "Стул офисный", "amount": 500000})
    conn.commit()
    _login(client)
    search = "/lots?applied=1&deadline_mode=&advance_min=0"
    page = client.get(search).text
    assert "Стул офисный" in page and 'name="status" value="interested"' in page and "В избранное" in page
    response = client.post("/lots/6/mark", data={"status": "interested", "next": search},
                           headers=ORIGIN, follow_redirects=False)
    assert response.headers["location"] == search + "#lot-6"
    assert 'aria-pressed="true"' in client.get(search).text
    my = client.get("/my").text
    assert "Стул офисный" in my and "В избранном" in my
    client.post("/lots/6/mark", data={"status": ""}, headers=ORIGIN)
    assert "Избранных лотов пока нет" in client.get("/my").text
    assert client.post("/lots/6/mark", data={"status": "won"}, headers=ORIGIN).status_code == 422
    assert client.post("/lots/999/mark", data={"status": "interested"}, headers=ORIGIN).status_code == 404


def test_subscribe_only_without_fixed_dates_and_matches_filter(client, conn):
    _login(client)
    base = "/lots?applied=1&deadline_mode=rule"
    assert "Подписаться на этот поиск" in client.get(base).text
    fixed = client.get("/lots?applied=1&deadline_mode=date&deadline_date=2026-11-20").text
    assert "Подписаться на этот поиск" not in fixed and "нет конкретных дат" in fixed
    data = {"applied": "1", "deadline_mode": "rule", "region": "75", "scope": "announcements",
            "name": "Алматы"}
    assert client.post("/subscriptions/filter", data=data, headers=ORIGIN).status_code == 200
    page = client.get("/subscriptions").text
    assert "Алматы" in page and "объявления целиком" in page and "/announcements?" in page
    data["deadline_date"] = "2026-11-20"
    assert client.post("/subscriptions/filter", data=data, headers=ORIGIN).status_code == 422
    data.pop("deadline_date")
    data["chat_id"] = "-100"
    assert client.post("/subscriptions/filter", data=data, headers=ORIGIN).status_code == 403
    sub = conn.execute("SELECT id FROM subscriptions").fetchone()[0]
    matches = client.get(f"/matches?sub={sub}").text
    assert f'value="{sub}" checked' in matches
    assert f'value="{sub}" checked' in client.get("/matches").text  # первая по умолчанию
    assert "Отметьте подписки" in client.get("/matches?filtered=1").text


def test_users_can_send_own_subscriptions_to_shared_group(client, conn):
    conn.execute("INSERT INTO telegram_groups (chat_id, title) VALUES ('-100', 'ZAKUP GROUP')")
    auth.set_password(conn, "amir", "amir-password-1")
    conn.execute("UPDATE users SET role = 'moderator' WHERE email = 'amir'")  # группы — для сотрудников
    conn.commit()
    client.post("/login", data={"email": "amir", "password": "amir-password-1"}, headers=ORIGIN)
    data = {"applied": "1", "deadline_mode": "rule", "scope": "lots", "chat_id": "-100", "name": "В группу"}
    assert client.post("/subscriptions/filter", data=data, headers=ORIGIN).status_code == 200
    data.update(chat_id="", name="Лично мне")
    client.post("/subscriptions/filter", data=data, headers=ORIGIN)
    rows = conn.execute("SELECT name, chat_id FROM subscriptions ORDER BY id").fetchall()
    assert rows == [("В группу", "-100"), ("Лично мне", None)]
    assert "ZAKUP GROUP" in client.get("/subscriptions").text


def test_edit_subscription_conditions_and_name(client, conn):
    _login(client)
    data = {"applied": "1", "deadline_mode": "rule", "scope": "lots", "region": "75", "name": "Алматы"}
    client.post("/subscriptions/filter", data=data, headers=ORIGIN)
    sub = conn.execute("SELECT id FROM subscriptions").fetchone()[0]
    assert f"/subscriptions/{sub}/edit" in client.get("/subscriptions").text
    edit = client.get(f"/subscriptions/{sub}/edit", follow_redirects=False).headers["location"]
    assert edit.startswith("/lots?") and f"edit={sub}" in edit and "region=75" in edit
    page = client.get(edit).text
    assert "Редактирование подписки" in page and 'value="Алматы"' in page and f"/subscriptions/{sub}/update" in page
    data.update(region="71", name="Астана")
    assert client.post(f"/subscriptions/{sub}/update", data=data, headers=ORIGIN,
                       follow_redirects=False).status_code == 303
    name, filters = conn.execute("SELECT name, filters FROM subscriptions WHERE id = %s", (sub,)).fetchone()
    assert name == "Астана" and filters["regions"] == ["71"]
    auth.set_password(conn, "other", "other-password-1")
    conn.execute("UPDATE users SET role = 'moderator' WHERE email = 'other'")
    conn.commit()
    client.post("/logout", headers=ORIGIN)
    client.post("/login", data={"email": "other", "password": "other-password-1"}, headers=ORIGIN)
    assert client.post(f"/subscriptions/{sub}/update", data=data, headers=ORIGIN).status_code == 404


def test_unpaid_client_gets_paywall_until_access_granted(client, conn):
    _announcement_with_lots(conn)
    auth.set_password(conn, "+77011112233", "client-password-1")
    conn.execute("UPDATE users SET role = 'user', phone = email WHERE email = '+77011112233'")
    conn.commit()
    client.post("/login", data={"email": "87011112233", "password": "client-password-1"}, headers=ORIGIN)
    assert client.post("/lots/5001/mark", data={"status": "interested"}, headers=ORIGIN).status_code == 402
    assert client.get("/my", follow_redirects=False).headers["location"] == "/pricing"
    # Клиенту по умолчанию — все открытые закупки, а не фильтр владельца.
    page = client.get("/lots").text
    assert "Бумага А4" in page
    client.post("/logout", headers=ORIGIN)
    _login(client)
    client_id = conn.execute("SELECT id FROM users WHERE email = '+77011112233'").fetchone()[0]
    assert "доступ к платному до" in client.post(f"/settings/users/{client_id}/access",
                                                 data={"days": "30"}, headers=ORIGIN).text
    client.post("/logout", headers=ORIGIN)
    client.post("/login", data={"email": "87011112233", "password": "client-password-1"}, headers=ORIGIN)
    assert client.post("/lots/5001/mark", data={"status": "interested"}, headers=ORIGIN,
                       follow_redirects=False).status_code == 303
