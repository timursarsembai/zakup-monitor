"""Кабинет → «Мой магазин» (zakup.shop): магазин, свои товары с оптовыми ценами; сотрудникам —
модерация магазинов и заявок «Это мой магазин» (/staff/sellers). docs/CATALOG_PLAN.md, этап 3."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from zakup import catalog, products, sellers, shop
from zakup.web import core
from zakup.web.core import MAX_PAGE, Staff, User, _audit, same_origin, templates

router = APIRouter()
Field = Annotated[str, Form()]
Many = Annotated[list[str] | None, Form()]


def _shop_page(request: Request, conn, user: int, *, form: dict | None = None, error: str | None = None,
               saved: str = "", q: str = "", page: int = 1, status_code: int = 200):
    current = shop.get(conn, user)
    items = shop.listing(conn, current["id"], q[:200], min(max(page, 1), MAX_PAGE)) if current else None
    return templates.TemplateResponse(request, "shop.html", {
        "shop": current, "form": form or current or {}, "items": items, "error": error, "saved": saved,
        "product_path": products.path, "seller_path": sellers.path}, status_code=status_code)


@router.get("/my/shop")
def shop_page(request: Request, user: User, saved: str = "", q: str = "", page: int = 1):
    with core.pool.connection() as conn:
        return _shop_page(request, conn, user, saved=saved, q=q, page=page)


@router.post("/my/shop", dependencies=[Depends(same_origin)])
def shop_save(request: Request, user: User, name: Field = "", phone: Many = None, whatsapp: Field = "",
              email: Field = "", website: Field = "", city: Field = "", regions: Field = "",
              description: Field = "", bin: Field = "", prices_with_vat: Field = ""):
    raw = {"name": name, "phones": phone or [], "whatsapp": whatsapp, "email": email, "website": website,
           "city": city, "regions": regions, "description": description, "bin": bin,
           "prices_with_vat": prices_with_vat}
    with core.pool.connection() as conn:
        try:
            data = shop.parse_shop(**raw)
        except shop.Invalid as error:
            form = {**raw, "phones": [{"number": p} for p in raw["phones"]], "regions": regions.split(","),
                    "prices_with_vat": prices_with_vat == "1"}
            return _shop_page(request, conn, user, form=form, error=str(error), status_code=422)
        current = shop.get(conn, user)
        # Реестр — когда БИН новый или ещё не подтверждён (прошлый раз сеть могла подвести);
        # сеть — до наших изменений: соединение коммитится.
        registry = shop.registry_name(conn, data["bin"]) if data["bin"] and (
            current is None or current["bin"] != data["bin"] or not current["verified"]) else None
        row = shop.save(conn, user, data, registry)
    _audit(request, user, "shop_save", str(row["id"]), status=row["status"], verified=row["verified"])
    return RedirectResponse("/my/shop?saved=shop", status_code=303)


def _own_shop(conn, user: int) -> dict:
    current = shop.get(conn, user)
    if current is None:
        raise HTTPException(404, "Сначала создайте магазин")
    return current


def _product_form(request: Request, current: dict, item: dict, error: str | None = None, status_code: int = 200):
    return templates.TemplateResponse(request, "shop_product.html", {
        "shop": current, "item": item, "error": error, "units": shop.UNITS, "divisions": catalog.DIVISIONS,
        "max_tiers": shop.MAX_TIERS, "product_path": products.path}, status_code=status_code)


@router.get("/my/shop/products/new")
def product_new(request: Request, user: User):
    with core.pool.connection() as conn:
        current = _own_shop(conn, user)
    return _product_form(request, current, {"id": None, "name": "", "tiers": [], "specs": [], "available": True,
                                            "price_unit": "шт", "price": None, "division": None, "enstru": None,
                                            "description": None})


@router.get("/my/shop/products/{product_id}")
def product_edit(request: Request, user: User, product_id: int):
    with core.pool.connection() as conn:
        current = _own_shop(conn, user)
        item = shop.product(conn, current["id"], product_id)
    if item is None:
        raise HTTPException(404, "Нет такого товара")
    return _product_form(request, current, item)


@router.post("/my/shop/products", dependencies=[Depends(same_origin)])
def product_save(request: Request, user: User, product_id: Field = "", name: Field = "", unit: Field = "",
                 retail: Field = "", qty: Many = None, tier_price: Many = None, category: Field = "",
                 enstru: Field = "", description: Field = "", specs: Field = "", available: Field = ""):
    pid = int(product_id) if product_id.isdigit() else None
    with core.pool.connection() as conn:
        current = _own_shop(conn, user)
        try:
            data = shop.parse_product(name=name, unit=unit, retail=retail, quantities=qty or [],
                                      prices=tier_price or [], category=category, enstru=enstru,
                                      description=description, specs=specs, available=available)
            saved = shop.save_product(conn, current, data, pid)
        except shop.Invalid as error:
            conn.rollback()
            item = {"id": pid, "name": name, "price_unit": unit, "retail": retail, "division": category,
                    "enstru": enstru, "description": description, "specs_text": specs, "available": available == "1",
                    "tiers": [], "raw_tiers": list(zip(qty or [], tier_price or [], strict=False))}
            return _product_form(request, current, item, str(error), status_code=422)
        if saved is None:
            raise HTTPException(404, "Нет такого товара")
    _audit(request, user, "shop_product", str(saved), op="update" if pid else "create")
    return RedirectResponse("/my/shop?saved=product", status_code=303)


@router.post("/my/shop/products/{product_id}/sale", dependencies=[Depends(same_origin)])
def product_sale(request: Request, user: User, product_id: int, on: Field = ""):
    with core.pool.connection() as conn:
        current = _own_shop(conn, user)
        if not shop.set_on_sale(conn, current["id"], product_id, on == "1"):
            raise HTTPException(404, "Нет такого товара")
    _audit(request, user, "shop_product", str(product_id), op="on_sale" if on == "1" else "off_sale")
    return RedirectResponse("/my/shop", status_code=303)


@router.post("/my/shop/products/{product_id}/delete", dependencies=[Depends(same_origin)])
def product_delete(request: Request, user: User, product_id: int):
    with core.pool.connection() as conn:
        current = _own_shop(conn, user)
        if not shop.delete_product(conn, current["id"], product_id):
            raise HTTPException(404, "Нет такого товара")
    _audit(request, user, "shop_product", str(product_id), op="delete")
    return RedirectResponse("/my/shop?saved=deleted", status_code=303)


@router.get("/staff/sellers")
def moderation_page(request: Request, staff: Staff, done: str = ""):
    with core.pool.connection() as conn:
        data = shop.moderation(conn)
    return templates.TemplateResponse(request, "staff_sellers.html", {
        **data, "done": done, "seller_path": sellers.path})


@router.post("/staff/sellers/{seller_id}/status", dependencies=[Depends(same_origin)])
def moderation_status(request: Request, staff: Staff, seller_id: int, status: Field = ""):
    with core.pool.connection() as conn:
        if not shop.set_status(conn, seller_id, status):
            raise HTTPException(404, "Нет такого продавца")
    _audit(request, staff, "seller_moderation", str(seller_id), status=status)
    return RedirectResponse("/staff/sellers?done=status", status_code=303)


@router.post("/staff/claims/{claim_id}", dependencies=[Depends(same_origin)])
def moderation_claim(request: Request, staff: Staff, claim_id: int, decision: Field = ""):
    if decision not in ("approve", "reject"):
        raise HTTPException(422, "Нет такого решения")
    with core.pool.connection() as conn:
        result = shop.decide_claim(conn, claim_id, staff, decision == "approve")
    _audit(request, staff, "seller_moderation", f"claim:{claim_id}", result=result)
    return RedirectResponse(f"/staff/sellers?done={result}", status_code=303)
