import argparse
import getpass
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from datetime import time as day_time

import psycopg

from zakup import applications, audit, auth, bot, config, db, details, incidents, marks, matcher, results, sync
from zakup.goszakup import ENTITIES, PORTAL_TZ, Client

# Сколько истории грузить, если start-from не запускали.
DEFAULT_HISTORY_DAYS = 7


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zakup")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="создать или обновить таблицы")
    sub.add_parser("probe", help="проверить токен и показать по одной записи каждой сущности")
    start = sub.add_parser("start-from", help="начать синхронизацию с даты, без старой истории")
    start.add_argument("since", type=date.fromisoformat, help="ГГГГ-ММ-ДД")
    past = sub.add_parser("backfill-from", help="запустить догрузку истории в прошлое до даты")
    past.add_argument("since", type=date.fromisoformat, help="ГГГГ-ММ-ДД")
    back = sub.add_parser("backfill", help="шаг догрузки истории (служба history)")
    back.add_argument("--max-pages", type=int, default=100)
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
    sub.add_parser("codes", help="заполнить списки кодов регионов и разделов у старых записей")
    sub.add_parser("payments", help="запасная проверка неоплаченных заказов kazpayment")
    orgs = sub.add_parser("subjects", help="догрузить карточки организаций: заказчики, организаторы, победители")
    orgs.add_argument("--limit", type=int, default=600)
    sub.add_parser("client-bot", help="клиентский Telegram-бот и группа клиентов (постоянно)")
    sub.add_parser("autofix", help="дежурный: разбирает и исправляет сбои (на хосте, постоянно)")
    sub.add_parser("seller-alerts", help="продавцам в Telegram: новые лоты на их товары (каталог, этап 5)")
    sub.add_parser("tidy", help="база регионов по IP (раз в месяц) и чистка старой статистики посещений")
    sub.add_parser("nightly", help="ночной ревьюер: отчёт владельцу за сутки (на хосте)")
    sub.add_parser("bot", help="Telegram-бот: привязка чатов (работает постоянно)")
    securities_cmd = sub.add_parser("securities", help="проценты обеспечений из документов открытых объявлений")
    securities_cmd.add_argument("--loop", action="store_true", help="работать постоянно")
    scores_cmd = sub.add_parser("scores", help="оценки открытых лотов для фильтров поиска (lot_scores)")
    scores_cmd.add_argument("--loop", action="store_true", help="работать постоянно")
    specs_cmd = sub.add_parser("specs", help="заранее разобрать техспецификации открытых лотов")
    specs_cmd.add_argument("--limit", type=int, default=200)
    specs_cmd.add_argument("--loop", action="store_true", help="работать постоянно")
    skk_cmd = sub.add_parser("skk", help="закупки Самрук-Казыны с платформы zakup.gov.kz")
    skk_cmd.add_argument("--loop", action="store_true", help="работать постоянно (проход раз в 10 минут)")
    reserve_cmd = sub.add_parser("reserve", help="резерв госзакупок: свежие объявления и пункты плана с zakup.gov.kz")
    reserve_cmd.add_argument("--loop", action="store_true", help="работать постоянно (проход раз в 2 минуты)")
    oldportal_cmd = sub.add_parser("oldportal", help="резерв лотов со страниц old.goszakup.gov.kz, пока стоит API")
    oldportal_cmd.add_argument("--loop", action="store_true", help="работать постоянно")
    research_cmd = sub.add_parser("research", help="поиск товаров под лоты с ИИ (работает постоянно)")
    research_cmd.add_argument("--providers", default="claude_code",
                              help="через запятую: claude_code, anthropic, openai")
    research_cmd.add_argument("--once", action="store_true", help="одна задача и выход")
    sub.add_parser("id-migrate", help="перенести учётные записи в сервис учётных записей (Казахстан)")
    scrub_cmd = sub.add_parser("id-scrub", help="стереть здесь логины, пароли и телефоны перенесённых")
    scrub_cmd.add_argument("--yes", action="store_true", help="да, стереть (без него — только проверка)")
    passwd = sub.add_parser("set-password", help="задать пароль (создаст пользователя, если нет)")
    passwd.add_argument("email")
    user = sub.add_parser("add-user", help="завести пользователя")
    user.add_argument("email")
    user.add_argument("--telegram-chat-id")
    return parser


