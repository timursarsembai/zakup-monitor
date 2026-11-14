"""Организация клиента: форма (ИП, ТОО, другое), БИН/ИИН и название, и «БИН для итогов»
сотрудника (users.company_bin).

У ИП БИН — это ИИН, название — имя человека: личные данные. С сервисом учётных записей
(zakup.idservice) они хранятся в Казахстане, сайт спрашивает их, когда нужно; без него —
таблица client_orgs и users.company_bin, как раньше. БИН нужен, чтобы в итогах отмечать
«это вы» и ставить итог отмеченным лотам: «БИН для итогов», если задан, иначе БИН организации.

Переезд: пока здешняя копия не перенесена (id-migrate) и не стёрта (id-scrub), её и берём —
у кого в Казахстане ещё ничего нет. Изменение при включённом сервисе уходит туда и стирает
здешнюю копию: повтор переноса не затрёт свежее старым.

db — соединение или пул (с пулом соединение берётся ненадолго и не во время запроса в
Казахстан).
"""

import logging
from contextlib import contextmanager
from typing import Any

import psycopg

from zakup import idservice

logger = logging.getLogger(__name__)

KINDS = {"ip": "ИП", "too": "ТОО", "other": "Другое"}


@contextmanager
def _conn(db: Any):
    if isinstance(db, psycopg.Connection):
        yield db
    else:   # пул
        with db.connection() as conn:
            yield conn


def local(conn: psycopg.Connection, ids: list[int] | None = None) -> dict[int, dict]:
    """Здешние копии {номер: {kind, bin, name, results_bin}} (все или только ids)."""
    rows = conn.execute(
        """SELECT u.id, o.kind, o.bin, o.name, u.company_bin
           FROM users u LEFT JOIN client_orgs o ON o.user_id = u.id
           WHERE (o.user_id IS NOT NULL OR u.company_bin IS NOT NULL)
             AND (%(all)s OR u.id = ANY(%(ids)s))""",
        {"all": ids is None, "ids": ids or []}).fetchall()
    return {r[0]: {"kind": r[1], "bin": r[2], "name": r[3], "results_bin": r[4]} for r in rows}


def as_item(org: dict) -> dict:
    """Строка для idservice.set_orgs: организация (если есть) и «БИН для итогов»."""
    has_org = org.get("bin") or org.get("name")
    return {"org": {"kind": org.get("kind"), "bin": org.get("bin"), "name": org.get("name")} if has_org else None,
            "results_bin": org.get("results_bin")}


def same(remote: dict | None, org: dict) -> bool:
    """В Казахстане то же, что здесь (перед стиранием здешней копии)."""
    keys = ("kind", "bin", "name", "results_bin") if org.get("bin") or org.get("name") else ("results_bin",)
    return bool(remote) and all((remote.get(k) or None) == (org.get(k) or None) for k in keys)


def effective_bin(org: dict | None) -> str | None:
    return (org.get("results_bin") or org.get("bin")) if org else None


def many(db: Any, ids: list[int], strict: bool = False, fresh: bool = False) -> dict[int, dict]:
    """{номер: {kind, bin, name, results_bin}}; у кого нет — нет ключа. Сервис не ответил:
    strict — ServiceError (рассылка итогов подождёт), иначе {} (страница — без организации)."""
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    if not idservice.enabled():
        with _conn(db) as conn:
            return local(conn, ids)
    try:
        found = idservice.orgs(ids, fresh=fresh)
    except idservice.ServiceError as error:
        if strict:
            raise
        logger.warning("orgs not loaded: %s", error)
        return {}
    rest = [i for i in ids if i not in found]
    if rest:   # ещё не перенесены — здешняя копия
        with _conn(db) as conn:
            found.update(local(conn, rest))
    return found


def bin_of(db: Any, user_id: int | None, fresh: bool = False) -> str | None:
    return effective_bin(many(db, [user_id], fresh=fresh).get(user_id)) if user_id else None


def _forget_local(conn: psycopg.Connection, user_id: int, org: bool, results: bool) -> None:
    if org:
        conn.execute("DELETE FROM client_orgs WHERE user_id = %s", (user_id,))
    if results:
        conn.execute("UPDATE users SET company_bin = NULL WHERE id = %s", (user_id,))


def save(db: Any, user_id: int, kind: str, bin_: str, name: str) -> None:
    """Шаг «Ваша организация» после регистрации. ServiceError — не сохранили."""
    if idservice.enabled():
        item = {"user_id": user_id, "org": {"kind": kind, "bin": bin_, "name": name}}
        with _conn(db) as conn:
            mine = local(conn, [user_id]).get(user_id)
        if mine and mine.get("results_bin"):
            item["results_bin"] = mine["results_bin"]   # ещё не перенесённый «БИН для итогов» — туда же
        idservice.set_orgs([item])
        with _conn(db) as conn:
            _forget_local(conn, user_id, org=True, results="results_bin" in item)
        return
    with _conn(db) as conn:
        conn.execute(
            """INSERT INTO client_orgs (user_id, kind, bin, name) VALUES (%s, %s, %s, %s)
               ON CONFLICT (user_id) DO UPDATE SET kind = excluded.kind, bin = excluded.bin,
                   name = excluded.name, updated_at = now()""",
            (user_id, kind, bin_, name))


def set_bin(db: Any, user_id: int, bin_: str | None) -> None:
    """«БИН для итогов» в настройках (организация клиента не меняется). ServiceError — не сохранили."""
    if idservice.enabled():
        # Здешняя организация (ещё не перенесена) — туда же, иначе перенос потом её бы не нашёл
        # полной; БИН для итогов — новый.
        with _conn(db) as conn:
            mine = local(conn, [user_id]).get(user_id)
        item = {"user_id": user_id, "results_bin": bin_}
        if mine and (mine.get("bin") or mine.get("name")):
            item["org"] = as_item(mine)["org"]
        idservice.set_orgs([item])
        with _conn(db) as conn:
            _forget_local(conn, user_id, org="org" in item, results=True)
        return
    with _conn(db) as conn:
        conn.execute("UPDATE users SET company_bin = %s WHERE id = %s", (bin_, user_id))
