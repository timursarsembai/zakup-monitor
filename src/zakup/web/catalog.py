"""Каталог товаров и продавцы: списки, страница товара и страница продавца — открыты всем и
поисковикам (решение владельца 2026-11-12, docs/CATALOG_PLAN.md). Лоты, где товар находили, —
по подписке; телефоны — вошедшим."""

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from zakup import i18n, products, sellers, shop
from zakup.web import core
from zakup.web.core import MAX_PAGE, SITE_URL, User, Viewer, _audit, same_origin, templates

router = APIRouter()


def _price(value: str) -> float | None:
    try:
        number = float(value.replace(" ", "").replace(",", ".")) if value else None
    except ValueError:
        return None
    return number if number is not None and 0 <= number < 1e13 else None


def _catalog_args(q: str, category: str, sort: str, page: int, price_min: str, price_max: str,
                  available: str) -> dict:
    return {"q": q[:200], "division": category[:2] if category.isdigit() else "", "sort": sort,
            "page": min(max(page, 1), MAX_PAGE), "price_min": _price(price_min), "price_max": _price(price_max),
            "available": available == "1"}


def _catalog_url(base: str, data: dict):
    def url(**changes) -> str:
        values = {"q": data["q"], "category": data["division"], "sort": data["sort"], "page": data["page"],
                  "price_min": data["price_min_text"], "price_max": data["price_max_text"],
                  "available": "1" if data["available"] else "", **changes}
        query = urlencode({k: v for k, v in values.items() if v and not (k == "page" and v == 1)})
        return base + ("?" + query if query else "")
    return url


@router.get("/products")
def products_page(request: Request, user: Viewer, q: str = "", category: str = "", sort: str = "updated",
                  page: int = 1, price_min: str = "", price_max: str = "", available: str = ""):
    """Каталог товаров: найденные поиском с ИИ (zakup.products), позже — и свои у продавцов."""
    with core.pool.connection() as conn:
        data = products.listing(conn, **_catalog_args(q, category, sort, page, price_min, price_max, available))
    return templates.TemplateResponse(request, "products.html", {
        "p": data, "sorts": products.SORTS, "fresh_days": products.FRESH_DAYS,
        "product_path": products.path, "seller_path": sellers.path,
        "products_url": _catalog_url("/products", data), "action": "/products"})


@router.get("/products/{key}")
def product_page(request: Request, user: Viewer, key: str):
    """Страница товара: /products/<номер>-<название латиницей>. Адрес держится на номере:
    другое название (товар переименовали, ссылку обрезали) — перенаправляем на верный."""
    with core.pool.connection() as conn:
        item = products.get(conn, _number_key(key))
    if item is None:
        raise HTTPException(404, "Нет такого товара")
    seller = item["seller_row"]
    # Снятый с продажи или у магазина не на виду (на проверке, скрыт) — только владельцу и сотрудникам.
    public = item["on_sale"] and (seller is None or seller["status"] == "active")
    if not public and not request.state.staff and not (seller and user and seller["user_id"] == user):
        raise HTTPException(404, "Нет такого товара")
    canonical = products.path(item)
    # Временная (302): адрес — от названия, а его обновляет каждая находка поиска; постоянную
    # браузер запомнит, и при смене названия туда-обратно — бесконечный круг. Поисковикам
    # верный адрес говорит <link rel="canonical"> на самой странице.
    if (moved := _redirect(request, canonical)) is not None:
        return moved
    return templates.TemplateResponse(request, "product.html", {
        "item": item, "fresh_days": products.FRESH_DAYS, "product_path": products.path,
        "seller_path": sellers.path,
        "structured": _structured(item, canonical)})


def _number_key(key: str) -> int:
    number = key.split("-", 1)[0]
    if not number.isdigit() or len(number) > 18:
        raise HTTPException(404, "Нет такой страницы")
    return int(number)


def _redirect(request: Request, canonical: str) -> RedirectResponse | None:
    """Неверное название в адресе — временно (302) на верный: см. product_page."""
    if request.url.path == canonical:
        return None
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(canonical + query, status_code=302)


@router.get("/sellers")
def sellers_page(request: Request, user: Viewer, q: str = "", city: str = "", category: str = "",
                 sort: str = "products", page: int = 1):
    """Продавцы: найденные поиском с ИИ и (этап 3) свои магазины пользователей."""
    page = min(max(page, 1), MAX_PAGE)
    with core.pool.connection() as conn:
        data = sellers.listing(conn, q[:200], city[:80], category[:2] if category.isdigit() else "", sort, page)

    def url(**changes) -> str:
        values = {"q": data["q"], "city": data["city"], "category": data["division"], "sort": data["sort"],
                  "page": data["page"], **changes}
        query = urlencode({k: v for k, v in values.items() if v and not (k == "page" and v == 1)})
        return "/sellers" + ("?" + query if query else "")
    return templates.TemplateResponse(request, "sellers.html", {
        "s": data, "sorts": sellers.SORTS, "seller_path": sellers.path, "sellers_url": url})


