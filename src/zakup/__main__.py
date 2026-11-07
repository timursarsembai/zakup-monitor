import argparse
import getpass
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from datetime import time as day_time

from zakup import audit, auth, bot, db, details, marks, matcher, notify, results, sync
from zakup.goszakup import ENTITIES, PORTAL_TZ, Client

# Сколько истории грузить, если start-from не запускали.
DEFAULT_HISTORY_DAYS = 7


def main() -> int:
    parser = argparse.ArgumentParser(prog="zakup")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="создать или обновить таблицы")
    sub.add_parser("probe", help="проверить токен и показать по одной записи каждой сущности")
    start = sub.add_parser("start-from", help="начать синхронизацию с даты, без старой истории")
    start.add_argument("since", type=date.fromisoformat, help="ГГГГ-ММ-ДД")
    run = sub.add_parser("sync", help="догрузить новое, сопоставить подписки, разослать")
    run.add_argument("--max-pages", type=int, default=50)
    again = sub.add_parser("rescan", help="перечитать последние дни: поздние публикации, статусы")
    again.add_argument("--days", type=int, default=30)
    again.add_argument("--max-pages", type=int, default=20)
    more = sub.add_parser(
        "details", help="дочитать подробности открытых объявлений и итоги отмеченных лотов"
    )
    more.add_argument("--limit", type=int, default=2000)
    more.add_argument("--part", choices=["open", "finished", "all"], default="all",
                      help="open — открытые (быстрый круг), finished — завершённые и протоколы")
    sub.add_parser("bot", help="Telegram-бот: привязка чатов (работает постоянно)")
    specs_cmd = sub.add_parser("specs", help="заранее разобрать техспецификации открытых лотов")
    specs_cmd.add_argument("--limit", type=int, default=200)
    specs_cmd.add_argument("--loop", action="store_true", help="работать постоянно")
    research_cmd = sub.add_parser("research", help="поиск товаров под лоты с ИИ (работает постоянно)")
    research_cmd.add_argument("--providers", default="claude_code",
                              help="через запятую: claude_code, anthropic, openai")
    research_cmd.add_argument("--once", action="store_true", help="одна задача и выход")
    passwd = sub.add_parser("set-password", help="задать пароль (создаст пользователя, если нет)")
    passwd.add_argument("email")
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
    if args.command == "bot":
        bot.run(os.environ["DATABASE_URL"])
        return 0
    if args.command == "specs":
        return _specs(args.limit, args.loop)
    if args.command == "research":
        from zakup import research

        research.work(os.environ["DATABASE_URL"], args.providers.split(","), once=args.once)
        return 0
    if args.command == "set-password":
        return _set_password(args.email)
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
            if args.command == "rescan":
                return _rescan(conn, client, args.days, args.max_pages)
            if args.command == "details":
                return _details(conn, client, args.limit, args.part)
            return _sync(conn, client, args.max_pages)
    finally:
        client.close()


def _specs(limit: int, loop: bool) -> int:
    from zakup import spec

    while True:
        with db.connect(os.environ["DATABASE_URL"]) as conn:
            done = spec.prefetch(conn, limit)
        print(f"specs: {done}", flush=True)
        if not loop:
            return 0
        # Пока есть очередь — сразу дальше, иначе раз в 5 минут.
        if done < limit:
            time.sleep(300)


def _set_password(email: str) -> int:
    if sys.stdin.isatty():
        password = getpass.getpass("Пароль: ")
        if password != getpass.getpass("Ещё раз: "):
            print("Пароли не совпали.", file=sys.stderr)
            return 1
    else:
        # Из скрипта: пароль первой строкой stdin, не аргументом (виден в ps).
        password = sys.stdin.readline().rstrip("\n")
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        print(f"Нужно не меньше {auth.MIN_PASSWORD_LENGTH} символов.", file=sys.stderr)
        return 1
    with db.connect(os.environ["DATABASE_URL"]) as conn:
        user_id = auth.set_password(conn, email, password)
    print(f"Пароль задан, пользователь {user_id}. Старые сессии закрыты.")
    return 0


def _probe(client: Client) -> int:
    for entity in ENTITIES:
        page = client.page(entity, limit=1)
        print(f"== {entity}: всего {page.total}, следующий курсор {page.next_after}")
        if page.items:
            print(json.dumps(page.items[0], ensure_ascii=False, indent=2))
    return 0