def main() -> int:
    args = _parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx пишет каждую строку запроса в INFO; при поиске старта их сотни.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command in LOCAL:
        return LOCAL[args.command](args) or 0
    return _with_portal(args)


# --- Команды без портала -------------------------------------------------------------


def _migrate(args) -> int:
    with db.connect(config.database_url()) as conn:
        db.migrate(conn)
    return 0


def _payments(args) -> int:
    from zakup import payments

    with db.connect(config.database_url()) as conn:
        if config.kazpayment_key():
            print(f"payments: {payments.poll(conn)}")
    return 0


def _autofix(args) -> int:
    from zakup import autofix

    autofix.run()
    return 0


def _seller_alerts(args) -> int:
    from zakup import offers, telegram

    with db.connect(config.database_url()) as conn:
        print(f"seller alerts sent: {offers.alerts(conn, telegram.send_message)}")
    return 0


def _tidy(args) -> int:
    from zakup import geo, photos, sellers, visits

    with db.connect(config.database_url()) as conn:
        print(f"geo: {geo.update(conn)}, purged visits/events: {visits.purge(conn)}")
        # Товары каталога без продавца (прежние находки; новые связывает products.remember).
        print(f"sellers linked: {sellers.link(conn)}")
        conn.commit()
        # Файлы фото без строки в базе (сбой загрузки) — том media есть и у архивного сборщика.
        print(f"orphan photo files removed: {photos.sweep(conn)}")
    return 0


def _nightly(args) -> int:
    from zakup import nightly

    return nightly.run()


def _client_bot(args) -> int:
    from zakup import client_bot

    client_bot.run(config.database_url())
    return 0


def _bot(args) -> int:
    bot.run(config.database_url())
    return 0


def _research(args) -> int:
    from zakup import research

    research.work(config.database_url(), args.providers.split(","), once=args.once)
    return 0


def _local_users_allowed() -> bool:
    """С сервисом учётных записей номера пользователей выдаёт он: своя запись здесь заняла бы
    чужой номер (docs/ACCOUNTS_KZ.md)."""
    from zakup import idservice
    if idservice.enabled():
        print("Включён сервис учётных записей (ZAKUP_ID_URL): пользователи и пароли — там.")
        return False
    return True


def _add_user(args) -> int:
    if not _local_users_allowed():
        return 1
    with db.connect(config.database_url()) as conn:
        row = conn.execute(
            """INSERT INTO users (email, telegram_chat_id) VALUES (%s, %s)
               ON CONFLICT (email) DO UPDATE SET telegram_chat_id = excluded.telegram_chat_id
               RETURNING id""",
            (args.email, args.telegram_chat_id),
        ).fetchone()
        conn.commit()
    print(row[0])
    return 0


def _backfill_from(args) -> int:
    from zakup import history

    with db.connect(config.database_url()) as conn:
        run = history.start(conn, args.since)
    print(f"Запуск {run}: служба history начнёт в течение минуты." if run else "Уже идёт другой запуск.")
    return 0 if run else 1


def _codes(args) -> int:
    with db.connect(config.database_url()) as conn:
        print(f"codes: {details.fill_codes(conn)}")
    return 0


LOCAL = {
    "codes": _codes,
    "backfill-from": _backfill_from,
    "migrate": _migrate, "payments": _payments, "autofix": _autofix, "tidy": _tidy, "seller-alerts": _seller_alerts, "nightly": _nightly,
    "client-bot": _client_bot, "bot": _bot, "research": _research, "add-user": _add_user,
    "specs": lambda args: _specs(args.limit, args.loop),
    "scores": lambda args: _scores(args.loop),
    "securities": lambda args: _securities(args.loop),
    "skk": lambda args: _skk(args.loop),
    "reserve": lambda args: _reserve(args.loop),
    "oldportal": lambda args: _oldportal(args.loop),
    "set-password": lambda args: _set_password(args.email),
    "id-migrate": lambda args: _id_migrate(),
    "id-scrub": lambda args: _id_scrub(args.yes),
}


