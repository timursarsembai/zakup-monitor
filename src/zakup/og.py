"""Картинки-превью 1200×630 для ссылок в соцсетях и мессенджерах (Open Graph).

Telegram, WhatsApp, Facebook и др. при отправке ссылки подгружают og:image: общую
картинку сайта или карточку лота/объявления — название, заказчик, сумма, срок.
Рисуется по запросу (десятки миллисекунд) и держится в памяти; шрифт — Noto Sans
(есть все казахские буквы и знак ₸, у Manrope с сайта их нет).
"""

import io
import threading
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal
from functools import cache
from importlib.resources import files

from PIL import Image, ImageDraw, ImageFont

from zakup.goszakup import PORTAL_TZ

WIDTH, HEIGHT = 1200, 630
PAD = 64
BG, FG, MUTED, ACCENT, LINE = "#151517", "#ececee", "#9a9aa0", "#ff7a1a", "#2c2c30"
SITE = "zakup.sarsembai.com"

TEXTS = {
    "ru": {"tagline": "Госзакупки Казахстана", "lead": "Поиск лотов goszakup.gov.kz, уведомления в Telegram, "
                                                    "выжимки техспецификаций и подбор товаров с ИИ",
           "lot": "Лот", "announcement": "Объявление №", "until": "Приём заявок до", "ended": "Приём заявок завершён",
           "customer": "Заказчик:"},
    "kk": {"tagline": "Қазақстанның мемлекеттік сатып алулары",
           "lead": "goszakup.gov.kz лоттарын іздеу, Telegram-дағы хабарламалар, техникалық ерекшеліктің "
                   "қысқаша мазмұны және ЖИ арқылы тауар іздеу",
           "lot": "Лот", "announcement": "Хабарландыру №", "until": "Өтінімдер қабылдау мерзімі:",
           "ended": "Өтінімдер қабылдау аяқталды", "customer": "Тапсырыс беруші:"},
}

CACHE_SIZE = 300
_cache: OrderedDict[tuple, bytes] = OrderedDict()
_lock = threading.Lock()
# Не больше двух картинок рисуется одновременно: перебор адресов не съест процессор копии сайта.
_drawing = threading.BoundedSemaphore(2)


class Busy(Exception):
    """Слишком много картинок рисуется прямо сейчас — пусть бот спросит позже."""


@cache
def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    name = "NotoSans-Bold.ttf" if bold else "NotoSans-Regular.ttf"
    return ImageFont.truetype(io.BytesIO(files("zakup").joinpath(f"fonts/{name}").read_bytes()), size)


@cache
def _logo() -> Image.Image:
    logo = Image.open(io.BytesIO(files("zakup").joinpath("static/logo-tenderim-light.png").read_bytes()))
    logo = logo.convert("RGBA")
    height = 56
    return logo.resize((round(logo.width * height / logo.height), height), Image.LANCZOS)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int, lines: int) -> list[str]:
    """Перенос по словам в lines строк; не влезло — многоточие в конце последней."""
    words, out, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= width:
            current = candidate
            continue
        if current:
            out.append(current)
        current = word
        if len(out) == lines:
            break
    if current and len(out) < lines:
        out.append(current)
    cut = len(" ".join(out)) < len(" ".join(words))
    if out and cut:
        last = out[-1]
        while last and draw.textlength(last + "…", font=font) > width:
            last = last[:-1].rstrip()
        out[-1] = last + "…"
    return out[:lines]


def _money(value) -> str:
    amount = Decimal(value or 0).quantize(Decimal(1))
    return f"{amount:,}".replace(",", " ") + " ₸"


def _canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH, 10), fill=ACCENT)
    logo = _logo()
    image.paste(logo, (PAD, 52), logo)
    return image, draw


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG")  # optimize — втрое дольше ради 3% размера
    return out.getvalue()


def site(lang: str) -> bytes:
    t = TEXTS[lang]
    image, draw = _canvas()
    y = 230
    for line in _wrap(draw, t["tagline"], _font(True, 64), WIDTH - 2 * PAD, 2):
        draw.text((PAD, y), line, font=_font(True, 64), fill=FG)
        y += 80
    y += 20
    for line in _wrap(draw, t["lead"], _font(False, 34), WIDTH - 2 * PAD, 3):
        draw.text((PAD, y), line, font=_font(False, 34), fill=MUTED)
        y += 48
    draw.text((PAD, HEIGHT - PAD - 34), SITE, font=_font(True, 30), fill=ACCENT)
    return _png(image)


def card(lang: str, *, kicker: str, title: str, customer: str | None, amount, end: datetime | None,
         now: datetime) -> bytes:
    """Карточка закупки: сверху номер, крупно название, ниже заказчик, внизу сумма и срок."""
    t = TEXTS[lang]
    image, draw = _canvas()
    draw.text((WIDTH - PAD, 66), kicker, font=_font(False, 30), fill=MUTED, anchor="ra")
    y = 170
    title_font = _font(True, 54)
    for line in _wrap(draw, title or "—", title_font, WIDTH - 2 * PAD, 3):
        draw.text((PAD, y), line, font=title_font, fill=FG)
        y += 70
    bottom = HEIGHT - PAD
    # Заказчик — сколько строк влезает над чертой (длинное название закупки оставляет одну).
    room = (bottom - 96 - 16 - (y + 14)) // 42
    if customer and room > 0:
        y += 14
        for line in _wrap(draw, f"{t['customer']} {customer}", _font(False, 30), WIDTH - 2 * PAD, min(room, 2)):
            draw.text((PAD, y), line, font=_font(False, 30), fill=MUTED)
            y += 42
    draw.line((PAD, bottom - 96, WIDTH - PAD, bottom - 96), fill=LINE, width=2)
    draw.text((PAD, bottom - 62), _money(amount), font=_font(True, 52), fill=ACCENT)
    if end:
        local = end.astimezone(PORTAL_TZ)
        when = (f"{t['until']} {local:%d.%m.%Y %H:%M}" if end > now else t["ended"])
        draw.text((WIDTH - PAD, bottom - 26), when, font=_font(False, 30), fill=FG, anchor="rs")
    return _png(image)


def cached(key: tuple, render) -> bytes:
    """Готовая картинка из памяти или нарисованная сейчас (render — без аргументов)."""
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    # Не ждать долго: ожидающие запросы держали бы потоки сайта.
    if not _drawing.acquire(timeout=0.3):
        raise Busy
    try:
        data = render()
    finally:
        _drawing.release()
    with _lock:
        _cache[key] = data
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return data
