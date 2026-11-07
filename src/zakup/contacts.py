"""Телефоны продавцов со страниц товаров — без ИИ.

Инструмент чтения страниц у ИИ превращает HTML в текст и теряет данные, которые
сайт держит в коде страницы: у Satu.kz телефон продавца лежит в JSON
(`"phone":"+7 (707) 303-70-63"`), а на экране — за кнопкой «Контакты».
Поэтому после поиска сервер сам открывает страницы без телефона и ищет:
ссылки tel: и WhatsApp, поля phone/telephone в JSON, казахстанские номера.
Ходим только на публичные адреса: адрес из ответа модели — чужие данные.
"""

import ipaddress
import logging
import re
import socket
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

MAX_BYTES = 3 * 1024 * 1024
MAX_PHONES = 3
# Казахстан: +7 / 8, затем код 7XX (мобильные 70x, 747…, города 727, 717…) и 7 цифр.
_KZ = re.compile(r"(?<![\d+])(?:\+7|8|7)[\s (.-]*(7\d{2})[\s ).-]*(\d{3})[\s .-]*(\d{2})[\s .-]*(\d{2})(?!\d)")
_JSON_PHONE = re.compile(r'"(?:phone|telephone|phoneNumber)"\s*:\s*"([^"]{7,40})"', re.I)
_TEL = re.compile(r'href="tel:([^"]{7,40})"', re.I)
_WA = re.compile(r'(?:wa\.me/|api\.whatsapp\.com/send\?phone=)(\+?\d{10,12})', re.I)


def _public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return True


def normalize(raw: str) -> str | None:
    match = _KZ.search(raw)
    if not match:
        return None
    a, b, c, d = match.groups()
    return f"+7 ({a}) {b}-{c}-{d}"


def phones_in(html: str) -> list[str]:
    """Телефоны со страницы: сначала явные (tel:, JSON, WhatsApp), потом из текста."""
    found: list[str] = []
    candidates = (_TEL.findall(html) + _JSON_PHONE.findall(html) + _WA.findall(html)
                  + ["".join(m.group(0)) for m in _KZ.finditer(re.sub(r"<[^>]+>", " ", html))])
    for raw in candidates:
        phone = normalize(raw)
        if phone and phone not in found:
            found.append(phone)
        if len(found) >= MAX_PHONES:
            break
    return found


# Площадки, где «Контакты» сайта — поддержка самой площадки, а не продавца:
# там берём только телефон со страницы товара.
MARKETPLACES = {"satu.kz", "kaspi.kz", "olx.kz", "market.kz", "flip.kz", "wildberries.kz",
                "ozon.kz", "halykmarket.kz", "lamoda.kz", "alser.kz", "technodom.kz"}