# --- Команды с порталом goszakup ------------------------------------------------------


def _with_portal(args) -> int:
    token = config.goszakup_token()
    if not token:
        print("GOSZAKUP_TOKEN не задан.", file=sys.stderr)
        return 2
    client = Client(token, delay=config.goszakup_delay())
    try:
        if args.command == "probe":
            return _probe(client)
        with db.connect(config.database_url()) as conn:
            if args.command == "start-from":
                return _start_from(conn, client, args.since)
            if args.command == "backfill":
                return _backfill(conn, client, args.max_pages)
            if args.command == "rescan":
                return _rescan(conn, client, args.days, args.max_pages)
            if args.command == "subjects":
                from zakup import orgs, subjects

                print(f"subjects: {subjects.refresh(conn, client, args.limit)}")
                orgs.refresh_directory(conn)  # справочник /orgs — с новыми закупками и итогами
                return 0
            if args.command == "details":
                return _details(conn, client, args.limit, args.part)
            return _sync(conn, client, args.max_pages)
    finally:
        client.close()


def _scores(loop: bool) -> int:
    from zakup import scores

    return scores.run(config.database_url(), loop)


SKK_PAUSE = 600


def _skk(loop: bool) -> int:
    from zakup import skk

    client = skk.Client()
    try:
        while True:
            started = time.monotonic()
            try:
                with db.connect(config.database_url()) as conn:
                    print(f"skk: {skk.run(conn, client)}", flush=True)
            except (skk.PlatformError, psycopg.OperationalError, ValueError) as error:
                # Платформа или база недоступны — не падаем, пробуем на следующем проходе.
                logging.warning("skk: %s", error)
                if not loop:
                    return 1
            if not loop:
                return 0
            time.sleep(max(60, SKK_PAUSE - (time.monotonic() - started)))
    finally:
        client.close()


def _reserve(loop: bool) -> int:
    from zakup import reserve

    return reserve.loop(config.database_url(), once=not loop)


def _oldportal(loop: bool) -> int:
    from zakup import oldportal

    return oldportal.loop(config.database_url(), once=not loop)


SPECS_BATCH = 50


def _specs(limit: int, loop: bool) -> int:
    from zakup import spec

    batch = min(limit, SPECS_BATCH)
    while True:
        try:
            with db.connect(config.database_url()) as conn:
                done = spec.prefetch(conn, batch)
        except psycopg.OperationalError as error:
            # Короткий обрыв связи с базой — не повод падать: через минуту снова.
            logging.getLogger(__name__).warning("specs: %r", error)
            if not loop:
                return 1
            time.sleep(60)
            continue
        print(f"specs: {done}", flush=True)
        if not loop:
            return 0
        # Пока есть очередь (проход полный) — сразу дальше, иначе раз в 5 минут.
        if done < batch:
            time.sleep(300)


def _securities(loop: bool) -> int:
    from zakup import securities

    while True:
        try:
            with db.connect(config.database_url()) as conn:
                done = securities.prefetch(conn, securities.BATCH)
        except psycopg.OperationalError as error:
            # Короткий обрыв связи с базой — не повод падать: через минуту снова.
            logging.getLogger(__name__).warning("securities: %r", error)
            if not loop:
                return 1
            time.sleep(60)
            continue
        print(f"securities: {done}", flush=True)
        if not loop:
            return 0
        if done < securities.BATCH:
            time.sleep(300)


def _id_migrate() -> int:
    """Номера записей — на экран; логины, телефоны и пароли — нет."""
    from zakup import idservice

    if not idservice.enabled():
        print("Сервис учётных записей не настроен (ZAKUP_ID_*).", file=sys.stderr)
        return 1
    with db.connect(config.database_url()) as conn:
        result = idservice.migrate(conn)
    print(f"orgs: {len(result.get('orgs', []))}, ips: {result.get('ips', [0])[0]}")
    for key in ("imported", "present", "conflicts", "errors"):
        print(f"{key}: {len(result.get(key, []))} {result.get(key, [])}")
    return 1 if result.get("conflicts") or result.get("errors") else 0


