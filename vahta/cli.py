"""CLI ядра. Задача этого этапа — убедиться, что прогресс реально капает."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import timedelta
from pathlib import Path

from .api import LoginRequired, Twitch, TwitchError
from .store import Store
from . import drops as dr
from . import planner
from . import scanner
from . import watcher as w

from .paths import DATA_DIR, DB_PATH


def setup_logging(verbose: bool) -> None:
    # на русской Windows консоль по умолчанию не utf-8
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def cmd_login(args) -> None:
    async with Twitch(DATA_DIR) as tw:
        if args.forget:
            tw.forget()
            print("Токен удалён")
            return
        await tw.ensure_login()
        print(f"Вошёл как {tw.login_name} (id {tw.user_id})")


async def cmd_campaigns(args) -> None:
    async with Twitch(DATA_DIR) as tw:
        await tw.ensure_login(interactive=False)
        campaigns = [item for item in await dr.list_campaigns(tw) if item.active]
        campaigns.sort(key=lambda x: (x.game, x.ends_at or dr.now()))
        if not campaigns:
            print("Активных кампаний нет")
            return
        for item in campaigns:
            link = "" if item.account_linked else "   [аккаунт не привязан]"
            print(f"{item}{link}")
        print(f"\nВсего: {len(campaigns)}")


async def cmd_inventory(args) -> None:
    async with Twitch(DATA_DIR) as tw:
        await tw.ensure_login(interactive=False)
        inv = await dr.inventory(tw)
        for item in inv.campaigns:
            print(f"\n{item}")
            for drop in item.drops:
                print(f"    {drop}")
        if not inv.campaigns:
            print("Кампаний в процессе нет")
        # Награды — вторая ветка инвентаря, и в ней всё, что аккаунт когда-либо
        # получил: по кампаниям видно только то, что идёт прямо сейчас
        print(f"\nНаград за всё время: {len(inv.rewards)}")
        if args.rewards:
            for item in sorted(
                inv.rewards, key=lambda r: r.awarded_at or dr.LONG_AGO, reverse=True
            ):
                when = item.awarded_at.astimezone().strftime("%d.%m.%Y") if item.awarded_at else "?"
                count = f" ×{item.count}" if item.count > 1 else ""
                print(f"  {when}  {item.name}{count}")


async def cmd_claim(args) -> None:
    async with Twitch(DATA_DIR) as tw:
        await tw.ensure_login(interactive=False)
        taken = await dr.claim_pending(tw)
        with Store(DB_PATH) as store:
            for campaign, drop in taken:
                store.record_claim(campaign, drop)
                print(f"  {drop.name} — {campaign.game}")
        print(f"Забрано дропов: {len(taken)}")


def _hours(minutes: int) -> str:
    return f"{minutes // 60} ч" if minutes >= 60 else f"{minutes} мин"


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 100 in (11, 12, 13, 14):
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)


async def cmd_history(args) -> None:
    """Что уже отфармлено, с группировкой по игре."""
    with Store(DB_PATH) as store:
        if args.game:
            rows = [
                row
                for row in store.claims()
                if args.game.casefold() in row["game_name"].casefold()
            ]
            if not rows:
                print(f"По «{args.game}» пока ничего не забрано")
                return
            for row in rows:
                when = row["claimed_at"][:16].replace("T", " ")
                print(f"  {when}  {row['drop_name']}  ({row['campaign_name']})")
            print(f"\nВсего: {len(rows)}")
            return

        totals = store.totals()
        games = store.games()
        if not games:
            print("История пуста — запусти 'watch', и она начнёт копиться")
            return

        for row in games:
            drops = _plural(row["drops"], "дроп", "дропа", "дропов")
            camps = _plural(row["campaigns"], "кампания", "кампании", "кампаний")
            print(
                f"  {row['name']:<30} {row['drops']:>4} {drops:<7} "
                f"{_hours(row['minutes']):>8}   {row['campaigns']} {camps}"
            )
        print(
            f"\nВсего: {totals['drops']} "
            f"{_plural(totals['drops'], 'дроп', 'дропа', 'дропов')}, "
            f"{_hours(totals['minutes'])} по {totals['games']} "
            f"{_plural(totals['games'], 'игре', 'играм', 'играм')}. "
            f"За неделю: {_hours(totals['minutes_week'])}"
        )


async def cmd_uptime(args) -> None:
    """Насколько часто по игре вообще есть канал с дропсами."""
    with Store(DB_PATH) as store:
        for row in store.games():
            share = store.availability(row["id"], args.days)
            value = "нет данных" if share < 0 else f"{share * 100:.0f}% времени"
            print(f"  {row['name']:<32} {value}")


async def cmd_channels(args) -> None:
    """Кто в эфире и какую из кампаний игры он крутит."""
    async with Twitch(DATA_DIR) as tw:
        await tw.ensure_login(interactive=False)
        slug = await w.game_slug(tw, args.game)
        campaigns = await dr.find_campaigns(tw, args.game)
        if not campaigns:
            print(f"Активных кампаний по «{args.game}» нет")
            return

        names = {item.id: item.name for item in campaigns}
        print(f"Кампаний по «{args.game}»: {len(campaigns)}")
        for item in campaigns:
            ends = item.ends_at.strftime("%d.%m %H:%M") if item.ends_at else "?"
            limit = (
                f", только {len(item.allowed_channels)} каналов"
                if item.allowed_channels
                else ""
            )
            print(f"    {item.name} — до {ends}, осталось {item.remaining_minutes} мин{limit}")

        print("\nВ эфире:")
        for channel in await w.live_channels(tw, slug, args.limit):
            live = await w.channel_campaigns(tw, channel)
            mine = [names[i] for i in live if i in names]
            print(
                f"    {channel.display_name:<26} {channel.viewers:>7}  "
                f"{', '.join(mine) or '—'}"
            )


async def cmd_watchlist(args) -> None:
    """Игры, за которыми следим. Порядок = приоритет при конфликте."""
    with Store(DB_PATH) as store:
        rows = store.watchlist()
        if not rows:
            print("Вотчлист пуст. Добавить: vahta watchlist-add \"Rust\"")
            return
        for row in rows:
            share = store.availability(row["game_id"], args.days)
            uptime = "—" if share < 0 else f"эфир {share * 100:.0f}%"
            print(f"  {row['priority']:>2}. {row['game_name']:<30} {uptime}")


async def cmd_watchlist_add(args) -> None:
    async with Twitch(DATA_DIR) as tw:
        game_id, name, slug = await scanner.resolve_game(tw, args.game)
        if not game_id:
            print(f"Twitch не знает игру «{args.game}»", file=sys.stderr)
            return
        with Store(DB_PATH) as store:
            if store.watch_add(game_id, name, slug):
                print(f"Добавил: {name}")
            else:
                print(f"{name} уже в вотчлисте")


async def cmd_watchlist_rm(args) -> None:
    with Store(DB_PATH) as store:
        row = store.watch_find(args.game)
        if row is None:
            print(f"«{args.game}» в вотчлисте нет", file=sys.stderr)
            return
        store.watch_remove(row["game_id"])
        print(f"Убрал: {row['game_name']}")


async def cmd_watchlist_move(args) -> None:
    with Store(DB_PATH) as store:
        row = store.watch_find(args.game)
        if row is None:
            print(f"«{args.game}» в вотчлисте нет", file=sys.stderr)
            return
        store.watch_move(row["game_id"], args.position)
        print(f"{row['game_name']} теперь на месте {args.position}")


async def cmd_scan(args) -> None:
    """Обойти вотчлист: найти кампании и замерить доступность эфира."""
    async with Twitch(DATA_DIR) as tw:
        await tw.ensure_login(interactive=False)
        with Store(DB_PATH) as store:
            if args.loop:
                await scanner.scan_loop(tw, store, args.interval)
                return

            statuses = await scanner.scan(tw, store)
            if not statuses:
                print("Вотчлист пуст")
                return
            for item in statuses:
                if not item.campaigns:
                    print(f"  {item.name:<30} кампаний нет")
                    continue
                mark = "в эфире" if item.available else "нет эфира"
                blocked = len(item.campaigns) - len(item.farmable)
                note = f", {blocked} без привязки" if blocked else ""
                print(
                    f"  {item.name:<30} {len(item.campaigns)} "
                    f"{_plural(len(item.campaigns), 'кампания', 'кампании', 'кампаний')}"
                    f"{note}, {item.remaining_minutes} мин — {mark}"
                )
                for fresh in item.fresh:
                    ends = fresh.ends_at.strftime("%d.%m %H:%M") if fresh.ends_at else "?"
                    print(f"       новая: {fresh.name} (до {ends})")


def _when(moment) -> str:
    return moment.astimezone().strftime("%d.%m %H:%M")


async def cmd_plan(args) -> None:
    """Построить расписание фарма и показать, что не помещается."""
    async with Twitch(DATA_DIR) as tw:
        await tw.ensure_login(interactive=False)
        with Store(DB_PATH) as store:
            statuses = await scanner.scan(tw, store)
            if not statuses:
                print("Вотчлист пуст")
                return
            tasks = planner.make_tasks(statuses, store)
            plan = planner.build(tasks, timedelta(days=args.days))

        if not plan.tasks:
            print("Фармить нечего: активных кампаний по вотчлисту нет")
            return

        print(f"План на {args.days} дней, с {_when(plan.starts_at)}\n")
        # Отрезки пересекаются: пока по одной игре нет эфира, идёт другая.
        # Поэтому в строке и минуты эфира, и доля календаря, которая на них уйдёт
        for block in plan.blocks:
            print(
                f"  {_when(block.start)} – {_when(block.end)}  "
                f"{block.task.game:<20} {block.task.campaign[:34]:<34} "
                f"{round(block.minutes):>5} мин эфира ({block.share:.0%} календаря)"
            )

        idle = sum(t.minutes for t in plan.tasks)
        print(f"\n  Всего работы: {idle} мин эфира по {len(plan.tasks)} "
              f"{_plural(len(plan.tasks), 'кампании', 'кампаниям', 'кампаниям')}")

        if plan.feasible:
            print("  Всё помещается в дедлайны")
        else:
            print(f"\n  Не помещается ({len(plan.shortfalls)}):")
            for gap in plan.shortfalls:
                print(
                    f"    {gap.task.game} — {gap.task.campaign}: "
                    f"не хватает {round(gap.missing_minutes)} мин эфира "
                    f"до {_when(gap.task.deadline)}"
                )
                for what, cost in planner.resolutions(gap, plan):
                    print(f"        {what} — {cost}")

        if plan.unconfident:
            names = sorted({t.game for t in plan.unconfident})
            print(
                f"\n  Мало замеров эфира: {', '.join(names)}. "
                "Пока считаю их доступными всегда — оценка оптимистичная. "
                "Держи 'scan --loop' запущенным, чтобы статистика набралась."
            )


async def cmd_telegram(args) -> None:
    """Записать токен бота. Владельцем станет первый, кто напишет /start."""
    from .telegram import Config

    config = Config(DATA_DIR / "telegram.json")
    if args.forget:
        (DATA_DIR / "telegram.json").unlink(missing_ok=True)
        print("Настройки Telegram удалены")
        return
    if args.token:
        config.token = args.token
        config.chat_id = None
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        config.save()
        print("Токен сохранён. Запусти 'serve' и напиши боту /start — "
              "первый написавший станет владельцем.")
        return
    if not config.token:
        print("Токен не задан. Возьми у @BotFather и передай: telegram <токен>")
        return
    print(f"Токен есть. Владелец: {config.chat_id or 'ещё не назначен'}")


async def _ensure_login() -> None:
    async with Twitch(DATA_DIR) as tw:
        await tw.ensure_login()
        print(f"Twitch: {tw.login_name}")


def cmd_serve(args) -> None:
    """Служба фармит и планирует, веб-интерфейс её показывает."""
    import threading
    import webbrowser

    from . import web
    from .service import Service

    # веб-сервер логиниться не умеет — код показывать некому, делаем это здесь
    asyncio.run(_ensure_login())

    service = Service()
    service.start()
    web.attach(service)

    url = f"http://{args.host}:{args.port}"
    print(f"Вахта на {url}  (Ctrl+C для выхода)")
    if args.open:
        # сервер поднимается пару секунд, раньше открывать нечего
        threading.Timer(3.0, lambda: webbrowser.open(url)).start()
    try:
        web.serve(args.host, args.port)
    finally:
        service.stop()


async def cmd_watch(args) -> None:
    async with Twitch(DATA_DIR) as tw:
        await tw.ensure_login(interactive=False)
        with Store(DB_PATH) as store:
            # Здесь фармится одна игра по прямой просьбе, плана и очереди нет —
            # значит и делить время не с кем, ломоть не нужен
            await w.watch(
                tw, args.game, store=store, minutes=None, unlinked=args.unlinked
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vahta", description="Фарм Twitch-дропсов")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="войти в Twitch")
    p.add_argument("--forget", action="store_true", help="забыть сохранённый токен")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("campaigns", help="активные кампании")
    p.set_defaults(func=cmd_campaigns)

    p = sub.add_parser("inventory", help="прогресс по кампаниям в процессе")
    p.add_argument("--rewards", action="store_true", help="и все полученные награды списком")
    p.set_defaults(func=cmd_inventory)

    p = sub.add_parser("claim", help="забрать всё готовое")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("channels", help="живые каналы по игре и есть ли на них дропсы")
    p.add_argument("game")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_channels)

    p = sub.add_parser("watch", help="фармить игру")
    p.add_argument("game")
    p.add_argument(
        "--unlinked",
        action="store_true",
        help="брать и кампании без привязанного игрового аккаунта",
    )
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("history", help="что отфармлено, по играм")
    p.add_argument("game", nargs="?", help="показать дропы одной игры")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("uptime", help="как часто по игре есть канал с дропсами")
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(func=cmd_uptime)

    p = sub.add_parser("watchlist", help="игры, за которыми следим")
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(func=cmd_watchlist)

    p = sub.add_parser("watchlist-add", help="добавить игру в вотчлист")
    p.add_argument("game")
    p.set_defaults(func=cmd_watchlist_add)

    p = sub.add_parser("watchlist-rm", help="убрать игру из вотчлиста")
    p.add_argument("game")
    p.set_defaults(func=cmd_watchlist_rm)

    p = sub.add_parser("watchlist-move", help="сменить приоритет игры")
    p.add_argument("game")
    p.add_argument("position", type=int)
    p.set_defaults(func=cmd_watchlist_move)

    p = sub.add_parser("scan", help="обойти вотчлист: кампании и доступность эфира")
    p.add_argument("--loop", action="store_true", help="крутиться в фоне")
    p.add_argument("--interval", type=float, default=scanner.SCAN_INTERVAL)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("plan", help="расписание фарма и разбор конфликтов")
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("telegram", help="настроить Telegram-бота")
    p.add_argument("token", nargs="?", help="токен от @BotFather")
    p.add_argument("--forget", action="store_true", help="забыть настройки")
    p.set_defaults(func=cmd_telegram)

    p = sub.add_parser("serve", help="веб-интерфейс и автопилот")
    p.add_argument("--port", type=int, default=8420)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--open", action="store_true", help="открыть браузер")
    p.set_defaults(func=cmd_serve, sync=True)

    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    try:
        if getattr(args, "sync", False):
            args.func(args)  # uvicorn заводит свой цикл событий
        else:
            asyncio.run(args.func(args))
    except KeyboardInterrupt:
        print("\nОстановлено")
    except LoginRequired as e:
        print(f"{e}", file=sys.stderr)
        return 2
    except (TwitchError, LookupError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
