import os
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from zakup import auth, db, details, web

ORIGIN = {"origin": "https://testserver"}


@pytest.fixture
def client(conn, monkeypatch):
    # Не conn.info.dsn: в нём нет пароля.
    test_pool = ConnectionPool(
        os.environ["TEST_DATABASE_URL"], min_size=1, max_size=2, open=False, timeout=5
    )
    monkeypatch.setattr(web, "pool", test_pool)
    # Карточки организаций догружаются с портала фоном — в тестах без сети.
    monkeypatch.setattr(web, "_subjects_in_background", lambda bins: None)
    # Тариф кэшируется в памяти процесса — между тестами сбрасываем.
    monkeypatch.setattr(web.tariff, "_cache", None)
    auth.set_password(conn, "owner@mail.kz", "long-enough-password")
    conn.execute("UPDATE users SET role = 'admin' WHERE email = 'owner@mail.kz'")
    conn.commit()
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


def test_announcement_page_shows_lots_and_loads_spec(client, conn, monkeypatch):
    db.upsert_announcement(conn, {"id": 77, "name_ru": "Столик", "end_date": "2099-01-01 09:00:00"})
    details.save(conn, {
        "id": 77, "numberAnno": "77-1", "nameRu": "Приобретение столика", "totalSum": 689655.16,
        "endDate": "2099-01-01 09:00:00", "disablePersonId": 1,
        "RefBuyStatus": {"nameRu": "Опубликовано (прием ценовых предложений)"},
        "Organizer": {"bin": "870240000026", "nameRu": "Школа №5",
                      "Address": [{"address": "г. Конаев, мкр 5"}]},
        "Files": [{"id": 1, "nameRu": "Проект договора", "filePath": "https://ows.goszakup.gov.kz/download/1"}],
        "Lots": [{"id": 5, "trdBuyId": 77, "lotNumber": "88448423-ЗЦП1", "nameRu": "Стол",
                  "amount": 689655.16, "count": 4,
                  "Files": [{"id": 2, "nameRu": "Техническая спецификация",
                             "filePath": "https://ows.goszakup.gov.kz/download/2"}],
                  "Plans": [{"prepayment": 30, "price": 172413.79, "RefUnits": {"nameRu": "Штука"},
                             "PlansKato": [{"refKatoCode": "191010000",
                                            "fullDeliveryPlaceNameRu": "г.Қонаев, мкр.5"}]}]}],
    })
    conn.commit()
    _login(client)
    page = client.get("/a/77")
    assert page.status_code == 200
    for text in ("Приобретение столика", "88448423-ЗЦП1", "аванс 30%", "г.Қонаев, мкр.5",
                 "Проект договора", "Техническая спецификация", "data-end="):
        assert text in page.text
    monkeypatch.setattr(web.spec, "get", lambda conn, lot_id, url: (None, "портал недоступен"))
    assert "Обновить" in client.get("/a/77/lots/5/spec").text
    monkeypatch.setattr(web.spec, "get", lambda conn, lot_id, url: ("Столик, дерево", None))
    fragment = client.get("/a/77/lots/5/spec?refresh=1")
    assert "Столик, дерево" in fragment.text
    assert client.get("/a/404404").status_code == 404


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

def test_finished_announcement_shows_winners_and_contracts(client, conn, monkeypatch):
    from tests.test_results import PROTOCOL, FakeClient, _announcement

    _announcement(conn)
    conn.commit()
    monkeypatch.setattr(web.results.spec, "download", lambda url: b"pdf")
    monkeypatch.setattr(web.results, "protocol_text", lambda pdf: PROTOCOL)
    monkeypatch.setattr(web, "_goszakup", lambda: FakeClient())
    # Подробности свежие: перечитывать объявление не нужно, только итоги.
    conn.execute("UPDATE announcements SET details_fetched_at = now()")
    conn.commit()
    _login(client)
    # Итоги перечитываются в фоне после ответа: видны со следующего открытия.
    client.get("/a/7")
    page = client.get("/a/7").text
    for text in ("Победители (2)", "Договоры (1)", 'объединение &#34;ОКА&#34;', "37 950.00",
                 "C-1", "отсутствием представленных ценовых предложений"):
        assert text in page, text


