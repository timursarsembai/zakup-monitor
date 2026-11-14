"""Кабинет → «Мой магазин» (zakup.shop): магазин, свои товары с оптовыми ценами; сотрудникам —
модерация магазинов и заявок «Это мой магазин» (/staff/sellers). docs/CATALOG_PLAN.md, этап 3."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, Response

from zakup import catalog, photos, products, reports, sellers, shop, shop_import
from zakup.web import core
from zakup.web.core import MAX_PAGE, Staff, User, _audit, same_origin, templates

router = APIRouter()
Field = Annotated[str, Form()]
Many = Annotated[list[str] | None, Form()]


def _shop_page(request: Request, conn, user: int, *, form: dict | None = None, error: str | None = None,
               saved: str = "", q: str = "", page: int = 1, status_code: int = 200):
    current = shop.get(conn, user)
    telegram = conn.execute("SELECT telegram_chat_id IS NOT NULL FROM users WHERE id = %s", (user,)).fetchone()[0]
    items = shop.listing(conn, current["id"], q[:200], min(max(page, 1), MAX_PAGE)) if current else None
    return templates.TemplateResponse(request, "shop.html", {
        "shop": current, "form": form or current or {}, "items": items, "error": error, "saved": saved,
        "product_path": products.path, "seller_path": sellers.path, "telegram": telegram},
        status_code=status_code)


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
        registry = shop.registry_name(conn, data["bin"]) if (
            current is None or current["bin"] != data["bin"] or not current["verified"]) else None
        row = shop.save(conn, user, data, registry)
    _audit(request, user, "shop_save", str(row["id"]), status=row["status"], verified=row["verified"])
    return RedirectResponse("/my/shop?saved=shop", status_code=303)


@router.post("/my/shop/alerts", dependencies=[Depends(same_origin)])
def shop_alerts(request: Request, user: User, on: Field = ""):
    """Оповещения «новый лот на ваш товар» в Telegram (zakup.offers.alerts) — включить/выключить."""
    with core.pool.connection() as conn:
        if conn.execute("UPDATE sellers SET alerts = %s WHERE user_id = %s", (on == "1", user)).rowcount != 1:
            raise HTTPException(404, "Сначала создайте магазин")
    _audit(request, user, "shop_save", "alerts", alerts=on == "1")
    return RedirectResponse("/my/shop?saved=alerts", status_code=303)


def _own_shop(conn, user: int) -> dict:
    current = shop.get(conn, user)
    if current is None:
        raise HTTPException(404, "Сначала создайте магазин")
    return current


def _product_form(request: Request, current: dict, item: dict, error: str | None = None, status_code: int = 200,
                  photo_list: list | None = None, photo_error: str | None = None):
    return templates.TemplateResponse(request, "shop_product.html", {
        "shop": current, "item": item, "error": error, "units": shop.UNITS, "divisions": catalog.DIVISIONS,
        "max_tiers": shop.MAX_TIERS, "product_path": products.path, "photos": photo_list or [],
        "photo_error": photo_error, "max_photos": photos.MAX_PER_PRODUCT}, status_code=status_code)


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
        photo_list = photos.listing(conn, product_id)
    return _product_form(request, current, item, photo_list=photo_list)


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
        keys = shop.delete_product(conn, current["id"], product_id)
        if keys is None:
            raise HTTPException(404, "Нет такого товара")
    photos.remove(keys)  # файлы — после коммита: откат не оставит товар без фото
    _audit(request, user, "shop_product", str(product_id), op="delete")
    return RedirectResponse("/my/shop?saved=deleted", status_code=303)


def _own_product(conn, user: int, product_id: int) -> tuple[dict, dict]:
    current = _own_shop(conn, user)
    item = shop.product(conn, current["id"], product_id)
    if item is None:
        raise HTTPException(404, "Нет такого товара")
    return current, item


def _convert_all(contents: list[bytes]) -> list[tuple | str]:
    """Сжатие — до соединения с базой (секунды на фото): сжатое или текст ошибки."""
    done: list[tuple | str] = []
    for content in contents:
        try:
            done.append(photos.convert(content))
        except ValueError as error:
            done.append(str(error))
    return done


def _save_photos(request: Request, user: int, product_id: int, converted: list[tuple | str]):
    """Сохраняет сжатые фото; не подошедшие — сообщением на форме товара."""
    errors = [c for c in converted if isinstance(c, str)]
    saved = 0
    with core.pool.connection() as conn:
        current, item = _own_product(conn, user, product_id)
        for c in converted:
            if isinstance(c, str):
                continue
            try:
                photos.add(conn, product_id, c)
                saved += 1
            except ValueError as error:
                errors.append(str(error))
        conn.commit()
        if saved:
            _audit(request, user, "shop_product", str(product_id), op="photos", count=saved)
        if not errors:
            return None
        return _product_form(request, current, item, photo_list=photos.listing(conn, product_id),
                             photo_error=errors[0], status_code=422)


@router.post("/my/shop/products/{product_id}/photos", dependencies=[Depends(same_origin)])
async def photos_upload(request: Request, user: User, product_id: int):
    """Загрузка фото товара (несколько сразу). Предел запроса шлюза на этом адресе — больше
    обычного (deploy/gateway/default.conf); каждый файл — не больше photos.MAX_BYTES."""
    with core.pool.connection() as conn:
        _own_product(conn, user, product_id)  # чужой товар — 404 до разбора файлов
    form = await request.form()
    uploads = [f for f in form.getlist("files") if hasattr(f, "read")][:photos.MAX_PER_PRODUCT]
    contents = [await f.read(photos.MAX_BYTES + 1) for f in uploads]
    contents = [c for c in contents if c] or [b""]
    # Сжатие картинок и база — не в цикле событий; сжатие — без соединения с базой.
    converted = await run_in_threadpool(_convert_all, contents)
    failed = await run_in_threadpool(_save_photos, request, user, product_id, converted)
    if failed is not None:
        return failed
    return RedirectResponse(f"/my/shop/products/{product_id}#photos", status_code=303)


@router.post("/my/shop/products/{product_id}/photos/{photo_id}/delete", dependencies=[Depends(same_origin)])
def photo_delete(request: Request, user: User, product_id: int, photo_id: int):
    with core.pool.connection() as conn:
        _own_product(conn, user, product_id)
        key = photos.delete(conn, product_id, photo_id)
        if key is None:
            raise HTTPException(404, "Нет такого фото")
    photos.remove([key])
    _audit(request, user, "shop_product", str(product_id), op="photo_delete")
    return RedirectResponse(f"/my/shop/products/{product_id}#photos", status_code=303)


@router.post("/my/shop/products/{product_id}/photos/{photo_id}/main", dependencies=[Depends(same_origin)])
def photo_main(request: Request, user: User, product_id: int, photo_id: int):
    with core.pool.connection() as conn:
        _own_product(conn, user, product_id)
        if not photos.make_main(conn, product_id, photo_id):
            raise HTTPException(404, "Нет такого фото")
    _audit(request, user, "shop_product", str(product_id), op="photo_main")
    return RedirectResponse(f"/my/shop/products/{product_id}#photos", status_code=303)


def _import_page(request: Request, current: dict, *, result: dict | None = None, error: str | None = None,
                 status_code: int = 200):
    return templates.TemplateResponse(request, "shop_import.html", {
        "shop": current, "result": result, "error": error, "columns": shop_import.COLUMNS,
        "max_rows": shop_import.MAX_ROWS}, status_code=status_code)


@router.get("/my/shop/import")
def import_page(request: Request, user: User):
    with core.pool.connection() as conn:
        current = _own_shop(conn, user)
    return _import_page(request, current)


@router.get("/my/shop/import/template.xlsx")
def import_template(user: User):
    return Response(shop_import.template(), headers={"Content-Disposition": 'attachment; filename="tovary.xlsx"'},
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _import(request: Request, user: int, content: bytes, filename: str):
    with core.pool.connection() as conn:
        current = _own_shop(conn, user)
        if shop.imports_today(conn, user) >= shop.DAILY_IMPORTS:
            return _import_page(request, current, error=shop.IMPORTS_MESSAGE,
                                status_code=429)
        # Попытка — в журнал до разбора: предел считает и файлы с ошибками.
        _audit(request, user, "shop_product", str(current["id"]), op="import_try", size=len(content))
        try:
            rows = shop_import.read(content, filename)
        except ValueError as error:
            return _import_page(request, current, error=str(error), status_code=422)
        result = shop_import.run(conn, current, rows)
    _audit(request, user, "shop_product", str(current["id"]), op="import", created=result["created"],
           updated=result["updated"], errors=len(result["errors"]))
    return _import_page(request, current, result=result)


@router.post("/my/shop/import", dependencies=[Depends(same_origin)])
async def import_upload(request: Request, user: User):
    """Загрузка файла каталога (предел запроса шлюза здесь — 6 МБ, deploy/gateway/default.conf)."""
    with core.pool.connection() as conn:
        _own_shop(conn, user)
    form = await request.form()
    upload = form.get("file")
    content = await upload.read(shop_import.MAX_BYTES + 1) if hasattr(upload, "read") else b""
    filename = getattr(upload, "filename", "") or ""
    # Разбор файла и сотни товаров — не в цикле событий.
    return await run_in_threadpool(_import, request, user, content, filename)


@router.get("/staff/sellers")
def moderation_page(request: Request, staff: Staff, done: str = ""):
    with core.pool.connection() as conn:
        data = shop.moderation(conn)
        complaints = reports.open_reports(conn)
        blocked = reports.blocked_products(conn)
    return templates.TemplateResponse(request, "staff_sellers.html", {
        **data, "done": done, "seller_path": sellers.path, "product_path": products.path,
        "complaints": complaints, "blocked": blocked, "reasons": reports.REASONS})


@router.post("/staff/reports", dependencies=[Depends(same_origin)])
def moderation_report(request: Request, staff: Staff, action: Field = "", product_id: Field = "",
                      seller_id: Field = ""):
    """Решение по жалобам: скрыть товар или продавца, вернуть товар, отклонить."""
    pid = int(product_id) if product_id.isdigit() and len(product_id) < 19 else None  # в bigint
    sid = int(seller_id) if seller_id.isdigit() and len(seller_id) < 19 else None
    with core.pool.connection() as conn:
        if not reports.decide(conn, staff, action, product_id=pid, seller_id=sid):
            raise HTTPException(422, "Нет такого действия")
    _audit(request, staff, "seller_moderation", f"report:{pid or ''}:{sid or ''}", decision=action)
    return RedirectResponse("/staff/sellers?done=report", status_code=303)


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
