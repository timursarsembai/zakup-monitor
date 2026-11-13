"""База сервиса учётных записей: подключение и миграции (zakup_id/migrations/NNNN_*.sql)."""

from importlib.resources import files

import psycopg

MIGRATE_LOCK = 7_501


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url)


def migrate(conn: psycopg.Connection) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATE_LOCK,))
    conn.execute("""CREATE TABLE IF NOT EXISTS id_migrations (
                        name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
    done = {row[0] for row in conn.execute("SELECT name FROM id_migrations")}
    folder = files("zakup_id").joinpath("migrations")
    for item in sorted(folder.iterdir(), key=lambda f: f.name):
        name = item.name.removesuffix(".sql")
        if item.name.endswith(".sql") and name not in done:
            conn.execute(item.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO id_migrations (name) VALUES (%s)", (name,))
    conn.commit()
