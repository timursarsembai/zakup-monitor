"""Открытые страницы: главная и поиск лотов/объявлений, страницы закупок, анализ рынка,
документы, robots и sitemap, события из браузера.
"""

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from zakup import (
    SITE_URL,
    ai_config,
    catalog,
    dashboard,
    details,
    i18n,
    legal,
    marks,
    og,
    orgs,
    research,
    results,
    spec,
    subjects,
    trends,
    visits,
)
from zakup.filters import (
    REGIONS,
    STATUS_NAMES,
    Filters,
)
from zakup.goszakup import GoszakupError, parse_date
from zakup.matcher import PORTAL_ANNOUNCEMENT_URL
from zakup.web import core
from zakup.web.ai import _research_view
from zakup.web.core import (
    DETAIL_TTL,
    MAX_PAGE,
    PAGE_SIZE,
    Viewer,
    _audit,
    _editing,
    _per_page,
    _safe_next,
    _search_context,
    _track,
    _track_search,
    _user_groups,
    logger,
    same_origin,
    templates,
)

router = APIRouter()


@router.get("/lang/{code}")
def switch_language(request: Request, user: Viewer, code: str):
    """Переключатель Рус/Қаз: запоминает выбор и возвращает на ту же страницу."""
    if code not in i18n.LANGUAGES:
        raise HTTPException(404, "нет такого языка")
    if user:
        # Язык сайта — язык уведомлений в Telegram, пока в настройках не выбран другой.
        with core.pool.connection() as conn:
            conn.execute("UPDATE users SET ui_lang = %s WHERE id = %s", (code, user))
    back = request.headers.get("referer") or "/"
    parts = urlsplit(back)
    target = _safe_next(parts.path + (f"?{parts.query}" if parts.query else ""))
    if parts.netloc and parts.netloc != request.headers.get("host"):
        target = "/"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(i18n.COOKIE, code, max_age=365 * 86400, secure=True, samesite="lax")
    return response


@router.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@router.get("/")
def home(request: Request, user: Viewer, page: int = 1, per_page: int = PAGE_SIZE):
    """Главная — поиск объявлений, открыт всем, как на портале."""
    return announcements_search(request, user, page, per_page)


@router.get("/favicon.ico")
def favicon() -> Response:
    """Браузеры и поисковики спрашивают иконку по этому адресу без ссылки на странице."""
    return Response(files("zakup").joinpath("static/favicon.ico").read_bytes(),
                    media_type="image/x-icon", headers={"Cache-Control": "public, max-age=86400"})


# Картинки-превью для соцсетей: день в кэше у ботов и браузеров, в памяти — у нас.
OG_HEADERS = {"Cache-Control": "public, max-age=86400"}


def _og_response(key: tuple, render) -> Response:
    try:
        return Response(og.cached(key, render), media_type="image/png", headers=OG_HEADERS)
    except og.Busy:
        return Response(status_code=503, headers={"Retry-After": "5"})


@router.get("/og/site.png")
def og_site() -> Response:
    lang = i18n.language()
    return _og_response(("site", lang), lambda: og.site(lang))


