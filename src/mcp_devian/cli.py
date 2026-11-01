import argparse
import hashlib
import html
import logging
import random
import sys
import time
import traceback

from . import background, da_access, da_market, db, digest, jobs, monitor
from . import deviantart as da
from . import toyhouse as th
from .config import DATA_DIR, load_config, load_dotenv

LOG_DIR = background.LOG_DIR
CRASH_STATE = DATA_DIR / "last-crash.txt"
MAX_LOG_BYTES = 1_000_000


def _problems(before: dict, results: list[monitor.SourceResult], mode: str = "bot", conn=None) -> list[str]:
    """Сообщаем об ошибке один раз, а не каждые 15 минут. Подсказки — словами для чата, без терминала.

    Пауза DeviantArt (403/429) — тоже один раз на серию (da_access.claim_notice), а не на каждой проверке
    и не на каждой ступени паузы; свой бюджет запросов уведомлением не сообщаем вовсе."""
    out = []
    for r in results:
        if r.error and r.error != before.get(r.source):
            hint = ""
            if r.needs_login and mode == "bot":
                hint = " Попроси Claude: «открой окно бота для входа»."
            out.append(f"{r.source}: {r.error}.{hint}")
        if r.limited and conn is not None and da_access.claim_notice(conn):
            out.append(f"{r.source}: {r.limited}.")
    return out


def _print_results(results: list[monitor.SourceResult]) -> None:
    for r in results:
        if r.skipped:
            print(f"{r.source}: пропуск ({r.skipped})")
            continue
        status = f"ошибка: {r.error}" if r.error else "ок"
        print(f"{r.source}: {status}; просмотрено {r.seen}, новых {r.new}, из них подходит {r.new_matched}")
        for note in r.notes[:5]:
            print(f"  ! {note}")


def _rotate_logs() -> None:
    for name in ("monitor.log", "monitor.err.log"):
        path = LOG_DIR / name
        try:
            if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
                path.replace(path.with_suffix(path.suffix + ".1"))
        except OSError:
            pass


def _report_crash(exc: BaseException) -> None:
    """Плановый запуск упал раньше, чем дошёл до дайджеста. Сообщаем один раз на одну и ту же ошибку."""
    text = f"{type(exc).__name__}: {exc}"
    key = hashlib.sha1(text.encode()).hexdigest()
    try:
        if CRASH_STATE.exists() and CRASH_STATE.read_text() == key:
            return
        CRASH_STATE.parent.mkdir(parents=True, exist_ok=True)
        CRASH_STATE.write_text(key)
    except OSError:
        pass
    digest.notify_macos("mcp_devian не запустился", f"{text[:180]}. Спроси у Claude: «что с мониторингом?»")


def cmd_check(args) -> int:
    if args.job:
        return _check_job(args)
    if args.scheduled:
        _rotate_logs()
        # Время слота запоминаем до случайной паузы, чтобы расписание не съезжало.
        tick = time.time()
        time.sleep(random.uniform(0, 90))
        try:
            code = _check(args, tick)
            CRASH_STATE.unlink(missing_ok=True)
            return code
        except Exception as e:
            traceback.print_exc()
            _report_crash(e)
            return 1
    return _check(args, None)


def _check_job(args) -> int:
    """Проверка, запущенная из чата (jobs.start): результат пишется в файл задачи."""
    try:
        cfg = load_config()
        sources = monitor.SOURCES if args.source == "all" else (args.source,)
        # Весь проход — один счёт запросов к DeviantArt (da_access): чистый проход сбрасывает ступень паузы.
        with db.session() as conn, da_access.session(conn):
            results = monitor.run_check(conn, cfg, sources=sources, force=True, interactive=True,
                                        manual_cooldown=True)
            lines = monitor.format_results(results)
            if any(r.source == "deviantart" and not r.skipped for r in results):
                bid_lines, events = monitor.refresh_auctions(conn, cfg)
                lines += bid_lines
                digest.notify_bids(cfg, events)
                lines += monitor.seller_pass(conn, cfg, enrich=False)
                lines += _market_sample(conn, cfg, None)
            if _toyhouse_read(results):
                # Она сама попросила проверку: в режиме chrome её аукционы-треды читаются только здесь.
                lines += _th_auctions(conn, cfg, None, interactive=True)
        jobs.finish(args.job, lines)
        return 0
    except Exception as e:
        traceback.print_exc()
        jobs.finish(args.job, [f"проверка упала: {type(e).__name__}: {e}"], ok=False)
        return 1


