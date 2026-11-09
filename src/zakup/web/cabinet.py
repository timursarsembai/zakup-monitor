"""Кабинет клиента: избранное, мои лоты, подписки, лента совпадений, выгрузка в Excel."""

import io
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from psycopg.types.json import Jsonb

from zakup import (
    SITE_URL,
    marks,
    matcher,
)
from zakup.filters import (
    Filters,
)
from zakup.goszakup import PORTAL_TZ
from zakup.web import core
from zakup.web.core import (
    Paid,
    _amount,
    _audit,
    _safe_next,
    _user_groups,
    same_origin,
    templates,
)

router = APIRouter()


EXPORT_LIMIT = 10_000


@router.get("/export.xlsx")
def export(request: Request, user: Paid):
    from openpyxl import Workbook

    _audit(request, user, "export", "/lots?" + request.url.query)
    current = Filters.from_form(parse_qs(request.url.query), staff=request.state.staff)
    where, params = current.where(datetime.now(UTC), "lots")
    with core.pool.connection() as conn:
        rows = conn.execute(
            f"""SELECT a.number_anno, a.name_ru, coalesce(a.customer_name_ru, l.customer_name_ru),
                       a.detail->'RefBuyStatus'->>'nameRu', a.detail->'RefTradeMethods'->>'nameRu',
                       a.end_date, l.lot_number, l.name_ru, coalesce(l.extra_desc, l.description_ru),
                       l.count, l.unit_name, l.price, l.amount, l.prepayment,
                       coalesce(nullif(l.kato, '{{}}'), a.kato), r.winner_name, r.winner_total,
                       a.id, l.id
                FROM lots l JOIN announcements a ON true
                LEFT JOIN lot_results r ON r.lot_id = l.id
                WHERE {where}
                ORDER BY {current.order_by("lots")} LIMIT {EXPORT_LIMIT}""",
            params,
        ).fetchall()
    book = Workbook()
    sheet = book.active
    sheet.title = "Лоты"
    sheet.append(["Объявление", "Название объявления", "Заказчик", "Статус", "Способ",
                  "Окончание приёма", "Лот", "Наименование лота", "Описание", "Кол-во",
                  "Ед.", "Цена за ед., ₸", "Сумма, ₸", "Аванс, %", "Место поставки",
                  "Победитель", "Сумма победителя, ₸", "Ссылка"])
    for row in rows:
        end = row[5].astimezone(PORTAL_TZ).replace(tzinfo=None) if row[5] else None
        sheet.append([row[0], row[1], row[2], row[3], row[4], end, row[6], row[7], row[8],
                      row[9], row[10], row[11], row[12], row[13],
                      ", ".join(templates.env.globals["region_names"](row[14])), row[15], row[16],
                      f"{SITE_URL}/lots/{row[18]}"])
        # Названия с портала пишет кто угодно: «=HYPERLINK(…)» Excel выполнил бы как
        # формулу. Такие ячейки — простым текстом.
        for cell in sheet[sheet.max_row]:
            if cell.data_type == "f":
                cell.data_type = "s"
    for column, width in zip("ABCDEFGHIJKLMNOPQR", (14, 50, 40, 22, 22, 17, 16, 40, 50, 9, 9,
                                                   14, 15, 9, 24, 36, 16, 40), strict=True):
        sheet.column_dimensions[column].width = width
    for cell in sheet["F"][1:]:
        cell.number_format = "dd.mm.yyyy hh:mm"
    sheet.freeze_panes = "A2"
    body = io.BytesIO()
    book.save(body)
    name = f"zakup-{datetime.now(PORTAL_TZ):%Y-%m-%d-%H%M}.xlsx"
    return Response(
        body.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/lots/{lot_id}/mark", dependencies=[Depends(same_origin)])
def mark_lot(
    request: Request,
    user: Paid,
    lot_id: int,
    status: Annotated[str, Form()] = "",
    note: Annotated[str | None, Form()] = None,
    next: Annotated[str, Form()] = "/my",
):
    if status not in ("", "interested"):
        raise HTTPException(422, "неизвестная отметка")
    with core.pool.connection() as conn:
        if not conn.execute("SELECT 1 FROM lots WHERE id = %s", (lot_id,)).fetchone():
            raise HTTPException(404, "Такого лота нет в базе")
        if note is not None:
            marks.set_note(conn, user, lot_id, note)
        else:
            marks.set_mark(conn, user, lot_id, status)
    if note is not None:
        _audit(request, user, "note", f"/lots/{lot_id}", text=note.strip()[:200])
    else:
        _audit(request, user, "mark", f"/lots/{lot_id}", status=status or "—")
    target = _safe_next(next)
    return RedirectResponse(f"{target}#lot-{lot_id}" if "#" not in target else target, status_code=303)


@router.get("/my")
def my_lots(request: Request, user: Paid):
    with core.pool.connection() as conn:
        rows = conn.execute(
            """SELECT m.status, m.note, m.status_at, l.id, l.trd_buy_id, l.name_ru,
                      coalesce(l.extra_desc, l.description_ru), l.amount, l.prepayment,
                      coalesce(a.customer_name_ru, l.customer_name_ru), a.end_date,
                      a.number_anno, l.lot_number,
                      (SELECT lot->'RefLotsStatus'->>'nameRu'
                       FROM jsonb_array_elements(
                           CASE WHEN jsonb_typeof(a.detail->'Lots') = 'array'
                                THEN a.detail->'Lots' ELSE '[]' END) lot
                       WHERE (lot->>'id')::bigint = l.id),
                      a.detail->'RefBuyStatus'->>'nameRu',
                      r.outcome, r.winner_name, r.winner_total, r.reason,
                      r.winner_bin = u.company_bin
               FROM lot_marks m
               JOIN users u ON u.id = m.user_id
               JOIN lots l ON l.id = m.lot_id
               LEFT JOIN announcements a ON a.id = l.trd_buy_id
               LEFT JOIN lot_results r ON r.lot_id = l.id
               WHERE m.user_id = %s AND m.status <> ''
               ORDER BY a.end_date NULLS LAST, l.trd_buy_id, l.id""",
            (user,),
        ).fetchall()
    now = datetime.now(UTC)
    # Сначала открытые — по сроку, потом завершённые.
    rows.sort(key=lambda r: (r[10] is None or r[10] < now, r[10] or now))
    return templates.TemplateResponse(
        request, "my.html",
        {"rows": rows, "now": now, "here": request.url.path},
    )


@router.post("/subscriptions/filter", dependencies=[Depends(same_origin)])
async def create_filter_subscription(request: Request, user: Paid):
    form = await request.form()
    data = {key: form.getlist(key) for key in form.keys()}
    current = Filters.from_form(data)
    if not current.relative():
        raise HTTPException(
            422, "Подписка работает только без конкретных дат: срок — «в течение 2 рабочих дней» "
                 "или без ограничения."
        )
    scope = "announcements" if form.get("scope") == "announcements" else "lots"
    name = (form.get("name") or "").strip() or current.describe()[:100] or "Мой фильтр"
    with core.pool.connection() as conn:
        chat_id = _own_chat(conn, user, form.get("chat_id") or "")
        sub_id = conn.execute(
            """INSERT INTO subscriptions (user_id, name, query, kind, filters, scope, chat_id)
               VALUES (%s, %s, '', 'announcements', %s, %s, %s) RETURNING id""",
            (user, name[:100], Jsonb(current.to_json()), scope, chat_id),
        ).fetchone()[0]
        # Только новое; уже открытое — одним сообщением, если попросили.
        matcher.seed(conn, sub_id, current, scope, send_open=form.get("send_open") == "1")
    _audit(request, user, "subscription_create", name[:100], scope=scope,
           to="group" if chat_id else "personal", conditions=current.describe()[:300])
    return RedirectResponse("/subscriptions", status_code=303)


@router.get("/subscriptions/{sub_id}/edit")
def edit_subscription(user: Paid, sub_id: int):
    with core.pool.connection() as conn:
        row = conn.execute(
            "SELECT filters, scope FROM subscriptions WHERE id = %s AND user_id = %s AND kind = 'announcements'",
            (sub_id, user),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Нет такой подписки")
    return RedirectResponse(f"{Filters.from_json(row[0]).to_query(row[1])}&edit={sub_id}", status_code=303)


@router.post("/subscriptions/{sub_id}/update", dependencies=[Depends(same_origin)])
async def update_subscription(request: Request, user: Paid, sub_id: int):
    """Сохраняет изменённые условия, название и получателя подписки."""
    form = await request.form()
    data = {key: form.getlist(key) for key in form.keys()}
    current = Filters.from_form(data)
    if not current.relative():
        raise HTTPException(
            422, "Подписка работает только без конкретных дат: срок — «в течение 2 рабочих дней» "
                 "или без ограничения."
        )
    scope = "announcements" if form.get("scope") == "announcements" else "lots"
    name = (form.get("name") or "").strip() or current.describe()[:100] or "Мой фильтр"
    with core.pool.connection() as conn:
        chat_id = _own_chat(conn, user, form.get("chat_id") or "")
        updated = conn.execute(
            """UPDATE subscriptions SET name = %s, filters = %s, scope = %s, chat_id = %s
               WHERE id = %s AND user_id = %s AND kind = 'announcements' RETURNING id""",
            (name[:100], Jsonb(current.to_json()), scope, chat_id, sub_id, user),
        ).fetchone()
        if updated is None:
            raise HTTPException(404, "Нет такой подписки")
        # Под новые условия может подойти много старого — шлём только новое (или по просьбе).
        matcher.seed(conn, sub_id, current, scope, send_open=form.get("send_open") == "1")
    _audit(request, user, "subscription_update", name[:100], scope=scope,
           conditions=current.describe()[:300])
    return RedirectResponse("/subscriptions", status_code=303)


def _own_chat(conn, user: int, chat_id: str) -> str | None:
    """Группа, в которую пользователь вправе слать; пусто — личный чат."""
    if not chat_id:
        return None
    if chat_id not in {group[0] for group in _user_groups(conn, user)}:
        raise HTTPException(403, "Эта группа не подключена к вашему аккаунту")
    return chat_id


@router.post("/subscriptions/{sub_id}/chat", dependencies=[Depends(same_origin)])
def subscription_chat(request: Request, user: Paid, sub_id: int,
                      chat_id: Annotated[str, Form()] = ""):
    with core.pool.connection() as conn:
        name = conn.execute(
            "UPDATE subscriptions SET chat_id = %s WHERE id = %s AND user_id = %s RETURNING name",
            (_own_chat(conn, user, chat_id), sub_id, user),
        ).fetchone()
    if name:
        _audit(request, user, "subscription_chat", name[0], to="group" if chat_id else "personal")
    return RedirectResponse("/subscriptions", status_code=303)


@router.get("/subscriptions")
def subscriptions(request: Request, user: Paid):
    with core.pool.connection() as conn:
        rows = conn.execute(
            """SELECT s.id, s.name, s.query, s.min_amount, s.max_amount, s.active,
                      (SELECT count(*) FROM matches m WHERE m.subscription_id = s.id),
                      s.kind, s.filters, s.scope, s.chat_id
               FROM subscriptions s WHERE s.user_id = %s ORDER BY s.id""",
            (user,),
        ).fetchall()
        groups = _user_groups(conn, user)
    filters = {r[0]: Filters.from_json(r[8]) for r in rows if r[7] == "announcements"}
    return templates.TemplateResponse(
        request, "subscriptions.html",
        {"rows": rows, "filters": filters, "groups": groups},
    )


@router.post("/subscriptions", dependencies=[Depends(same_origin)])
def create_subscription(
    request: Request,
    user: Paid,
    name: Annotated[str, Form()],
    query: Annotated[str, Form()],
    min_amount: Annotated[str, Form()] = "",
    max_amount: Annotated[str, Form()] = "",
):
    if not name.strip() or not query.strip():
        raise HTTPException(422, "нужны название и запрос")
    with core.pool.connection() as conn:
        conn.execute(
            """INSERT INTO subscriptions (user_id, name, query, min_amount, max_amount,
                                          matched_up_to)
               VALUES (%s, %s, %s, %s, %s,
                       (SELECT coalesce(max(seen_seq), 0) FROM lots))""",
            (user, name.strip(), query.strip(), _amount(min_amount), _amount(max_amount)),
        )
    _audit(request, user, "subscription_create", name.strip()[:100], words=query.strip()[:200])
    return RedirectResponse("/subscriptions", status_code=303)


@router.post("/subscriptions/{sub_id}/toggle", dependencies=[Depends(same_origin)])
def toggle_subscription(request: Request, user: Paid, sub_id: int):
    with core.pool.connection() as conn:
        row = conn.execute(
            """UPDATE subscriptions SET active = NOT active WHERE id = %s AND user_id = %s
               RETURNING name, active""",
            (sub_id, user),
        ).fetchone()
    if row:
        _audit(request, user, "subscription_toggle", row[0], active=row[1])
    return RedirectResponse("/subscriptions", status_code=303)


@router.post("/subscriptions/{sub_id}/delete", dependencies=[Depends(same_origin)])
def delete_subscription(request: Request, user: Paid, sub_id: int):
    with core.pool.connection() as conn:
        row = conn.execute(
            "DELETE FROM subscriptions WHERE id = %s AND user_id = %s RETURNING name",
            (sub_id, user),
        ).fetchone()
    if row:
        _audit(request, user, "subscription_delete", row[0])
    return RedirectResponse("/subscriptions", status_code=303)


@router.get("/matches")
def matches(request: Request, user: Paid):
    query = parse_qs(request.url.query)
    chosen = [int(v) for v in query.get("sub", []) if v.isdigit()]
    with core.pool.connection() as conn:
        subs = conn.execute(
            """SELECT s.id, s.name, s.active,
                      (SELECT count(*) FROM matches m WHERE m.subscription_id = s.id)
               FROM subscriptions s WHERE s.user_id = %s ORDER BY s.id""",
            (user,),
        ).fetchall()
        # Первый заход — первая подписка; сняты все галочки — пусто.
        if "filtered" not in query and not chosen and subs:
            chosen = [subs[0][0]]
        rows = conn.execute(
            """SELECT m.matched_at, s.name, l.trd_buy_id, l.name_ru, l.amount,
                      coalesce(a.customer_name_ru, l.customer_name_ru), a.end_date, l.prepayment,
                      coalesce(l.extra_desc, l.description_ru), a.number_anno
               FROM matches m
               JOIN subscriptions s ON s.id = m.subscription_id
               JOIN lots l ON l.id = m.lot_id
               LEFT JOIN announcements a ON a.id = l.trd_buy_id
               WHERE s.user_id = %(user)s
                 AND s.id = ANY(%(chosen)s)
               ORDER BY m.matched_at DESC, a.end_date, l.trd_buy_id, l.id LIMIT 300""",
            {"user": user, "chosen": chosen},
        ).fetchall()
    return templates.TemplateResponse(
        request, "matches.html", {"rows": rows, "subs": subs, "chosen": chosen}
    )
