"""Инвентарь Twitch: дропы, которые приходят сами, и награды без игры.

Ради чего это написано. Дроп может прийти без клейма — у benefit'а бывает
`distributionType: DIRECT_ENTITLEMENT`, и тогда забирать нечего, награда
приходит сама. «Вахта» принимала это за отказ Twitch: трижды писала «дроп
готов, но не отдают» и откладывала кампанию на час. С THE FINALS так вышло
восемь кругов подряд по 266/240 минут — то есть часы просмотра впустую при
уже полученной награде.

Сверять по одному id награды нельзя: `totalCount` в инвентаре за всё время,
и та же награда за прошлую неделю пометила бы веху забранной зря. У Ксена
ровно такой случай — «Tactical Stockpile» выдан 18 июля, а окно нынешнего
дропа началось 24-го. Поэтому здесь проверяется именно окно.

Сети нет: `FakeTwitch` отдаёт заранее собранный ответ `Inventory`.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vahta import constants as c
from vahta import drops as dr
from vahta import scanner
from vahta import watcher as w
from vahta.api import LoginRequired
from vahta.store import Store

from test_watcher import CHANNEL, FakeHttp

WINDOW_FROM = datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)
WINDOW_TO = datetime(2026, 7, 31, 13, 0, tzinfo=timezone.utc)
INSIDE = WINDOW_FROM + timedelta(days=5)
BEFORE = WINDOW_FROM - timedelta(days=6)


def check(label: str, condition: bool) -> bool:
    print(f"  {'ок  ' if condition else 'ПЛОХО'}  {label}")
    return bool(condition)


def drop(
    drop_id: str,
    minutes: int,
    *,
    benefits: list[str],
    claimed: bool = False,
) -> dr.Drop:
    return dr.Drop(
        id=drop_id,
        name=f"Награда {drop_id}",
        required_minutes=minutes,
        current_minutes=minutes,
        claimed=claimed,
        benefit_ids=benefits,
        starts_at=WINDOW_FROM,
        ends_at=WINDOW_TO,
    )


def campaign(drops: list[dr.Drop]) -> dr.Campaign:
    return dr.Campaign(
        id="camp-1",
        name="Summer 2026, Week 3",
        game="Stalzone",
        game_id="516866",
        starts_at=WINDOW_FROM,
        ends_at=WINDOW_TO,
        account_linked=True,
        drops=drops,
        game_slug="stalcraft",
    )


def reward(benefit: str, when: datetime | None) -> dr.Reward:
    return dr.Reward(id=benefit, name=f"Награда {benefit}", awarded_at=when, count=3)


def check_windows() -> bool:
    """Сверка награды с окном дропа — сердце всей задачи."""
    inside = campaign([drop("d1", 60, benefits=["b1"])])
    ok = check(
        "награда в окне закрывает веху",
        bool(dr.mark_granted([inside], [reward("b1", INSIDE)]))
        and inside.drops[0].claimed
        and inside.drops[0].granted,
    )

    old = campaign([drop("d1", 840, benefits=["b1"])])
    ok &= check(
        "та же награда за прошлую неделю — не закрывает (случай Tactical Stockpile)",
        not dr.mark_granted([old], [reward("b1", BEFORE)]) and not old.drops[0].claimed,
    )

    never = campaign([drop("d1", 60, benefits=["b1"])])
    ok &= check(
        "невыданная награда веху не закрывает",
        not dr.mark_granted([never], [reward("b1", None)]),
    )

    half = campaign([drop("d1", 60, benefits=["b1", "b2"])])
    ok &= check(
        "дроп из двух наград не закрывается одной",
        not dr.mark_granted([half], [reward("b1", INSIDE)]),
    )
    ok &= check(
        "а обеими — закрывается",
        bool(dr.mark_granted([half], [reward("b1", INSIDE), reward("b2", INSIDE)])),
    )

    blind = campaign([dr.Drop(id="d1", name="Без наград", required_minutes=60)])
    ok &= check(
        "дроп без известных наград не закрывается ничем",
        not dr.mark_granted([blind], [reward("b1", INSIDE)]),
    )

    # Ловушка, которая дороже всех остальных: одна и та же награда на четырёх
    # вехах кампании (у Stalzone так стоят «Twitch Shards» на 150, 300, 510
    # и 600 минутах), а момент выдачи один. Закрыть ими всё разом — значит уйти
    # с кампании, не добрав дропы
    shards = campaign([
        drop("d150", 150, benefits=["shards"]),
        drop("d300", 300, benefits=["shards"]),
        drop("d600", 600, benefits=["shards"]),
    ])
    for item in shards.drops:
        item.current_minutes = 150  # отсмотрено ровно до первой вехи
    closed = dr.mark_granted([shards], [reward("shards", INSIDE)])
    ok &= check(
        "делённая награда закрывает только отсмотренную веху",
        [item.id for item in closed] == ["d150"],
    )
    ok &= check(
        "а неотсмотренные вехи остаются работой",
        shards.remaining_minutes == 450,
    )

    # Прогресс внутри кампании общий: помечая веху забранной, нельзя менять
    # минуты остальных — иначе поедет вся работа по кампании
    two = campaign([drop("d1", 60, benefits=["b1"]), drop("d2", 840, benefits=["b2"])])
    dr.mark_granted([two], [reward("b1", INSIDE)])
    ok &= check(
        "закрытая веха не трогает минуты остальных",
        two.drops[1].current_minutes == 840 and not two.drops[1].claimed,
    )
    ok &= check("а работа по кампании считается по незакрытым", two.remaining_minutes == 0)
    return ok


class FakeTwitch:
    """Инвентарь: кампании в процессе и все полученные награды.

    `in_progress` — какие кампании Twitch показывает в инвентаре; в случае
    THE FINALS там не было ни одной, а награда в `gameEventDrops` лежала.
    """

    def __init__(self, awards: dict[str, datetime | None], in_progress: bool = False):
        self.awards = awards
        self.in_progress = in_progress
        self.asked = 0
        self.user_id = "42"
        self.http = FakeHttp()
        self.asked_stream = 0
        self.claimed: list[str] = []

    def _drop(self, drop_id: str, minutes: int, benefit: str) -> dict:
        return {
            "id": drop_id,
            "name": f"Награда {drop_id}",
            "requiredMinutesWatched": minutes,
            "startAt": WINDOW_FROM.isoformat().replace("+00:00", "Z"),
            "endAt": WINDOW_TO.isoformat().replace("+00:00", "Z"),
            "benefitEdges": [{"benefit": {"id": benefit, "name": "Награда"}}],
            "self": {"currentMinutesWatched": minutes, "isClaimed": False},
        }

    async def gql(self, query: dict):
        if query["operationName"] == "PlaybackAccessToken":
            return {"streamPlaybackAccessToken": {"value": "test", "signature": "test"}}
        name = query["operationName"]
        if name == "Inventory":
            self.asked += 1
            camps = []
            if self.in_progress:
                camps.append(
                    {
                        "id": "camp-1",
                        "name": "Summer 2026, Week 3",
                        "game": {
                            "id": "516866",
                            "name": "Stalzone",
                            "slug": "stalcraft",
                            "boxArtURL": "art.jpg",
                        },
                        "startAt": WINDOW_FROM.isoformat().replace("+00:00", "Z"),
                        "endAt": WINDOW_TO.isoformat().replace("+00:00", "Z"),
                        "self": {"isAccountConnected": True},
                        "timeBasedDrops": [self._drop("d1", 240, "b1")],
                    }
                )
            return {
                "currentUser": {
                    "inventory": {
                        "dropCampaignsInProgress": camps,
                        "gameEventDrops": [
                            {
                                "id": benefit,
                                "name": "Награда",
                                "imageURL": "img.png",
                                "lastAwardedAt": (
                                    when.isoformat().replace("+00:00", "Z") if when else None
                                ),
                                "totalCount": 3,
                                "isConnected": True,
                                "requiredAccountLink": "",
                                "game": None,
                            }
                            for benefit, when in self.awards.items()
                        ],
                    }
                }
            }
        if name == "VideoPlayerStreamInfoOverlayChannel":
            self.asked_stream += 1
            return {
                "user": {
                    "id": "7",
                    "displayName": "Streamer",
                    "stream": {"id": "b1", "viewersCount": 100},
                    "broadcastSettings": {"game": {"id": "1", "name": "Stalzone"}},
                }
            }
        if name == "DropCurrentSessionContext":
            return {
                "currentUser": {
                    "dropCurrentSession": {
                        "dropID": "d1",
                        "currentMinutesWatched": 266,
                        "requiredMinutesWatched": 240,
                    }
                }
            }
        raise AssertionError(f"тест не ждал запроса {name}")


def check_merge() -> bool:
    """Кампании нет в инвентаре, а награда есть — ровно случай THE FINALS."""
    tw = FakeTwitch({"b1": INSIDE})
    camp = campaign([drop("d1", 240, benefits=["b1"])])
    asyncio.run(dr.merge_progress(tw, [camp]))
    ok = check(
        "веха закрывается наградой, даже когда кампании нет в инвентаре",
        camp.drops[0].claimed and camp.remaining_minutes == 0,
    )

    tw = FakeTwitch({"b1": BEFORE})
    camp = campaign([drop("d1", 240, benefits=["b1"])])
    camp.drops[0].current_minutes = 100
    asyncio.run(dr.merge_progress(tw, [camp]))
    ok &= check(
        "старая награда кампанию не закрывает",
        not camp.drops[0].claimed and camp.remaining_minutes == 140,
    )

    tw = FakeTwitch({"b1": INSIDE}, in_progress=True)
    inv = asyncio.run(dr.inventory(tw))
    ok &= check(
        "инвентарь сам помечает такие вехи, не дожидаясь сверки",
        len(inv.campaigns) == 1
        and inv.campaigns[0].drops[0].granted
        and len(inv.rewards) == 1,
    )
    ok &= check("и отдаёт slug игры — вотчлисту он нужен",
                inv.campaigns[0].game_slug == "stalcraft")
    return ok


def check_no_user() -> bool:
    """Ответ без `currentUser` — это «не авторизованы», а не «инвентарь пуст».

    Twitch на непринятый токен не ругается: отдаёт 200 и `currentUser: null`.
    Пустой инвентарь из такого ответа однажды лёг в кэш на три минуты, и мастер
    первого запуска сказал «игр нет» через секунду после входа — при восьми
    кампаниях на аккаунте.
    """

    class Anonymous(FakeTwitch):
        async def gql(self, query: dict):
            if query["operationName"] == "Inventory":
                return {"currentUser": None}
            return await super().gql(query)

    try:
        asyncio.run(dr.inventory(Anonymous({})))
        return check("ответ без currentUser не считается пустым инвентарём", False)
    except LoginRequired:
        return check("ответ без currentUser не считается пустым инвентарём", True)


def check_watch() -> bool:
    """Досмотренная веха с пришедшей самой наградой — это не отказ Twitch."""
    было, c.WATCH_INTERVAL = c.WATCH_INTERVAL, 0
    try:
        tw = FakeTwitch({"b1": INSIDE})
        camp = campaign([drop("d1", 240, benefits=["b1"])])
        camp.drops[0].current_minutes = 0
        outcome = asyncio.run(w._watch_channel(tw, CHANNEL, camp))
        ok = check("дроп, пришедший сам, не считается отказом Twitch",
                   outcome == w.CLAIMED)

        tw = FakeTwitch({"b1": BEFORE})
        camp = campaign([drop("d1", 240, benefits=["b1"])])
        camp.drops[0].current_minutes = 0
        outcome = asyncio.run(w._watch_channel(tw, CHANNEL, camp))
        ok &= check("а настоящий отказ по-прежнему отказ", outcome == w.STALLED)
        return ok
    finally:
        c.WATCH_INTERVAL = было


def check_benefit_map() -> bool:
    """Игра у награды берётся через benefit — своего `game` у неё нет."""
    with tempfile.TemporaryDirectory() as raw:
        with Store(Path(raw) / "test.db") as store:
            camp = campaign([drop("d1", 60, benefits=["b1", "b2"])])
            store.remember(camp)
            games = store.games_by_benefit()
            ok = check(
                "по id награды находится игра",
                games.get("b1") == ("516866", "Stalzone")
                and games.get("b2") == ("516866", "Stalzone"),
            )
            ok &= check("а незнакомая награда так и остаётся без игры",
                        "b9" not in games)

            # инвентарь отдаёт дропы без benefit'ов; знание, добытое из деталей
            # кампании, из-за этого терять нельзя
            store.remember(campaign([drop("d1", 60, benefits=[])]))
            ok &= check("пустой список benefit'ов не затирает известные",
                        store.games_by_benefit().get("b1") == ("516866", "Stalzone"))
            return ok


def check_store_rewards() -> bool:
    """Своя копия инвентаря и «что нового с прошлого раза».

    Первое знакомство новостями быть не должно: у Ксена 137 наград, и рассказать
    о них всех в первый же запуск — значит соврать про каждую.
    """
    with tempfile.TemporaryDirectory() as raw:
        with Store(Path(raw) / "test.db") as store:
            first = store.remember_rewards(
                [reward("b1", INSIDE), reward("b2", BEFORE)]
            )
            ok = check("первое знакомство новостями не считается", first == [])
            ok &= check("но награды сохранены", len(store.rewards()) == 2)
            ok &= check("и новыми не помечены", store.unseen_rewards() == [])

            # повторный обход того же инвентаря ничего не открывает
            again = store.remember_rewards([reward("b1", INSIDE), reward("b2", BEFORE)])
            ok &= check("тот же инвентарь — не новости", again == [])

            added = store.remember_rewards(
                [reward("b1", INSIDE), reward("b2", BEFORE), reward("b3", INSIDE)]
            )
            ok &= check("а незнакомая награда — новость", added == ["b3"])
            ok &= check("и видна как непоказанная",
                        [r["id"] for r in store.unseen_rewards()] == ["b3"])

            # выросло количество — это новая награда с прежним именем
            more = dr.Reward(id="b1", name="Награда b1", awarded_at=INSIDE, count=4)
            grown = store.remember_rewards([more])
            ok &= check("выросшее количество — тоже новость", grown == ["b1"])
            ok &= check("количество обновилось",
                        {r["id"]: r["count"] for r in store.rewards()}["b1"] == 4)

            ok &= check("экран открыли — новое перестало быть новым",
                        store.mark_rewards_seen() == 2 and store.unseen_rewards() == [])

            # порядок: свежие сверху, без даты — в конце
            store.remember_rewards([dr.Reward(id="b9", name="Без даты", count=1)])
            ok &= check("награда без даты выдачи уходит в конец",
                        store.rewards()[-1]["id"] == "b9")

            # игра, вписанная руками: Twitch её не отдаёт, а подстановка добивает
            # только награды из знакомых кампаний — у Ксена 20 из 137
            ok &= check("пометка легла", store.name_reward_game("b2", "  Delta Force  "))
            by_id = {r["id"]: r["game_note"] for r in store.rewards()}
            ok &= check("и хранится обрезанной", by_id["b2"] == "Delta Force")
            ok &= check("чужие награды не задеты", by_id["b1"] == "")
            # обход не должен её стирать: иначе вписанное живёт до утра
            store.remember_rewards([reward("b1", INSIDE), reward("b2", BEFORE)])
            ok &= check("обход пометку не трогает",
                        {r["id"]: r["game_note"] for r in store.rewards()}["b2"] == "Delta Force")
            ok &= check("пустая строка снимает пометку",
                        store.name_reward_game("b2", "")
                        and {r["id"]: r["game_note"] for r in store.rewards()}["b2"] == "")
            ok &= check("несуществующей награде вписать нечего",
                        not store.name_reward_game("нет-такой", "X"))
            return ok


def check_unlinked() -> bool:
    """Кампания без привязки: по умолчанию мимо плана, по просьбе — в план."""
    linked = campaign([drop("d1", 60, benefits=["b1"])])
    loose = campaign([drop("d2", 60, benefits=["b2"])])
    loose.account_linked = False
    status = scanner.GameStatus(
        game_id="516866", name="Stalzone", campaigns=[linked, loose],
        available=True, fresh=[],
    )
    ok = check("по умолчанию непривязанная кампания не работа",
               [item.account_linked for item in status.farmable] == [True])
    status.unlinked = True
    ok &= check("а с настройкой — работа", len(status.farmable) == 2)
    return ok


def main() -> int:
    ok = True
    print("\nСверка наград с окнами дропов")
    ok &= check_windows()
    print("\nПрогресс из инвентаря")
    ok &= check_merge()
    ok &= check_no_user()
    print("\nОбходчик")
    ok &= check_watch()
    print("\nНаграда → игра")
    ok &= check_benefit_map()
    print("\nСвоя копия инвентаря")
    ok &= check_store_rewards()
    print("\nКампании без привязки")
    ok &= check_unlinked()
    print("\nВсё сошлось" if ok else "\nЕсть упавшие проверки")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
