"""Для сотрудников: дашборд платформы, журнал действий, каталог товаров из поиска."""

from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, HTTPException, Request

from zakup import (
    audit,
    insights,
    products,
)
from zakup.goszakup import PORTAL_TZ
from zakup.web import core
from zakup.web.core import (
    MAX_PAGE,
    Staff,
    templates,
)

router = APIRouter()


@router.get("/dashboard")
def dashboard_page(request: Request, staff: Staff):
    """Дашборд платформы — главная страница администратора и модераторов."""
    return templates.TemplateResponse(request, "platform.html", {"sections": insights.SECTIONS})


@router.get("/dashboard/data")
def dashboard_data(staff: Staff, section: str = "overview", days: int = 30):
    if section not in insights.SECTIONS:
        raise HTTPException(404)
    with core.pool.connection() as conn:
        return insights.section(conn, section, days)


@router.get("/products")
def products_page(request: Request, _: Staff, q: str = "", category: str = "", sort: str = "updated",
                  page: int = 1):
    """Каталог товаров, найденных поиском с ИИ (zakup.products) — пока только сотрудникам."""
    page = min(max(page, 1), MAX_PAGE)
    with core.pool.connection() as conn:
        data = products.listing(conn, q[:200], category[:2] if category.isdigit() else "", sort, page)
    return templates.TemplateResponse(request, "products.html", {
        "p": data, "sorts": products.SORTS, "fresh_days": products.FRESH_DAYS,
        "page_url": lambda **changes: "/products?" + urlencode(
            {k: v for k, v in {"q": data["q"], "category": data["division"], "sort": data["sort"],
                               "page": data["page"], **changes}.items() if v and not (k == "page" and v == 1)})})


LOG_PAGE_SIZES = (20, 50, 100)


@router.get("/log")
def action_log(request: Request, _: Staff, page: int = 1, per_page: int = 50):
    """Журнал действий: фильтр по пользователю, действию и датам, постранично."""
    query = parse_qs(request.url.query)
    users = [int(v) for v in query.get("user", []) if v.isdigit()]
    actions = [v for v in query.get("action", []) if v in audit.ACTIONS]
    date_from = (query.get("from") or [""])[0]
    date_to = (query.get("to") or [""])[0]
    per_page = per_page if per_page in LOG_PAGE_SIZES else 50
    page = min(max(page, 1), MAX_PAGE)
    clauses, params = ["true"], {}
    moderator = request.state.role != "admin"
    if moderator:
        # Модератор видит всех, кроме администраторов (в том числе их неудачные входы).
        clauses.append("""coalesce(u.role, (SELECT r.role FROM users r WHERE r.email = a.login), '')
                          <> 'admin'""")
    if users:
        clauses.append("a.user_id = ANY(%(users)s)")
        params["users"] = users
    if actions:
        clauses.append("a.action = ANY(%(actions)s)")
        params["actions"] = actions
    for name, value, op, shift in (("from", date_from, ">=", 0), ("to", date_to, "<", 1)):
        try:
            day = datetime.fromisoformat(value).date() if value else None
        except ValueError:
            day = None
        if day:
            clauses.append(f"a.at {op} %({name})s")
            params[name] = datetime.combine(day + timedelta(days=shift), datetime.min.time(),
                                            PORTAL_TZ)
    where = " AND ".join(clauses)
    with core.pool.connection() as conn:
        rows = conn.execute(
            f"""SELECT a.at, a.user_id, coalesce(u.email, a.login), a.action, a.target,
                       a.details, a.ip
                FROM user_actions a LEFT JOIN users u ON u.id = a.user_id
                WHERE {where} ORDER BY a.at DESC, a.id DESC
                LIMIT {per_page} OFFSET {(page - 1) * per_page}""",
            params,
        ).fetchall()
        total = conn.execute(
            f"""SELECT count(*) FROM user_actions a LEFT JOIN users u ON u.id = a.user_id
                WHERE {where}""", params).fetchone()[0]
        all_users = conn.execute(
            "SELECT id, email FROM users WHERE NOT (%s AND role = 'admin') ORDER BY email", (moderator,)
        ).fetchall()
    keep = [(k, v) for k, vs in query.items() if k not in ("page",) for v in vs]
    return templates.TemplateResponse(request, "log.html", {
        "rows": rows, "total": total, "page": page, "per_page": per_page,
        "pages": max((total + per_page - 1) // per_page, 1), "page_sizes": LOG_PAGE_SIZES,
        "users": all_users, "chosen_users": users, "chosen_actions": actions,
        "actions": audit.ACTIONS, "date_from": date_from, "date_to": date_to,
        "query": urlencode([(k, v) for k, v in keep if k != "per_page"]
                           + [("per_page", per_page)]),
    })
