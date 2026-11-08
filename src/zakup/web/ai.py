"""ИИ: подбор товаров под лот, выжимки техспецификаций, вопросы поставщику, источники."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from zakup import (
    auth,
    contacts,
    credits,
    i18n,
    questions,
    research,
    sources,
)
from zakup.web import core
from zakup.web.core import (
    Paid,
    _audit,
    same_origin,
    templates,
)

router = APIRouter()


def _research_view(request: Request, conn, user: int, lot_id: int) -> tuple[dict | None, dict | None]:
    """(последний запуск, готовый результат) по лоту. Клиент видит только свои поиски;
    сотрудники — все поиски по лоту одной таблицей без повторов."""
    if request.state.staff:
        return research.latest(conn, lot_id), research.merged_done(conn, lot_id)
    return research.latest(conn, lot_id, user), research.latest_done(conn, lot_id, user)


@router.post("/lots/{lot_id}/research", dependencies=[Depends(same_origin)])
def start_research(
    request: Request,
    user: Paid,
    lot_id: int,
    region: Annotated[str, Form()] = "",
    whole_kz: Annotated[str, Form()] = "",
    target: Annotated[int, Form()] = 10,
    more: Annotated[str, Form()] = "",
):
    with core.pool.connection() as conn:
        if research.lot_context(conn, lot_id) is None:
            raise HTTPException(404, "Нет данных лота: откройте страницу объявления")
        staff = request.state.staff
        try:
            research.enqueue(conn, user, lot_id, region, whole_kz == "1",
                             is_admin=staff, target=target, more=more == "1", charge=not staff)
            _audit(request, user, "research", f"/lots/{lot_id}", region=region[:120],
                   count=target, more=more == "1")
        except research.QuotaExceeded as error:
            raise HTTPException(429, f"Лимит поиска товаров: {error}") from None
        except credits.NoCredits:
            # Поисков на балансе нет — на страницу покупки.
            return RedirectResponse("/pricing?need=searches", status_code=303)
    return RedirectResponse(f"/lots/{lot_id}#research", status_code=303)


@router.post("/lots/{lot_id}/spec-summary", dependencies=[Depends(same_origin)])
def request_spec_summary(request: Request, user: Paid, lot_id: int):
    with core.pool.connection() as conn:
        if not conn.execute("SELECT 1 FROM lot_specs WHERE lot_id = %s AND text IS NOT NULL",
                            (lot_id,)).fetchone():
            raise HTTPException(404, "Нет текста техспецификации")
        status = research.request_summary(conn, lot_id)
    _audit(request, user, "spec_summary", f"/lots/{lot_id}")
    return {"status": status}


@router.get("/lots/{lot_id}/spec-summary")
def spec_summary(request: Request, _: Paid, lot_id: int):
    with core.pool.connection() as conn:
        summary = research.summary_of(conn, lot_id)
    return templates.TemplateResponse(request, "summary_fragment.html", {"s": summary})


@router.post("/lots/{lot_id}/questions", dependencies=[Depends(same_origin)])
def request_questions(request: Request, user: Paid, lot_id: int, url: Annotated[str, Form()]):
    with core.pool.connection() as conn:
        status = questions.request(conn, lot_id, url)
    if status is None:
        raise HTTPException(404, "Такого предложения нет в результатах подбора")
    _audit(request, user, "supplier_questions", f"/lots/{lot_id}", url=url[:300])
    return {"status": status}


@router.get("/lots/{lot_id}/questions")
def questions_fragment(request: Request, _: Paid, lot_id: int, url: str):
    with core.pool.connection() as conn:
        found = questions.of(conn, lot_id, url)
    return templates.TemplateResponse(request, "questions_fragment.html", {"q": found})


@router.get("/lots/{lot_id}/research")
def research_fragment(request: Request, user: Paid, lot_id: int):
    with core.pool.connection() as conn:
        job, done = _research_view(request, conn, user, lot_id)
        lot_info = research.lot_context(conn, lot_id)
    return templates.TemplateResponse(
        request, "research_fragment.html",
        {"job": job, "done": done, "lot_info": lot_info,
         "is_admin": request.state.role == "admin", "targets": research.TARGETS, "lot_id": lot_id},
    )


@router.get("/research")
def research_list(request: Request, user: Paid, q: str = "", sources_open: int = 0):
    """Сохранённые подборки товаров: последний готовый поиск по каждому лоту."""
    with core.pool.connection() as conn:
        rows = conn.execute(
            """SELECT DISTINCT ON (r.lot_id) r.lot_id, r.finished_at, r.region, r.result,
                      l.name_ru, l.name_kz, l.lot_number, l.amount, l.count,
                      coalesce(l.price, l.amount / nullif(l.count, 0)), l.prepayment,
                      a.id, a.number_anno, a.end_date
               FROM lot_research r JOIN lots l ON l.id = r.lot_id
               JOIN announcements a ON a.id = l.trd_buy_id
               WHERE r.status = 'done' AND (%(staff)s OR r.user_id = %(me)s)
                 AND (%(q)s = '' OR l.name_ru ILIKE %(like)s OR l.name_kz ILIKE %(like)s
                      OR a.number_anno ILIKE %(like)s)
               ORDER BY r.lot_id, r.id DESC""",
            {"q": q.strip(), "like": f"%{q.strip()}%", "staff": request.state.staff, "me": user},
        ).fetchall()
        running = conn.execute(
            """SELECT r.lot_id, r.status, r.base_id IS NOT NULL, l.name_ru, l.name_kz, r.created_at
               FROM lot_research r JOIN lots l ON l.id = r.lot_id
               WHERE r.status IN ('queued', 'running') AND (%s OR r.user_id = %s) ORDER BY r.id""",
            (request.state.staff, user),
        ).fetchall()
        active = len(running)
        source_rows = sources.listing(conn)
    items = []
    for row in rows:
        found = (row[3] or {}).get("items") or []
        price = float(row[9]) if row[9] else None
        # Маржа — по лучшему уровню соответствия, который есть: полностью → частично → аналоги.
        margins, margin_basis = [], None
        for fit in ("полностью", "частично", "аналог"):
            margins = [(price - i["unit_price"]) / price * 100 for i in found
                       if price and i.get("unit_price") and i.get("fit") == fit]
            if margins:
                margin_basis = fit
                break
        cheapest = min((i["unit_price"] for i in found if i.get("unit_price")), default=None)
        items.append({
            "lot_id": row[0], "finished_at": row[1], "region": row[2], "name": row[4],
            "name_kz": row[5], "lot_number": row[6], "amount": row[7], "price": price,
            "prepayment": row[10], "announcement_id": row[11], "number": row[12],
            "end_date": row[13], "found": len(found),
            "in_region": sum(1 for i in found if i.get("in_region")),
            "full_fit": sum(1 for i in found if i.get("fit") == "полностью"),
            "best_margin": max(margins) if margins else None, "cheapest": cheapest,
            "margin_basis": margin_basis,
            "partial_fit": sum(1 for i in found if i.get("fit") == "частично"),
        })
    items.sort(key=lambda i: i["finished_at"], reverse=True)
    return templates.TemplateResponse(
        request, "research_list.html",
        {"items": items, "q": q, "active": active, "running": running, "now": datetime.now(UTC),
         "sources": source_rows, "sources_open": sources_open, "me": user,
         "is_admin": request.state.role == "admin"},
    )


@router.post("/sources", dependencies=[Depends(same_origin)])
def add_source(
    request: Request,
    user: Paid,
    url: Annotated[str, Form()],
    name: Annotated[str, Form()] = "",
    categories: Annotated[str, Form()] = "",
    city: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
):
    with core.pool.connection() as conn:
        domain = sources.add(conn, user, url, name, categories, city, note)
    if domain is None:
        raise HTTPException(422, i18n.gettext("Укажите адрес сайта, например zeta.kz"))
    _audit(request, user, "source_add", domain)
    return RedirectResponse("/research?sources_open=1#sources", status_code=303)


@router.get("/sources/{source_id}/contacts")
def source_contacts(request: Request, _: Paid, source_id: int):
    with core.pool.connection() as conn:
        row = conn.execute("SELECT domain, name FROM sources WHERE id = %s", (source_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Нет такого источника")
        sellers = [(name, contacts.links(phones), checked)
                   for name, phones, checked in contacts.of_domain(conn, row[0])]
    return templates.TemplateResponse(request, "source_contacts.html",
                                      {"domain": row[0], "name": row[1], "sellers": sellers})


@router.post("/sources/{source_id}/toggle", dependencies=[Depends(same_origin)])
def toggle_source(request: Request, user: Paid, source_id: int):
    with core.pool.connection() as conn:
        row = conn.execute(
            """UPDATE sources SET active = NOT active
               WHERE id = %s AND (%s OR added_by = %s OR (auto AND %s)) RETURNING domain, active""",
            (source_id, request.state.role == "admin", user, request.state.role in auth.STAFF_ROLES),
        ).fetchone()
    if row:
        _audit(request, user, "source_toggle", row[0], active=row[1])
    return RedirectResponse("/research?sources_open=1#sources", status_code=303)
