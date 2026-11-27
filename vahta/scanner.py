"""Фоновый обход вотчлиста.

Делает две вещи: замечает кампании по играм, которые тебе нужны, и копит
статистику доступности эфира. Второе важнее — без него планировщик не сможет
отличить «успею до дедлайна» от «не успею», потому что календарного времени
почти всегда хватает, а эфирного с дропсами — далеко не всегда.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from time import monotonic

from . import constants as tc
from . import drops as dr
from . import watcher as w
from .api import Twitch
from .store import Store

log = logging.getLogger("vahta")

SCAN_INTERVAL = 1800.0  # полчаса: чаще нет смысла, реже — грубая статистика


@dataclass
class GameStatus:
    game_id: str
    name: str
    campaigns: list[dr.Campaign]
    available: bool  # есть канал, на котором капает нужная кампания
    fresh: list[dr.Campaign]
    air: bool = False  # по игре вообще кто-то стримит — это не то же самое
    # брать ли в работу кампании без привязанного игрового аккаунта. Настройка
    # человека, а не свойство игры: идёт ли по таким прогресс — вопрос открытый
    unlinked: bool = False

    @property
    def farmable(self) -> list[dr.Campaign]:
        return [item for item in self.campaigns if item.account_linked or self.unlinked]

    @property
    def remaining_minutes(self) -> int:
        return sum(item.remaining_minutes for item in self.farmable)


async def resolve_game(tw: Twitch, name: str) -> tuple[str, str, str]:
    """Название игры -> (id, каноничное название, slug).

    Заодно проверяет, что Twitch вообще знает такую игру.
    """
    slug = await w.game_slug(tw, name)
    data = await tw.gql(tc.game_directory(slug, 1))
    node = data.get("game") or {}
    return (
        str(node.get("id") or ""),
        node.get("displayName") or node.get("name") or name,
        slug,
    )


async def scan(
    tw: Twitch,
    store: Store,
    unlinked: bool = False,
    inv: dr.Inventory | None = None,
    on_game=None,
) -> list[GameStatus]:
    """Пройти вотчлист: запомнить кампании, замерить доступность.

    `unlinked` — считать ли рабочими кампании без привязанного игрового
    аккаунта; настройка приходит сюда, потому что дальше по ней строится план.

    `inv` — уже прочитанный инвентарь. Он один на аккаунт, а запрашивался
    по разу на каждую игру вотчлиста: десять игр — десять одинаковых запросов
    за обход. Прочитанный один раз, он же годится всем.

    `on_game(индекс, всего, название)` — куда обход дошёл. Нужен потому, что он
    долгий: на прогретой базе одиннадцать игр обходятся за 42 секунды, а
    на пустой (первый запуск, детали всех кампаний неизвестны) — минутами.
    Всё это время экран говорил одно «обхожу список игр», и разницы между
    «работает» и «завис» человеку было не видно.
    """
    rows = store.watchlist()
    if not rows:
        return []
    inv = inv or await dr.inventory(tw)

    active: dict[str, list[dr.Campaign]] = {}
    watched_ids = {row["game_id"] for row in rows}
    for item in await dr.farming_campaigns(tw, inv, lambda c: c.game_id in watched_ids):
        active.setdefault(item.game_id, []).append(item)

    result: list[GameStatus] = []
    started = monotonic()
    for number, row in enumerate(rows, start=1):
        game_id = row["game_id"]
        found = active.get(game_id, [])
        if on_game:
            on_game(number, len(rows), row["game_name"])

        # детали тянем только у незнакомых кампаний — иначе каждый скан
        # превращался бы в десятки лишних запросов
        detailed: list[dr.Campaign] = []
        fresh: list[dr.Campaign] = []
        for item in found:
            known = store.knows_campaign(item.id)
            full = await dr.active_campaign_details(tw, item)
            if full is None:
                continue
            # обложку отдаёт только список кампаний, в деталях её нет
            store.remember(full, item.box_art)
            detailed.append(full)
            if not known:
                fresh.append(full)
        for camp in detailed:
            store.restore_progress(camp)
        await dr.merge_progress(tw, detailed, inv)
        for camp in detailed:
            store.restore_progress(camp)
            store.remember_progress(camp)

        # Доступность меряем по всем кампаниям, а не только по фармящимся:
        # эфир — свойство игры, привязка аккаунта влияет лишь на то, капает ли
        # он нам. Иначе после привязки история выглядела бы искусственно пустой.
        available = air = False
        if detailed:
            available = await w.pick_channel(tw, row["slug"], detailed) is not None
            # «Нет канала с нужной кампанией» и «нет эфира вовсе» — разные вещи.
            # У Overwatch стримы идут круглосуточно, а обе кампании — только
            # на своих каналах: пока это было одним числом, ноль читался как
            # «игру никто не стримит», и план обещал фармить её по 14 минут
            # в сутки шесть дней подряд. Спрашиваем только когда канала
            # не нашлось: нашёлся — значит эфир был, и второй запрос не нужен.
            air = available or bool(await w.live_channels(tw, row["slug"], limit=1))
            store.record_uptime(game_id, available, air)

        result.append(
            GameStatus(
                game_id=game_id,
                name=row["game_name"],
                campaigns=detailed,
                available=available,
                air=air,
                fresh=fresh,
                unlinked=unlinked,
            )
        )
    # Одна строка на весь обход, зато с длительностью: без неё «почему фарм
    # не начинается» из лога не отвечался — между строками просто зияли минуты.
    # Первый обход на пустой базе дороже прогретого в разы: детали кампаний
    # тянутся у всех незнакомых, а знакомых там нет
    log.info(
        "Обход: %d игр, %d кампаний, в эфире %d — %.0f с",
        len(result),
        sum(len(item.campaigns) for item in result),
        sum(item.available for item in result),
        monotonic() - started,
    )
    return result


async def scan_loop(tw: Twitch, store: Store, interval: float = SCAN_INTERVAL) -> None:
    while True:
        try:
            statuses = await scan(tw, store)
            live = sum(item.available for item in statuses)
            log.info(
                "Скан: %d игр, в эфире %d, следующий через %s",
                len(statuses),
                live,
                f"{round(interval / 60)} мин" if interval >= 60 else f"{interval:.0f} сек",
            )
            for status in statuses:
                for item in status.fresh:
                    log.info(
                        "Новая кампания: %s — %s, до %s",
                        status.name,
                        item.name,
                        item.ends_at.strftime("%d.%m %H:%M") if item.ends_at else "?",
                    )
        except Exception as exc:  # скан не должен ронять долгоживущий процесс
            log.error("Скан сорвался: %s: %s", type(exc).__name__, exc)
        await asyncio.sleep(interval)
