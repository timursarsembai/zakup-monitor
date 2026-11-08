"""Русская и казахская версии интерфейса.

Тексты в шаблонах и коде пишутся по-русски и оборачиваются в `_()`; перевод
на казахский — словарь KK в zakup/i18n_kk.py (ключ — русский текст). Нет
перевода — показывается русский. Язык запроса — в contextvar: его ставит
middleware по cookie, и он виден и шаблонам, и коду, и макросам.

Данные портала (названия, справочники, техспецификации) на казахском берутся
из полей *Kz / name_kz / text_kk; нет казахского — показывается русский.
"""

from contextvars import ContextVar
from typing import Any

from zakup.i18n_kk import KK

LANGUAGES = {"ru": "Рус", "kk": "Қаз"}
COOKIE = "zakup_lang"
# Казахская версия страницы по своему адресу (?lang=kk) — для поисковиков и hreflang.
QUERY = "lang"
_current: ContextVar[str] = ContextVar("lang", default="ru")


def set_language(code: str | None) -> None:
    _current.set(code if code in LANGUAGES else "ru")


def language() -> str:
    return _current.get()


def gettext(text: str, **values: Any) -> str:
    """Перевод. Ключ «nav:Текст» — короткая подпись для меню (по-русски — сам текст)."""
    if _current.get() == "kk":
        translated = KK.get(text, KK.get(text.removeprefix("nav:"), text.removeprefix("nav:")))
    else:
        translated = text.removeprefix("nav:")
    return translated.format(**values) if values else translated


def loc(item: dict | None, field: str = "nameRu") -> Any:
    """Поле из ответа портала на языке интерфейса: nameRu → nameKz."""
    if not item:
        return None
    if _current.get() == "kk" and field.endswith("Ru"):
        kazakh = item.get(field[:-2] + "Kz")
        if kazakh:
            return kazakh
    return item.get(field)


def pick(russian: Any, kazakh: Any) -> Any:
    """Из двух значений — на языке интерфейса (казахское, если оно есть)."""
    return kazakh if _current.get() == "kk" and kazakh else russian
