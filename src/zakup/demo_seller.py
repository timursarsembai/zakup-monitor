"""Тестовый продавец с товарами — посмотреть, как выглядят каталог, страница продавца и блок
«Есть у продавцов» на лотах. Без владельца (user_id пуст), в названии — «(демо)».

Запуск (контейнер archive — у него том с фото):
  docker compose run --rm --no-deps -T --entrypoint python archive -m zakup.demo_seller create
  docker compose run --rm --no-deps -T --entrypoint python archive -m zakup.demo_seller delete
"""

import io
import sys
from decimal import Decimal

from PIL import Image, ImageDraw, ImageFont
from psycopg.types.json import Jsonb

from zakup import config, db, photos, shop

KEY = "demo"  # sellers.seller_key тестового продавца
NAME = "Тестовый магазин (демо)"

# Название, единица, розница, опт [(от, цена)], код ЕНС ТРУ, характеристики, в наличии, цвет фото
PRODUCTS = [
    ("Бумага офисная А4, 80 г/м², 500 листов", "пачка", 2400, [(10, 2300), (100, 2150)], "172314",
     "Формат: А4\nПлотность: 80 г/м²\nЛистов в пачке: 500\nБелизна: 146% CIE", True, "#e8eef7"),
    ("Средство моющее для посуды, 5 л", "шт", 3500, [(10, 3200), (50, 2950)], "204132",
     "Объём: 5 л\nНазначение: посуда, кухонные поверхности", True, "#dff3e4"),
    ("Кабель ВВГнг-LS 3×2,5 мм²", "м", 650, [(100, 590), (1000, 540)], "273213",
     "Сечение: 3×2,5 мм²\nИзоляция: ПВХ пониженной пожароопасности\nСтандарт: ГОСТ 31996-2012", True, "#f3e6d8"),
    ("Блок питания для ноутбука 65 Вт, универсальный", "шт", 7900, [(5, 7400)], "262040",
     "Мощность: 65 Вт\nВыходное напряжение: 19 В\nНасадок: 8", True, "#e4e4e4"),
    ("Шина летняя 205/55 R16", "шт", 32000, [(4, 30500), (20, 29000)], "221111",
     "Размер: 205/55 R16\nСезон: лето\nИндекс скорости: V", False, "#d9d9e8"),
    ("Комплект: клавиатура и мышь, проводные, USB", "компл", 6500, [(10, 6000)], "262016",
     "Подключение: USB\nРаскладка: русская, казахская, английская", True, "#e9e2f3"),
    ("Клей ПВА канцелярский, 100 мл", "шт", 450, [(50, 400), (500, 360)], "205210",
     "Объём: 100 мл\nДозатор: есть", True, "#f6f0d6"),
    ("Стул офисный на колёсиках, сетчатая спинка", "шт", 25000, [(10, 23000), (50, 21500)], "310011",
     "Спинка: сетка\nМеханизм: качание\nНагрузка: до 120 кг", True, "#f3dede"),
]


def _picture(text: str, color: str) -> bytes:
    image = Image.new("RGB", (800, 800), color)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=44)
    words, lines, line = text.split(), [], ""
    for word in words:
        if draw.textlength(f"{line} {word}".strip(), font=font) > 680:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    lines.append(line)
    y = 400 - len(lines) * 30
    for row in lines + ["", "ДЕМО"]:
        draw.text((400, y), row, fill="#333333", font=font, anchor="ma")
        y += 60
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def create(conn) -> None:
    if conn.execute("SELECT 1 FROM sellers WHERE seller_key = %s AND domain IS NULL", (KEY,)).fetchone():
        print("Тестовый продавец уже есть")
        return
    seller = conn.execute(
        """INSERT INTO sellers (kind, seller_key, name, status, phones, email, city, regions, description,
                                prices_with_vat)
           VALUES ('own', %s, %s, 'active', %s, 'demo@example.com', 'Алматы',
                   ARRAY['Алматы', 'Алматинская область'], %s, true)
           RETURNING id, name, city, phones""",
        (KEY, NAME, Jsonb([{"number": "+7 (700) 000-00-00", "label": "отдел продаж", "whatsapp": True}]),
         "Демонстрационный магазин: показывает, как выглядят товары продавцов в каталоге. Будет удалён.")).fetchone()
    seller = dict(zip(("id", "name", "city", "phones"), seller, strict=True))
    written: list[str] = []
    try:
        for name, unit, retail, wholesale, code, specs, available, color in PRODUCTS:
            data = shop.parse_product(name=name, unit=unit, retail=str(retail),
                                      quantities=[str(n) for n, _ in wholesale],
                                      prices=[str(p) for _, p in wholesale], enstru=code, specs=specs,
                                      available="1" if available else "",
                                      description="Демонстрационный товар — цены и наличие не настоящие.")
            assert data["tiers"][0] == (1, Decimal(retail))
            pid = shop.save_product(conn, seller, data, limit_checked=True)
            photos.add(conn, pid, photos.convert(_picture(name, color)))
            written += photos.keys(conn, pid)
        conn.commit()
    except Exception:
        # Строки откатятся, а файлы фото уже на диске — убрать и их.
        conn.rollback()
        photos.remove(written)
        raise
    print(f"Создан: продавец {seller['id']}, товаров {len(PRODUCTS)}")


def delete(conn) -> None:
    row = conn.execute("SELECT id FROM sellers WHERE seller_key = %s AND domain IS NULL", (KEY,)).fetchone()
    if row is None:
        print("Тестового продавца нет")
        return
    keys = [k for (pid,) in conn.execute("SELECT id FROM product_catalog WHERE seller_id = %s", row)
            for k in photos.keys(conn, pid)]
    n = conn.execute("DELETE FROM product_catalog WHERE seller_id = %s", row).rowcount
    conn.execute("DELETE FROM sellers WHERE id = %s", row)
    conn.commit()
    photos.remove(keys)
    print(f"Удалён: продавец {row[0]}, товаров {n}, файлов фото {len(keys)}")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action not in ("create", "delete"):
        sys.exit("create или delete")
    with db.connect(config.database_url()) as conn:
        {"create": create, "delete": delete}[action](conn)
