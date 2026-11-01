import argparse
import hashlib
import logging
import plistlib
import random
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

from . import db, digest, monitor
from . import deviantart as da
from . import toyhouse as th
from .config import DATA_DIR, PROJECT_DIR, load_config, load_dotenv

LAUNCHD_LABEL = "com.mcp-devian.monitor"
LAUNCHD_PLIST = Path("~/Library/LaunchAgents").expanduser() / f"{LAUNCHD_LABEL}.plist"
LOG_DIR = DATA_DIR / "logs"
CRASH_STATE = DATA_DIR / "last-crash.txt"
MAX_LOG_BYTES = 1_000_000


def _problems(before: dict, results: list[monitor.SourceResult]) -> list[str]:
    """Сообщаем об ошибке один раз, а не каждые 15 минут."""
    out = []
    for r in results:
        if r.error and r.error != before.get(r.source):
            hint = " Запусти: mcp-devian login" if r.needs_login else ""
            out.append(f"{r.source}: {r.error}.{hint}")
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
    digest.notify_macos("mcp_devian не запустился", f"{text[:180]}. Проверь config.toml и mcp-devian status")


def cmd_check(args) -> int:
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


def _check(args, tick: float | None) -> int:
    cfg = load_config()
    for w in cfg.warnings:
        print(f"config.toml: {w}", file=sys.stderr)
    sources = monitor.SOURCES if args.source == "all" else (args.source,)
    with db.session() as conn:
        before = {s: (db.last_run(conn, s) or {"last_error": None})["last_error"] for s in monitor.SOURCES}
        results = monitor.run_check(conn, cfg, sources=sources, force=not args.scheduled, now=tick,
                                    interactive=not args.scheduled)
        _print_results(results)
        if args.notify or args.scheduled:
            sent = digest.deliver(conn, cfg, _problems(before, results))
            if sent["items"]:
                print(f"дайджест: {sent['items']} шт. → {sent['file']}")
            if sent["telegram_error"]:
                print(f"telegram: {sent['telegram_error']}")
    return 0


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
        print(f"{f.name or '—':32} {f.url}")
    print("\nНужные строки скопируй в config.toml → [deviantart] groups.")
    return 0


def cmd_status(args) -> int:
    cfg = load_config()
    print(f"конфиг: {cfg.path or 'нет config.toml, работаю с настройками по умолчанию'}")
    for w in cfg.warnings:
        print(f"  ! {w}")
    print(f"данные: {DATA_DIR}")
    print(f"DA: групп {len(cfg.deviantart.groups)}, художников {len(cfg.deviantart.artists)}")
    print(f"TH: доски {cfg.toyhouse.boards}")
    with db.session() as conn:
        for s in monitor.SOURCES:
            row = db.last_run(conn, s)
            if not row:
                print(f"{s}: ещё не запускался")
                continue
            ok = digest.ago(row["last_ok_ts"]) if row["last_ok_ts"] else "никогда"
            err = f", ошибка: {row['last_error']}" if row["last_error"] else ""
            print(f"{s}: последний запуск {digest.ago(row['last_run_ts'])}, успешный {ok}{err}")
    print(f"автозапуск: {'включён' if LAUNCHD_PLIST.exists() else 'выключен'}")
    return 0


def cmd_install(args) -> int:
    uv = shutil.which("uv")
    if not uv:
        print("Не нашёл uv. Поставь: https://docs.astral.sh/uv/")
        return 1
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [uv, "run", "--project", str(PROJECT_DIR), "mcp-devian", "check", "--scheduled"],
        "WorkingDirectory": str(PROJECT_DIR),
        "StartInterval": args.minutes * 60,
        "RunAtLoad": True,
        "StandardOutPath": str(LOG_DIR / "monitor.log"),
        "StandardErrorPath": str(LOG_DIR / "monitor.err.log"),
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"},
    }
    LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
    # Если агент ещё не загружен, unload ругается — это нормально.
    subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)], capture_output=True, check=False)
    LAUNCHD_PLIST.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "load", str(LAUNCHD_PLIST)], check=True)
    print(f"Автозапуск включён: раз в {args.minutes} мин, пока мак не спит.")
    print(f"Логи: {LOG_DIR}")
    return 0


def cmd_uninstall(args) -> int:
    if LAUNCHD_PLIST.exists():
        subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)], capture_output=True, check=False)
        LAUNCHD_PLIST.unlink()
        print("Автозапуск выключен.")
    else:
        print("Автозапуск и так выключен.")
    return 0


def cmd_serve(args) -> int:
    from .mcp_server import main as serve

    serve()
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    # Логи только в stderr: stdout у MCP-сервера занят протоколом.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    p = argparse.ArgumentParser(prog="mcp-devian", description="Мониторинг адоптов на DeviantArt и Toyhouse")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="проверить источники сейчас")
    c.add_argument("source", nargs="?", default="all", choices=["all", *monitor.SOURCES])
    c.add_argument("--notify", action="store_true", help="разослать дайджест после проверки")
    c.add_argument("--scheduled", action="store_true", help="режим для launchd: пауза, расписание, дайджест")
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

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
