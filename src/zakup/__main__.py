import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, time

from zakup import db, matcher, notify, sync
from zakup.goszakup import ENTITIES, PORTAL_TZ, Client


def main() -> int:
    parser = argparse.ArgumentParser(prog="zakup")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="создать или обновить таблицы")
    sub.add_parser("probe", help="проверить токен и показать по одной записи каждой сущности")
    start = sub.add_parser("start-from", help="начать синхронизацию с даты, без старой истории")
    start.add_argument("since", type=date.fromisoformat, help="ГГГГ-ММ-ДД")
    run = sub.add_parser("sync", help="догрузить новое, сопоставить подписки, разослать")
    run.add_argument("--max-pages", type=int, default=50)
    refresh = sub.add_parser("refresh", help="перечитать открытые объявления (статусы, сроки)")
    refresh.add_argument("--limit", type=int, default=500)
    user = sub.add_parser("add-user", help="завести пользователя")
    user.add_argument("email")
    user.add_argument("--telegram-chat-id")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx пишет каждую строку запроса в INFO; при поиске старта их сотни.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.command == "migrate":
        with db.connect(os.environ["DATABASE_URL"]) as conn:
            db.migrate(conn)
        return 0
    if args.command == "add-user":
        with db.connect(os.environ["DATABASE_URL"]) as conn:
            row = conn.execute(
                """INSERT INTO users (email, telegram_chat_id) VALUES (%s, %s)
                   ON CONFLICT (email) DO UPDATE SET telegram_chat_id = excluded.telegram_chat_id
                   RETURNING id""",
                (args.email, args.telegram_chat_id),
            ).fetchone()
            conn.commit()
        print(row[0])
        return 0

    token = os.environ.get("GOSZAKUP_TOKEN", "").strip()
    if not token:
        print("GOSZAKUP_TOKEN не задан.", file=sys.stderr)
        return 2
    client = Client(
        token,
        delay=float(os.environ.get("GOSZAKUP_REQUEST_DELAY") or 0.5),
    )
    try:
        if args.command == "probe":
            return _probe(client)
        with db.connect(os.environ["DATABASE_URL"]) as conn:
            if args.command == "start-from":
                return _start_from(conn, client, args.since)
            if args.command == "refresh":
                return _refresh(conn, client, args.limit)
            return _sync(conn, client, args.max_pages)
    finally:
        client.close()


def _probe(client: Client) -> int:
    for entity in ENTITIES:
        page = client.page(entity, limit=1)
        print(f"== {entity}: всего {page.total}, следующий курсор {page.next_after}")
        if page.items:
            print(json.dumps(page.items[0], ensure_ascii=False, indent=2))
    return 0


def _start_from(conn, client: Client, since: date) -> int:
    since_at = datetime.combine(since, time.min, PORTAL_TZ)
    announcements_after = sync.find_start(client, "announcements", sync.published_since(since_at))
    if announcements_after is None:
        print("Объявлений после этой даты нет.", file=sys.stderr)
        return 1
    first = client.page("announcements", after=announcements_after, limit=1).items[0]
    lots_after = sync.find_start(client, "lots", sync.lot_of_announcement_from(int(first["id"])))
    db.set_cursor(conn, "announcements", announcements_after)
    if lots_after is not None:
        db.set_cursor(conn, "lots", lots_after)
    conn.commit()
    print(f"announcements after {announcements_after}, lots after {lots_after}")
    return 0


def _sync(conn, client: Client, max_pages: int) -> int:
    try:
        for entity in ENTITIES:
            print(sync.sync(conn, client, entity, max_pages))
        print(f"matches: {matcher.run(conn)}, delivered: {matcher.deliver(conn)}")
    except Exception as error:
        # Only the type: database errors can quote the connection string.
        notify.send(f"zakup: синхронизация упала с ошибкой {type(error).__name__}.")
        raise
    return 0


def _refresh(conn, client: Client, limit: int) -> int:
    ids = [
        row[0]
        for row in conn.execute(
            """SELECT id FROM announcements WHERE end_date > now()
               ORDER BY fetched_at LIMIT %s""",
            (limit,),
        )
    ]
    for item_id in ids:
        if item := client.get("announcements", item_id):
            db.upsert_announcement(conn, item)
            conn.commit()
    print(f"refreshed {len(ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
