"""Проверка обходчика: замечает ли он, что просмотр не засчитывается.

Ради чего это написано. В ночь на 30 июля «Вахта» просидела на одном канале
семь часов и набрала 63 минуты из 240 — пятнадцать процентов. `stream_of`
спрашивался один раз, при выборе канала, а дальше цикл только шлёл пинги
и читал сессию: ушедший стример выглядел как работающий фарм, и в логе три
часа стояла одна и та же строчка «Прогресс: 48/240».

Сети тут нет: `FakeTwitch` отдаёт заранее заданную последовательность значений
счётчика, а `WATCH_INTERVAL` на время теста равен нулю — иначе один прогон
занимал бы пять минут.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vahta import constants as c
from vahta import drops as dr
from vahta import watcher as w


class FakeHttp:
    """Страница канала со spade-ссылкой и приём пингов."""

    def __init__(self, accept: bool = True):
        self.accept = accept
        self.pings = 0

    async def get(self, url, headers=None, params=None, timeout=None):
        if params:
            return FakeResponse('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhttps://test.ttvnw.net/live.m3u8')
        return FakeResponse('#EXTM3U\n#EXTINF:2,\nhttps://test.ttvnw.net/segment.ts')

    async def head(self, url, timeout=None, headers=None):
        self.pings += 1
        return httpx.Response(200 if self.accept else 500)

    async def post(self, url, headers=None, data=None):
        self.pings += 1
        return FakeResponse("", 204 if self.accept else 500)


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        return None


class FakeTwitch:
    """Отдаёт счётчик по заранее заданному списку.

    `progress` — что `dropCurrentSession` покажет на каждом тике; `None`
    означает «сессии нет». Когда список кончился, последнее значение
    повторяется — так удобно задавать «встал и стоит».
    """

    def __init__(self, progress: list[int | None], *, game_id="1", broadcast="b1",
                 owners: list[str] | None = None, drops: list[str] | None = None):
        self.progress = list(progress)
        # Чей канал показывает `dropCurrentSession`. Сессия у аккаунта одна
        # на все каналы, поэтому «наш» и «чужой» — это разные вещи, и чужие
        # минуты за свои принимать нельзя. `None` — поля вовсе нет
        self.owners = list(owners) if owners else None
        # Какая веха стоит в сессии. Канал может совпасть, а веха оказаться
        # от другой кампании — тогда минуты тоже не наши
        self.drops = list(drops) if drops else None
        # сколько раз подряд отвечать «в эфире»: стример, который ушёл посреди
        # просмотра, — это `live_checks = 1`, при выборе канала он ещё был
        self.live_checks: int | None = None
        self.game_id = game_id
        self.broadcast = broadcast
        # что произойдёт со второго вопроса о стриме: смена игры или перезапуск
        self.game_after: tuple[str, str] | None = None
        self.broadcast_after: str | None = None
        self.user_id = "42"
        self.http = FakeHttp()
        self.asked_stream = 0

    async def gql(self, query: dict):
        name = query["operationName"]
        if name == "PlaybackAccessToken":
            return {"streamPlaybackAccessToken": {"value": "test", "signature": "test"}}
        if name == "VideoPlayerStreamInfoOverlayChannel":
            self.asked_stream += 1
            if self.live_checks is not None and self.asked_stream > self.live_checks:
                return {"user": {"id": "7", "stream": None}}
            game_id, game = self.game_id, "Игра"
            broadcast = self.broadcast
            if self.asked_stream > 1:  # первый вопрос — при выборе канала
                if self.game_after:
                    game_id, game = self.game_after
                if self.broadcast_after:
                    broadcast = self.broadcast_after
            return {
                "user": {
                    "id": "7",
                    "displayName": "Streamer",
                    "stream": {"id": broadcast, "viewersCount": 100},
                    "broadcastSettings": {"game": {"id": game_id, "name": game}},
                }
            }
        if name == "DropCurrentSessionContext":
            current = self.progress.pop(0) if len(self.progress) > 1 else self.progress[0]
            if current is None:
                return {"currentUser": {"dropCurrentSession": {}}}
            drop = "d1"
            if self.drops:
                drop = self.drops.pop(0) if len(self.drops) > 1 else self.drops[0]
            session = {
                "dropID": drop,
                "currentMinutesWatched": current,
                "requiredMinutesWatched": 240,
            }
            if self.owners:
                owner = self.owners.pop(0) if len(self.owners) > 1 else self.owners[0]
                session["channel"] = {"id": owner}
            return {"currentUser": {"dropCurrentSession": session}}
        raise AssertionError(f"тест не ждал запроса {name}")


def check(label: str, condition: bool) -> bool:
    print(f"  {'ок  ' if condition else 'ПЛОХО'}  {label}")
    return condition


CHANNEL = w.Channel(id="7", login="streamer", display_name="Streamer", viewers=100)
CAMPAIGN = dr.Campaign(
    id="camp-1", name="Тестовая кампания", game="Игра", game_id="1",
    starts_at=None, ends_at=None, account_linked=True,
    drops=[dr.Drop(id="d1", name="Награда", required_minutes=240)],
)


def run(tw: FakeTwitch) -> str:
    return asyncio.run(w._watch_channel(tw, CHANNEL, CAMPAIGN))


def main() -> int:
    ok = True
    # Тик длиной 59 секунд превратил бы каждую проверку в пятиминутное ожидание
    было, c.WATCH_INTERVAL = c.WATCH_INTERVAL, 0

    try:
        # Пока минуты капают, канал не меняется: одиннадцать тиков счёт идёт,
        # и только пять следующих, где он стоит, уводят с канала
        tw = FakeTwitch([*range(10, 21)])
        ok &= check("стоящий счётчик уводит с канала", run(tw) == w.STALE)
        ok &= check("пока минуты капают, канал не меняется",
                    tw.http.pings == 11 + w.STALE_LIMIT)

        # Одиночный пропуск — норма даже на здоровом стриме, из-за него уходить
        # нельзя. Здесь их два, а уход всё равно только на пятом подряд
        tw = FakeTwitch([10, 11, 11, 12, 13, 13, 14, 15])
        ok &= check("одиночный пропуск не считается простоем",
                    run(tw) == w.STALE and tw.http.pings == 8 + w.STALE_LIMIT)

        # Сколько именно терпим — на голом простое, без роста вовсе
        tw = FakeTwitch([48, 48])
        ok &= check(f"терпим ровно {w.STALE_LIMIT} тиков, не больше",
                    run(tw) == w.STALE and tw.http.pings == w.STALE_LIMIT + 1)
        ok &= check("состояние стрима спрашивается каждый круг, а не раз при выборе",
                    tw.asked_stream == w.STALE_LIMIT + 1)

        # Стример ушёл, а счётчик замер на прежнем числе — самый частый случай.
        # При выборе канала он был в эфире, поэтому первый вопрос — «жив»,
        # а следующий, через круг, — уже «нет». Ждать пяти тиков тут нечего:
        # состояние стрима переспрашивается каждый круг
        tw = FakeTwitch([48, 48])
        tw.live_checks = 1
        ok &= check("ушедший стример замечается за один круг, а не за пять",
                    run(tw) == w.OVER and tw.http.pings == 1)

        # Стример сменил игру: наш пинг ещё принимается, но капать нечему
        tw = FakeTwitch([10, 11, 12])
        tw.game_after = ("999", "Другая игра")
        ok &= check("сменённая игра тоже уводит с канала", run(tw) == w.OVER)

        # Перезапущенный эфир — не повод уходить: канал тот же, кампания та же.
        # Раньше это выглядело как вставший счётчик, и хороший канал терялся
        tw = FakeTwitch([10, 11, 12, 13, 14, 15])
        tw.broadcast_after = "b2"
        ok &= check("перезапуск эфира канала не теряет",
                    run(tw) == w.STALE and tw.http.pings == 6 + w.STALE_LIMIT)

        # Сессии нет вовсе. Уходить с первого же круга нельзя: сессия
        # открывается не мгновенно, и до 31 июля «Вахта» бросала канал через
        # секунду после захода — 239 заходов за ночь и 193 пустые сессии
        tw = FakeTwitch([None])
        ok &= check("без дроп-сессии уходим, но не сразу", run(tw) == w.STALE)
        ok &= check(f"дав ей ровно {w.ADOPT_LIMIT} круга", tw.http.pings == w.ADOPT_LIMIT)

        # Сессия про чужой канал: её минуты не наши, сколько бы их там ни было.
        # Раньше они читались как свои — стоящий чужой счётчик выглядел как
        # «Twitch тормозит», и «Вахта» пять минут ждала у моря погоды
        ticks: list[tuple[int, int]] = []
        tw = FakeTwitch([100, 101, 102, 103], owners=["999"])
        ok &= check("чужая сессия уводит с канала",
                    asyncio.run(w._watch_channel(
                        tw, CHANNEL, CAMPAIGN,
                        on_tick=lambda *a: ticks.append(a[2:]))) == w.STALE)
        ok &= check("и чужие минуты не показываются как свои", ticks == [])

        # Канал наш, а веха — чужой кампании. Так было 31 июля: на канале
        # Overwatch приехала веха SMITE 2 (1405 минут при нужных 720), «Вахта»
        # показала это как свой прогресс, решила, что дроп готов, не нашла его
        # и отложила свою же кампанию на час
        ticks.clear()
        tw = FakeTwitch([1405, 1406, 1407, 1408], drops=["чужая-веха"])
        ok &= check("чужая веха уводит с канала",
                    asyncio.run(w._watch_channel(
                        tw, CHANNEL, CAMPAIGN,
                        on_tick=lambda *a: ticks.append(a[2:]))) == w.STALE)
        ok &= check("и чужие минуты не показываются как свои", ticks == [])

        # А своя веха на том же канале — обычная работа
        tw = FakeTwitch([10, 11, 12], drops=["d1"])
        ok &= check("своя веха не мешает", run(tw) == w.STALE and tw.http.pings > 3)

        # Но переезжает сессия не мгновенно: два круга чужая, потом наша —
        # это норма, и уходить из-за неё нельзя
        tw = FakeTwitch([100, 100, 10, 11, 12], owners=["999", "999", "7"])
        ok &= check("подождали, пока сессия переедет к нам",
                    run(tw) == w.STALE and tw.http.pings == 5 + w.STALE_LIMIT)

        # Пинг не приняли — канал сменить, счётчик тут не при чём
        tw = FakeTwitch([10, 11, 12])
        tw.http.accept = False
        ok &= check("непринятый пинг уводит с канала", run(tw) == w.OVER)

        ok &= check_slice()

        ok &= check_avoid()
        ok &= check_allowed()
    finally:
        c.WATCH_INTERVAL = было

    print("\nВсё сошлось" if ok else "\nЕсть расхождения")
    return 0 if ok else 1


def check_slice() -> bool:
    """Ломоть кончается, и решение возвращается автопилоту.

    Ради этого всё и делается: пока обходчик решения не возвращал, игра с живым
    каналом держала окно часами — тем прочнее, чем популярнее. Проверяем, что
    здоровый счётчик и живой стрим уходу не мешают: ломоть сильнее их обоих.
    """
    # эфир жив, минуты капают — и всё равно уходим, когда время вышло
    tw = FakeTwitch([*range(10, 40)])
    got = check("вышедший ломоть отдаёт решение автопилоту",
                asyncio.run(w._watch_channel(tw, CHANNEL, CAMPAIGN, until=0.0)) == w.SLICE)
    got &= check("но минуту доработали, а не бросили на полуслове",
                 tw.http.pings == 1)

    # без ломтя (`minutes=None`, как в `vahta watch`) уход по времени не бывает
    tw = FakeTwitch([48, 48])
    got &= check("без ломтя работает прежнее правило — сторож счётчика",
                 asyncio.run(w._watch_channel(tw, CHANNEL, CAMPAIGN, until=None)) == w.STALE)
    return got


def check_avoid() -> bool:
    """Канал, на котором счёт стоял, обходится при следующем выборе.

    Выдача каталога стабильна: без этого `pick_channel` возвращал бы тот же
    канал круг за кругом — та же беда, что была с THE FINALS и восемью
    кругами по 266/240.
    """

    class Directory(FakeTwitch):
        async def gql(self, query: dict):
            name = query["operationName"]
            if name == "DirectoryPage_Game":
                return {"game": {"streams": {"edges": [
                    {"node": {"viewersCount": 9, "broadcaster": {
                        "id": "7", "login": "streamer", "displayName": "Streamer"}}},
                    {"node": {"viewersCount": 3, "broadcaster": {
                        "id": "8", "login": "другой", "displayName": "Другой"}}},
                ]}}}
            if name == "DropsHighlightService_AvailableDrops":
                return {"channel": {"viewerDropCampaigns": [{"id": CAMPAIGN.id}]}}
            return await super().gql(query)

    tw = Directory([10])
    first = asyncio.run(w.pick_channel(tw, "игра", [CAMPAIGN]))
    got = check("без обхода берётся первый по выдаче",
                first is not None and first[0].login == "streamer")
    second = asyncio.run(w.pick_channel(tw, "игра", [CAMPAIGN], {"Streamer"}))
    got &= check("обойдённый канал не выбирается снова (регистр не важен)",
                 second is not None and second[0].login == "другой")
    return got


def check_allowed() -> bool:
    """Кампания со списком каналов ищется каталогом, а не опросом логинов.

    До 31 июля список разбирался по одному запросу на логин, и когда из него
    никого не было в эфире, это стоило двадцати пяти запросов ради ответа
    «нет». В ночном логе такое случалось 210 раз.
    """
    asked: list[str] = []

    class Directory(FakeTwitch):
        def __init__(self, *a, catalog=True, **kw):
            super().__init__(*a, **kw)
            self.catalog = catalog

        async def gql(self, query: dict):
            name = query["operationName"]
            asked.append(name)
            if name == "DirectoryPage_Game":
                edges = [{"node": {"viewersCount": 5, "broadcaster": {
                    "id": "9", "login": "мелкий", "displayName": "Мелкий"}}}]
                return {"game": {"streams": {"edges": edges if self.catalog else []}}}
            if name == "DropsHighlightService_AvailableDrops":
                return {"channel": {"viewerDropCampaigns": [{"id": LIMITED.id}]}}
            return await super().gql(query)

    LIMITED = dr.Campaign(
        id="camp-2", name="Со списком", game="Игра", game_id="1",
        starts_at=None, ends_at=None, account_linked=True,
        drops=[dr.Drop(id="d1", name="Награда", required_minutes=60)],
        allowed_channels=["Мелкий", "кто-то-ещё"],
    )

    tw = Directory([10])
    picked = asyncio.run(w.pick_channel(tw, "игра", [LIMITED]))
    got = check("канал из списка находится в каталоге",
                picked is not None and picked[0].login == "мелкий")
    got &= check("и лишнего запроса про дропсы не было",
                 "DropsHighlightService_AvailableDrops" not in asked)

    # Каталог показывает не всех: мелкий канал в выдачу не попал — тогда
    # и только тогда опрашиваем логины поодиночке
    asked.clear()
    tw = Directory([10], catalog=False)
    picked = asyncio.run(w.pick_channel(tw, "игра", [LIMITED]))
    got &= check("не нашёлся в каталоге — спрашиваем по списку",
                 picked is not None and picked[0].login == "Мелкий")
    got &= check("и вот теперь дропсы на канале проверены",
                 "DropsHighlightService_AvailableDrops" in asked)
    return got


if __name__ == "__main__":
    raise SystemExit(main())
