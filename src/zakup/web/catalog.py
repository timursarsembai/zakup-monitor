"""Каталог товаров: список и страница товара — открыты всем и поисковикам (решение
владельца 2026-11-12, docs/CATALOG_PLAN.md). Лоты, где товар находили, — по подписке."""

from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from zakup import i18n, products
from zakup.web import core
from zakup.web.core import MAX_PAGE, SITE_URL, Viewer, templates

router = APIRouter()


@router.get("/products")
def products_page(request: Request, user: Viewer, q: str = "", category: str = "", sort: str = "updated",
                  page: int = 1):
    """Каталог товаров, найденных поиском с ИИ (zakup.products)."""
    page = min(max(page, 1), MAX_PAGE)
    with core.pool.connection() as conn:
        data = products.listing(conn, q[:200], category[:2] if category.isdigit() else "", sort, page)
    return templates.TemplateResponse(request, "products.html", {
        "p": data, "sorts": products.SORTS, "fresh_days": products.FRESH_DAYS,
        "product_path": products.path,
        "products_url": lambda **changes: "/products?" + urlencode(
            {k: v for k, v in {"q": data["q"], "category": data["division"], "sort": data["sort"],
                               "page": data["page"], **changes}.items() if v and not (k == "page" and v == 1)})})


@router.get("/products/{key}")
def product_page(request: Request, user: Viewer, key: str):
    """Страница товара: /products/<номер>-<название латиницей>. Адрес держится на номере:
    другое название (товар переименовали, ссылку обрезали) — перенаправляем на верный."""
    number = key.split("-", 1)[0]
    if not number.isdigit() or len(number) > 18:
        raise HTTPException(404, "Нет такого товара")
    with core.pool.connection() as conn:
        item = products.get(conn, int(number))
    if item is None:
        raise HTTPException(404, "Нет такого товара")
    canonical = products.path(item)
    if request.url.path != canonical:
        query = f"?{request.url.query}" if request.url.query else ""
        # Временная (302): адрес — от названия, а его обновляет каждая находка поиска; постоянную
        # браузер запомнит, и при смене названия туда-обратно — бесконечный круг. Поисковикам
        # верный адрес говорит <link rel="canonical"> на самой странице.
        return RedirectResponse(canonical + query, status_code=302)
    return templates.TemplateResponse(request, "product.html", {
        "item": item, "fresh_days": products.FRESH_DAYS, "product_path": products.path,
        "structured": _structured(item, canonical)})


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
                       "url": item["url"]}
        if item.get("seller"):
            offer["seller"] = {"@type": "Organization", "name": item["seller"]}
        product["offers"] = offer
    crumbs = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": i18n.gettext("Каталог товаров"), "item": _url("/products")},
        {"@type": "ListItem", "position": 2, "name": item["name"], "item": url}]}
    return [crumbs, product]
