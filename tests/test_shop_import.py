"""Импорт каталога продавца из Excel/CSV (zakup.shop_import)."""

import io
from decimal import Decimal

import pytest
from openpyxl import Workbook, load_workbook

from tests.test_shop import SHOP, _as_client
from tests.web_helpers import ORIGIN
from zakup import shop, shop_import

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx(rows) -> bytes:
    book = Workbook()
    book.active.append(shop_import.COLUMNS)
    for row in rows:
        book.active.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def test_template_and_rows():
    sheet = load_workbook(io.BytesIO(shop_import.template())).active
    assert [c.value for c in sheet[1]] == shop_import.COLUMNS
    item = shop_import.parse_row([c.value if c.value is not None else "" for c in sheet[2]])
    assert item["division"] == "31" and item["tiers"] == [(1, Decimal("25000")), (10, Decimal("23000")),
                                                          (50, Decimal("21000"))]
    assert item["specs"] == [{"name": "Цвет", "value": "чёрный"}, {"name": "Материал", "value": "сетка"}]
    assert shop_import.parse_row(["Стол", "шт", "1000", "", "", "", "", "нет"])["available"] is False
    with pytest.raises(shop.Invalid):
        shop_import.parse_row(["Стол", "шт", "1000", "", "", "", "", "может быть"])
    rows = shop_import.read("Название;Единица;Цена\n;;\nКлей;шт;500\n".encode("cp1251"), "товары.csv")
    assert rows == [(3, ["Клей", "шт", "500"])]  # номер — как в файле, пустая строка не сдвигает
    assert shop_import.read(b"name\nGlue\n", "one.csv") == [(2, ["Glue"])]
    # Excel съел нули у кодов — вернём; неизвестная категория — ошибка строки.
    item = shop_import.parse_row(["Стол", "шт", "1000", "1", "11111"])
    assert item["division"] == "01" and item["enstru"] == "011111"
    assert shop_import._enstru("139224.99") == "139224.990"
    with pytest.raises(shop.Invalid):
        shop_import.parse_row(["Стол", "шт", "1000", "Всякое разное"])
    for content, name in ((b"", "a.xlsx"), (b"PK\x03\x04broken", "a.xlsx"), (b"x", "a.xls"),
                          (b"0" * (shop_import.MAX_BYTES + 1), "a.csv")):
        with pytest.raises(ValueError):
            shop_import.read(content, name)


def test_import_creates_updates_and_reports(client, conn):
    _as_client(client, conn)
    assert client.get("/my/shop/import").status_code == 404  # магазина ещё нет
    client.post("/my/shop", data=SHOP, headers=ORIGIN)
    assert client.get("/my/shop/import/template.xlsx").content[:2] == b"PK"
    content = _xlsx([["Стул", "шт", 25000, "31", "", "", "", "да", 10, 23000],
                     ["Стол", "", 9000],  # нет единицы
                     ["Шкаф", "шт", 50000, "", "", "", "", "", 5, 60000],  # опт дороже розницы
                     ["стул", "шт", 24000]])  # тот же товар — обновится
    page = client.post("/my/shop/import", files={"file": ("tovary.xlsx", content, XLSX)}, headers=ORIGIN)
    assert page.status_code == 200
    assert "Добавлено: 1" in page.text and "обновлено: 1" in page.text and "с ошибками: 2" in page.text
    assert "<td>3</td>" in page.text and "<td>4</td>" in page.text
    rows = conn.execute("SELECT name, price FROM product_catalog WHERE own").fetchall()
    assert rows == [("стул", 24000)]  # название — как в файле
    # Короткий файл (только цена) — описание и опт остаются прежними.
    client.post("/my/shop/import", files={"file": ("a.xlsx", _xlsx([["стул", "шт", 24000, "", "", "Удобный", "", "нет",
                                                                    10, 22000]]), XLSX)}, headers=ORIGIN)
    client.post("/my/shop/import", files={"file": ("b.xlsx", _xlsx([["стул", "шт", 23000]]), XLSX)}, headers=ORIGIN)
    pid = conn.execute("SELECT id FROM product_catalog WHERE own").fetchone()[0]
    kept = shop.product(conn, shop.get(conn, conn.execute(
        "SELECT user_id FROM sellers").fetchone()[0])["id"], pid)
    assert kept["description"] == "Удобный" and kept["available"] is False and kept["tiers"][1] == (10, 22000)
    bad = client.post("/my/shop/import", files={"file": ("x.pdf", b"%PDF", "application/pdf")}, headers=ORIGIN)
    assert bad.status_code == 422 and ".xlsx" in bad.text


def test_zip_bomb_and_broken_sheet_rejected(monkeypatch):
    monkeypatch.setattr(shop_import, "MAX_UNPACKED", 1000)
    with pytest.raises(ValueError, match="слишком большой"):
        shop_import.read(_xlsx([["Стул", "шт", 1]] * 50), "a.xlsx")
