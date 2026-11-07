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
    monkeypatch.setattr(web.spec, "get", lambda conn, lot_id, url: ("Столик, дерево", None))
    fragment = client.get("/a/77/lots/5/spec")
    assert "Столик, дерево" in fragment.text
    assert client.get("/a/404404").status_code == 404


def test_marks_on_search_my_page_and_hiding(client, conn):
    db.upsert_announcement(conn, {"id": 78, "name_ru": "Стулья", "end_date": "2099-01-01 09:00:00"})
    db.upsert_lot(conn, {"id": 6, "trd_buy_id": 78, "name_ru": "Стул офисный", "amount": 500000})
    conn.commit()
    _login(client)
    search = "/?applied=1&deadline_mode="
    assert "Стул офисный" in client.get(search).text
    response = client.post("/lots/6/mark", data={"status": "preparing", "next": search},
                           headers=ORIGIN, follow_redirects=False)
    assert response.headers["location"] == search + "#lot-6"
    client.post("/lots/6/mark", data={"note": "поставщик Алатау", "next": "/my"}, headers=ORIGIN)
    my = client.get("/my").text
    assert "Готовлю заявку" in my and "Стул офисный" in my and "поставщик Алатау" in my
    client.post("/lots/6/mark", data={"status": "rejected"}, headers=ORIGIN)
    page = client.get(search).text
    assert "Стул офисный" not in page and "Скрыто лотов" in page
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
    monkeypatch.setattr(web.results.spec, "download", lambda url: b"pdf")
    monkeypatch.setattr(web.results, "protocol_text", lambda pdf: PROTOCOL)
    detail = conn.execute("SELECT detail FROM announcements WHERE id = 7").fetchone()[0]
    web.results.parse_announcement(conn, 7, detail)
    conn.commit()
    _login(client)
    base = "/?applied=1&deadline_mode=&status="
    assert "Весы" not in client.get(base + "240").text
    page = client.get(base + "350").text
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