def _check(args, tick: float | None) -> int:
    cfg = load_config()
    for w in cfg.warnings:
        print(f"настройки: {w}", file=sys.stderr)
    sources = monitor.SOURCES if args.source == "all" else (args.source,)
    with db.session() as conn, da_access.session(conn):
        before = {s: (db.last_run(conn, s) or {"last_error": None})["last_error"] for s in monitor.SOURCES}
        results = monitor.run_check(conn, cfg, sources=sources, force=not args.scheduled, now=tick,
                                    interactive=not args.scheduled)
        _print_results(results)
        if any(r.source == "deviantart" and not r.skipped for r in results):
            bid_lines, events = monitor.refresh_auctions(conn, cfg)
            for line in bid_lines:
                print(line)
            digest.notify_bids(cfg, events)
            for line in monitor.seller_pass(conn, cfg, tick, enrich=args.scheduled):
                print(line)
            for line in _market_sample(conn, cfg, tick):
                print(line)
        if cfg.toyhouse.mode == "bot" and _toyhouse_read(results):
            # Её Chrome (режим chrome) в фоне не трогаем — только окно бота.
            for line in _th_auctions(conn, cfg, tick, interactive=not args.scheduled):
                print(line)
        if args.scheduled:
            # Время отправки — настоящее, не время слота: отправка партии идёт минутами.
            _send_scheduled(conn, cfg, None)
            try:
                report = digest.deliver_weekly(conn, cfg, tick)
            except Exception:  # noqa: BLE001 — отчёт не должен мешать дайджесту
                traceback.print_exc()
                report = None
            if report:
                print(f"отчёт недели → {report}")
        if args.notify or args.scheduled:
            sent = digest.deliver(conn, cfg, _problems(before, results, cfg.toyhouse.mode, conn))
            if sent["items"]:
                print(f"дайджест: {sent['items']} шт. → {sent['file']}")
            if sent["telegram_error"]:
                print(f"telegram: {sent['telegram_error']}")
    return 0


def _market_sample(conn, cfg, tick: float | None) -> list[str]:
    """Раз в 6 часов: чужие лоты её ниши поиском DA (только данные рынка). Ошибка не мешает проверке."""
    try:
        return da_market.maybe_sample(conn, cfg, tick)
    except Exception:  # noqa: BLE001 — maybe_sample и так не бросает; на всякий случай
        traceback.print_exc()
        return []


def _toyhouse_read(results: list[monitor.SourceResult]) -> bool:
    """Toyhouse только что прочитался без проверки Cloudflare и ошибок — можно дочитать аукционы."""
    return any(r.source == "toyhouse" and not r.skipped and not r.error and not r.needs_login for r in results)


def _th_auctions(conn, cfg, tick: float | None, interactive: bool) -> list[str]:
    """Её аукционы-треды Toyhouse: перечитать и уведомить о новых ставках. Ошибка не мешает проверке."""
    try:
        lines, events = monitor.refresh_th_auctions(conn, cfg, tick, interactive=interactive)
        digest.notify_bids(cfg, events)
        return lines
    except Exception:  # noqa: BLE001 — refresh_th_auctions и так не бросает; на всякий случай
        traceback.print_exc()
        return []


def _send_scheduled(conn, cfg, now: float | None) -> None:
    """Запланированные ею публикации, чьё время пришло. Итог — уведомлением."""
    from . import posting

    try:
        results = posting.send_due(conn, cfg, now)
    except Exception as e:  # noqa: BLE001 — отправка не должна ронять проверку
        traceback.print_exc()
        results = [{"draft_id": "?", "status": "failed", "result": f"{type(e).__name__}: {e}"}]
    told = []
    for r in results:
        print(f"публикация {r['draft_id']}: {r['status']} — {r['result']}")
        if r["status"] == "scheduled":
            continue  # другая отправка была занята — повторим на следующей проверке сами, без уведомления
        told.append(f"{r['draft_id']}: {r['result']}")
        if cfg.notify.macos:
            digest.notify_macos("Публикация на DeviantArt", f"{r['result']}"[:200])
    if told and cfg.notify.telegram:
        # Не только мак: «исход неизвестен» или отказ по расписанию она должна увидеть и в Telegram.
        err = digest.send_telegram(["\n".join(["Публикация на DeviantArt по расписанию",
                                                *(html.escape(t) for t in told)])])
        if err:
            print(f"telegram: {err}")


def cmd_digest(args) -> int:
    cfg = load_config()
    with db.session() as conn:
        rows = db.pending_digest(conn)
        if not rows:
            print("Ничего нового.")
            return 0
        print(digest.render_markdown(rows))
        if args.send:
            digest.deliver(conn, cfg)
    return 0


