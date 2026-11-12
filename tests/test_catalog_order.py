"""Порядок в каталоге (этап 6): жалобы, скрытие товара сотрудником, суточные пределы, sitemap."""

from tests.test_shop import SHOP, _as_admin, _as_client
from tests.web_helpers import ORIGIN
from zakup import photos, products, sellers, shop


def _shop_with_product(client, conn):
    user = _as_client(client, conn)
    client.post("/my/shop", data=SHOP, headers=ORIGIN)
    seller = shop.get(conn, user)
    conn.execute("UPDATE sellers SET status = 'active' WHERE id = %s", (seller["id"],))
    conn.commit()
    client.post("/my/shop/products", data={"name": "Стул", "unit": "шт", "retail": "5000"}, headers=ORIGIN)
    pid = conn.execute("SELECT id FROM product_catalog WHERE name = 'Стул'").fetchone()[0]
    return user, seller, pid, products.path({"id": pid, "name": "Стул"})


def test_report_and_block_product(client, conn):
    user, seller, pid, path = _shop_with_product(client, conn)
    # Владелец на своё не жалуется.
    assert "Пожаловаться" not in client.get(path).text
    assert client.post(f"{path}/report", data={"reason": "spam"}, headers=ORIGIN).status_code == 404
    client.cookies.clear()
    assert "Пожаловаться" in client.get(path).text and "чтобы пожаловаться" in client.get(path).text
    _as_client(client, conn, "+77019998877")
    sent = client.post(f"{path}/report", data={"reason": "fraud", "comment": "Не отвечают"}, headers=ORIGIN)
    assert "жалоба отправлена" in sent.text
    again = client.post(f"{path}/report", data={"reason": "fraud"}, headers=ORIGIN)
    assert "уже пожаловались" in again.text
    assert "Выберите причину" in client.post(f"{path}/report", data={"reason": "x"}, headers=ORIGIN).text
    client.post(f"{sellers.path(seller)}/report", data={"reason": "spam"}, headers=ORIGIN)
    assert conn.execute("SELECT count(*) FROM catalog_reports").fetchone()[0] == 2

    _as_admin(client)
    page = client.get("/staff/sellers").text
    assert "Не отвечают" in page and "Мошенничество" in page
    client.post("/staff/reports", data={"action": "block_product", "product_id": str(pid)}, headers=ORIGIN)
    assert conn.execute("SELECT status FROM catalog_reports WHERE product_id = %s", (pid,)).fetchone()[0] == "resolved"
    client.cookies.clear()
    assert client.get(path).status_code == 404 and "Стул" not in client.get("/products").text
    # На скрытое не жалуются; несуществующее сотрудник не «скроет».
    _as_client(client, conn, "+77019998877")
    assert client.post(f"{path}/report", data={"reason": "spam"}, headers=ORIGIN).status_code == 404
    _as_admin(client)
    assert client.post("/staff/reports", data={"action": "block_product", "product_id": "999999"},
                       headers=ORIGIN).status_code == 422
    assert client.post("/staff/reports", data={"action": "hide_seller", "seller_id": "9" * 30},
                       headers=ORIGIN).status_code == 422
    client.cookies.clear()
    # Владелец видит пометку и сам не вернёт (снять/вернуть в продажу — не то же самое).
    _as_client(client, conn)
    client.post(f"/my/shop/products/{pid}/sale", data={"on": "1"}, headers=ORIGIN)
    assert "скрыт модератором" in client.get("/my/shop").text
    client.cookies.clear()
    assert client.get(path).status_code == 404
    _as_admin(client)
    client.post("/staff/reports", data={"action": "unblock_product", "product_id": str(pid)}, headers=ORIGIN)
    client.post("/staff/reports", data={"action": "dismiss", "seller_id": str(seller["id"])}, headers=ORIGIN)
    assert conn.execute("SELECT count(*) FROM catalog_reports WHERE status = 'open'").fetchone()[0] == 0
    client.cookies.clear()
    assert client.get(path).status_code == 200


def test_daily_limits(client, conn, monkeypatch):
    monkeypatch.setattr(shop, "DAILY_PRODUCTS", 1)
    _, seller, pid, _ = _shop_with_product(client, conn)
    second = client.post("/my/shop/products", data={"name": "Стол", "unit": "шт", "retail": "5000"}, headers=ORIGIN)
    assert second.status_code == 422 and "в сутки" in second.text
    monkeypatch.setattr(photos, "DAILY", 0)
    from tests.test_photos import _jpeg
    page = client.post(f"/my/shop/products/{pid}/photos", files=[("files", ("a.jpg", _jpeg(), "image/jpeg"))],
                       headers=ORIGIN)
    assert page.status_code == 422 and "300 фото" in page.text
    # Предел загрузок считает и файлы с ошибками.
    monkeypatch.setattr(shop, "DAILY_IMPORTS", 1)
    page = client.post("/my/shop/import", files={"file": ("a.txt", b"x", "text/plain")}, headers=ORIGIN)
    assert page.status_code == 422
    page = client.post("/my/shop/import", files={"file": ("a.csv", b"x\n", "text/csv")}, headers=ORIGIN)
    assert page.status_code == 429


def test_sitemap_has_products_and_sellers(client, conn):
    _, seller, pid, path = _shop_with_product(client, conn)
    index = client.get("/sitemap.xml").text
    assert "sitemap-p-0.xml" in index and "sitemap-s-0.xml" in index
    assert path in client.get("/sitemap-p-0.xml").text
    assert sellers.path(seller) in client.get("/sitemap-s-0.xml").text
    assert "/products</loc>" in client.get("/sitemap-static.xml").text
    conn.execute("UPDATE product_catalog SET blocked = true WHERE id = %s", (pid,))
    conn.commit()
    assert path not in client.get("/sitemap-p-0.xml").text