@router.get("/og/lot/{lot_id}.png")
def og_lot(lot_id: int) -> Response:
    lang, now = i18n.language(), datetime.now(UTC)
    with core.pool.connection() as conn:
        row = conn.execute(
            """SELECT l.name_ru, l.name_kz, l.lot_number, l.amount,
                      coalesce(l.customer_name_ru, a.customer_name_ru, a.org_name_ru), a.end_date
               FROM lots l LEFT JOIN announcements a ON a.id = l.trd_buy_id WHERE l.id = %s""",
            (lot_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Такого лота нет в базе")
    name_ru, name_kz, number, amount, customer, end = row
    kicker = f"{og.TEXTS[lang]['lot']} {number or lot_id}"
    return _og_response(("lot", lot_id, lang, now.date(), end and end > now), lambda: og.card(
        lang, kicker=kicker, title=i18n.pick(name_ru, name_kz), customer=customer, amount=amount,
        end=end, now=now))


@router.get("/og/a/{announcement_id}.png")
def og_announcement(announcement_id: int) -> Response:
    lang, now = i18n.language(), datetime.now(UTC)
    with core.pool.connection() as conn:
        row = conn.execute(
            """SELECT name_ru, name_kz, number_anno, total_sum, coalesce(customer_name_ru, org_name_ru),
                      end_date FROM announcements WHERE id = %s""", (announcement_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Такого объявления нет в базе")
    name_ru, name_kz, number, amount, customer, end = row
    kicker = f"{og.TEXTS[lang]['announcement']} {number or announcement_id}"
    return _og_response(("a", announcement_id, lang, now.date(), end and end > now), lambda: og.card(
        lang, kicker=kicker, title=i18n.pick(name_ru, name_kz), customer=customer, amount=amount,
        end=end, now=now))


@router.get("/llms.txt")
def llms_txt() -> Response:
    """Описание сайта для нейросетей (llmstxt.org): что здесь есть и как устроены адреса."""
    return Response(LLMS_TXT.format(site=SITE_URL), media_type="text/plain; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=86400"})


LLMS_TXT = """# Tenderim

> Tenderim — сервис по государственным закупкам Казахстана. Собирает объявления и лоты
> с портала goszakup.gov.kz (официальный API), показывает их удобнее, присылает новые лоты
> по подпискам в Telegram, делает краткие выжимки технических спецификаций и с помощью ИИ
> подбирает товары у поставщиков с расчётом маржи. Есть версии на русском и казахском.

Данные о закупках — открытые сведения портала goszakup.gov.kz; первоисточник каждой
закупки — ссылка «на портале» на её странице. Сведения обновляются несколько раз в час.

## Закупки

- [Поиск объявлений]({site}/announcements): открытые объявления о госзакупках, фильтры по региону, сумме, сроку, способу закупки.
- [Поиск лотов]({site}/lots): то же по отдельным лотам.
- Страница объявления: {site}/a/<id> — номер, заказчик, сроки, все лоты, документы, итоги и победители.
- Страница лота: {site}/lots/<id> — товар или услуга, количество, цена, место поставки, техспецификация.
- Казахская версия любой страницы: тот же адрес с ?lang=kk.
- [Карта сайта]({site}/sitemap.xml): все открытые объявления и лоты.

## Аналитика

- [Анализ рынка]({site}/analytics): сколько закупок и на какие суммы по регионам, способам и категориям, динамика.

## Сервис

- [Тариф]({site}/pricing): что входит в подписку и сколько стоит.
- [Публичная оферта]({site}/offer), [Политика конфиденциальности]({site}/privacy), [Контакты]({site}/contacts).
"""


@router.get("/orgs")
def orgs_page(request: Request, user: Viewer, role: str = "", form: str = "", region: str = "", q: str = "",
              sort: str = "amount", page: int = 1):
    """Справочник организаций: заказчики и поставщики с фильтрами по роли, форме, региону."""
    role = role if role in orgs.ROLES else ""
    region = region if region in REGIONS else ""
    sort = sort if sort in orgs.SORTS else "amount"
    with core.pool.connection() as conn:
        rows, total = orgs.directory(conn, role=role, form=form, region=region, q=q, sort=sort, page=page)
    page = min(max(page, 1), orgs.MAX_PAGE)
    pages = min((total + orgs.PAGE_SIZE - 1) // orgs.PAGE_SIZE, orgs.MAX_PAGE)
    return templates.TemplateResponse(request, "orgs.html", {
        "rows": rows, "total": total, "page": page, "pages": pages,
        "f": {"role": role, "form": form if form in orgs.FORMS else "", "region": region, "q": q, "sort": sort},
        "roles": orgs.ROLES, "forms": orgs.FORMS, "sorts": orgs.SORTS, "regions": REGIONS})


@router.get("/org/{bin_}")
def org_page(request: Request, user: Viewer, bin_: str):
    """Открытая страница организации: закупки заказчика, победы и участие поставщика."""
    with core.pool.connection() as conn:
        profile = orgs.profile(conn, bin_)
    if profile is None:
        raise HTTPException(404, "Такой организации нет в базе")
    return templates.TemplateResponse(request, "org.html", {"o": profile})


@router.get("/categories")
def categories_page(request: Request, user: Viewer):
    """Все категории закупок (разделы КПВЭД) с числом открытых лотов."""
    with core.pool.connection() as conn:
        totals = catalog.totals(conn, "division")
    items = sorted(((code, totals.get(code, (0, 0))) for code in catalog.DIVISIONS), key=lambda i: -i[1][0])
    return templates.TemplateResponse(request, "catalog_index.html", {"kind": "division", "items": items})


@router.get("/regions")
def regions_page(request: Request, user: Viewer):
    with core.pool.connection() as conn:
        totals = catalog.totals(conn, "region")
    items = sorted(((code, totals.get(code, (0, 0))) for code in REGIONS), key=lambda i: -i[1][0])
    return templates.TemplateResponse(request, "catalog_index.html", {"kind": "region", "items": items})


def _catalog_page(request: Request, division: str | None, region: str | None):
    if (division and division not in catalog.DIVISIONS) or (region and region not in REGIONS):
        raise HTTPException(404, "Нет такой категории или региона")
    with core.pool.connection() as conn:
        data = catalog.page(conn, division, region)
        counts = catalog.counts(conn)
    # Соседние подборки: в категории — по регионам, в регионе — по категориям.
    if division:
        nearby = sorted(((r, n) for (d, r), (n, _) in counts.items() if d == division and r in REGIONS and n),
                        key=lambda i: -i[1])
    else:
        nearby = sorted(((d, n) for (d, r), (n, _) in counts.items() if r == region and d in catalog.DIVISIONS and n),
                        key=lambda i: -i[1])
    return templates.TemplateResponse(request, "catalog_page.html", {
        "division": division, "region": region, "d": data, "nearby": nearby})


@router.get("/category/{division}")
def category_page(request: Request, user: Viewer, division: str):
    return _catalog_page(request, division, None)


@router.get("/category/{division}/{region}")
def category_region_page(request: Request, user: Viewer, division: str, region: str):
    return _catalog_page(request, division, region)


@router.get("/region/{region}")
def region_page(request: Request, user: Viewer, region: str):
    return _catalog_page(request, None, region)


@router.get("/offer")
def offer_page(request: Request, user: Viewer):
    return templates.TemplateResponse(request, "legal_offer.html", {"docs_date": legal.DOCS_DATE})


@router.get("/privacy")
def privacy_page(request: Request, user: Viewer):
    return templates.TemplateResponse(request, "legal_privacy.html", {"docs_date": legal.DOCS_DATE})


@router.get("/contacts")
def contacts_page(request: Request, user: Viewer):
    return templates.TemplateResponse(request, "legal_contacts.html", {"fields": legal.FIELDS})


@router.get("/robots.txt")
def robots() -> Response:
    lines = ["User-agent: *", "Allow: /",
             *[f"Disallow: {path}" for path in ("/settings", "/log", "/my", "/matches",
                                                "/subscriptions", "/research", "/welcome",
                                                "/staff", "/export.xlsx", "/lang/")],
             "Disallow: /lots/*/research", "Disallow: /lots/*/questions",
             # Фильтры, сортировки и страницы поиска — бесконечное число адресов одного и
             # того же; в индекс идут сами закупки (sitemap) и чистые /lots, /announcements.
             "Disallow: /*?", "Allow: /*?lang=kk$", "Disallow: /balance", "Disallow: /pay/", "Disallow: /sources/",
             f"Sitemap: {SITE_URL}/sitemap.xml"]
    return Response("\n".join(lines) + "\n", media_type="text/plain")


SITEMAP_CHUNK = 20_000


# Лоты для sitemap: только те, что есть в объявлении на портале и не удалены — иначе
# страница отдаёт 404, а поисковик тратит на неё обход (так же отбирает лоты страница
# объявления). Подробности объявлений тяжёлые (секунды на запрос): список — раз в час.
SITEMAP_TTL = 3600
_sitemap_lots: dict[str, Any] = {"at": 0.0, "rows": []}
_sitemap_lock = threading.Lock()


def _live_lots() -> list[tuple[int, Any]]:
    with _sitemap_lock:
        if time.monotonic() - _sitemap_lots["at"] > SITEMAP_TTL:
            with core.pool.connection() as conn:
                _sitemap_lots["rows"] = conn.execute(
                    """SELECT l.id, l.last_update_date FROM announcements a
                       CROSS JOIN LATERAL jsonb_array_elements(
                           CASE WHEN jsonb_typeof(a.detail->'Lots') = 'array' THEN a.detail->'Lots' ELSE '[]' END) x
                       JOIN lots l ON l.id = (x->>'id')::bigint AND l.trd_buy_id = a.id
                       WHERE a.end_date > now() AND a.detail IS NOT NULL
                         AND NOT coalesce((x->>'isDeleted')::boolean, false)
                       ORDER BY l.id""").fetchall()
            _sitemap_lots["at"] = time.monotonic()
        return _sitemap_lots["rows"]


@router.get("/sitemap.xml")
def sitemap_index() -> Response:
    """Открытые объявления и лоты — для поисковиков. Индекс ведёт на части по 20 000."""
    with core.pool.connection() as conn:
        anns = conn.execute(
            "SELECT count(*) FROM announcements WHERE end_date > now() AND detail IS NOT NULL").fetchone()[0]
    lots_n = len(_live_lots())
    with core.pool.connection() as conn:
        orgs_n = len(orgs.sitemap_bins(conn))
    parts = [f"{SITE_URL}/sitemap-static.xml"]
    parts += [f"{SITE_URL}/sitemap-a-{i}.xml" for i in range((anns + SITEMAP_CHUNK - 1) // SITEMAP_CHUNK)]
    parts += [f"{SITE_URL}/sitemap-l-{i}.xml" for i in range((lots_n + SITEMAP_CHUNK - 1) // SITEMAP_CHUNK)]
    parts += [f"{SITE_URL}/sitemap-o-{i}.xml" for i in range((orgs_n + SITEMAP_CHUNK - 1) // SITEMAP_CHUNK)]
    body = "".join(f"<sitemap><loc>{url}</loc></sitemap>" for url in parts)
    return Response('<?xml version="1.0" encoding="UTF-8"?>'
                    f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</sitemapindex>',
                    media_type="application/xml")


@router.get("/sitemap-{kind}.xml")
def sitemap_part(kind: str) -> Response:
    if kind == "static":
        with core.pool.connection() as conn:
            catalog_paths = catalog.sitemap_paths(conn)
        urls = [(f"{SITE_URL}{path}", None) for path in (
            "/", "/announcements", "/lots", "/orgs", "/analytics", "/pricing", "/offer", "/privacy", "/contacts",
            *catalog_paths)]
    else:
        prefix, _, index = kind.partition("-")
        if prefix not in ("a", "l", "o") or not index.isdigit():
            raise HTTPException(404, "Нет такой части")
        offset = int(index) * SITEMAP_CHUNK
        with core.pool.connection() as conn:
            if prefix == "a":
                rows = conn.execute(
                    """SELECT id, last_update_date FROM announcements
                       WHERE end_date > now() AND detail IS NOT NULL ORDER BY id
                       LIMIT %s OFFSET %s""", (SITEMAP_CHUNK, offset)).fetchall()
                urls = [(f"{SITE_URL}/a/{i}", d) for i, d in rows]
            elif prefix == "o":
                bins = orgs.sitemap_bins(conn)[offset:offset + SITEMAP_CHUNK]
                urls = [(f"{SITE_URL}/org/{b}", None) for b in bins]
            else:
                rows = _live_lots()[offset:offset + SITEMAP_CHUNK]
                urls = [(f"{SITE_URL}/lots/{i}", d) for i, d in rows]
    body = "".join(f"<url><loc>{url}</loc>{f'<lastmod>{d.date().isoformat()}</lastmod>' if d else ''}</url>"
                   for url, d in urls)
    return Response('<?xml version="1.0" encoding="UTF-8"?>'
                    f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>',
                    media_type="application/xml")


@router.get("/analytics/trends")
def analytics_trends(days: int = 30):
    """Динамика рынка за период (вкладка «Динамика» в «Анализе рынка»): открыто всем, с кэшем."""
    return trends.cached(core.pool.connection, days)


@router.get("/analytics")
def analytics(request: Request, user: Viewer, background: BackgroundTasks):
    market, stale = dashboard.market_cached()
    if market is None:
        # Холодный кэш (после перезапуска): считает один запрос, остальные ждут его.
        with _market_lock:
            market, _ = dashboard.market_cached()
            if market is None:
                with core.pool.connection() as conn:
                    market = dashboard.market(conn)
    elif stale and not _market_lock.locked():
        # Показываем прежние цифры сразу, а пересчёт — после ответа, один за раз.
        background.add_task(_recount_market)
    context = {"m": market, "now": datetime.now(UTC)}
    return templates.TemplateResponse(request, "dashboard.html", context)


EVENTS_PER_HOUR = 120
_recent_events: dict[str, list[float]] = {}


def _event_allowed(visitor: str) -> bool:
    now = time.monotonic()
    if len(_recent_events) > 50_000:
        _recent_events.clear()
    recent = [t for t in _recent_events.get(visitor, []) if now - t < 3600]
    recent.append(now)
    _recent_events[visitor] = recent
    return len(recent) <= EVENTS_PER_HOUR


@router.post("/t/event", dependencies=[Depends(same_origin)])
async def track_event(request: Request):
    """События из браузера: окно «платная функция» (какая функция), смена вида и т.п."""
    body = await request.body()
    if len(body) > 2000:
        raise HTTPException(413)
    # Без метки посетителя и сверх лимита — молча не считаем: накрутить цифры дашборда
    # скриптом без браузера нельзя.
    visitor = request.cookies.get(visits.COOKIE, "")
    if not visitor or not _event_allowed(visitor):
        return Response(status_code=204)
    try:
        data = json.loads(body)
        name, props = str(data.get("name", "")), data.get("props") or {}
    except (ValueError, AttributeError):
        raise HTTPException(400) from None
    if name in visits.EVENT_NAMES and name != "search" and isinstance(props, dict):
        _track(request, name, {str(k)[:40]: str(v)[:200] for k, v in list(props.items())[:10]})
    return Response(status_code=204)


@router.get("/lots")
def lots_search(request: Request, user: Viewer, page: int = 1, per_page: int = PAGE_SIZE):
    form = parse_qs(request.url.query)
    current = Filters.from_form(form, staff=request.state.staff)
    page = min(max(page, 1), MAX_PAGE)
    per_page = _per_page(per_page)
    now = datetime.now(UTC)
    where, params = current.where(now, "lots")
    with core.pool.connection() as conn:
        rows = conn.execute(
            f"""SELECT l.id, l.lot_number, a.id, a.number_anno, a.name_ru,
                       coalesce(a.customer_name_ru, l.customer_name_ru, a.org_name_ru),
                       l.name_ru, coalesce(l.extra_desc, l.description_ru), l.count,
                       l.unit_name, l.price, l.amount, l.prepayment, a.trade_method_id,
                       l.status_id, a.end_date, coalesce(nullif(l.kato, '{{}}'), a.kato),
                       r.outcome, r.winner_name, r.winner_total,
                       jsonb_array_length(r.offers), r.reason,
                       l.name_kz, a.name_kz,
                       l.extra_desc_kz, l.unit_name_kz
                FROM lots l JOIN announcements a ON true
                LEFT JOIN lot_results r ON r.lot_id = l.id
                WHERE {where} ORDER BY {current.order_by("lots")}
                LIMIT {per_page} OFFSET {(page - 1) * per_page}""",
            params,
        ).fetchall()
        total = conn.execute(
            f"SELECT count(*) FROM lots l, announcements a WHERE {where}", params
        ).fetchone()[0]
        lot_marks = marks.of_user(conn, user, [row[0] for row in rows]) if user else {}
        stats = conn.execute(
            """SELECT (SELECT count(*) FROM announcements WHERE end_date > now()),
                      (SELECT count(*) FROM announcements
                       WHERE end_date > now() AND details_fetched_at IS NULL),
                      (SELECT max(updated_at) FROM sync_state)"""
        ).fetchone()
        groups = _user_groups(conn, user)
        editing = _editing(conn, user, form)
    _track_search(request, "lots", current, form, page, total)
    context = _search_context(request, current, "lots", total, page, form, now, per_page)
    context.update(rows=rows, marks=lot_marks, stats=stats, groups=groups, editing=editing)
    return templates.TemplateResponse(request, "lots.html", context)


@router.get("/announcements")
def announcements_search(request: Request, user: Viewer, page: int = 1, per_page: int = PAGE_SIZE):
    form = parse_qs(request.url.query)
    current = Filters.from_form(form, staff=request.state.staff)
    page = min(max(page, 1), MAX_PAGE)
    per_page = _per_page(per_page)
    now = datetime.now(UTC)
    where, params = current.where(now, "announcements")
    with core.pool.connection() as conn:
        rows = conn.execute(
            f"""SELECT a.id, a.number_anno,
                       coalesce(a.count_lots,
                                (SELECT count(*) FROM lots c WHERE c.trd_buy_id = a.id)),
                       a.name_ru, coalesce(a.org_name_ru, a.customer_name_ru),
                       a.trade_method_id, a.start_date, a.end_date, a.total_sum, a.status_id,
                       a.prepayment_max, a.detail->'RefBuyStatus'->>'nameRu',
                       a.detail->'RefTradeMethods'->>'nameRu',
                       -- Авансы лотов без повторов: 30 и 100, а не 30, 30, 100.
                       (SELECT array_agg(DISTINCT coalesce(p.prepayment, 0) ORDER BY coalesce(p.prepayment, 0))
                        FROM lots p WHERE p.trd_buy_id = a.id),
                       a.name_kz, a.detail->'RefTradeMethods'->>'nameKz',
                       a.detail->'RefBuyStatus'->>'nameKz', a.detail->'Organizer'->>'nameKz'
                FROM announcements a WHERE {where} ORDER BY {current.order_by("announcements")}
                LIMIT {per_page} OFFSET {(page - 1) * per_page}""",
            params,
        ).fetchall()
        total = conn.execute(
            f"SELECT count(*) FROM announcements a WHERE {where}", params
        ).fetchone()[0]
        groups = _user_groups(conn, user)
        editing = _editing(conn, user, form)
    _track_search(request, "announcements", current, form, page, total)
    context = _search_context(request, current, "announcements", total, page, form, now,
                              per_page)
    context.update(rows=rows, groups=groups, editing=editing)
    return templates.TemplateResponse(request, "announcements.html", context)


@router.get("/a/{announcement_id}")
def announcement(request: Request, user: Viewer, announcement_id: int, background: BackgroundTasks):
    context = _announcement_context(request, user, announcement_id, background)
    if user:
        _audit(request, user, "view_announcement", f"/a/{announcement_id}")
    return templates.TemplateResponse(request, "announcement.html", context)


@router.get("/lots/{lot_id}")
def lot_page(request: Request, user: Viewer, lot_id: int, background: BackgroundTasks):
    with core.pool.connection() as conn:
        row = conn.execute("SELECT trd_buy_id FROM lots WHERE id = %s", (lot_id,)).fetchone()
    if row is None or row[0] is None:
        raise HTTPException(404, "Такого лота нет в базе")
    context = _announcement_context(request, user, row[0], background)
    lot = next((lot for lot in context["lots"] if lot["id"] == lot_id), None)
    if lot is None:
        raise HTTPException(404, "Лот удалён из объявления")
    with core.pool.connection() as conn:
        # Результаты подбора — платная часть (тариф или сотрудник), у каждого клиента свои.
        job, done = _research_view(request, conn, user, lot_id) if user and request.state.paid else (None, None)
        lot_info = research.lot_context(conn, lot_id)
        provider = ai_config.provider(conn)
    if user:
        _audit(request, user, "view_lot", f"/lots/{lot_id}")
    context.update(lot=lot, others=[other for other in context["lots"] if other["id"] != lot_id],
                   here=request.url.path, job=job, done=done, lot_info=lot_info,
                   region=research.default_region(lot_info) if lot_info else "",
                   manual_links=research.manual_links(
                       lot_info, research.default_region(lot_info) if lot_info else ""),
                   provider_name=research.PROVIDERS.get(provider, provider),
                   targets=research.TARGETS,
                   is_admin=request.state.role == "admin")
    return templates.TemplateResponse(request, "lot.html", context)


_market_lock = threading.Lock()


def _recount_market() -> None:
    if not _market_lock.acquire(blocking=False):
        return
    try:
        with core.pool.connection() as conn:
            dashboard.market(conn)
    finally:
        _market_lock.release()


_refreshing: set[int] = set()


def _refresh_in_background(announcement_id: int, with_detail: bool, with_results: bool) -> None:
    """Перечитывает объявление с портала после ответа страницы: пользователь не ждёт
    портал, а следующий заход увидит свежие данные. Одно объявление — один раз."""
    if announcement_id in _refreshing:
        return
    _refreshing.add(announcement_id)
    try:
        with core.pool.connection() as conn:
            if with_detail:
                details.fetch_one(conn, core._goszakup(), announcement_id)
            if with_results:
                results.refresh(conn, core._goszakup(), announcement_id)
    except (GoszakupError, httpx.HTTPError, psycopg.Error) as error:
        logger.warning("background refresh of %s failed: %s", announcement_id, error)
    finally:
        _refreshing.discard(announcement_id)


def _subjects_in_background(bins: list[str]) -> None:
    try:
        with core.pool.connection() as conn:
            subjects.ensure(conn, core._goszakup(), *bins)
    except (GoszakupError, httpx.HTTPError, psycopg.Error) as error:
        logger.warning("background subjects %s failed: %s", bins, error)


def _announcement_context(request: Request, user: int | None, announcement_id: int,
                          background: BackgroundTasks) -> dict:
    """Всё для страницы объявления и лота. user=None — гость или поисковый робот:
    только данные из базы, портал не трогаем."""
    now = datetime.now(UTC)
    with core.pool.connection() as conn:
        detail, fetched_at, outcome, results_at = _load_announcement(
            conn, user, announcement_id, background, now)
        subscribed = _subscribed(conn, user, announcement_id)
    documents, protocols = _files(detail)
    lots = [lot for lot in detail.get("Lots") or [] if not lot.get("isDeleted")]
    info = {lot["id"]: details.parse_lot(lot) for lot in lots}
    extras = _lot_extras(request, user, detail, lots, info, background)
    text_phones = {lot["id"]: subjects.phones_in_text(lot.get("descriptionRu"),
                                                      info[lot["id"]].get("extra_desc"))
                   for lot in lots}
    # Сначала лоты с авансом побольше, дальше по номеру.
    lots.sort(key=lambda lot: (-(info[lot["id"]]["prepayment"] or 0), lot.get("lotNumber") or ""))
    return {
        "a": detail,
        "lots": lots,
        "info": info,
        "subscribed": subscribed,
        **extras,
        "contracts": (outcome or {}).get("contracts") or [],
        "contracts_by_lot": results.contracts_by_lot(outcome),
        "appeals": (outcome or {}).get("appeals") or [],
        "results_at": results_at,
        "now": now,
        "text_phones": text_phones,
        "lot_numbers": {lot["id"]: lot.get("lotNumber") for lot in lots},
        "here": request.url.path,
        "documents": documents,
        "protocols": protocols,
        "end": parse_date(detail.get("endDate")),
        "dt": parse_date,
        "fetched_at": fetched_at,
        "refresh_error": None,
        "spec_file": spec.spec_file,
        "portal_url": PORTAL_ANNOUNCEMENT_URL.format(announcement_id),
        "status_names": STATUS_NAMES,
    }


def _load_announcement(conn, user: int | None, announcement_id: int, background: BackgroundTasks,
                       now: datetime) -> tuple:
    """(подробности, когда загружены, итоги, когда проверены итоги). Если подробностей нет —
    загружаем с портала сразу (для вошедших); если устарели — обновим после ответа."""
    row = conn.execute(
        """SELECT detail, details_fetched_at, end_date, results, results_fetched_at
           FROM announcements WHERE id = %s""",
        (announcement_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Такого объявления нет в базе")
    detail, fetched_at, end_date, outcome, results_at = row
    stale = fetched_at is None or now - fetched_at > (
        DETAIL_TTL if end_date and end_date > now else timedelta(days=1)
    )
    ended = bool(end_date and end_date < now)
    if detail is None and user is None:
        raise HTTPException(404, "Подробности объявления ещё загружаются")
    if detail is None:
        # Подробностей ещё нет совсем (сборщик не дошёл) — тут без портала никак.
        try:
            details.fetch_one(conn, core._goszakup(), announcement_id)
            if ended:
                results.refresh(conn, core._goszakup(), announcement_id)
        except GoszakupError:
            conn.rollback()
        detail, fetched_at, outcome, results_at = conn.execute(
            """SELECT detail, details_fetched_at, results, results_fetched_at
               FROM announcements WHERE id = %s""",
            (announcement_id,),
        ).fetchone()
        if detail is None:
            raise HTTPException(502, "Портал сейчас не отвечает, попробуйте через минуту")
    elif user is not None and (stale or (ended and results.stale(results_at, now))):
        background.add_task(_refresh_in_background, announcement_id, stale,
                            ended and results.stale(results_at, now))
    return detail, fetched_at, outcome, results_at


def _subscribed(conn, user: int | None, announcement_id: int) -> dict[int, str]:
    """Лоты объявления, пришедшие по подпискам пользователя: {лот: названия подписок}."""
    return {
        r[0]: r[1]
        for r in conn.execute(
            """SELECT m.lot_id, string_agg(s.name, ', ') FROM matches m
               JOIN subscriptions s ON s.id = m.subscription_id
               JOIN lots l ON l.id = m.lot_id
               WHERE s.user_id = %s AND l.trd_buy_id = %s GROUP BY m.lot_id""",
            (user, announcement_id),
        )
    }


def _files(detail: dict) -> tuple[list, list]:
    """(документы, протоколы) объявления и его лотов, без повторов."""
    files, seen = [], set()
    for file in (detail.get("Files") or []) + [
        f for lot in detail.get("Lots") or [] for f in lot.get("Files") or []
    ]:
        if file.get("id") not in seen and file.get("filePath"):
            seen.add(file.get("id"))
            files.append(file)
    for file in detail.get("GeneralFiles") or []:
        if file.get("filePathRu"):
            files.append({"nameRu": file.get("nameRu"), "filePath": file["filePathRu"],
                          "originalName": None})
    protocols = [f for f in files if "протокол" in (f.get("nameRu") or "").lower()]
    return [f for f in files if f not in protocols], protocols


def _lot_extras(request: Request, user: int | None, detail: dict, lots: list, info: dict,
                background: BackgroundTasks) -> dict:
    """Из базы по лотам: техспецификации, выжимки (платно), отметки, итоги, карточки
    организаций (недостающие догружаются после ответа — только для вошедших)."""
    with core.pool.connection() as conn:
        specs = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT lot_id, text, error, text_kk FROM lot_specs WHERE lot_id = ANY(%s)",
            (list(info),))}
        # Готовые выжимки — тоже платная часть.
        summaries = {r[0]: {"status": r[1], "result": r[2], "error": r[3]} for r in conn.execute(
            "SELECT lot_id, status, result, error FROM spec_summaries WHERE lot_id = ANY(%s)",
            (list(info),))} if request.state.paid else {}
        lot_marks = marks.of_user(conn, user, list(info)) if user else {}
        lot_results = results.by_lot(conn, list(info))
        company_bin = (conn.execute(
            "SELECT company_bin FROM users WHERE id = %s", (user,)
        ).fetchone() or (None,))[0]
        org_bins = [detail.get("customerBin"), detail.get("orgBin")] + [
            lot.get("customerBin") for lot in lots]
        org_cards = {b: subjects.card(conn, b) for b in dict.fromkeys(filter(None, org_bins))}
    missing = [b for b, found in org_cards.items() if found is None]
    if missing and user is not None:
        # Карточки организации ещё нет — догрузим после ответа, следующий заход увидит.
        # Гостям и поисковым роботам — только база: обход сайта не должен бить по порталу.
        background.add_task(_subjects_in_background, missing)
    return {"specs": specs, "summaries": summaries, "marks": lot_marks, "results": lot_results,
            "company_bin": company_bin, "org_cards": org_cards}


@router.get("/a/{announcement_id}/lots/{lot_id}/spec")
def lot_spec(request: Request, viewer: Viewer, announcement_id: int, lot_id: int, refresh: int = 0):
    with core.pool.connection() as conn:
        row = conn.execute(
            "SELECT detail FROM announcements WHERE id = %s", (announcement_id,)
        ).fetchone()
        lot = next(
            (item for item in ((row[0] or {}).get("Lots") or []) if item.get("id") == lot_id), None
        ) if row else None
        url = spec.spec_file(lot) if lot else None
        if url is None:
            text, error = None, "У лота нет файла технической спецификации."
        else:
            if refresh and viewer:
                # Повтор после ошибки: прошлую ошибку из кэша не берём.
                conn.execute(
                    "DELETE FROM lot_specs WHERE lot_id = %s AND text IS NULL", (lot_id,)
                )
            if viewer:
                text, error = spec.get(conn, lot_id, url)
            else:
                # Гостям и роботам — только уже сохранённый текст, без скачивания с портала.
                cached = conn.execute("SELECT text FROM lot_specs WHERE lot_id = %s",
                                      (lot_id,)).fetchone()
                text = cached[0] if cached else None
                error = None if text else i18n.gettext("Текст ещё не загружен — откройте PDF.")
            if i18n.language() == "kk":
                kazakh = conn.execute(
                    "SELECT text_kk FROM lot_specs WHERE lot_id = %s", (lot_id,)
                ).fetchone()
                text = (kazakh and kazakh[0]) or text
        # Готовая выжимка — сразу в ответе (платная часть), иначе кнопка «Кратко».
        summary = research.summary_of(conn, lot_id) if text and request.state.paid else None
    return templates.TemplateResponse(
        request, "spec_fragment.html", {"text": text, "error": error, "url": url, "lot_id": lot_id, "s": summary}
    )