def test_search_finds_finished_lots_with_winner(client, conn, monkeypatch):
    from tests.test_results import PROTOCOL, _announcement

    _announcement(conn)
    conn.execute("UPDATE announcements SET status_id = 350, disable_person = false")
    conn.execute("UPDATE lots SET status_id = 360 WHERE id = 70")
    monkeypatch.setattr(web.results.spec, "download", lambda url: b"pdf")
    monkeypatch.setattr(web.results, "protocol_text", lambda pdf: PROTOCOL)
    detail = conn.execute("SELECT detail FROM announcements WHERE id = 7").fetchone()[0]
    web.results.parse_announcement(conn, 7, detail)
    conn.commit()
    _login(client)
    base = "/lots?applied=1&deadline_mode="
    assert "Весы" not in client.get(base + "&status=240").text
    page = client.get(base + "&stage=done").text
    assert "Весы" in page and "🏁" in page and "37 950.00" in page and "−15.7%" in page
    # Разобранный протокол второй раз не качается.
    monkeypatch.setattr(web.results.spec, "download", lambda url: 1 / 0)
    assert web.results.parse_pending(conn, 10) == 0


def test_plain_login_without_email_works(client, conn):
    auth.set_password(conn, "Friend1", "another-long-password")
    response = client.post(
        "/login",
        data={"email": " friend1 ", "password": "another-long-password", "next": "/"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303 and "zakup_session" in response.cookies


def test_announcements_page_lot_page_and_portal_fields(client, conn):
    _announcement_with_lots(conn)
    _login(client)
    base = "/announcements?applied=1&deadline_mode="
    page = client.get(base + "&q=канцтовары&number=500-1").text
    assert "Канцтовары для школы" in page and "Лотов: 2" in page and "Организатор: Школа" in page
    assert "Канцтовары для школы" not in client.get(base + "&q=мебель").text
    assert "Канцтовары для школы" not in client.get(base + "&min_sum=2000000").text
    lots = client.get("/lots?applied=1&deadline_mode=&exclude=ручка&year=2026").text
    assert "Бумага А4" in lots and "Ручка шариковая" not in lots
    assert "Бумага А4" not in client.get("/lots?applied=1&deadline_mode=&year=2025").text
    assert "Бумага А4" in client.get("/lots?applied=1&deadline_mode=&enstru=1712").text
    lot = client.get("/lots/5001").text
    assert "Бумага А4" in lot and "/a/500" in lot and "Другие лоты этого объявления (1)" in lot
    assert "/lots/5002" in lot
    workbook = client.get("/export.xlsx?applied=1&deadline_mode=")
    assert workbook.status_code == 200 and workbook.content[:2] == b"PK"
    assert client.get("/lots/999999").status_code == 404


def _announcement_with_lots(conn):
    db.upsert_announcement(conn, {"id": 500, "number_anno": "500-1", "name_ru": "Канцтовары для школы",
                                  "total_sum": 1500000, "org_name_ru": "Школа №1",
                                  "ref_buy_status_id": 240, "end_date": "2099-01-01 09:00:00"})
    details.save(conn, {
        "id": 500, "finYear": [2026, 2027], "endDate": "2099-01-01 09:00:00",
        "Lots": [
            {"id": 5001, "trdBuyId": 500, "lotNumber": "L-1", "nameRu": "Бумага А4", "amount": 1000000,
             "count": 100, "Plans": [{"price": 10000, "refEnstruCode": "171211.100.000000"}]},
            {"id": 5002, "trdBuyId": 500, "lotNumber": "L-2", "nameRu": "Ручка шариковая",
             "amount": 500000, "count": 1000, "Plans": [{"price": 500}]},
        ],
    })
    conn.execute("UPDATE lots SET status_id = 240")
    conn.commit()


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
    assert 'value="%d" checked' % sub in matches
    assert 'value="%d" checked' % sub in client.get("/matches").text  # первая по умолчанию
    assert "Отметьте подписки" in client.get("/matches?filtered=1").text


def test_lot_page_research_section_and_start(client, conn, monkeypatch):
    from tests.test_research import _lot

    _lot(conn)
    _login(client)
    page = client.get("/lots/90").text
    assert "Поиск товаров с ИИ" in page and "Актюбинская область, г.Актобе" in page
    response = client.post("/lots/90/research", data={"region": "г.Актобе", "whole_kz": "1"},
                           headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert "В очереди" in client.get("/lots/90/research").text
    conn.execute("""UPDATE lot_research SET status = 'done', finished_at = now(),
        result = '{"summary": "s", "region_searched": "Актобе", "items": [{"name": "Стол дуб",
        "seller": "Мебель", "city": "Актобе", "in_region": true, "price": 80000, "unit_price": 80000,
        "url": "https://m.kz/1", "image": null, "fit": "полностью"}]}'""")
    conn.commit()
    fragment = client.get("/lots/90/research").text
    assert "Стол дуб" in fragment and "20 000.00 ₸" in fragment and "20.0%" in fragment
    assert "80 000.00" in fragment  # маржа на лот: 20 000 × 4
    monkeypatch.setenv("ZAKUP_SECRET_KEY", "")
    bad = client.post("/settings/ai", data={"provider": "openai"}, headers=ORIGIN)
    assert bad.status_code == 422


def test_links_keep_default_filter(client):
    _login(client)
    page = client.get("/lots").text
    # Ссылка на поиск объявлений несёт значения по умолчанию, а не пустой фильтр.
    link = page.split('href="/announcements?', 1)[1].split('"', 1)[0].replace("&amp;", "&")
    assert "status=240" in link and "advance_min=30" in link and "deadline_mode=rule" in link
    announcements = client.get("/announcements?" + link).text
    assert 'value="240" checked' in announcements and 'value="75" checked' in announcements


def test_logo_and_share_buttons(client, conn):
    assert client.get("/static/logo-mark.png").status_code == 200
    db.upsert_announcement(conn, {"id": 501, "name_ru": "Стулья", "end_date": "2099-01-01 09:00:00"})
    details.save(conn, {"id": 501, "nameRu": "Стулья", "endDate": "2099-01-01 09:00:00",
                        "Lots": [{"id": 5011, "trdBuyId": 501, "nameRu": "Стул", "amount": 1}]})
    conn.commit()
    _login(client)
    page = client.get("/a/501").text
    assert 'data-share="https://zakup.sarsembai.com/a/501"' in page
    assert 'data-share="https://goszakup.gov.kz/ru/announce/index/501"' in page
    assert 'data-share="https://zakup.sarsembai.com/lots/5011"' in client.get("/lots/5011").text


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
    assert client.get("/settings", follow_redirects=False).status_code == 303  # сессия сброшена
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
        assert f'role="tab" data-tab' in page and tab in page
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


def test_announcements_advance_column_groups_values(client, conn):
    _announcement_with_lots(conn)
    conn.execute("UPDATE lots SET prepayment = 30")
    db.upsert_lot(conn, {"id": 5003, "trd_buy_id": 500, "name_ru": "Скрепки", "amount": 1})
    conn.execute("UPDATE lots SET prepayment = 100 WHERE id = 5003")
    conn.commit()
    _login(client)
    page = client.get("/announcements?applied=1&deadline_mode=").text
    cell = page.split('class="advances">', 1)[1].split("</td>", 1)[0]
    assert cell.count("30%") == 1 and cell.count("100%") == 1


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
    assert "Новый пароль для client" in client.post(f"/settings/users/{ids['client']}/reset",
                                                     headers=ORIGIN).text
    assert "удалён" in client.post(f"/settings/users/{ids['client']}/delete", headers=ORIGIN).text
    assert client.post(f"/settings/users/{ids['client']}/restore", headers=ORIGIN).status_code == 403
    assert client.post("/settings/ai", data={"provider": "claude_code"}, headers=ORIGIN).status_code == 403


def test_spec_shown_from_database_without_portal(client, conn, monkeypatch):
    _announcement_with_lots(conn)
    conn.execute("""UPDATE announcements SET detail = jsonb_set(detail, '{Lots,0,Files}',
        '[{"id": 9, "nameRu": "Техническая спецификация", "filePath": "https://ows.goszakup.gov.kz/download/9"}]')""")
    conn.execute("INSERT INTO lot_specs (lot_id, file_url, text) VALUES (5001, 'https://ows.goszakup.gov.kz/download/9', 'Бумага офисная А4, 80 г/м2')")
    conn.commit()
    monkeypatch.setattr(web.details, "fetch_one", lambda *a: (_ for _ in ()).throw(AssertionError("portal")))
    _login(client)
    page = client.get("/lots/5001").text
    assert "Бумага офисная А4, 80 г/м2" in page and 'data-loaded="1"' in page


def test_pagination_and_per_page(client, conn):
    db.upsert_announcement(conn, {"id": 600, "name_ru": "Много лотов", "end_date": "2099-01-01 09:00:00"})
    for i in range(25):
        db.upsert_lot(conn, {"id": 6000 + i, "trd_buy_id": 600, "name_ru": f"Лот {i}", "amount": 1})
    conn.commit()
    _login(client)
    page = client.get("/lots?applied=1&deadline_mode=&per_page=20").text
    assert 'aria-current="page">1<' in page and "page=2" in page and "per_page=20" in page
    second = client.get("/lots?applied=1&deadline_mode=&per_page=20&page=2").text
    assert second.count('href="/lots/60') == 5
    assert 'value="100"' in page and "Показывать по" in page


def test_dashboard_is_home(client, conn):
    _announcement_with_lots(conn)
    page = client.get("/analytics")  # без входа: аналитика открыта
    assert page.status_code == 200
    for text in ("Обзор рынка", "Новые объявления по дням", "По дням недели", "Регионы", "Способы закупки",
                 "Самые дорогие открытые лоты", "Чаще всех побеждают", "Самые частые закупки"):
        assert text in page.text, text
    # Только общая аналитика, без личных блоков.
    for text in ("лотов под ваш фильтр", "Мои подписки", "Сроки по вашему фильтру"):
        assert text not in page.text, text
    # Главная — поиск объявлений; старые ссылки /?applied=1&… работают там же.
    assert client.get("/?applied=1", follow_redirects=False).status_code == 200


def test_kazakh_interface_and_data(client, conn):
    db.upsert_announcement(conn, {"id": 700, "name_ru": "Столы", "end_date": "2099-01-01 09:00:00"})
    details.save(conn, {"id": 700, "nameRu": "Приобретение столов", "nameKz": "Үстелдер сатып алу",
                        "endDate": "2099-01-01 09:00:00",
                        "RefBuyStatus": {"nameRu": "Опубликовано", "nameKz": "Жарияланды"},
                        "Lots": [{"id": 7001, "trdBuyId": 700, "nameRu": "Стол", "nameKz": "Үстел",
                                  "amount": 1, "count": 1,
                                  "Plans": [{"price": 1, "RefUnits": {"nameRu": "Штука", "nameKz": "Дана"}}]}]})
    conn.commit()
    _login(client)
    assert "Хабарландыру" not in client.get("/a/700").text
    switched = client.get("/lang/kk", headers={"referer": "https://testserver/a/700"}, follow_redirects=False)
    assert switched.headers["location"] == "/a/700" and "zakup_lang=kk" in switched.headers["set-cookie"]
    page = client.get("/a/700").text
    assert "Үстелдер сатып алу" in page and "Үстел" in page and "Жарияланды" in page
    assert "Лоттар (" in page and 'lang="kk"' in page and "дана" in page
    search = client.get("/lots").text
    assert "Лоттарды іздеу" in search and "Мәртебе" in search
    client.get("/lang/ru")
    assert "Поиск лотов" in client.get("/lots").text


def test_users_can_send_own_subscriptions_to_shared_group(client, conn):
    conn.execute("INSERT INTO telegram_groups (chat_id, title) VALUES ('-100', 'ZAKUP GROUP')")
    auth.set_password(conn, "amir", "amir-password-1")
    conn.execute("UPDATE users SET paid_until = now() + interval '30 days' WHERE email = 'amir'")
    conn.commit()
    client.post("/login", data={"email": "amir", "password": "amir-password-1"}, headers=ORIGIN)
    data = {"applied": "1", "deadline_mode": "rule", "scope": "lots", "chat_id": "-100", "name": "В группу"}
    assert client.post("/subscriptions/filter", data=data, headers=ORIGIN).status_code == 200
    data.update(chat_id="", name="Лично мне")
    client.post("/subscriptions/filter", data=data, headers=ORIGIN)
    rows = conn.execute("SELECT name, chat_id FROM subscriptions ORDER BY id").fetchall()
    assert rows == [("В группу", "-100"), ("Лично мне", None)]
    assert "ZAKUP GROUP" in client.get("/subscriptions").text


def test_research_list_page(client, conn):
    from tests.test_research import _lot

    _lot(conn)
    conn.execute("""INSERT INTO lot_research (lot_id, user_id, provider, region, status, finished_at, result)
        SELECT 90, id, 'claude_code', 'г.Актобе', 'done', now(),
        '{"summary": "s", "items": [{"name": "Стол", "url": "https://m.kz/1", "in_region": true,
          "unit_price": 80000, "fit": "полностью"}]}' FROM users LIMIT 1""")
    conn.commit()
    client.post("/login", data={"email": "owner@mail.kz", "password": "long-enough-password"},
                headers=ORIGIN)
    page = client.get("/research").text
    assert "Подбор товаров" in page and "/lots/90#research" in page and "маржа до 20.0%" in page
    assert "/lots/90" not in client.get("/research?q=кровать").text


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


def test_sources_sheet_on_research_page(client, conn):
    _login(client)
    response = client.post("/sources", data={"url": "https://zeta.kz", "name": "Zeta",
                                             "categories": "Мебель"},
                           headers=ORIGIN, follow_redirects=False)
    assert response.headers["location"] == "/research?sources_open=1#sources"
    page = client.get("/research?sources_open=1").text
    assert "Источники для подбора" in page and "zeta.kz" in page and 'class="sheet open"' in page
    assert client.post("/sources", data={"url": "нет"}, headers=ORIGIN).status_code == 422


def test_spec_summary_shown_in_lot_card(client, conn):
    from tests.test_research import _lot

    _lot(conn)
    _login(client)
    conn.execute("""UPDATE announcements SET detail = jsonb_set(detail, '{Lots,0,Files}',
        '[{"id": 9, "nameRu": "Техническая спецификация", "filePath": "https://ows.goszakup.gov.kz/download/9"}]')""")
    conn.execute("UPDATE lot_specs SET file_url = 'https://ows.goszakup.gov.kz/download/9'")
    conn.commit()
    page = client.get("/lots/90").text
    assert "Кратко: главное в таблице" in page
    assert client.post("/lots/90/spec-summary", headers=ORIGIN).json() == {"status": "queued"}
    conn.execute("""UPDATE spec_summaries SET status = 'done', result =
        '{"item": "Стол", "params": [{"name": "Материал", "value": "дуб"}], "requirements": []}'""")
    conn.commit()
    page = client.get("/lots/90").text
    assert "Материал" in page and "дуб" in page


def test_research_list_margin_falls_back_to_partial_fit(client, conn):
    from tests.test_research import _lot

    _lot(conn)
    conn.execute("""INSERT INTO lot_research (lot_id, user_id, provider, region, status, finished_at, result)
        SELECT 90, id, 'claude_code', 'Актобе', 'done', now(),
        '{"items": [{"name": "Стол", "url": "https://m.kz/1", "unit_price": 50000, "fit": "частично"}]}'
        FROM users LIMIT 1""")
    conn.commit()
    _login(client)
    page = client.get("/research").text
    assert "маржа до 50.0%" in page and "по частично подходящим" in page


def test_old_results_stay_visible_while_searching_more(client, conn):
    from tests.test_research import _lot

    _lot(conn)
    conn.execute("""INSERT INTO lot_research (lot_id, user_id, provider, region, status, finished_at, result)
        SELECT 90, id, 'claude_code', 'Актобе', 'done', now(),
        '{"items": [{"name": "Стол дубовый", "url": "https://m.kz/1", "unit_price": 50000, "fit": "полностью", "in_region": true}]}'
        FROM users LIMIT 1""")
    conn.commit()
    _login(client)
    client.post("/lots/90/research", data={"more": "1", "target": "5"}, headers=ORIGIN)
    fragment = client.get("/lots/90/research").text
    assert "Ищу ещё предложения" in fragment and "Стол дубовый" in fragment
    assert "Найти ещё" not in fragment  # пока идёт поиск — кнопки нет
    page = client.get("/research").text
    assert "Сейчас ищем" in page and "ещё предложения" in page


def test_customer_contacts_on_lot_page(client, conn):
    from zakup import subjects

    _announcement_with_lots(conn)
    conn.execute("""UPDATE announcements SET detail = detail || '{"orgBin": "123456789012", "customerBin": "123456789012"}'
                    WHERE id = 500""")
    conn.execute("""UPDATE announcements SET detail = jsonb_set(detail, '{Lots,0,customerBin}', '"123456789012"')
                    WHERE id = 500""")
    conn.execute("""UPDATE announcements SET detail = jsonb_set(detail, '{Lots,0,descriptionRu}',
                    '"Вопросы по тел. 8 (701) 111-22-33 (снабжение)"') WHERE id = 500""")
    conn.commit()
    _login(client)
    page = client.get("/lots/5001").text
    assert "Контакты загружаются" in page and 'href="tel:+77011112233"' in page and "снабжение" in page
    subjects.save(conn, "123456789012", {
        "nameRu": "Школа №1", "phone": "+77273382995", "email": "school@mail.kz", "website": "school.kz",
        "Address": [{"address": "г.Алматы, ул. Абая, 1", "phone": "87017573388"},
                    {"address": "г.Алматы,  ул. Абая, 1", "phone": ""}]})
    page = client.get("/lots/5001").text
    assert 'href="tel:+77273382995"' in page and 'href="https://wa.me/77017573388"' in page
    assert "wa.me/77273382995" not in page  # городской номер — без WhatsApp
    assert "mailto:school@mail.kz" in page and 'href="https://school.kz"' in page
    assert page.count("ул. Абая, 1") == 1
    announcement = client.get("/a/500").text
    assert "mailto:school@mail.kz" in announcement and "Найти официальный сайт" not in announcement


def test_supplier_questions_on_demand_and_shared(client, conn, monkeypatch):
    from zakup import questions, research

    _announcement_with_lots(conn)
    conn.execute("""INSERT INTO lot_research (lot_id, user_id, provider, region, status, finished_at, result)
        SELECT 5001, id, 'claude_code', 'г.Алматы', 'done', now(),
        '{"summary": "s", "items": [{"name": "Бумага Svetocopy", "url": "https://m.kz/p1", "in_region": true,
          "matches": ["формат А4"], "mismatches": ["плотность не указана"]}]}' FROM users LIMIT 1""")
    conn.commit()
    _login(client)
    page = client.get("/lots/5001").text
    assert "Что спросить у поставщика" in page and 'data-questions="https://m.kz/p1"' in page
    assert conn.execute("SELECT count(*) FROM supplier_questions").fetchone()[0] == 0  # сам не запускается
    assert client.post("/lots/5001/questions", data={"url": "https://evil.kz/x"},
                       headers=ORIGIN).status_code == 404
    assert client.post("/lots/5001/questions", data={"url": "https://m.kz/p1"},
                       headers=ORIGIN).json() == {"status": "queued"}
    assert 'data-q-status="queued"' in client.get("/lots/5001/questions?url=https://m.kz/p1").text
    prompts = []
    monkeypatch.setattr(research, "_claude_code", lambda prompt, model, schema=None, web=True: (
        prompts.append(prompt) or {"confirm": ["Формат точно А4?"], "clarify": ["Плотность 80 г/м²?"],
                                   "terms": ["Цена на сайте актуальна?"]}))
    assert questions.work_one(conn, ["claude_code"]) is True
    assert "плотность не указана" in prompts[0] and "Бумага Svetocopy" in prompts[0]
    page = client.get("/lots/5001/questions?url=https://m.kz/p1").text
    assert "Плотность 80 г/м²?" in page and "Цена на сайте актуальна?" in page
    # Повторное нажатие (в т.ч. другим пользователем) не запускает ИИ заново.
    assert client.post("/lots/5001/questions", data={"url": "https://m.kz/p1"},
                       headers=ORIGIN).json() == {"status": "done"}
    assert questions.work_one(conn, ["claude_code"]) is False


def test_source_contacts_dialog(client, conn):
    from zakup import contacts

    conn.execute("INSERT INTO sources (domain, name, auto) VALUES ('satu.kz', 'Satu', true)")
    contacts._store(conn, "satu.kz", "тоо мебель", "ТОО «Мебель»",
                    [{"number": "+7 (707) 303-70-63", "label": "страница товара", "whatsapp": False}])
    conn.commit()
    _login(client)
    page = client.get("/research").text
    assert 'data-contacts="/sources/' in page and 'class="count">1<' in page
    source = conn.execute("SELECT id FROM sources WHERE domain = 'satu.kz'").fetchone()[0]
    fragment = client.get(f"/sources/{source}/contacts").text
    assert "ТОО «Мебель»" in fragment and 'href="tel:+77073037063"' in fragment
    assert 'href="https://wa.me/77073037063"' in fragment
    assert client.get("/sources/999999/contacts").status_code == 404


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


def test_research_hides_offers_without_margin(client, conn):
    _announcement_with_lots(conn)  # лот 5001: 10 000 ₸ за единицу
    conn.execute("""INSERT INTO lot_research (lot_id, user_id, provider, region, status, finished_at, result)
        SELECT 5001, id, 'claude_code', 'г.Алматы', 'done', now(),
        '{"summary": "s", "items": [
          {"name": "Дешёвая бумага", "url": "https://m.kz/1", "in_region": true, "unit_price": 8000},
          {"name": "Дорогая бумага", "url": "https://m.kz/2", "in_region": true, "unit_price": 12000}]}'
        FROM users LIMIT 1""")
    conn.commit()
    _login(client)
    page = client.get("/lots/5001").text
    assert "Дешёвая бумага" in page and "Дорогая бумага" not in page


def _fake_sms(monkeypatch, code="1234"):
    sent = []
    monkeypatch.setattr(web.signup.kazpayment, "request_otp",
                        lambda phone: sent.append(phone) or f"req-{len(sent)}")
    monkeypatch.setattr(web.signup.kazpayment, "verify_otp",
                        lambda request_id, entered: "77011112233" if entered == code else None)
    return sent


def _flow(page: str) -> str:
    return page.split('name="flow" value="', 1)[1].split('"', 1)[0]


def test_register_by_phone_then_company(client, conn, monkeypatch):
    sent = _fake_sms(monkeypatch)
    assert "Регистрация скоро откроется" in client.get("/register").text
    assert client.post("/register", data={"phone": "87011112233"}, headers=ORIGIN).status_code == 403
    web.signup.set_registration_open(conn, True)
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
    for _ in range(web.signup.PER_IP_HOUR):
        client.post("/forgot", data={"phone": "87011112233"}, headers=ORIGIN)
    blocked = client.post("/forgot", data={"phone": "87011112233"}, headers=ORIGIN)
    assert blocked.status_code == 429 and len(sent) == web.signup.PER_IP_HOUR



def test_guest_sees_public_pages_and_paywall(client, conn):
    _announcement_with_lots(conn)
    conn.execute("INSERT INTO spec_summaries (lot_id, provider, model, status, result) VALUES "
                 "(5001, 'claude_code', 'haiku', 'done', '{\"item\": \"Секретная выжимка\", \"params\": [], \"requirements\": []}')")
    conn.commit()
    web.app.dependency_overrides.clear()
    page = client.get("/lots/5001")
    assert page.status_code == 200 and "Бумага А4" in page.text and 'id="paywall"' in page.text
    assert "Секретная выжимка" not in page.text  # платное содержимое гостю не показываем
    assert 'data-paid' in page.text and "Войти" in page.text
    assert client.get("/announcements").status_code == 200
    assert client.post("/lots/5001/mark", data={"status": "interested"}, headers=ORIGIN,
                       follow_redirects=False).headers["location"].startswith("/login")
    assert "9 900 ₸" in client.get("/pricing").text
    assert "Sitemap:" in client.get("/robots.txt").text
    assert "/sitemap-a-0.xml" in client.get("/sitemap.xml").text
    assert "/a/500" in client.get("/sitemap-a-0.xml").text and "/lots/5001" in client.get("/sitemap-l-0.xml").text


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


def test_search_balance_charge_refund_history(client, conn):
    from zakup import credits, research

    _announcement_with_lots(conn)
    auth.set_password(conn, "+77011112233", "client-password-1")
    conn.execute("UPDATE users SET role = 'user' WHERE email = '+77011112233'")
    conn.commit()
    client_id = conn.execute("SELECT id FROM users WHERE email = '+77011112233'").fetchone()[0]
    client.post("/login", data={"email": "87011112233", "password": "client-password-1"}, headers=ORIGIN)
    # Без поисков — на страницу покупки, ничего не ставится в очередь.
    empty = client.post("/lots/5001/research", data={"region": "Алматы", "whole_kz": "1"}, headers=ORIGIN,
                        follow_redirects=False)
    assert empty.headers["location"] == "/pricing?need=searches"
    assert conn.execute("SELECT count(*) FROM lot_research").fetchone()[0] == 0
    credits.add(conn, client_id, 20, "grant")
    conn.commit()
    page = client.get("/lots/5001").text
    assert "🔍 20" in page and "×1" in page and "Запустить поиск товаров?" in page
    client.post("/lots/5001/research", data={"region": "Алматы", "whole_kz": "1"}, headers=ORIGIN)
    job = conn.execute("SELECT id FROM lot_research").fetchone()[0]
    assert credits.balance(conn, client_id) == 19
    assert credits.refund(conn, job) and not credits.refund(conn, job)  # возврат — один раз
    conn.commit()
    assert credits.balance(conn, client_id) == 20
    history = client.get("/balance").text
    assert "Поиск товаров" in history and "Возврат" in history and "/lots/5001" in history
    assert "+20" in history and "-1" in history
    # Сотрудник ищет без баланса.
    client.post("/logout", headers=ORIGIN)
    _login(client)
    conn.execute("UPDATE lot_research SET status = 'error'")
    conn.commit()
    client.post("/lots/5002/research", data={"region": "Алматы", "whole_kz": "1"}, headers=ORIGIN)
    assert conn.execute("SELECT count(*) FROM search_ledger WHERE kind = 'search'").fetchone()[0] == 1
    assert "поисков на балансе — 40" in client.post(f"/settings/users/{client_id}/searches",
                                                  data={"count": "20"}, headers=ORIGIN).text


def test_admin_edits_tariff_shown_to_clients(client, conn):
    from zakup import tariff

    page = client.get("/pricing").text
    assert "краткая выжимка техспецификации — без ограничений" in page and "выгрузка в Excel" in page
    _login(client)
    assert "Цена в месяц" in client.get("/settings").text
    saved = client.post("/settings/tariff", data={"name_ru": "Профи", "price": "12 900",
                                                  "features_ru": "• уведомления\n- избранное;\n\n"},
                        headers=ORIGIN, follow_redirects=False)
    assert saved.headers["location"] == "/settings?saved=tariff#tariff"
    assert tariff.get(conn)["features_ru"] == ["уведомления", "избранное"]
    page = client.get("/pricing").text
    assert "«Профи»" in page and "12 900 ₸" in page and "<li>избранное</li>" in page
    assert client.post("/settings/tariff", data={"name_ru": "x", "price": "0", "features_ru": "a"},
                       headers=ORIGIN).status_code == 422


def _signed(body: dict, secret: str = "whsec") -> tuple[bytes, dict]:
    import hashlib
    import hmac
    import json as _json

    raw = _json.dumps(body).encode()
    return raw, {"x-kazpayment-signature": "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
                 "content-type": "application/json"}


def test_pay_tariff_and_searches_via_webhook_and_poll(client, conn, monkeypatch):
    from zakup import credits, payments

    monkeypatch.setenv("KAZPAYMENT_WEBHOOK_SECRET", "whsec")
    orders = []
    monkeypatch.setattr(web.kazpayment, "create_order",
                        lambda ref, amount, comment, phone=None: orders.append((ref, amount, phone)) or f"https://kazpayment.kz/pay/{ref}")
    auth.set_password(conn, "+77011112233", "client-password-1")
    conn.execute("UPDATE users SET role = 'user', phone = email WHERE email = '+77011112233'")
    conn.commit()
    uid = conn.execute("SELECT id FROM users WHERE email = '+77011112233'").fetchone()[0]
    client.post("/login", data={"email": "87011112233", "password": "client-password-1"}, headers=ORIGIN)
    go = client.post("/pay/tariff", headers=ORIGIN, follow_redirects=False)
    assert go.headers["location"].startswith("https://kazpayment.kz/pay/zk-")
    ref, amount, phone = orders[0]
    assert amount == 9900 and phone == "+77011112233"
    raw, headers = _signed({"event_id": "e1", "event": "payment.received", "order_ref": ref,
                            "amount": 9900, "paid_amount": 9900, "late": False})
    assert client.post("/webhooks/kazpayment", content=raw, headers={**headers, "x-kazpayment-signature": "sha256=bad"}).status_code == 401
    assert client.post("/webhooks/kazpayment", content=raw, headers=headers).status_code == 200
    assert client.post("/webhooks/kazpayment", content=raw, headers=headers).status_code == 200  # повтор
    until = conn.execute("SELECT paid_until FROM users WHERE id = %s", (uid,)).fetchone()[0]
    assert until is not None and (until - datetime.now(UTC)).days in (29, 30)
    # Повтор события не продлевает второй раз.
    assert conn.execute("SELECT paid_until FROM users WHERE id = %s", (uid,)).fetchone()[0] == until
    # Пакет поисков: недоплата не выдаёт, полная оплата через запасной опрос — выдаёт.
    client.post("/pay/searches", data={"count": "20"}, headers=ORIGIN)
    ref2, amount2, _ = orders[1]
    assert amount2 == 4900 and payments.searches_price(30) == 7350 and payments.searches_price(5) == 1250
    raw, headers = _signed({"event_id": "e2", "event": "payment.received", "order_ref": ref2,
                            "amount": 4900, "paid_amount": 1000})
    client.post("/webhooks/kazpayment", content=raw, headers=headers)
    assert credits.balance(conn, uid) == 0
    monkeypatch.setattr(payments.kazpayment, "order_status",
                        lambda ref: {"status": "paid", "paidAmount": 4900} if ref == ref2 else {"status": "pending"})
    assert payments.poll(conn) == 1 and credits.balance(conn, uid) == 20
    assert payments.poll(conn) == 0  # уже выдано
    page = client.get("/pricing").text
    assert "оплачено" in page and "Мои оплаты" in page


def test_spec_tables_rendered_as_table():
    blocks = web._spec_blocks("Заголовок\nНаименование | Кол-во\nСтол | 5\nИтого")
    assert blocks == [("text", "Заголовок"), ("table", [["Наименование", "Кол-во"], ["Стол", "5"]]),
                      ("text", "Итого")]
