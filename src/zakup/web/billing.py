"""Тариф и поиски: цены, оплата через kazpayment, вебхук, баланс поисков."""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from zakup import (
    audit,
    auth,
    credits,
    kazpayment,
    payments,
    tariff,
)
from zakup.web import core
from zakup.web.core import (
    Paid,
    User,
    Viewer,
    _audit,
    logger,
    not_impersonating,
    same_origin,
    templates,
)

router = APIRouter()


@router.get("/pricing")
def pricing(request: Request, user: Viewer, need: str = "", error: str = ""):
    paid_until, history = None, []
    if user:
        with core.pool.connection() as conn:
            paid_until = conn.execute("SELECT paid_until FROM users WHERE id = %s AND paid_until > now()",
                                      (user,)).fetchone()
            history = payments.history(conn, user)
    return templates.TemplateResponse(request, "pricing.html", {
        "packs": payments.SEARCH_PACKS, "search_price": payments.SEARCH_PRICE,
        "paid_until": paid_until[0] if paid_until else None, "history": history,
        "need": need, "error": error})


@router.post("/pay/tariff", dependencies=[Depends(same_origin), Depends(not_impersonating)])
def pay_tariff(request: Request, user: User):
    with core.pool.connection() as conn:
        price = tariff.get(conn)["price"]
    return _pay(request, user, "tariff", payments.TARIFF_DAYS, price,
                f"Tenderim: тариф на {payments.TARIFF_DAYS} дней")


@router.post("/pay/searches", dependencies=[Depends(same_origin), Depends(not_impersonating)])
def pay_searches(request: Request, user: Paid, count: Annotated[int, Form()]):
    # Поиски — дополнение к тарифу: без оплаченного тарифа купить нельзя.
    if not 1 <= count <= payments.MAX_SEARCHES:
        raise HTTPException(422, f"от 1 до {payments.MAX_SEARCHES} поисков")
    return _pay(request, user, "searches", count, payments.searches_price(count),
                f"Tenderim: {count} поисков товаров с ИИ")


def _pay(request: Request, user: int, kind: str, quantity: int, amount: int, title: str):
    with core.pool.connection() as conn:
        phone = conn.execute("SELECT phone FROM users WHERE id = %s", (user,)).fetchone()[0]
        try:
            ref, new = payments.start(conn, user, kind, quantity, amount)
        except payments.TooManyOrders:
            return RedirectResponse("/pricing?error=orders", status_code=303)
    if not new:
        return RedirectResponse(f"/pay/{ref}", status_code=303)
    # Запрос к kazpayment — до 20 секунд: соединение с базой на это время не держим.
    try:
        pay_url = kazpayment.create_order(ref, amount, title, phone)
    except kazpayment.KazpaymentError as error:
        logger.warning("payment create failed: %s", error.code)
        with core.pool.connection() as conn:
            payments.drop(conn, ref)
        return RedirectResponse("/pricing?error=pay", status_code=303)
    with core.pool.connection() as conn:
        payments.attach(conn, ref, pay_url)
    _audit(request, user, "payment_start", kind, quantity=quantity, amount=amount)
    return RedirectResponse(f"/pay/{ref}", status_code=303)


@router.get("/pay/{ref}")
def pay_page(request: Request, user: User, ref: str, sent: str = "", error: str = ""):
    """Своя страница оплаты: счёт в Kaspi на номер, оплата в Kaspi одним нажатием и QR
    (страница kazpayment внутри нашей — они разрешают встраивание)."""
    with core.pool.connection() as conn:
        payment = payments.get(conn, ref, user)
        if payment is None:
            raise HTTPException(404, "Нет такого платежа")
        phone = conn.execute("SELECT phone FROM users WHERE id = %s", (user,)).fetchone()[0]
    return templates.TemplateResponse(request, "pay.html", {
        "p": payment, "phone": phone, "token": kazpayment.pay_token(payment["pay_url"]),
        "sent": sent, "error": error})


@router.get("/pay/{ref}/status")
def pay_status(user: User, ref: str):
    with core.pool.connection() as conn:
        if payments.get(conn, ref, user) is None:
            raise HTTPException(404, "Нет такого платежа")
        return {"status": payments.check(conn, ref)}


INVOICES_PER_HOUR = 5


@router.post("/pay/{ref}/kaspi", dependencies=[Depends(same_origin), Depends(not_impersonating)])
def pay_kaspi_invoice(request: Request, user: User, ref: str, phone: Annotated[str, Form()] = ""):
    """Счёт в приложение Kaspi на номер (свой или указанный)."""
    with core.pool.connection() as conn:
        payment = payments.get(conn, ref, user)
        if payment is None or payment["status"] != "pending":
            raise HTTPException(404, "Нет такого неоплаченного платежа")
        own = conn.execute("SELECT phone FROM users WHERE id = %s", (user,)).fetchone()[0]
        number = auth.normalize_phone(phone) if phone.strip() else own
        if not number:
            return RedirectResponse(f"/pay/{ref}?error=phone", status_code=303)
        # Счёт приходит в Kaspi от имени магазина: чужим номерам — не чаще нескольких раз.
        # Счёт записываем до отправки и под блокировкой: параллельные запросы считают
        # по очереди, а не все по старому числу.
        conn.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (user,))
        sent = conn.execute(
            """SELECT count(*) FROM user_actions WHERE user_id = %s AND action = 'payment_invoice'
               AND at > now() - interval '1 hour'""", (user,)).fetchone()[0]
        if sent >= INVOICES_PER_HOUR:
            conn.commit()
            return RedirectResponse(f"/pay/{ref}?error=invoice", status_code=303)
        audit.log(conn, user, "payment_invoice", ref, None,
                  ip=request.client.host if request.client else None)
    try:
        kazpayment.remote_invoice(ref, number)
    except kazpayment.KazpaymentError as error:
        code = "nokaspi" if error.code == "invoice_rejected" else "invoice"
        return RedirectResponse(f"/pay/{ref}?error={code}", status_code=303)
    return RedirectResponse(f"/pay/{ref}?sent=1", status_code=303)


@router.post("/webhooks/kazpayment")
async def kazpayment_webhook(request: Request):
    """Уведомление kazpayment об оплате. Подпись проверяется по сырому телу до разбора."""
    body = await request.body()
    if not payments.verify_signature(body, request.headers.get("x-kazpayment-signature")):
        raise HTTPException(401, "bad signature")
    try:
        event = json.loads(body)
    except ValueError:
        raise HTTPException(400, "bad json") from None
    if not isinstance(event, dict):
        raise HTTPException(400, "bad json")
    with core.pool.connection() as conn:
        outcome = await run_in_threadpool(payments.handle_event, conn, event)
    logger.info("kazpayment %s %s: %s", event.get("event"), event.get("order_ref"), outcome)
    return {"ok": True}


@router.get("/balance")
def balance_page(request: Request, user: User):
    with core.pool.connection() as conn:
        rows = credits.history(conn, user)
        left = credits.balance(conn, user)
    return templates.TemplateResponse(request, "balance.html", {
        "rows": rows, "balance": left, "kinds": credits.KINDS})
