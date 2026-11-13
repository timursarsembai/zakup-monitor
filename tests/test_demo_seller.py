"""Скрипт тестового продавца (zakup.demo_seller): создаёт магазин с товарами и фото и
удаляет всё вместе с файлами."""

from zakup import demo_seller as demo
from zakup import offers, sellers


def test_create_and_delete(client, conn, tmp_path, monkeypatch):
    monkeypatch.setenv("ZAKUP_MEDIA_DIR", str(tmp_path))
    demo.create(conn)
    demo.create(conn)  # повторно — не дублирует
    sid = conn.execute("SELECT id FROM sellers WHERE seller_key = 'demo'").fetchone()[0]
    assert conn.execute("SELECT count(*) FROM product_catalog WHERE seller_id = %s", (sid,)).fetchone()[0] == 8
    assert len(list(tmp_path.rglob("*.webp"))) == 16
    page = client.get(sellers.path({"id": sid, "name": demo.NAME})).text
    assert "Бумага офисная А4" in page and "+7 (700) 000-00-00" not in page  # гостю телефон не виден
    assert "Тестовый магазин (демо)" in client.get("/products").text
    assert offers.for_lot(conn, 1) == []  # лота нет — и без ошибок
    demo.delete(conn)
    assert conn.execute("SELECT count(*) FROM sellers WHERE seller_key = 'demo'").fetchone()[0] == 0
    assert not list(tmp_path.rglob("*.webp"))