def _id_scrub(yes: bool) -> int:
    """Без --yes — только проверка (то же, что сверит scrub); на экран — только номера."""
    from zakup import idservice

    if not idservice.enabled():
        print("Сервис учётных записей не настроен (ZAKUP_ID_*).", file=sys.stderr)
        return 1
    with db.connect(config.database_url()) as conn:
        stats = idservice.scrub(conn, dry_run=not yes)
    print(f"id-scrub{'' if yes else ' (проверка, без --yes ничего не стёрто)'}: {stats}")
    return 1 if stats["missing"] or stats["different"] or stats["orgs_missing"] or stats["ips_missing"] else 0


def _set_password(email: str) -> int:
    if not _local_users_allowed():
        return 1
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
    with db.connect(config.database_url()) as conn:
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


BACKFILL_LOCK = 7_402


def _backfill(conn, client: Client, max_pages: int) -> int:
    """Шаг службы history; запуск и остановка — в настройках (вкладка «Данные»)."""
    from zakup import history

    locked = conn.execute("SELECT pg_try_advisory_lock(%s)", (BACKFILL_LOCK,)).fetchone()[0]
    conn.commit()  # блокировка — на сеанс; транзакцию не держим
    if not locked:
        print("backfill: уже идёт в другом процессе")
        return 0
    try:
        print(f"backfill: {history.step(conn, client, max_pages)}")
    except Exception as error:
        conn.rollback()
        incidents.failed("догрузка истории", error)
        return _outage_or_raise("догрузка истории", error)
    return 0


def _outage_or_raise(stage: str, error: Exception) -> int:
    """Портал недоступен (уже записано incidents.failed) — одна строка в лог вместо трассировки:
    ошибки в коде нет (ночной отчёт 2026-11-13: 49 трассировок за сутки). Остальное — дальше."""
    if incidents.is_outage(error):
        logging.getLogger(__name__).warning("%s: портал недоступен: %s", stage, error)
        return 1
    raise error


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
        # Недоступность портала — одно сообщение за перерыв; остальное — на автоисправление.
        conn.rollback()
        incidents.failed("синхронизация", error)
        return _outage_or_raise("синхронизация", error)
    incidents.portal_ok(conn)
    try:
        incidents.check_lots(conn)
    except psycopg.Error as error:  # проверка — польза сверху, проход сборщика уже удался
        conn.rollback()
        logging.getLogger(__name__).warning("lots check failed: %r", error)
    return 0


RESCAN_LOCK = 7_401


def _rescan(conn, client: Client, days: int, max_pages: int) -> int:
    # Граница по id: самое старое объявление, опубликованное за эти дни. Черновики,
    # созданные раньше и опубликованные позже, сюда не попадут, но таких единицы.
    floor = conn.execute(
        "SELECT min(id) FROM announcements WHERE publish_date >= now() - make_interval(days => %s)"
        " AND system = 1",
        (days,),
    ).fetchone()[0]
    if floor is None:
        print("Нечего перечитывать: объявлений за эти дни нет.")
        return 0
    lot_floor = conn.execute(
        "SELECT min(id) FROM lots WHERE trd_buy_id >= %s AND system = 1", (floor,)
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
        # Недоступность портала — одно сообщение за перерыв; остальное — на автоисправление.
        conn.rollback()
        incidents.failed("повторный проход", error)
        return _outage_or_raise("повторный проход", error)
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
            # Необязательная проверка — после основной работы и не роняет её.
            try:
                print(f"trd-app: {applications.check(conn, client)}")
            except Exception:
                conn.rollback()
                logging.getLogger(__name__).exception("trd-app check")
    except Exception as error:
        # Недоступность портала — одно сообщение за перерыв; остальное — на автоисправление.
        conn.rollback()
        incidents.failed("подробности объявлений", error)
        return _outage_or_raise("подробности объявлений", error)
    return 0


def _match_and_deliver(conn) -> None:
    lots = matcher.run(conn)
    announcements = matcher.run_filters(conn)
    print(f"matches: lots {lots}, announcements {announcements}, "
          f"delivered: {matcher.deliver(conn)}, reminders: {marks.remind(conn)}")


if __name__ == "__main__":
    sys.exit(main())