CONTACT_PATHS = ("/contacts", "/contacts/", "/kontakty", "/kontakty/", "/contact", "/kontakti")
MAX_CONTACTS = 15
_CONTACT_LINK = re.compile(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', re.I | re.S)


def _get(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or not _public(parts.hostname):
        return None
    try:
        with httpx.Client(timeout=httpx.Timeout(8, connect=4), follow_redirects=False,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; zakup-bot)"}) as http:
            with http.stream("GET", url) as response:
                if response.status_code != 200:
                    return None
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        break
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
    except httpx.HTTPError as error:
        logger.info("contacts of %s: %s", parts.hostname, type(error).__name__)
        return None
    return b"".join(chunks).decode(encoding, errors="replace")


def _lines(html_text: str) -> list[str]:
    import html as html_lib

    text = re.sub(r"<script.*?</script>|<style.*?</style>", "", html_text, flags=re.S | re.I)
    text = re.sub(r"<(br|/p|/div|/li|/h\d|/tr|/td|/section|/article)[^>]*>", "\n", text, flags=re.I)
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
    return [line for line in (re.sub(r"\s+", " ", raw).strip() for raw in text.split("\n")) if line]


def labeled_phones(html_text: str) -> list[dict]:
    """Телефоны с подписями: раздел («Call-центр») и пометка у номера («WhatsApp»)."""
    lines = _lines(html_text)
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()
    section = ""
    for index, line in enumerate(lines):
        matches = list(_KZ.finditer(line))
        if not matches:
            # Короткая строка без цифр — заголовок раздела; длинная — пояснение, пропускаем.
            if len(line) <= 40 and not any(c.isdigit() for c in line) and not line.startswith("("):
                section = line
            continue
        for match in matches:
            number = normalize(match.group(0))
            rest = line[match.end():]
            nxt = _KZ.search(rest)
            note = rest[: nxt.start()] if nxt else rest
            note = note.strip(" ,;:—-")
            # Пометка бывает следующей строкой: «(WhatsApp)».
            if not note and index + 1 < len(lines) and lines[index + 1].startswith("(") \
                    and not _KZ.search(lines[index + 1]):
                note = lines[index + 1]
            note = note.strip("() ")[:60]
            label = " · ".join(part for part in (section, note) if part)[:90]
            key = (number, note)
            if number and key not in seen:
                seen.add(key)
                found.append({"number": number, "label": label,
                              "whatsapp": "whatsapp" in label.lower()})
            if len(found) >= MAX_CONTACTS:
                return found
    return found


def _contact_page_urls(base_url: str, html_text: str) -> list[str]:
    parts = urlsplit(base_url)
    root = f"{parts.scheme}://{parts.netloc}"
    urls = []
    for href, text in _CONTACT_LINK.findall(html_text):
        plain = re.sub(r"<[^>]+>", " ", text).lower()
        if "контакт" in plain or "contact" in href.lower() or "kontakt" in href.lower():
            target = href if href.startswith("http") else root + (href if href.startswith("/") else "/" + href)
            if urlsplit(target).netloc == parts.netloc and target not in urls:
                urls.append(target)
    urls += [root + path for path in CONTACT_PATHS if root + path not in urls]
    return urls[:6]


def site_contacts(url: str) -> list[dict]:
    """Контакты сайта из раздела «Контакты» (ссылка со страницы или обычные адреса)."""
    page = _get(url) or ""
    for candidate in _contact_page_urls(url, page):
        html_text = _get(candidate)
        if html_text:
            phones = labeled_phones(html_text)
            if phones:
                return phones
    return []


def fetch_phones(url: str) -> list[str]:
    html_text = _get(url)
    return phones_in(html_text) if html_text else []


def _domain(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def enrich(result: dict, site_cache: dict | None = None) -> int:
    """Собирает все телефоны предложения в item["phones"]: [{number, label, whatsapp}].

    Источники: что нашёл ИИ, страница товара (код страницы), раздел «Контакты»
    сайта продавца (не у маркетплейсов). site_cache — {домен: контакты} между
    предложениями одного поиска и из справочника источников.
    """
    site_cache = {} if site_cache is None else site_cache
    added = 0
    for item in result.get("items") or []:
        url = item.get("url") or ""
        if not url:
            continue
        phones: list[dict] = []

        def add(number: str | None, label: str = "", whatsapp: bool = False) -> None:
            if number and not any(p["number"] == number and p["label"] == label for p in phones):
                phones.append({"number": number, "label": label[:90], "whatsapp": whatsapp})

        for raw in (item.get("phone") or "").split(","):
            add(normalize(raw))
        for number in fetch_phones(url):
            add(number, "страница товара")
        domain = _domain(url)
        if domain and domain not in MARKETPLACES:
            if domain not in site_cache:
                site_cache[domain] = site_contacts(url)
            for contact in site_cache[domain]:
                add(contact["number"], contact["label"], contact["whatsapp"])
        # Один номер без подписи, если он же есть с подписью, — лишний.
        phones = [p for p in phones if p["label"] or not any(
            q["number"] == p["number"] and q["label"] for q in phones)][:MAX_CONTACTS]
        if phones:
            item["phones"] = phones
            item["phone"] = ", ".join(dict.fromkeys(p["number"] for p in phones))
            added += 1
    return added