def cmd_login(args) -> int:
    print("Откроется Chrome с отдельным профилем для бота.")
    print("1. Если Toyhouse спросит «подтвердите, что вы человек» — нажми галочку.")
    print("2. Залогинься на Toyhouse во второй вкладке (по желанию и на DeviantArt).")
    print("3. Закрой все вкладки или нажми Cmd+Q — сессия сохранится.")
    try:
        with th.open_browser(headless=False) as context:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(th.board_url(13))
            context.new_page().goto("https://toyhou.se/~account/login")
            # На маке закрытие последнего окна не закрывает Chrome, поэтому ждём,
            # пока не останется открытых вкладок (или Chrome не закроют совсем).
            while context.pages:
                try:
                    context.pages[0].wait_for_timeout(500)
                except Exception:
                    break
    except th.BrowserBusy:
        print("Профиль сейчас занят проверкой, попробуй через пару минут.")
        return 1
    print("Готово, сессия сохранена.")
    return 0


def cmd_da_folders(args) -> int:
    try:
        with da.client() as http:
            folders = da.list_group_folders(http, args.group)
    except da.DAError as e:
        print(f"Не получилось: {e}")
        return 1
    if not folders:
        print("Папок не нашлось. Проверь имя группы.")
        return 1
    for f in folders:
        size = f" — {f.size} работ" if f.size is not None else ""
        print(f"{f.name or '—':32} {f.url}{size}")
    print("\nДобавить папки проще всего в чате с Claude: «добавь папки auctions и points adopts».")
    return 0


def cmd_status(args) -> int:
    from .mcp_server import describe_settings

    cfg = load_config()
    print(describe_settings(cfg).split("\n\n")[0])
    print(f"настройки: {cfg.path}")
    print(f"данные: {DATA_DIR}")
    with db.session() as conn:
        for s in monitor.SOURCES:
            row = db.last_run(conn, s)
            if not row:
                print(f"{s}: ещё не запускался")
                continue
            ok = digest.ago(row["last_ok_ts"]) if row["last_ok_ts"] else "никогда"
            err = f", ошибка: {row['last_error']}" if row["last_error"] else ""
            print(f"{s}: последний запуск {digest.ago(row['last_run_ts'])}, успешный {ok}{err}")
    return 0


def cmd_install(args) -> int:
    try:
        print(background.enable(max(15, args.minutes)))
    except (OSError, RuntimeError) as e:
        print(f"Не получилось: {e}")
        return 1
    return 0


def cmd_uninstall(args) -> int:
    print(background.disable())
    return 0


def cmd_send_draft(args) -> int:
    """Отправка черновика в отдельном процессе (запускает submit_draft)."""
    from . import posting

    cfg = load_config()
    with db.session() as conn:
        print(posting.send_any(conn, cfg, args.draft_id))
    return 0


def cmd_da_connect(args) -> int:
    from . import da_api

    cfg = load_config()
    if not cfg.posting.da_client_id:
        print("Нет client_id приложения DeviantArt.")
        return 1
    return da_api.connect_main(cfg.posting.da_client_id)


def cmd_serve(args) -> int:
    from .mcp_server import main as serve

    serve()
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    # Любой запрос к DeviantArt из этого процесса — в общем бюджете и с общей паузой (da_access).
    da_access.enable_default()
    # Логи только в stderr: stdout у MCP-сервера занят протоколом.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    p = argparse.ArgumentParser(prog="mcp-devian", description="Мониторинг адоптов на DeviantArt и Toyhouse")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="проверить источники сейчас")
    c.add_argument("source", nargs="?", default="all", choices=["all", *monitor.SOURCES])
    c.add_argument("--notify", action="store_true", help="разослать дайджест после проверки")
    c.add_argument("--scheduled", action="store_true", help="режим для launchd: пауза, расписание, дайджест")
    c.add_argument("--job", metavar="ID", help="запуск из чата: итог пишется в файл задачи с этим id")
    c.set_defaults(func=cmd_check)

    d = sub.add_parser("digest", help="показать, что накопилось")
    d.add_argument("--send", action="store_true", help="и разослать")
    d.set_defaults(func=cmd_digest)

    sub.add_parser("login", help="залогиниться в Chrome-профиле бота").set_defaults(func=cmd_login)

    f = sub.add_parser("da-folders", help="показать папки группы DA")
    f.add_argument("group")
    f.set_defaults(func=cmd_da_folders)

    sub.add_parser("status", help="что настроено и когда последний раз проверялось").set_defaults(func=cmd_status)

    i = sub.add_parser("install", help="включить автозапуск через launchd")
    i.add_argument("--minutes", type=int, default=15)
    i.set_defaults(func=cmd_install)
    sub.add_parser("uninstall", help="выключить автозапуск").set_defaults(func=cmd_uninstall)

    sub.add_parser("serve", help="MCP-сервер для Claude (stdio)").set_defaults(func=cmd_serve)

    sd = sub.add_parser("send-draft", help="отправить черновик (запускается из чата)")
    sd.add_argument("draft_id")
    sd.set_defaults(func=cmd_send_draft)
    sub.add_parser("da-connect", help="подключить DeviantArt (кнопка «Разрешить» в браузере)").set_defaults(func=cmd_da_connect)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
