"""Регион посетителя по IP: только Казахстан (область, город), остальное — «другие страны».

Источник — DB-IP Lite (бесплатно, условие — ссылка «IP Geolocation by DB-IP»), раз в
месяц: из 80 МБ файла берём только казахстанские диапазоны (~6 тыс.) в таблицу geo_kz.
"""

import csv
import gzip
import io
import ipaddress
import logging
import tempfile
from datetime import date

import httpx
import psycopg

logger = logging.getLogger(__name__)

URL = "https://download.db-ip.com/free/dbip-city-lite-{month}.csv.gz"
ATTRIBUTION = '<a href="https://db-ip.com">IP Geolocation by DB-IP</a>'
REFRESH_DAYS = 30

REGIONS = {
    "Abai Region": "Абайская обл.", "Aktobe": "Актюбинская обл.", "Aktyubinskaya Oblast’": "Актюбинская обл.",
    "Aqtöbe": "Актюбинская обл.", "Almaty": "г. Алматы", "Almaty Oblysy": "Алматинская обл.",
    "Almaty Region": "Алматинская обл.", "Aqmola": "Акмолинская обл.", "Astana": "г. Астана",
    "Nur-Sultan": "г. Астана", "Atyrau Oblisi": "Атырауская обл.", "Atyraū": "Атырауская обл.",
    "Baikonur": "г. Байконыр", "Batys Qazaqstan": "Западно-Казахстанская обл.",
    "West Kazakhstan": "Западно-Казахстанская обл.", "East Kazakhstan": "Восточно-Казахстанская обл.",
    "Jetisu Region": "Жетысуская обл.", "Zhetysu Region": "Жетысуская обл.",
    "Karaganda": "Карагандинская обл.", "Kostanay": "Костанайская обл.", "Qostanay": "Костанайская обл.",
    "Kyzylorda": "Кызылординская обл.", "Qyzylorda": "Кызылординская обл.",
    "Mangghystaū": "Мангистауская обл.", "Mangystau": "Мангистауская обл.",
    "North Kazakhstan": "Северо-Казахстанская обл.", "Soltüstik Qazaqstan": "Северо-Казахстанская обл.",
    "Pavlodar Region": "Павлодарская обл.", "Shymkent": "г. Шымкент", "Turkistan": "Туркестанская обл.",
    "Ulytau Region": "Улытауская обл.", "Zhambyl": "Жамбылская обл.",
}
CITIES = {
    "Almaty": "Алматы", "Astana": "Астана", "Nur-Sultan": "Астана", "Shymkent": "Шымкент",
    "Karaganda": "Караганда", "Qaraghandy": "Караганда", "Aktobe": "Актобе", "Aqtobe": "Актобе",
    "Pavlodar": "Павлодар", "Ust-Kamenogorsk": "Усть-Каменогорск", "Oskemen": "Усть-Каменогорск",
    "Semey": "Семей", "Atyrau": "Атырау", "Kostanay": "Костанай", "Qostanay": "Костанай",
    "Kyzylorda": "Кызылорда", "Qyzylorda": "Кызылорда", "Oral": "Уральск", "Uralsk": "Уральск",
    "Petropavl": "Петропавловск", "Petropavlovsk": "Петропавловск", "Aktau": "Актау", "Aqtau": "Актау",
    "Taraz": "Тараз", "Turkistan": "Туркестан", "Kokshetau": "Кокшетау", "Taldykorgan": "Талдыкорган",
    "Ekibastuz": "Экибастуз", "Temirtau": "Темиртау", "Zhezqazghan": "Жезказган", "Konaev": "Конаев",
}


def parse(lines) -> list[tuple[str, str, str | None, str | None]]:
    rows = []
    for row in csv.reader(lines):
        if len(row) >= 6 and row[3] == "KZ":
            rows.append((row[0], row[1], REGIONS.get(row[4], row[4] or None), CITIES.get(row[5], row[5] or None)))
    return rows


def update(conn: psycopg.Connection, today: date | None = None, force: bool = False) -> int:
    """Скачивает свежий файл (этот месяц, иначе прошлый) и заменяет geo_kz. 0 — не нужно."""
    today = today or date.today()
    if not force:
        fresh = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'geo_updated'").fetchone()
        conn.commit()
        if fresh and (today - date.fromisoformat(fresh[0])).days < REFRESH_DAYS:
            return 0
    previous = date(today.year - (today.month == 1), (today.month - 2) % 12 + 1, 1)
    # 80 МБ — во временный файл, не в память: у контейнеров тесный лимит.
    with tempfile.TemporaryFile() as raw:
        for month in (f"{today:%Y-%m}", f"{previous:%Y-%m}"):
            with httpx.stream("GET", URL.format(month=month), timeout=300, follow_redirects=True) as response:
                if response.status_code != 200:
                    continue
                for chunk in response.iter_bytes():
                    raw.write(chunk)
                break
        else:
            raise RuntimeError("DB-IP: файл не скачался")
        raw.seek(0)
        rows = parse(io.TextIOWrapper(gzip.GzipFile(fileobj=raw), encoding="utf-8"))
    with conn.transaction():
        conn.execute("DELETE FROM geo_kz")
        with conn.cursor().copy("COPY geo_kz (ip_from, ip_to, region, city) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)
        conn.execute(
            """INSERT INTO app_settings (key, value) VALUES ('geo_updated', %s)
               ON CONFLICT (key) DO UPDATE SET value = excluded.value""", (today.isoformat(),))
    return len(rows)


def lookup(conn: psycopg.Connection, ip: str | None) -> tuple[str | None, str | None, str | None]:
    """(страна, область, город): ('KZ', …) | ('other', None, None) | (None, …) — адрес не публичный."""
    try:
        address = ipaddress.ip_address(ip or "")
    except ValueError:
        return None, None, None
    if not address.is_global:
        return None, None, None
    row = conn.execute(
        """SELECT region, city FROM (SELECT * FROM geo_kz WHERE ip_from <= %s::inet
               ORDER BY ip_from DESC LIMIT 1) g WHERE ip_to >= %s::inet""",
        (str(address), str(address)),
    ).fetchone()
    if row:
        return "KZ", row[0], row[1]
    known = conn.execute("SELECT EXISTS (SELECT 1 FROM geo_kz)").fetchone()[0]
    return ("other" if known else None), None, None
