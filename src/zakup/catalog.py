"""Подборки закупок по категориям и регионам — открытые страницы для поисковиков.

Категория — раздел классификатора КПВЭД: первые две цифры кода ЕНС ТРУ лота
(26 — компьютеры и электроника, 31 — мебель, 43 — строительные работы…). Код есть
у большинства лотов на товары; у части работ и услуг его нет — они только в регионах.
Регион — первые две цифры КАТО места поставки (как в фильтре поиска).
"""

import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import psycopg

from zakup.filters import REGIONS, Filters

# Разделы КПВЭД (ru, kk). Казахские названия — рабочий перевод (см. ROADMAP, проверка носителем).
DIVISIONS: dict[str, tuple[str, str]] = {
    "01": ("Сельскохозяйственная продукция", "Ауыл шаруашылығы өнімдері"),
    "02": ("Продукция лесоводства", "Орман шаруашылығы өнімдері"),
    "03": ("Рыба и продукция рыболовства", "Балық және балық аулау өнімдері"),
    "05": ("Уголь", "Көмір"),
    "06": ("Нефть и природный газ", "Мұнай және табиғи газ"),
    "07": ("Металлические руды", "Металл кендері"),
    "08": ("Песок, щебень, соль и другое сырьё", "Құм, қиыршық тас, тұз және басқа шикізат"),
    "09": ("Услуги в горнодобывающей отрасли", "Тау-кен өндіру саласындағы қызметтер"),
    "10": ("Продукты питания", "Азық-түлік өнімдері"),
    "11": ("Напитки", "Сусындар"),
    "12": ("Табачные изделия", "Темекі бұйымдары"),
    "13": ("Текстиль", "Тоқыма бұйымдары"),
    "14": ("Одежда", "Киім"),
    "15": ("Обувь и изделия из кожи", "Аяқ киім және былғары бұйымдары"),
    "16": ("Древесина и изделия из дерева", "Ағаш және ағаш бұйымдары"),
    "17": ("Бумага и изделия из бумаги", "Қағаз және қағаз бұйымдары"),
    "18": ("Полиграфия", "Полиграфия"),
    "19": ("Топливо и нефтепродукты", "Отын және мұнай өнімдері"),
    "20": ("Химическая продукция", "Химия өнімдері"),
    "21": ("Лекарства и фармацевтика", "Дәрілер және фармацевтика"),
    "22": ("Изделия из резины и пластмасс", "Резеңке және пластмасса бұйымдары"),
    "23": ("Стройматериалы, стекло, керамика", "Құрылыс материалдары, шыны, керамика"),
    "24": ("Металлы", "Металдар"),
    "25": ("Металлические изделия", "Металл бұйымдары"),
    "26": ("Компьютеры, электроника и оптика", "Компьютерлер, электроника және оптика"),
    "27": ("Электрооборудование", "Электр жабдықтары"),
    "28": ("Машины и оборудование", "Машиналар мен жабдықтар"),
    "29": ("Автомобили и запчасти", "Автокөліктер және қосалқы бөлшектер"),
    "30": ("Прочий транспорт", "Басқа көлік құралдары"),
    "31": ("Мебель", "Жиһаз"),
    "32": ("Медизделия, спорттовары, игрушки и прочее", "Медициналық бұйымдар, спорт тауарлары, ойыншықтар және басқалар"),
    "33": ("Ремонт и монтаж оборудования", "Жабдықтарды жөндеу және монтаждау"),
    "35": ("Электроэнергия, газ, тепло", "Электр энергиясы, газ, жылу"),
    "36": ("Вода", "Су"),
    "37": ("Канализация", "Кәріз"),
    "38": ("Вывоз и утилизация отходов", "Қалдықтарды шығару және кәдеге жарату"),
    "39": ("Рекультивация", "Рекультивация"),
    "41": ("Строительство зданий", "Ғимараттар салу"),
    "42": ("Дороги и инженерные сооружения", "Жолдар және инженерлік құрылыстар"),
    "43": ("Строительные и ремонтные работы", "Құрылыс және жөндеу жұмыстары"),
    "45": ("Ремонт и обслуживание автомобилей", "Автокөліктерді жөндеу және қызмет көрсету"),
    "46": ("Оптовая торговля", "Көтерме сауда"),
    "47": ("Розничная торговля", "Бөлшек сауда"),
    "49": ("Перевозки и такси", "Тасымалдау және такси"),
    "50": ("Водный транспорт", "Су көлігі"),
    "51": ("Авиаперевозки и авиабилеты", "Әуе тасымалы және авиабилеттер"),
    "52": ("Склады и транспортные услуги", "Қоймалар және көлік қызметтері"),
    "53": ("Почта и курьеры", "Пошта және курьерлер"),
    "55": ("Проживание", "Тұру орны"),
    "56": ("Питание", "Тамақтандыру"),
    "58": ("Издательские услуги и подписка", "Баспа қызметтері және жазылым"),
    "59": ("Кино, видео и звукозапись", "Кино, бейне және дыбыс жазу"),
    "60": ("Теле- и радиовещание", "Теле- және радио хабар тарату"),
    "61": ("Связь и интернет", "Байланыс және интернет"),
    "62": ("Программное обеспечение и IT-услуги", "Бағдарламалық қамтамасыз ету және IT қызметтері"),
    "63": ("Информационные услуги", "Ақпараттық қызметтер"),
    "64": ("Финансовые услуги", "Қаржылық қызметтер"),
    "65": ("Страхование", "Сақтандыру"),
    "66": ("Вспомогательные финансовые услуги", "Қосалқы қаржылық қызметтер"),
    "68": ("Недвижимость и аренда помещений", "Жылжымайтын мүлік және үй-жай жалдау"),
    "69": ("Юридические и бухгалтерские услуги", "Заң және бухгалтерлік қызметтер"),
    "70": ("Консалтинг", "Консалтинг"),
    "71": ("Проектирование, экспертиза, испытания", "Жобалау, сараптама, сынақтар"),
    "72": ("Научные исследования", "Ғылыми зерттеулер"),
    "73": ("Реклама и исследования рынка", "Жарнама және нарықты зерттеу"),
    "74": ("Прочие профессиональные услуги", "Басқа кәсіби қызметтер"),
    "75": ("Ветеринарные услуги", "Ветеринарлық қызметтер"),
    "77": ("Аренда и лизинг", "Жалдау және лизинг"),
    "78": ("Подбор персонала", "Персонал іріктеу"),
    "79": ("Туризм и бронирование", "Туризм және брондау"),
    "80": ("Охрана", "Күзет"),
    "81": ("Уборка и обслуживание зданий", "Тазалау және ғимараттарға қызмет көрсету"),
    "82": ("Офисные и административные услуги", "Кеңсе және әкімшілік қызметтер"),
    "84": ("Государственные услуги", "Мемлекеттік қызметтер"),
    "85": ("Образование и обучение", "Білім беру және оқыту"),
    "86": ("Медицинские услуги", "Медициналық қызметтер"),
    "87": ("Уход с проживанием", "Тұрғылықты күтім"),
    "88": ("Социальные услуги", "Әлеуметтік қызметтер"),
    "90": ("Культура и искусство", "Мәдениет және өнер"),
    "91": ("Библиотеки, архивы, музеи", "Кітапханалар, мұрағаттар, мұражайлар"),
    "93": ("Спорт и отдых", "Спорт және демалыс"),
    "94": ("Услуги общественных организаций", "Қоғамдық ұйымдардың қызметтері"),
    "95": ("Ремонт техники и бытовых вещей", "Техника мен тұрмыстық заттарды жөндеу"),
    "96": ("Прочие бытовые услуги", "Басқа тұрмыстық қызметтер"),
}

