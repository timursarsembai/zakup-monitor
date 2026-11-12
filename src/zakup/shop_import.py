"""Импорт каталога продавца из Excel или CSV по шаблону (docs/CATALOG_PLAN.md, этап 3, второй шаг).

Шаблон — COLUMNS: строка заголовков, дальше по товару в строке. Каждая строка проверяется как
форма товара (shop.parse_product); ошибки — списком с номерами строк, верные строки
сохраняются. Товар с тем же названием (без учёта регистра) у магазина — обновляется, иначе —
новый. Фото по ссылкам из файла не скачиваем (чужие адреса с сервера — риск), их добавляют
в карточке товара.
"""

import csv
import io
from typing import Any

import psycopg

from zakup import catalog, shop

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2000
TIERS = 3
COLUMNS = ["Название*", "Единица измерения*", "Цена за единицу, ₸*", "Категория (код раздела или название)",
           "Код ЕНС ТРУ", "Описание", "Характеристики (Цвет: белый; Размер: L)", "В наличии (да/нет)"] + [
    name for n in range(1, TIERS + 1) for name in (f"Опт {n}: от, единиц", f"Опт {n}: цена за единицу, ₸")]
EXAMPLE = ["Стул офисный", "шт", "25000", "31", "310011.100.000000", "Сетчатая спинка, подлокотники",
           "Цвет: чёрный; Материал: сетка", "да", "10", "23000", "50", "21000", "", ""]
_YES = {"да", "есть", "yes", "y", "1", "+", "иә", "true", "в наличии"}
_NO = {"нет", "no", "n", "0", "-", "жоқ", "false"}


MAX_UNPACKED = 60 * 1024 * 1024  # .xlsx — zip: распакованное не больше (защита от «zip-бомбы»)
CODE_COLUMNS = (3, 4)  # категория и код ЕНС ТРУ — в шаблоне текстом: Excel не съест нули


def template() -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    book = Workbook()
    sheet = book.active
    sheet.title = "Товары"
    sheet.append(COLUMNS)
    sheet.append(EXAMPLE)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for column, width in zip("ABCDEFGHIJKLMN", (36, 12, 14, 22, 20, 36, 36, 12, 10, 12, 10, 12, 10, 12), strict=True):
        sheet.column_dimensions[column].width = width
    for index in CODE_COLUMNS:
        for row in range(2, MAX_ROWS + 2):
            sheet.cell(row=row, column=index + 1).number_format = "@"
    sheet.freeze_panes = "A2"
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _numbered(rows) -> list[tuple[int, list[str]]]:
    """(номер строки как в Excel, ячейки) без заголовка и пустых строк; больше MAX_ROWS — ошибка."""
    out: list[tuple[int, list[str]]] = []
    for number, row in enumerate(rows, start=1):
        cells = [_cell(v) for v in row[:len(COLUMNS)]]
        if number == 1 or not any(cells):
            continue
        out.append((number, cells))
        if len(out) > MAX_ROWS:
            raise ValueError(f"Строк больше {MAX_ROWS} — разбейте файл на несколько.")
    if not out:
        raise ValueError("В файле нет строк.")
    return out


def _check_zip(content: bytes) -> None:
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if sum(info.file_size for info in archive.infolist()) > MAX_UNPACKED:
                raise ValueError("Файл Excel слишком большой внутри — сохраните его заново или разбейте.")
    except zipfile.BadZipFile as error:
        raise ValueError("Не удалось прочитать файл Excel (.xlsx).") from error


def read(content: bytes, filename: str) -> list[tuple[int, list[str]]]:
    """Строки файла с их номерами (как в Excel; заголовок — строка 1); ValueError — файл не
    читается (текст для продавца)."""
    if not content:
        raise ValueError("Выберите файл.")
    if len(content) > MAX_BYTES:
        raise ValueError("Файл больше 5 МБ.")
    name = (filename or "").lower()
    if name.endswith(".csv"):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("cp1251", errors="replace")  # Excel на Windows сохраняет так
        try:
            try:
                dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t")
            except csv.Error:
                dialect = csv.excel  # один столбец или разделитель не угадать — запятая
            return _numbered(csv.reader(io.StringIO(text), dialect))
        except csv.Error as error:
            raise ValueError("Не удалось прочитать файл CSV.") from error
    if name.endswith(".xlsx") or content[:2] == b"PK":
        from openpyxl import load_workbook

        _check_zip(content)
        try:
            book = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            try:
                return _numbered(book.worksheets[0].iter_rows(values_only=True))
            finally:
                book.close()
        except ValueError as error:
            if error.args and str(error.args[0]).startswith(("Строк больше", "В файле нет")):
                raise
            raise ValueError("Не удалось прочитать файл Excel (.xlsx).") from error
        except Exception as error:  # noqa: BLE001 — битый или чужой файл: любой сбой разбора
            raise ValueError("Не удалось прочитать файл Excel (.xlsx).") from error
    raise ValueError("Подходят файлы .xlsx или .csv (старый .xls — пересохраните в .xlsx).")


