"""Продавцы каталога (zakup.sellers): кто продаёт найденные товары, их страницы и каталоги."""

import json

from tests.web_helpers import _login
from zakup import products, sellers


def _add(conn, url, seller, price=1000, city="Алматы", division="31", available=True, name=None):
    conn.execute(
        """INSERT INTO product_catalog (url_key, url, name, seller, city, price, price_unit, division, available)
           VALUES (%s, %s, %s, %s, %s, %s, 'шт', %s, %s)""",
        (products.url_key(url), url, name or f"Товар {url[-3:]}", seller, city, price, division, available))


def test_identity_like_contacts():
    assert sellers.identity("https://almaty.domdivanov.kz/p/1", "Дом Диванов (almaty.domdivanov.kz)") == (
        "almaty.domdivanov.kz", "", "Дом Диванов")
    assert sellers.identity("https://satu.kz/p/1", "TOMILI (Satu.kz)") == ("satu.kz", "tomili", "TOMILI")
    assert sellers.identity("https://satu.kz/p/2", 'ТОО "МЖ Буилдинг"')[1] == "тоо мж буилдинг"
    assert sellers.identity("https://kaspi.kz/p/1", "Kaspi.kz (несколько продавцов)") is None
    assert sellers.identity("https://obyavleniya.kaspi.kz/1", "частное объявление (Kaspi)") is None
    assert sellers.identity("https://kaspi.kz/p/1", "Kaspi.kz") is None  # кто продаёт — неизвестно
    assert sellers.identity("https://zeta.kz/p/1", None) == ("zeta.kz", "", "zeta.kz")


def test_link_groups_site_and_marketplace_sellers(conn):
    _add(conn, "https://almaty.domdivanov.kz/sofa", "Дом Диванов")
    _add(conn, "https://almaty.domdivanov.kz/bed", "Дом Диванов (almaty.domdivanov.kz)")
    _add(conn, "https://satu.kz/p/1", "TOMILI (Satu.kz)")
    _add(conn, "https://satu.kz/p/2", "Tomili")
    _add(conn, "https://satu.kz/p/3", "Другой продавец")
    _add(conn, "https://kaspi.kz/p/9", "Kaspi.kz (несколько продавцов)")
    assert sellers.link(conn) == 5 and sellers.link(conn) == 0
    rows = conn.execute("SELECT s.name, s.website, count(*) FROM product_catalog p JOIN sellers s ON s.id = p.seller_id "
                        "GROUP BY 1, 2 ORDER BY 1").fetchall()
    assert rows == [("TOMILI", None, 2), ("Дом Диванов", "https://almaty.domdivanov.kz", 2),
                    ("Другой продавец", None, 1)]
    # Новая находка поиска — сразу с продавцом.
    conn.execute("INSERT INTO lots (id, raw) VALUES (91, '{}')")
    products.remember(conn, 91, [{"name": "Кресло", "url": "https://almaty.domdivanov.kz/chair", "seller": "Дом Диванов"}])
    assert conn.execute("SELECT s.name FROM product_catalog p JOIN sellers s ON s.id = p.seller_id "
                        "WHERE p.name = 'Кресло'").fetchone() == ("Дом Диванов",)


def test_seller_pages(client, conn):
    _add(conn, "https://almaty.domdivanov.kz/sofa", "Дом Диванов", price=90000, name="Диван угловой")
    _add(conn, "https://almaty.domdivanov.kz/bed", "Дом Диванов", price=150000, name="Кровать двуспальная",
         available=False)
    _add(conn, "https://almaty.domdivanov.kz/lamp", "Дом Диванов", price=5000, name="Лампа", division="27")
    _add(conn, "https://satu.kz/p/3", "Другой продавец", city="Астана")
    sellers.link(conn)
    conn.execute("""INSERT INTO seller_contacts (domain, seller, name, phones)
                    VALUES ('almaty.domdivanov.kz', '', 'Дом Диванов', %s)""",
                 (json.dumps([{"number": "+7 (717) 278-82-60", "label": "страница товара", "whatsapp": False}]),))
    sid = conn.execute("SELECT id FROM sellers WHERE name = 'Дом Диванов'").fetchone()[0]
    conn.commit()
    path = f"/sellers/{sid}-dom-divanov"

    listing = client.get("/sellers").text
    assert f'href="{path}"' in listing and "найден в интернете" in listing and "Другой продавец" in listing
    assert "Дом Диванов" not in client.get("/sellers?city=Астана").text

    page = client.get(path)
    assert page.status_code == 200
    assert "Диван угловой" in page.text and "Другой продавец" not in page.text
    assert "278-82-60" not in page.text and "Войдите, чтобы увидеть" in page.text
    # Фильтры каталога продавца: цена, наличие, категория.
    assert "Лампа" not in client.get(path + "?price_min=10000").text
    assert "Кровать" not in client.get(path + "?available=1").text
    only = client.get(path + "?category=27").text
    assert "Лампа" in only and "Диван угловой" not in only
    # Неверное название — на верный адрес; скрытый продавец — 404 (и его товары).
    assert client.get(f"/sellers/{sid}-x", follow_redirects=False).headers["location"] == path
    pid = conn.execute("SELECT id FROM product_catalog WHERE name = 'Лампа'").fetchone()[0]
    product = client.get(f"/products/{pid}-lampa").text
    assert f'href="{path}"' in product
    _login(client)
    assert "278-82-60" in client.get(path).text
    client.cookies.clear()
    conn.execute("UPDATE sellers SET status = 'hidden' WHERE id = %s", (sid,))
    conn.commit()
    assert client.get(path).status_code == 404 and client.get(f"/products/{pid}-lampa").status_code == 404
    assert "Диван угловой" not in client.get("/products").text


def test_link_review_cases(client, conn):
    # Поддомены маркетплейса — один продавец.
    assert sellers.identity("https://almaty.satu.kz/p/1", "TOMILI")[:2] == ("satu.kz", "tomili")
    assert products.price_text(1234567.0) == "1234567" and products.price_text(99.5) == "99.5"
    assert products.price_text(None) == ""
    # Продавец в тексте сменился — товар переходит к новому.
    conn.execute("INSERT INTO lots (id, raw) VALUES (92, '{}')")
    item = {"name": "Стул", "url": "https://satu.kz/p/77", "seller": "TOMILI"}
    products.remember(conn, 92, [item])
    products.remember(conn, 92, [dict(item, seller="ТОО Мебель")])
    assert conn.execute("SELECT s.name FROM product_catalog p JOIN sellers s ON s.id = p.seller_id "
                        "WHERE p.name = 'Стул'").fetchone() == ("ТОО Мебель",)
    # Город в списке — тот, по которому фильтр найдёт продавца.
    for n, city in enumerate(["Алматы", "Алматы", "Астана"]):
        _add(conn, f"https://zeta.kz/p/{n}{n}{n}", "Zeta", city=city)
    sellers.link(conn)
    data = sellers.listing(conn)
    assert "Астана" not in data["cities"] and "Алматы" in data["cities"]
    # Ожидает модерации — страницы закрыты; сотруднику скрытый — с каталогом.
    sid = conn.execute("SELECT id FROM sellers WHERE domain = 'zeta.kz'").fetchone()[0]
    conn.execute("UPDATE sellers SET status = 'pending' WHERE id = %s", (sid,))
    conn.commit()
    assert client.get(f"/sellers/{sid}-zeta").status_code == 404
    assert products.listing(conn, seller_id=sid, hidden=True)["total"] == 3
    assert products.listing(conn, seller_id=sid)["total"] == 0
    assert client.get("/products?price_min=1234567").text.count("price_min=1.2") == 0
