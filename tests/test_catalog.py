"""Каталог товаров: открытый список и страница товара (web/catalog.py, zakup.products)."""

import json

from tests.web_helpers import _announcement_with_lots, _login
from zakup import products


def _product(conn, name="Подушка спальная 50×70", division="13", lots=(5001,), url="https://shop.kz/p/1"):
    return conn.execute(
        """INSERT INTO product_catalog (url_key, url, name, seller, city, phone, price, price_unit, specs,
                                        division, lots, price_history)
           VALUES (%s, %s, %s, 'ТОО «Текстиль»', 'Алматы', '+7 701 000 00 00', 3000, 'шт', %s, %s, %s, %s)
           RETURNING id""",
        (products.url_key(url), url, name, json.dumps([{"name": "Наполнитель", "value": "холлофайбер"}]),
         division, list(lots), json.dumps([{"at": "2026-10-01T10:00:00+05:00", "price": 3200}]))).fetchone()[0]


def test_slug():
    assert products.slug("Подушка спальная 50×70") == "podushka-spalnaia-50-70"
    assert products.slug("Қазақ тілі") == "kazak-tili"
    assert products.slug("!!!") == "tovar" and len(products.slug("а" * 200)) <= products.SLUG_MAX


def test_catalog_open_to_guests_lots_by_subscription(client, conn):
    _announcement_with_lots(conn)
    pid = _product(conn, lots=(5001, 777))  # 777 в базе нет (удалён) — в счёт не идёт
    other = _product(conn, name="Подушка детская", url="https://shop.kz/p/2", lots=())
    conn.commit()
    path = f"/products/{pid}-podushka-spalnaia-50-70"
    listing = client.get("/products")
    assert listing.status_code == 200 and f'href="{path}"' in listing.text
    assert 'data-feature="catalog_lots"' in listing.text and 'href="/lots/5001"' not in listing.text

    page = client.get(path)
    assert page.status_code == 200
    text = page.text
    assert "Подушка спальная 50×70" in text and "холлофайбер" in text and "3 000.00" in text
    assert "3 200.00" in text  # история цены
    assert "+7 701 000 00 00" not in text and "Войдите, чтобы увидеть" in text  # телефон — только вошедшим
    assert f'href="/products/{other}-podushka-detskaia"' in text  # похожий, та же категория
    assert 'href="/lots/5001"' not in text and "Какие — по подписке" in text
    assert "лоты: <b>1</b>" in text
    ld = json.loads(text.split('<script type="application/ld+json">', 1)[1].split("</script>", 1)[0])
    offer = next(x for x in ld if x["@type"] == "Product")["offers"]
    assert offer["price"] == "3000.00" and offer["priceCurrency"] == "KZT"

    # Неверное или старое название в адресе — на верный; нет такого — 404.
    moved = client.get(f"/products/{pid}-old-name", follow_redirects=False)
    assert moved.status_code == 302 and moved.headers["location"] == path
    assert client.get(f"/products/{pid}", follow_redirects=False).headers["location"] == path
    assert client.get("/products/999999-x").status_code == 404
    assert client.get("/products/abc").status_code == 404

    # С подпиской — лоты товара ссылками.
    _login(client)
    page = client.get(path).text
    assert 'href="/lots/5001"' in page and "+7 701 000 00 00" in page
