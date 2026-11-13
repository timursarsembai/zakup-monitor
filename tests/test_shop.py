"""Свой магазин в кабинете (zakup.shop, web/shop.py): магазин, товары с оптовыми ценами,
проверка по БИН, «Это мой магазин» и модерация."""

from decimal import Decimal

import pytest

from tests.web_helpers import ORIGIN
from zakup import auth, products, sellers, shop

SHOP = {"name": "ТОО «Мебель-Строй»", "phone": ["8 701 111 22 33", ""], "city": "Алматы",
        "regions": "Алматы, Алматинская область", "prices_with_vat": "1"}


def _as_client(client, conn, login="+77011112233"):
    auth.set_password(conn, login, "client-password-1")
    conn.execute("UPDATE users SET role = 'user', phone = email WHERE email = %s", (login,))
    conn.commit()
    client.cookies.clear()
    client.post("/login", data={"email": login, "password": "client-password-1"}, headers=ORIGIN)
    return conn.execute("SELECT id FROM users WHERE email = %s", (login,)).fetchone()[0]


def _as_admin(client):
    client.cookies.clear()
    client.post("/login", data={"email": "owner@mail.kz", "password": "long-enough-password"}, headers=ORIGIN)


def test_tiers_and_forms_validated():
    assert shop.parse_tiers("1 500,50", ["10", "", "100"], ["1400", "", "1300"]) == [
        (1, Decimal("1500.50")), (10, Decimal("1400")), (100, Decimal("1300"))]
    for retail, qty, price in (("", [], []), ("100", ["10"], ["120"]), ("100", ["10", "10"], ["90", "80"]),
                               ("100", ["1"], ["90"]), ("100", ["10"], [""]), ("abc", [], []), ("-5", [], [])):
        with pytest.raises(shop.Invalid):
            shop.parse_tiers(retail, qty, price)
    with pytest.raises(shop.Invalid):
        shop.parse_shop(name="Магазин", phones=["", ""])
    with pytest.raises(shop.Invalid):
        shop.parse_shop(name="Магазин", phones=["701"])
    data = shop.parse_shop(name="  Магазин  ", phones=["87011112233", "+7 701 111 22 33"], website="zeta.kz")
    assert data["phones"] == [{"number": "+7 (701) 111-22-33", "label": "", "whatsapp": False}]
    assert data["website"] == "https://zeta.kz" and data["name"] == "Магазин"
    assert shop.parse_specs("Цвет: белый\n\nРазмер: 50×70") == [{"name": "Цвет", "value": "белый"},
                                                               {"name": "Размер", "value": "50×70"}]
    with pytest.raises(shop.Invalid):
        shop.parse_specs("просто строка")
    item = shop.parse_product(name="Стул", unit="шт", retail="5000", quantities=[], prices=[],
                              enstru="310011.100.000000")
    assert item["division"] == "31" and item["tiers"] == [(1, Decimal("5000"))]


