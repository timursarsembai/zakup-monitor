"""Каталоги продавцов и закупки (zakup.offers): блок «Есть у продавцов», подсказка ИИ,
оповещения продавцам."""

from tests.test_shop import SHOP, _as_admin, _as_client
from tests.web_helpers import ORIGIN, _announcement_with_lots
from zakup import SITE_URL, offers, products, shop


def _shop_with_paper(client, conn):
    user = _as_client(client, conn)
    client.post("/my/shop", data=SHOP, headers=ORIGIN)
    seller = shop.get(conn, user)
    shop.set_status(conn, seller["id"], "active")
    conn.commit()
    client.post("/my/shop/products", data={"name": "Бумага А4 офисная", "unit": "пачка", "retail": "2500",
                                           "qty": ["50", "100"], "tier_price": ["2300", "2100"],
                                           "enstru": "171211.100.000000", "available": "1"}, headers=ORIGIN)
    client.post("/my/shop/products", data={"name": "Ручка гелевая синяя", "unit": "шт", "retail": "150"},
                headers=ORIGIN)
    return user, seller


def test_lot_shows_seller_offers_with_tier_price(client, conn):
    _announcement_with_lots(conn)  # лот 5001: бумага А4, 100 шт по 10 000; 5002: ручка, 1000 шт
    _, seller = _shop_with_paper(client, conn)
    paper = offers.for_lot(conn, 5001)
    assert [o["name"] for o in paper] == ["Бумага А4 офисная"]
    assert paper[0]["same_code"] and paper[0]["price"] == 2100 and paper[0]["retail"] == 2500
    assert round(paper[0]["margin"]) == 79
    assert [o["name"] for o in offers.for_lot(conn, 5002)] == ["Ручка гелевая синяя"]
    # Подсказка ИИ — адрес нашей страницы; найденное ИИ по ней не копируется в каталог.
    text = offers.for_prompt(paper)
    assert f"{SITE_URL}/products/{paper[0]['id']}-" in text and "2100 тенге" in text
    url = SITE_URL + products.path(paper[0])
    assert offers.own_product_id(url) == paper[0]["id"]
    before = conn.execute("SELECT count(*) FROM product_catalog").fetchone()[0]
    products.remember(conn, 5001, [{"name": "Бумага", "url": url, "price": 2100}])
    assert conn.execute("SELECT count(*) FROM product_catalog").fetchone()[0] == before
    found = {"items": [{"url": url}]}
    offers.fill_own(conn, found)
    assert found["items"][0]["phones"][0]["number"] == "+7 (701) 111-22-33"
    # Гостю — только что есть; по подписке (сотрудник) — какие и почём.
    client.cookies.clear()
    page = client.get("/lots/5001").text
    assert "Есть у продавцов" in page and "2 100" not in page and 'data-feature="catalog_lots"' in page
    _as_admin(client)
    page = client.get("/lots/5001").text
    assert "Бумага А4 офисная" in page and "2 100" in page and "тот же код" in page
    # Магазин скрыт — товаров нет.
    shop.set_status(conn, seller["id"], "hidden")
    conn.commit()
    assert offers.for_lot(conn, 5001) == []


def test_alerts_new_lots_once(client, conn):
    user, seller = _shop_with_paper(client, conn)
    conn.execute("UPDATE users SET telegram_chat_id = '555' WHERE id = %s", (user,))
    conn.commit()
    sent = []
    assert offers.alerts(conn, lambda chat, text: sent.append((chat, text))) == 0  # первый запуск — отметка
    _announcement_with_lots(conn)
    assert offers.alerts(conn, lambda chat, text: sent.append((chat, text))) == 1
    chat, text = sent[0]
    assert chat == "555" and "/lots/5001" in text and "Бумага А4 офисная" in text
    # Оповещение строже страницы лота: «Ручка гелевая синяя» ≠ «Ручка шариковая».
    assert "/lots/5002" not in text
    # Повторно — не шлём; выключил — тоже.
    conn.execute("UPDATE job_marks SET value = 0")
    conn.commit()
    assert offers.alerts(conn, lambda chat, text: sent.append((chat, text))) == 0
    client.post("/my/shop/alerts", data={"on": ""}, headers=ORIGIN)
    assert shop.get(conn, user)["alerts"] is False


def test_alert_retried_after_telegram_failure_and_escaped(client, conn):
    user, seller = _shop_with_paper(client, conn)
    conn.execute("UPDATE users SET telegram_chat_id = '555' WHERE id = %s", (user,))
    conn.execute("UPDATE sellers SET name = 'Ж&Б <опт>' WHERE id = %s", (seller["id"],))
    conn.commit()
    offers.alerts(conn, lambda chat, text: None)
    _announcement_with_lots(conn)

    def down(chat, text):
        raise OSError("telegram down")
    assert offers.alerts(conn, down) == 0
    sent = []
    assert offers.alerts(conn, lambda chat, text: sent.append(text)) == 1
    assert "Ж&amp;Б &lt;опт&gt;" in sent[0] and "/lots/5001" in sent[0]
    # Свой адрес — и с www.
    assert offers.is_own_url("https://www." + SITE_URL.split("://")[1] + "/products/1-x")
    assert offers.own_product_id("https://www." + SITE_URL.split("://")[1] + "/products/12-x") == 12
    assert offers.own_product_id("https://other.kz/products/12-x") is None