def _division(value: str) -> str:
    """Код раздела ЕНС ТРУ по ячейке: «31», «1» (Excel съел ноль), название; не узнали — ошибка."""
    value = value.strip()
    if not value:
        return ""
    if value.isdigit() and len(value) <= 2 and value.zfill(2) in catalog.DIVISIONS:
        return value.zfill(2)
    if value[:2].isdigit() and value[:2] in catalog.DIVISIONS:
        return value[:2]
    lowered = value.lower()
    for code, (ru, kk) in catalog.DIVISIONS.items():
        if lowered in (ru.lower(), kk.lower()):
            return code
    raise shop.Invalid("Категория не найдена — укажите код раздела ЕНС ТРУ (2 цифры) или его название.")


def _enstru(value: str) -> str:
    """Код ЕНС ТРУ, который Excel превратил в число: 11111 → 011111, 139224.99 → 139224.990."""
    parts = value.strip().split(".")
    if not value.strip() or not all(p.isdigit() for p in parts) or len(parts) > 3:
        return value.strip()
    widths = (6, 3, 6)
    return ".".join([parts[0].zfill(6)] + [p.ljust(w, "0") for p, w in zip(parts[1:], widths[1:], strict=False)])


def _available(value: str) -> str:
    lowered = value.strip().lower()
    if not lowered or lowered in _YES:
        return "1"
    if lowered in _NO:
        return ""
    raise shop.Invalid("«В наличии» — да или нет.")


def parse_row(row: list[str]) -> dict[str, Any]:
    """Строка → данные товара; «given» — какие необязательные столбцы заполнены (пустые при
    обновлении оставляют прежнее)."""
    row = row + [""] * (len(COLUMNS) - len(row))
    name, unit, retail, category, enstru, description, specs, available = row[:8]
    tiers = row[8:8 + 2 * TIERS]
    data = shop.parse_product(
        name=name, unit=unit, retail=retail, quantities=tiers[0::2], prices=tiers[1::2],
        category=_division(category), enstru=_enstru(enstru), description=description,
        specs="\n".join(part.strip() for part in specs.split(";") if part.strip()),
        available=_available(available))
    data["given"] = {key for key, value in (("division", category), ("enstru", enstru), ("description", description),
                                             ("specs", specs), ("available", available),
                                             ("tiers", "".join(tiers))) if value.strip()}
    return data


def _merge(data: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    """Обновление: пустые необязательные столбцы — прежнее значение товара. Прежние оптовые
    ступени — только те, что не дороже новой розницы (иначе ступени нарушили бы порядок)."""
    given = data.pop("given")
    for key in ("division", "enstru", "description", "specs", "available"):
        if key not in given:
            data[key] = old[key]
    if "tiers" not in given:
        retail = data["tiers"][0][1]
        data["tiers"] = data["tiers"][:1] + [(n, p) for n, p in old["tiers"] if n > 1 and p <= retail]
    return data


def run(conn: psycopg.Connection, seller: dict[str, Any], rows: list[tuple[int, list[str]]]) -> dict[str, Any]:
    """Сохраняет верные строки; {"created", "updated", "errors": [(номер строки, текст)]}."""
    existing = {name.lower(): pid for pid, name in conn.execute(
        "SELECT id, name FROM product_catalog WHERE seller_id = %s AND own", (seller["id"],))}
    room = shop.MAX_PRODUCTS - len(existing)
    created = updated = 0
    errors: list[tuple[int, str]] = []
    for index, (number, row) in enumerate(rows):
        try:
            data = parse_row(row)
            pid = existing.get(data["name"].lower())
            if pid is None:
                data.pop("given")
                if room <= 0:
                    raise shop.Invalid(f"В магазине — не больше {shop.MAX_PRODUCTS} товаров.")
            else:
                data = _merge(data, shop.product(conn, seller["id"], pid))
            with conn.transaction():
                saved = shop.save_product(conn, seller, data, pid, limit_checked=True)
            if pid is None:
                existing[data["name"].lower()] = saved
                created += 1
                room -= 1
            else:
                updated += 1
        except shop.Invalid as error:
            errors.append((number, str(error)))
        if index % 200 == 199:
            conn.commit()  # большие файлы — частями: не держим блокировки до конца
    return {"created": created, "updated": updated, "errors": errors}
