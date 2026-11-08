"""Баланс поисков товаров с ИИ: сколько поисков у клиента осталось.

Баланс — сумма записей журнала search_ledger: пополнение (+N: покупка пакета или
начисление администратором) и списание (−1 за каждый запуск поиска и «Найти ещё»).
Поиск, завершившийся ошибкой, возвращается (+1). Купленные поиски не сгорают.
Сотрудники ищут без баланса.
"""

from typing import Any

import psycopg

KINDS = {"purchase": "Покупка", "grant": "Начисление", "search": "Поиск товаров",
         "refund": "Возврат: поиск не удался"}


class NoCredits(Exception):
    pass


def balance(conn: psycopg.Connection, user_id: int) -> int:
    return conn.execute(
        "SELECT coalesce(sum(delta), 0) FROM search_ledger WHERE user_id = %s", (user_id,)
    ).fetchone()[0]


def charge(conn: psycopg.Connection, user_id: int, lot_id: int, research_id: int) -> None:
    """Списывает один поиск в той же транзакции, что и постановка поиска в очередь.
    Строка пользователя блокируется, чтобы два параллельных нажатия не ушли в минус."""
    conn.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (user_id,))
    if balance(conn, user_id) < 1:
        raise NoCredits()
    conn.execute(
        """INSERT INTO search_ledger (user_id, delta, kind, lot_id, research_id)
           VALUES (%s, -1, 'search', %s, %s)""",
        (user_id, lot_id, research_id),
    )


def refund(conn: psycopg.Connection, research_id: int) -> bool:
    """Возвращает поиск, если за него списывали и ещё не возвращали."""
    row = conn.execute(
        """INSERT INTO search_ledger (user_id, delta, kind, lot_id, research_id)
           SELECT user_id, 1, 'refund', lot_id, research_id FROM search_ledger
           WHERE research_id = %s AND kind = 'search'
             AND NOT EXISTS (SELECT 1 FROM search_ledger r
                             WHERE r.research_id = %s AND r.kind = 'refund')
           RETURNING id""",
        (research_id, research_id),
    ).fetchone()
    return row is not None


def add(conn: psycopg.Connection, user_id: int, count: int, kind: str = "grant",
        by: int | None = None, note: str = "") -> None:
    conn.execute(
        """INSERT INTO search_ledger (user_id, delta, kind, created_by, note)
           VALUES (%s, %s, %s, %s, %s)""",
        (user_id, count, kind, by, note[:200] or None),
    )


def history(conn: psycopg.Connection, user_id: int, limit: int = 500) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT g.created_at, g.delta, g.kind, g.lot_id, g.research_id, g.note,
                  l.name_ru, l.name_kz, l.lot_number, r.status,
                  jsonb_array_length(coalesce(r.result->'items', '[]'))
           FROM search_ledger g
           LEFT JOIN lots l ON l.id = g.lot_id
           LEFT JOIN lot_research r ON r.id = g.research_id
           WHERE g.user_id = %s ORDER BY g.created_at DESC, g.id DESC LIMIT %s""",
        (user_id, limit),
    ).fetchall()
    keys = ("at", "delta", "kind", "lot_id", "research_id", "note", "lot_ru", "lot_kz",
            "lot_number", "status", "found")
    return [dict(zip(keys, row)) for row in rows]


def has_access(conn: psycopg.Connection, user_id: int) -> bool:
    """Пользовался ли поиском с ИИ (покупал или получал поиски) — тогда видит результаты."""
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM search_ledger WHERE user_id = %s AND delta > 0)", (user_id,)
    ).fetchone()[0]