def test_shop_without_bin_waits_for_moderation(client, conn):
    user = _as_client(client, conn)
    assert "Стать продавцом" in client.get("/my/shop").text
    assert client.post("/my/shop", data={**SHOP, "phone": [""]}, headers=ORIGIN).status_code == 422
    assert client.post("/my/shop", data=SHOP, headers=ORIGIN, follow_redirects=False).status_code == 303
    seller = shop.get(conn, user)
    assert seller["status"] == "pending" and seller["kind"] == "own" and not seller["verified"]
    assert seller["regions"] == ["Алматы", "Алматинская область"] and seller["prices_with_vat"]

    form = {"name": "Стул офисный", "unit": "шт", "retail": "25000", "qty": ["10", "50", ""],
            "tier_price": ["23000", "21000", ""], "category": "31", "specs": "Цвет: чёрный",
            "description": "Сетчатая спинка", "available": "1"}
    bad = client.post("/my/shop/products", data={**form, "tier_price": ["26000", "21000", ""]}, headers=ORIGIN)
    assert bad.status_code == 422 and "выше цены" in bad.text and 'value="26000"' in bad.text
    assert client.post("/my/shop/products", data=form, headers=ORIGIN, follow_redirects=False).status_code == 303
    pid = conn.execute("SELECT id FROM product_catalog WHERE own AND name = 'Стул офисный'").fetchone()[0]
    assert shop.tiers(conn, pid) == [(1, 25000), (10, 23000), (50, 21000)]
    path = products.path({"id": pid, "name": "Стул офисный"})
    # Владельцу — видно (с пометкой), остальным — нет, пока магазин на проверке.
    page = client.get(path).text
    # Узкая таблица — по ширине экрана телефона, без прокрутки вбок.
    assert "Цены по количеству" in page and '<table class="narrow">' in page
    own_page = client.get(sellers.path(seller)).text
    assert "на проверке" in own_page and "Стул офисный" in own_page  # свои товары владельцу видны
    conn.execute("""INSERT INTO product_catalog (url_key, url, name, price, division)
                    VALUES ('zeta.kz/chair', 'https://zeta.kz/chair', 'Стул обычный', 9000, '31')""")
    public = conn.execute("SELECT id FROM product_catalog WHERE name = 'Стул обычный'").fetchone()[0]
    conn.commit()
    client.cookies.clear()
    assert client.get(path).status_code == 404 and client.get(sellers.path(seller)).status_code == 404
    assert "Стул офисный" not in client.get("/products").text
    # …и в «похожих» на чужих страницах его тоже нет.
    assert "Стул офисный" not in client.get(products.path({"id": public, "name": "Стул обычный"})).text

    _as_admin(client)
    assert "ТОО «Мебель-Строй»" in client.get("/staff/sellers").text
    client.post(f"/staff/sellers/{seller['id']}/status", data={"status": "active"}, headers=ORIGIN)
    client.cookies.clear()
    page = client.get(path).text
    assert "21 000" in page and "с НДС" in page and "Сетчатая спинка" in page and "На сайте продавца" not in page
    assert "Стул офисный" in client.get("/products").text and "ТОО «Мебель-Строй»" in client.get("/sellers").text

    # Владелец меняет цену — прежняя в истории; снимает с продажи — из каталога пропадает.
    _as_client(client, conn)
    client.post("/my/shop/products", data={**form, "product_id": str(pid), "retail": "24000"}, headers=ORIGIN)
    price, history = conn.execute("SELECT price, price_history FROM product_catalog WHERE id = %s", (pid,)).fetchone()
    assert price == 24000 and history[0]["price"] == 25000
    client.post(f"/my/shop/products/{pid}/sale", data={"on": ""}, headers=ORIGIN)
    client.cookies.clear()
    assert client.get(path).status_code == 404 and "Стул офисный" not in client.get("/products").text
    # Чужой товар не изменить и не удалить.
    _as_client(client, conn, "+77019998877")
    client.post("/my/shop", data={**SHOP, "name": "Другой"}, headers=ORIGIN)
    assert client.post(f"/my/shop/products/{pid}/delete", headers=ORIGIN).status_code == 404
    assert client.get(f"/my/shop/products/{pid}").status_code == 404


def test_bin_in_registry_makes_shop_verified(client, conn, monkeypatch):
    user = _as_client(client, conn)
    asked = []
    monkeypatch.setattr(shop, "registry_name", lambda conn, bin_: asked.append(bin_) or "ТОО МЕБЕЛЬ-СТРОЙ")
    client.post("/my/shop", data={**SHOP, "bin": "123456789012"}, headers=ORIGIN)
    seller = shop.get(conn, user)
    assert seller["status"] == "active" and seller["verified"] and asked == ["123456789012"]
    # Тот же БИН — реестр снова не спрашиваем; скрытый модератором — владелец не откроет.
    conn.execute("UPDATE sellers SET status = 'hidden' WHERE id = %s", (seller["id"],))
    conn.commit()
    client.post("/my/shop", data={**SHOP, "bin": "123456789012", "name": "Новое"}, headers=ORIGIN)
    seller = shop.get(conn, user)
    assert asked == ["123456789012"] and seller["status"] == "hidden" and seller["name"] == "Новое"


