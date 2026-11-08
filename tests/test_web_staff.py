"""Журнал действий, дашборд, статистика посещений."""



from tests.web_helpers import ORIGIN, _announcement_with_lots, _login
from zakup import auth, web


def test_action_log(client, conn):
    _announcement_with_lots(conn)
    auth.set_password(conn, "amir", "amir-password-1")
    conn.execute("UPDATE users SET paid_until = now() + interval '30 days' WHERE email = 'amir'")
    conn.commit()
    client.post("/login", data={"email": "amir", "password": "wrong-password"}, headers=ORIGIN)
    client.post("/login", data={"email": "amir", "password": "amir-password-1"}, headers=ORIGIN)
    client.get("/lots/5001")
    client.get("/lots/5001")  # повтор в течение 10 минут не пишется
    client.get("/my")
    client.get("/lots/5001/research")  # фрагменты и опросы — не страницы, не пишем
    client.post("/lots/5001/mark", data={"status": "interested"}, headers=ORIGIN)
    client.post("/logout", headers=ORIGIN)
    assert client.get("/log").status_code in (303, 200)
    _login(client)
    assert client.get("/log").status_code == 200
    amir = conn.execute("SELECT id FROM users WHERE email = 'amir'").fetchone()[0]
    page = client.get(f"/log?user={amir}").text
    assert page.count(">Открыл лот</td>") == 1 and ">Отметка лота</td>" in page
    assert "status: interested" in page
    assert page.count(">Открыл страницу</td>") == 2 and ">/my<" in page  # главная после входа и /my
    assert "Неудачный вход" in client.get("/log?action=login_failed").text
    conn.execute("UPDATE user_actions SET at = now() - interval '31 days' WHERE action = 'view_lot'")
    conn.commit()
    from zakup import audit
    assert audit.purge(conn) == 1
    client.post("/logout", headers=ORIGIN)
    client.post("/login", data={"email": "amir", "password": "amir-password-1"}, headers=ORIGIN)
    assert client.get("/log").status_code == 403


def test_staff_dashboard_and_market_trends(client, conn):
    auth.set_password(conn, "amir", "amir-password-1")
    auth.set_password(conn, "client", "client-password-1")
    conn.execute("UPDATE users SET role = 'moderator' WHERE email = 'amir'")
    conn.execute("UPDATE users SET role = 'user', paid_until = now() + interval '30 days' WHERE email = 'client'")
    conn.commit()
    # Сотрудник после входа попадает на дашборд; логотип ведёт туда же.
    login = client.post("/login", data={"email": "amir", "password": "amir-password-1"}, headers=ORIGIN,
                        follow_redirects=False)
    assert login.headers["location"] == "/dashboard"
    page = client.get("/dashboard").text
    assert "Дашборд платформы" in page and 'href="/dashboard" class="brand"' in page
    for section in web.insights.SECTIONS:
        for days in (7, 365):
            assert client.get(f"/dashboard/data?section={section}&days={days}").status_code == 200
    assert client.get("/dashboard/data?section=nope").status_code == 404
    assert 'data-tab="overview"' not in client.get("/settings").text  # «Обзор» из настроек переехал
    client.post("/logout", headers=ORIGIN)
    # Клиенту дашборд не положен; динамика рынка открыта всем.
    client.post("/login", data={"email": "client", "password": "client-password-1"}, headers=ORIGIN)
    assert client.get("/dashboard", follow_redirects=False).status_code in (303, 403)
    assert client.get("/dashboard/data").status_code == 403
    data = client.get("/analytics/trends?days=90").json()
    assert data["days"] == 90 and len(data["labels"]) == len(data["series"]["lots"]) and len(data["hours"]) == 24


def test_visits_and_events_are_recorded(client, conn, monkeypatch):
    import time as _time

    from zakup import visits

    conn.execute("INSERT INTO geo_kz VALUES ('2.27.130.0', '2.27.131.255', 'г. Астана', 'Астана')")
    conn.commit()
    # Записи идут в фоне — в тесте пишем сразу.
    monkeypatch.setattr(web, "_spawn", lambda job: job())
    browser = {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Mobile/15E148 Safari/604.1",
               "referer": "https://www.google.com/", "sec-fetch-dest": "document", "x-forwarded-for": "2.27.130.7"}
    first = client.get("/analytics?utm_source=tg&utm_campaign=launch", headers=browser)
    assert visits.COOKIE in first.cookies
    client.get("/lots/1/research", headers={**browser, "sec-fetch-dest": "empty"})  # фрагмент — не посещение
    client.get("/robots.txt", headers={"user-agent": "Googlebot/2.1"})
    row = conn.execute("SELECT path, source, utm, device, browser, is_bot, visitor FROM visits").fetchall()
    assert len(row) == 1 and row[0][:6] == ("/analytics", "google", "tg / launch", "mobile", "Safari", False)
    assert visits.first_seen(row[0][6]) and abs(visits.first_seen(row[0][6]) - _time.time()) < 60
    assert visits.source("https://t.me/x", "zakup.sarsembai.com") == "telegram"
    assert visits.source("https://zakup.sarsembai.com/lots", "zakup.sarsembai.com") == "internal"
    assert visits.is_bot("Mozilla/5.0 (compatible; YandexBot/3.0)") and visits.is_bot(None)
    assert client.post("/t/event", json={"name": "paywall", "props": {"feature": "export"}},
                       headers=ORIGIN).status_code == 204
    assert client.post("/t/event", json={"name": "hack"}, headers=ORIGIN).status_code == 204
    assert client.post("/t/event", json={"name": "paywall"}).status_code == 403  # чужой сайт
    client.get("/lots?q=стол&applied=1", headers=browser)
    names = [r[0] for r in conn.execute("SELECT name FROM events ORDER BY id").fetchall()]
    assert names == ["paywall", "search"]