# Лот относится к категории по первому коду ЕНС ТРУ, к региону — по первому месту поставки.
LOT_DIVISION = "left(l.enstru[1], 2)"
LOT_REGION = "left((coalesce(nullif(l.kato, '{}'), a.kato))[1], 2)"
LIST_LIMIT = 50
TTL = 3600

PAGE_TTL = 900

_counts: dict[str, Any] = {"at": 0.0, "rows": {}}
_pages: dict[tuple, tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def division_name(code: str | None, lang: str = "ru") -> str | None:
    names = DIVISIONS.get(code or "")
    return (names[1] if lang == "kk" else names[0]) if names else None


def open_filter(division: str | None = None, region: str | None = None) -> Filters:
    """Фильтр открытых лотов — как в поиске для гостей, плюс категория и регион (для ссылки
    «все в поиске»: там лот попадает по любому своему коду и месту поставки)."""
    return replace(Filters.public(), categories=[division] if division else [], regions=[region] if region else [],
                   sort="end")


def counts(conn: psycopg.Connection, now: datetime | None = None) -> dict[tuple[str, str], tuple[int, float]]:
    """Открытые лоты по (категория, регион): число и сумма. Пересчёт раз в час. Запрос — вне
    блокировки: пересчитывает один поток, остальные пока получают прежние цифры."""
    with _lock:
        if _counts["at"] > time.monotonic() and _counts["rows"]:
            return _counts["rows"]
        _counts["at"] = time.monotonic() + 60  # пересчёт взят; не удастся — повтор через минуту
    where, params = open_filter().where(now or datetime.now(UTC), "lots")
    rows = conn.execute(
        f"""SELECT coalesce({LOT_DIVISION}, ''), coalesce({LOT_REGION}, ''), count(*), coalesce(sum(l.amount), 0)
            FROM lots l, announcements a WHERE {where} GROUP BY 1, 2""", params).fetchall()
    result = {(d, r): (n, float(s)) for d, r, n, s in rows}
    with _lock:
        _counts.update(rows=result, at=time.monotonic() + TTL)
    return result

def totals(conn: psycopg.Connection, by: str) -> dict[str, tuple[int, float]]:
    """Сводка по категориям (by="division") или регионам (by="region")."""
    out: dict[str, list] = {}
    for (division, region), (n, amount) in counts(conn).items():
        key = division if by == "division" else region
        if key in (DIVISIONS if by == "division" else REGIONS):
            item = out.setdefault(key, [0, 0.0])
            item[0] += n
            item[1] += amount
    return {key: (n, amount) for key, (n, amount) in out.items()}


def _scope(division: str | None, region: str | None) -> tuple[str, dict[str, str]]:
    """Лот в подборке — по первому коду ЕНС ТРУ и первому месту поставки, как в counts()."""
    scope, params = [], {}
    if division:
        scope.append(f"{LOT_DIVISION} = %(division)s")
        params["division"] = division
    if region:
        scope.append(f"{LOT_REGION} = %(region)s")
        params["region"] = region
    return " AND ".join(scope) or "true", params


def page(conn: psycopg.Connection, division: str | None, region: str | None,
         now: datetime | None = None) -> dict[str, Any]:
    """Открытые лоты подборки и кто в ней покупает и выигрывает (за всё время). Держится
    в памяти PAGE_TTL: «кто покупает» — за всё время и считается сотни миллисекунд."""
    key = (division, region)
    cached = _pages.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    now = now or datetime.now(UTC)
    n, amount = 0, 0.0
    for (d, r), (count, total) in counts(conn, now).items():
        if (not division or d == division) and (not region or r == region):
            n, amount = n + count, amount + total
    condition, scope_params = _scope(division, region)
    where, params = open_filter().where(now, "lots")
    lots = conn.execute(
        f"""SELECT l.id, l.name_ru, l.name_kz, l.amount, l.customer_bin,
                   coalesce(a.customer_name_ru, l.customer_name_ru), a.end_date, l.lot_number
            FROM lots l, announcements a WHERE {where} AND {condition}
            ORDER BY a.end_date, l.id LIMIT {LIST_LIMIT}""", {**params, **scope_params}).fetchall()
    customers = conn.execute(
        f"""SELECT l.customer_bin, max(coalesce(a.customer_name_ru, l.customer_name_ru)), count(*),
                   coalesce(sum(l.amount), 0)
            FROM lots l JOIN announcements a ON a.id = l.trd_buy_id
            WHERE {condition} AND l.customer_bin IS NOT NULL
            GROUP BY 1 ORDER BY 3 DESC, 4 DESC LIMIT 10""", scope_params).fetchall()
    winners = conn.execute(
        f"""SELECT r.winner_bin, max(r.winner_name), count(*), coalesce(sum(r.winner_total), 0)
            FROM lot_results r JOIN lots l ON l.id = r.lot_id JOIN announcements a ON a.id = l.trd_buy_id
            WHERE {condition} AND r.outcome = 'winner'
            GROUP BY 1 ORDER BY 3 DESC, 4 DESC LIMIT 10""", scope_params).fetchall()
    data = {"total": n, "amount": amount, "lots": lots, "customers": customers, "winners": winners,
            "search": "/lots?" + open_filter(division, region).to_query("lots")}
    with _lock:
        if len(_pages) > 2000:  # пар «категория × регион» около тысячи
            _pages.clear()
        _pages[key] = (time.monotonic() + PAGE_TTL, data)
    return data

def sitemap_paths(conn: psycopg.Connection) -> list[str]:
    """Хабы, категории, регионы и пары «категория в регионе», где есть открытые лоты."""
    paths = ["/categories", "/regions"]
    paths += [f"/category/{code}" for code in sorted(totals(conn, "division"))]
    paths += [f"/region/{code}" for code in sorted(totals(conn, "region"))]
    paths += [f"/category/{d}/{r}" for (d, r), (n, _) in sorted(counts(conn).items())
              if n and d in DIVISIONS and r in REGIONS]
    return paths