def _seller_page(request: Request, conn, seller: dict, user: int | None, *, q: str = "", category: str = "",
                 sort: str = "updated", page: int = 1, price_min: str = "", price_max: str = "",
                 available: str = "", claim_error: str | None = None, status_code: int = 200):
    """Страница продавца — и обычная, и с ошибкой в заявке «Это мой магазин»."""
    owner = user is not None and seller["user_id"] == user
    canonical = sellers.path(seller)
    data = products.listing(conn, **_catalog_args(q, category, sort, page, price_min, price_max, available),
                            seller_id=seller["id"], divisions=seller["divisions"],
                            hidden=request.state.staff or owner)
    return templates.TemplateResponse(request, "seller.html", {
        "owner": owner, "claimed": shop.pending_claim(conn, seller["id"], user), "claim_error": claim_error,
        "seller": seller, "p": data, "sorts": products.SORTS, "fresh_days": products.FRESH_DAYS,
        "product_path": products.path, "seller_path": sellers.path,
        "products_url": _catalog_url(canonical, data), "action": canonical}, status_code=status_code)


@router.get("/sellers/{key}")
def seller_page(request: Request, user: Viewer, key: str, q: str = "", category: str = "", sort: str = "updated",
                page: int = 1, price_min: str = "", price_max: str = "", available: str = ""):
    """Страница продавца: карточка и его каталог с фильтрами. Не на виду (на проверке, скрыт) —
    только владельцу и сотрудникам."""
    with core.pool.connection() as conn:
        seller = sellers.get(conn, _number_key(key))
        owner = seller is not None and user is not None and seller["user_id"] == user
        if seller is None or (seller["status"] != "active" and not request.state.staff and not owner):
            raise HTTPException(404, "Нет такого продавца")
        if (moved := _redirect(request, sellers.path(seller))) is not None:
            return moved
        return _seller_page(request, conn, seller, user, q=q, category=category, sort=sort, page=page,
                            price_min=price_min, price_max=price_max, available=available)


@router.post("/sellers/{key}/claim", dependencies=[Depends(same_origin)])
def seller_claim(request: Request, user: User, key: str, bin: Annotated[str, Form()] = "",
                 comment: Annotated[str, Form()] = ""):
    """«Это мой магазин»: заявка на найденный ИИ магазин — решает сотрудник (/staff/sellers)."""
    with core.pool.connection() as conn:
        seller = sellers.get(conn, _number_key(key))
        if seller is None or seller["status"] != "active":
            raise HTTPException(404, "Нет такого продавца")
        try:
            ok = shop.claim(conn, seller["id"], user, bin, comment)
        except shop.Invalid as error:
            return _seller_page(request, conn, seller, user, claim_error=str(error), status_code=422)
    if ok:
        _audit(request, user, "seller_claim", str(seller["id"]))
    return RedirectResponse(sellers.path(seller) + "?claimed=1", status_code=303)


def _url(path: str) -> str:
    """Полный адрес на языке страницы — как canonical (base.html)."""
    return SITE_URL + path + ("?lang=kk" if i18n.language() == "kk" else "")


def _structured(item: dict, path: str) -> list[dict]:
    """Разметка schema.org для поисковиков: хлебные крошки и товар с ценой (Product/Offer)."""
    url = _url(path)
    product: dict = {"@context": "https://schema.org", "@type": "Product", "name": item["name"], "url": url}
    if item.get("image"):
        product["image"] = item["image"]
    if item.get("specs"):
        product["additionalProperty"] = [
            {"@type": "PropertyValue", "name": str(s.get("name")), "value": str(s.get("value"))}
            for s in item["specs"][:20] if isinstance(s, dict) and s.get("name")]
    if item.get("price") is not None:
        offer: dict = {"@type": "Offer", "price": f"{item['price']:.2f}", "priceCurrency": "KZT",
                       "availability": "https://schema.org/" + ("InStock" if item["available"] else "OutOfStock"),
                       "url": item["url"] or url}
        if item.get("seller"):
            offer["seller"] = {"@type": "Organization", "name": item["seller"]}
        product["offers"] = offer
    crumbs = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": i18n.gettext("Каталог товаров"), "item": _url("/products")},
        {"@type": "ListItem", "position": 2, "name": item["name"], "item": url}]}
    return [crumbs, product]
