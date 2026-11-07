import os

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
    assert "Неверный логин или пароль" in response.text
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


def test_marks_on_search_my_page_and_hiding(client, conn):
    db.upsert_announcement(conn, {"id": 78, "name_ru": "Стулья", "end_date": "2099-01-01 09:00:00"})
    db.upsert_lot(conn, {"id": 6, "trd_buy_id": 78, "name_ru": "Стул офисный", "amount": 500000})
    conn.commit()
    _login(client)
    search = "/lots?applied=1&deadline_mode="
    assert "Стул офисный" in client.get(search).text
    response = client.post("/lots/6/mark", data={"status": "preparing", "next": search},
                           headers=ORIGIN, follow_redirects=False)
    assert response.headers["location"] == search + "#lot-6"
    client.post("/lots/6/mark", data={"note": "поставщик Алатау", "next": "/my"}, headers=ORIGIN)
    my = client.get("/my").text
    assert "Готовлю заявку" in my and "Стул офисный" in my and "поставщик Алатау" in my
    client.post("/lots/6/mark", data={"status": "rejected"}, headers=ORIGIN)
    page = client.get(search).text
    assert "Стул офисный" not in page and "Скрыто с отметкой" in page
    assert "Стул офисный" in client.get(search + "&hidden=1").text
    assert client.post("/lots/6/mark", data={"status": "bogus"}, headers=ORIGIN).status_code == 422
    assert client.post("/lots/999/mark", data={"status": "won"}, headers=ORIGIN).status_code == 404


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


def test_my_lots_filter_by_mark(client, conn):
    db.upsert_announcement(conn, {"id": 79, "name_ru": "Мебель", "end_date": "2099-01-01 09:00:00"})
    db.upsert_lot(conn, {"id": 7, "trd_buy_id": 79, "name_ru": "Шкаф", "amount": 1})
    db.upsert_lot(conn, {"id": 8, "trd_buy_id": 79, "name_ru": "Тумба", "amount": 1})
    conn.commit()
    _login(client)
    client.post("/lots/7/mark", data={"status": "interested"}, headers=ORIGIN)
    client.post("/lots/8/mark", data={"status": "submitted"}, headers=ORIGIN)
    both = client.get("/my").text
    assert "Шкаф" in both and "Тумба" in both
    only = client.get("/my?status=submitted").text
    assert "Тумба" in only and "Шкаф" not in only


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
