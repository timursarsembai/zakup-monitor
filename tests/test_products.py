"""Каталог товаров из поиска с ИИ."""

import os

from tests.test_research import _lot
from tests.web_helpers import _login
from zakup import products, research


def test_url_key_drops_tracking_and_trailing_slash():
    assert products.url_key("https://WWW.Mebel.kz/stol/?utm_source=x&b=2&a=1#top") == "https://mebel.kz/stol?a=1&b=2"
    assert products.url_key("http://mebel.kz/stol") == products.url_key("https://mebel.kz/stol/")


def test_remember_dedupes_keeps_price_history_and_date(conn):
    _lot(conn)
    item = {"name": "Стол дубовый", "url": "https://mebel.kz/stol?utm_source=a", "seller": "Мебель", "city": "Актобе",
            "price": 90000, "price_unit": "шт", "specs": [{"name": "Материал", "value": "дуб"}]}
    products.remember(conn, 90, [item])
    conn.execute("UPDATE product_catalog SET checked_at = now() - interval '10 days'")
    key = products.url_key(item["url"])
    # «Не открывал» — только для товара, который ему дали из каталога; чужой — как проверенный.
    products.remember(conn, 90, [{**item, "url": "https://mebel.kz/stol/", "price": 1, "checked": False}], offered={key})
    row = conn.execute("SELECT count(*), max(price), max(checked_at) < now() - interval '9 days' FROM product_catalog").fetchone()
    assert row == (1, 90000, True)
    # Перепроверил — новая цена, прежняя в истории, дата — сейчас.
    products.remember(conn, 90, [{**item, "price": None, "specs": []}])  # цену не прочитал — прежняя
    assert conn.execute("SELECT price FROM product_catalog").fetchone()[0] == 90000
    products.remember(conn, 90, [{**item, "price": 85000, "specs": []}])
    price, history, specs, fresh = conn.execute(
        "SELECT price, price_history, specs, checked_at > now() - interval '1 minute' FROM product_catalog").fetchone()
    assert price == 85000 and history[0]["price"] == 90000 and specs[0]["value"] == "дуб" and fresh
    products.remember(conn, 90, [], unavailable=["https://mebel.kz/stol"])  # не из выданных — не трогаем
    assert conn.execute("SELECT available FROM product_catalog").fetchone()[0] is True
    products.remember(conn, 90, [], unavailable=["https://mebel.kz/stol"], offered={key})
    assert conn.execute("SELECT available FROM product_catalog").fetchone()[0] is False


def test_catalog_candidates_go_into_next_search(conn, monkeypatch):
    user = _lot(conn)
    found = {"summary": "s", "region_searched": "Актобе", "unavailable": [], "items": [
        {"name": "Стол офисный дубовый", "url": "https://a.kz/1", "in_region": True, "price": 90000,
         "unit_price": 90000, "fit": "полностью", "specs": [{"name": "Материал", "value": "дуб"}]}]}
    prompts = []

    def fake(prompt, model, schema=None, web=True, **_):
        prompts.append(prompt)
        return found
    monkeypatch.setattr(research.providers, "_claude_code", fake)
    url = os.environ["TEST_DATABASE_URL"]
    research.enqueue(conn, user, 90, "Актобе", True)
    research.work(url, ["claude_code"], once=True)
    assert "ИЗ НАШЕГО КАТАЛОГА" not in prompts[0]
    item = research.latest_done(conn, 90)["result"]["items"][0]
    assert item["price_date"] and item["specs"] == [{"name": "Материал", "value": "дуб"}]
    # Второй поиск: товар из каталога — в подсказке как свежий, с характеристиками.
    conn.execute("UPDATE lot_research SET created_at = created_at - interval '1 day'")
    conn.commit()
    research.enqueue(conn, user, 90, "Актобе", True)
    research.work(url, ["claude_code"], once=True)
    assert "ИЗ НАШЕГО КАТАЛОГА" in prompts[1] and "https://a.kz/1" in prompts[1] and "Материал: дуб" in prompts[1]
    assert "страницу НЕ открывай" in prompts[1]


def test_products_page_open_to_all(client, conn):
    _lot(conn)
    products.remember(conn, 90, [{"name": "Стол дубовый", "url": "https://mebel.kz/stol", "price": 90000,
                                  "price_unit": "шт", "specs": [{"name": "Материал", "value": "дуб"}]}])
    conn.commit()
    # Каталог открыт всем (решение владельца 2026-11-12), без входа.
    page = client.get("/products?q=стол&sort=price").text
    assert "Стол дубовый" in page and "Материал: дуб" in page and "цена на" in page
    assert 'data-rview-box="catalog" data-rview-default="cards"' in page  # по умолчанию — плитка
    assert "Ничего не нашлось" in client.get("/products?q=диван").text


def test_candidates_include_own_lot_finds_with_other_words(conn):
    _lot(conn)
    products.remember(conn, 90, [{"name": "Утюг Philips", "url": "https://a.kz/iron", "price": 9000, "price_unit": "шт"}])
    products.remember(conn, 90, [{"name": "Утюг дорогой", "url": "https://a.kz/iron2", "price": 900000, "price_unit": "шт"}])
    rows = products.candidates(conn, 90, "Электроутюг", max_price=100000)
    assert [r["url"] for r in rows] == ["https://a.kz/iron"]  # дороже цены лота — не предлагаем


def test_products_page_pager(client, conn, monkeypatch):
    # Больше одной страницы — ссылки «Дальше»/«Назад» (имя функции не должно совпадать с page_url из base.html).
    _lot(conn)
    products.remember(conn, 90, [{"name": f"Стол {i}", "url": f"https://mebel.kz/stol{i}", "price": 1000 + i}
                                 for i in range(3)])
    conn.commit()
    monkeypatch.setattr(products, "PAGE_SIZE", 2)
    _login(client)
    page = client.get("/products").text
    assert "стр. 1 из 2" in page and "page=2" in page
    assert client.get("/products?page=2").status_code == 200