def test_claim_found_shop(client, conn, monkeypatch):
    monkeypatch.setattr(shop, "registry_name", lambda conn, bin_: "ТОО")
    conn.execute("""INSERT INTO product_catalog (url_key, url, name, seller, price)
                    VALUES ('zeta.kz/p/1', 'https://zeta.kz/p/1', 'Диван', 'Zeta', 90000)""")
    sellers.link(conn)
    found = conn.execute("SELECT id FROM sellers WHERE domain = 'zeta.kz'").fetchone()[0]
    conn.commit()
    path = f"/sellers/{found}-zeta"
    assert "Это мой магазин" in client.get(path).text
    user = _as_client(client, conn)
    # Свой магазин у пользователя уже был — сольётся в найденный.
    client.post("/my/shop", data={**SHOP, "bin": "123456789012"}, headers=ORIGIN)
    client.post("/my/shop/products", data={"name": "Кресло", "unit": "шт", "retail": "40000"}, headers=ORIGIN)
    assert client.post(path + "/claim", data={"comment": ""}, headers=ORIGIN).status_code == 422
    bad = client.post(path + "/claim", data={"comment": "x", "bin": "12"}, headers=ORIGIN)
    assert bad.status_code == 422 and f'action="{path}/claim"' in bad.text
    # БИН в заявке не указан — останется БИН прежнего (проверенного) магазина.
    client.post(path + "/claim", data={"comment": "Пишу с почты info@zeta.kz"}, headers=ORIGIN)
    assert "Заявка отправлена" in client.get(path).text
    claim = conn.execute("SELECT id FROM seller_claims WHERE seller_id = %s", (found,)).fetchone()[0]

    _as_admin(client)
    assert "info@zeta.kz" in client.get("/staff/sellers").text
    client.post(f"/staff/claims/{claim}", data={"decision": "approve"}, headers=ORIGIN)
    seller = shop.get(conn, user)
    assert seller["id"] == found and seller["kind"] == "own" and seller["bin"] == "123456789012"
    assert seller["verified"] and seller["status"] == "active"
    assert conn.execute("SELECT seller FROM product_catalog WHERE name = 'Кресло'").fetchone()[0] == "Zeta"
    assert seller["phones"][0]["number"] == "+7 (701) 111-22-33"
    assert conn.execute("SELECT count(*) FROM sellers WHERE user_id = %s", (user,)).fetchone()[0] == 1
    names = {r[0] for r in conn.execute("SELECT name FROM product_catalog WHERE seller_id = %s", (found,))}
    assert names == {"Диван", "Кресло"}
    # Повторно решить нельзя; найденный ИИ товар владелец только снимает с продажи.
    client.post(f"/staff/claims/{claim}", data={"decision": "reject"}, headers=ORIGIN)
    assert conn.execute("SELECT status FROM seller_claims WHERE id = %s", (claim,)).fetchone()[0] == "approved"
    _as_client(client, conn)
    sofa = conn.execute("SELECT id FROM product_catalog WHERE name = 'Диван'").fetchone()[0]
    assert client.post(f"/my/shop/products/{sofa}/delete", headers=ORIGIN).status_code == 404
    assert client.post(f"/my/shop/products/{sofa}/sale", data={"on": ""}, headers=ORIGIN).status_code == 200
    assert conn.execute("SELECT on_sale FROM product_catalog WHERE id = %s", (sofa,)).fetchone()[0] is False
    # В счёте товаров продавца — только то, что видно в каталоге.
    assert sellers.get(conn, found)["products"] == 1


def test_hidden_shop_stays_hidden_after_claim(client, conn):
    conn.execute("""INSERT INTO product_catalog (url_key, url, name, seller, price)
                    VALUES ('beta.kz/p/1', 'https://beta.kz/p/1', 'Шкаф', 'Beta', 50000)""")
    sellers.link(conn)
    found = conn.execute("SELECT id FROM sellers WHERE domain = 'beta.kz'").fetchone()[0]
    conn.commit()
    user = _as_client(client, conn)
    client.post("/my/shop", data=SHOP, headers=ORIGIN)
    conn.execute("UPDATE sellers SET status = 'hidden' WHERE user_id = %s", (user,))
    conn.commit()
    client.post(f"/sellers/{found}-beta/claim", data={"comment": "мой"}, headers=ORIGIN)
    claim = conn.execute("SELECT id FROM seller_claims").fetchone()[0]
    assert shop.decide_claim(conn, claim, user, True) == "approved"
    assert shop.get(conn, user)["status"] == "hidden"