def _start_from(conn, client: Client, since: date) -> int:
    since_at = datetime.combine(since, day_time.min, PORTAL_TZ)
    first_announcement = sync.find_start(client, "announcements", sync.published_since(since_at))
    if first_announcement is None:
        print("Объявлений после этой даты нет.", file=sys.stderr)
        return 1
    first_lot = sync.find_start(
        client, "lots", sync.lot_of_announcement_from(first_announcement)
    )
    if first_lot is None:
        print("Лотов этих объявлений пока нет.", file=sys.stderr)
        return 1
    # Всё, что ниже первой нужной записи, считаем уже загруженным.
    db.save_state(conn, "announcements", db.SyncState(first_announcement - 1, None, None))
    db.save_state(conn, "lots", db.SyncState(first_lot - 1, None, None))
    conn.commit()
    print(f"start: announcement {first_announcement}, lot {first_lot}")
    return 0


def _sync(conn, client: Client, max_pages: int) -> int:
    if any(db.get_state(conn, entity) is None for entity in ENTITIES):
        since = date.today() - timedelta(days=DEFAULT_HISTORY_DAYS)
        print(f"Начальной точки нет, начинаю с {since}.")
        if _start_from(conn, client, since) != 0:
            return 1
    try:
        for entity in ENTITIES:
            print(sync.sync(conn, client, entity, max_pages))
        _match_and_deliver(conn)
    except Exception as error:
        # Only the type: database errors can quote the connection string.
        notify.send(f"zakup: синхронизация упала с ошибкой {type(error).__name__}.")
        raise
    return 0


RESCAN_LOCK = 7_401


def _rescan(conn, client: Client, days: int, max_pages: int) -> int:
    # Граница по id: самое старое объявление, опубликованное за эти дни. Черновики,
    # созданные раньше и опубликованные позже, сюда не попадут, но таких единицы.
    floor = conn.execute(
        "SELECT min(id) FROM announcements WHERE publish_date >= now() - make_interval(days => %s)",
        (days,),
    ).fetchone()[0]
    if floor is None:
        print("Нечего перечитывать: объявлений за эти дни нет.")
        return 0
    lot_floor = conn.execute(
        "SELECT min(id) FROM lots WHERE trd_buy_id >= %s", (floor,)
    ).fetchone()[0]
    # Быстрый и медленный круги перечитывают одно и то же: по очереди, не вместе.
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (RESCAN_LOCK,)).fetchone()[0]:
        print("rescan: уже идёт в другом процессе")
        return 0
    try:
        saved = sync.rescan(conn, client, "announcements", floor, max_pages)
        print(f"announcements rescanned: {saved}")
        if lot_floor is not None:
            print(f"lots rescanned: {sync.rescan(conn, client, 'lots', lot_floor, max_pages)}")
        _match_and_deliver(conn)
    except Exception as error:
        # Only the type: database errors can quote the connection string.
        notify.send(f"zakup: повторный проход упал с ошибкой {type(error).__name__}.")
        raise
    return 0


# Протоколы итогов — PDF, каждый качается отдельно: за проход не больше стольких.
PROTOCOLS_PER_RUN = 300


def _details(conn, client: Client, limit: int, part: str = "all") -> int:
    try:
        if part in ("open", "all"):
            print(f"details: {details.refresh(conn, client, limit)}, "
                  f"results checked: {results.follow(conn, client)}, "
                  f"announced: {marks.announce_results(conn)}")
            _match_and_deliver(conn)
        if part in ("finished", "all"):
            print(f"finished details: {details.refresh_finished(conn, client, limit)}, "
                  f"protocols: {results.parse_pending(conn, PROTOCOLS_PER_RUN)}, "
                  f"old actions purged: {audit.purge(conn)}")
    except Exception as error:
        # Only the type: database errors can quote the connection string.
        notify.send(f"zakup: подробности объявлений упали с ошибкой {type(error).__name__}.")
        raise
    return 0


def _match_and_deliver(conn) -> None:
    lots = matcher.run(conn)
    announcements = matcher.run_filters(conn)
    print(f"matches: lots {lots}, announcements {announcements}, "
          f"delivered: {matcher.deliver(conn)}, reminders: {marks.remind(conn)}")


if __name__ == "__main__":
    sys.exit(main())
