"""Служба отдельно от интерфейса.

Проверяется не фарм (для него нужен Twitch), а разделение слоёв: что служба
живёт в своём потоке, что веб до неё дотягивается, что перезапуск веба фарм
не трогает и что sqlite действительно не пускает чужой поток — ради этого
разделение и делалось.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from vahta import __version__ as vahta_version  # noqa: E402
from vahta import web  # noqa: E402
from vahta.api import Meter, TwitchError  # noqa: E402
from vahta import planner  # noqa: E402
from vahta import service as _svc  # noqa: E402
from vahta.service import EVENTS_KEEP, Live, Service  # noqa: E402
from vahta.settings import Settings  # noqa: E402
from vahta.store import Store  # noqa: E402

ok = fail = 0
opened: list = []  # сюда пишет подставной «покажи окно»


def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
    else:
        fail += 1
        print(f"  ПЛОХО {name}: {got!r} вместо {want!r}")


class FakeTwitch:
    """Ровно то, чем службу дёргает веб-слой, — без сети."""

    login_name = "tester"
    user_id = "42"

    def __init__(self):
        self.meter = Meter()

    async def __aexit__(self, *exc):
        return None

    def forget(self):
        self.login_name = self.user_id = None


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _boom(title, text):
    """Уведомления Windows умеют падать по-разному. Фарму до этого дела нет."""
    raise OSError("трей отвалился")


class OfflineTwitch(FakeTwitch):
    """Интернета нет: проверка токена не отвечает."""

    async def ensure_login(self, interactive: bool = True):
        raise httpx.ConnectError("нет связи")


class FakeService(Service):
    """Та же служба, но вместо Twitch и автопилота — счётчик кругов.

    Подменяется только загрузка: поток, цикл, мост `call()` и остановка —
    настоящие, их и проверяем.
    """

    def __init__(self, tmp: Path):
        super().__init__()
        self.tmp = tmp
        self.laps = 0

    async def _boot(self) -> None:
        self.tw = FakeTwitch()  # type: ignore[assignment]
        self.store = Store(self.tmp / "vahta.db")
        self.settings = Settings(self.tmp / "settings.json")
        self.tg_config = None
        self.shown: list[tuple[str, str]] = []
        self.notifier = lambda title, text: self.shown.append((title, text))
        self.jobs.append(asyncio.create_task(self._farm()))

    async def _farm(self) -> None:
        while True:
            self.laps += 1
            self.live = Live(game="Rust", note=f"круг {self.laps}")
            await asyncio.sleep(0.05)


with tempfile.TemporaryDirectory() as raw:
    tmp = Path(raw)
    service = FakeService(tmp)
    service.start()
    web.attach(service)
    try:
        check("служба в своём потоке", service._thread.name, "vahta-service")
        check("поток жив", service._thread.is_alive(), True)
        check("цикл поднят", service.loop is not None, True)

        # --- sqlite стережёт свой поток -------------------------------------
        # Ради этого всё и затевалось: если кто-то полезет в store мимо службы,
        # он получит вот это. Раньше store жил в одном потоке с вебом, и ошибка
        # не всплывала — а вместе с ней не всплывало и то, что фарм привязан
        # к жизни интерфейса.
        try:
            service.store.watchlist()
            check("sqlite не пускает чужой поток", "пустил", "ProgrammingError")
        except sqlite3.ProgrammingError:
            check("sqlite не пускает чужой поток", True, True)

        # --- запись через мост доходит до базы -------------------------------
        asyncio.run(service.call(lambda s: s.store.watch_add("29307", "Rust", "rust")))
        asyncio.run(service.call(lambda s: s.store.watch_add("512710", "CoD", "cod")))

        # --- веб дотягивается через мост -------------------------------------
        with TestClient(web.api, base_url="http://127.0.0.1:8420") as c:
            check("сейчас", c.get("/api/now").status_code, 200)
            check("фарм идёт", c.get("/api/now").json()["farming"], True)

            # Отметка плана в опросе — единственное, по чему экран узнаёт,
            # что служба пересчитала план сама (после дропа, по старению).
            # Пока её не было, «График» держал первый попавшийся план до
            # ближайшего действия человека и показывал доеденные кампании
            check("плана ещё нет", c.get("/api/now").json()["plan_at"], "")
            asyncio.run(service.call(lambda s: setattr(
                s, "plan", planner.build([]))))
            stamp = c.get("/api/now").json()["plan_at"]
            check("отметка появилась", bool(stamp), True)
            check("это время сборки плана", stamp, c.get("/api/plan").json()["starts_at"])
            check("твич виден", c.get("/api/twitch").json()["login"], "tester")
            # промокодов больше нет: ни ручки, ни закрытых экранов
            check("ручки лицензии нет", c.get("/api/license").status_code, 404)
            check("инвентарь открыт всем",
                  c.get("/api/inventory").status_code != 402, True)
            check(
                "записанное видно",
                sorted(r["name"] for r in c.get("/api/watchlist").json()),
                ["CoD", "Rust"],
            )

            check("удаление доходит", c.delete("/api/watchlist/512710").json()["removed"], True)

            # --- отказ обратим --------------------------------------------
            # «Дожать» в разборе конфликта отказывается от соперника навсегда.
            # Пока отказ был невидим и необратим, одно нажатие мимо стоило
            # кампании: она пропадала из плана без следа
            def refuse(s):
                s.store.skip("camp-x")
                return sorted(s.store.skipped())

            check("отказ записан", asyncio.run(service.call(refuse)), ["camp-x"])
            check("и снимается по API",
                  c.delete("/api/decide/camp-x").json()["returned"], True)
            check("а второй раз уже нечего снимать",
                  c.delete("/api/decide/camp-x").json()["returned"], False)

            # --- отложенные кампании --------------------------------------
            # Twitch иногда считает время, но дроп не отдаёт: обходчик
            # откладывает такую кампанию, чтобы не жечь часы впустую
            def hold(s):
                s.store.hold("camp-1", 60, "Twitch не отдаёт дроп")
                return s.store.held()

            held = asyncio.run(service.call(hold))
            check("кампания отложена", "camp-1" in held, True)
            check("и с причиной", held["camp-1"]["reason"], "Twitch не отдаёт дроп")
            check("срок в будущем", held["camp-1"]["until"] > _now_iso(), True)

            def expire(s):
                s.store.hold("camp-2", -1, "просрочена")
                return s.store.held()

            check("просроченная забывается", "camp-2" in asyncio.run(service.call(expire)), False)
            check("снять можно руками",
                  asyncio.run(service.call(lambda s: s.store.unhold("camp-1"))), True)

            # --- уведомления ---------------------------------------------
            check("по умолчанию включены", c.get("/api/settings").json()["notifications"], True)
            check("и есть чем показать", c.get("/api/settings").json()["available"], True)
            check("проба показалась", c.post("/api/settings/test").json()["shown"], True)
            check("и правда показалась", service.shown[-1][0], "Вахта на связи")
            check("текст не пустой", len(service.shown[-1][1]) > 10, True)

            # --- окно: настройки и побудка --------------------------------
            check("крестик по умолчанию сворачивает", c.get("/api/settings").json()["close_to_tray"], True)
            check("автозапуск по умолчанию тихий",
                  c.get("/api/settings").json()["window_on_autostart"], False)
            check("окном никто не владеет", c.get("/api/settings").json()["windowed"], False)
            check("будить некого", c.post("/api/window/show").json()["shown"], False)

            asyncio.run(service.call(lambda s: setattr(s, "open_ui", lambda: opened.append(1))))
            check("теперь владеет", c.get("/api/settings").json()["windowed"], True)
            check("разбудили", c.post("/api/window/show").json()["shown"], True)
            check("окно попросили показать", len(opened), 1)

            # Выход по просьбе: этим инсталлер закрывает «Вахту» перед
            # обновлением. Переименуют ручку — обновление молча начнёт требовать
            # закрывать программу руками, и заметится это через месяц
            check("выходить некому", c.post("/api/quit").json()["quitting"], False)
            left: list = []
            asyncio.run(service.call(lambda s: setattr(s, "quit_ui", lambda: left.append(1))))
            check("попросили выйти", c.post("/api/quit").json()["quitting"], True)
            check("выход тот же, что в трее", len(left), 1)

            one = c.post("/api/settings", json={"close_to_tray": False}).json()
            check("крестик переключается", one["close_to_tray"], False)
            check("а уведомления не задело", one["notifications"], True)
            check("выбор лёг на диск",
                  Settings(tmp / "settings.json").close_to_tray, False)
            c.post("/api/settings", json={"close_to_tray": True})

            off = c.post("/api/settings", json={"notifications": False}).json()
            check("выключаются", off["notifications"], False)
            was = len(service.shown)
            check("выключенные не шлются", c.post("/api/settings/test").json()["shown"], False)
            check("и правда не шлются", len(service.shown), was)
            check(
                "выбор пережил перечитывание",
                Settings(tmp / "settings.json").notifications,
                False,
            )
            c.post("/api/settings", json={"notifications": True})

            # длинное режется по пределам Shell_NotifyIcon, иначе Windows
            # обрежет сам и в непредсказуемом месте
            asyncio.run(service.call(lambda s: s.push("з" * 200, "т" * 900)))
            check("заголовок обрезан", len(service.shown[-1][0]), 63)
            check("текст обрезан", len(service.shown[-1][1]), 255)

            # --- лента событий для тостов в окне --------------------------
            # окно показывает своим тостом то, чего ещё не видело, поэтому
            # `push` копит события, а не только зовёт трей
            events = c.get("/api/now").json()["events"]
            check("события едут вместе с «сейчас»", len(events) > 0, True)
            check("у события есть номер", "id" in events[-1], True)
            last_id = events[-1]["id"]
            asyncio.run(service.call(lambda s: s.push("Дроп забран", "THE FINALS · 3 из 4")))
            fresh = c.get("/api/now").json()["events"][-1]
            check("новое событие получает следующий номер", fresh["id"], last_id + 1)
            check("и несёт заголовок", fresh["title"], "Дроп забран")

            c.post("/api/settings", json={"notifications": False})
            was = len(c.get("/api/now").json()["events"])
            asyncio.run(service.call(lambda s: s.push("тихо", "не должно попасть")))
            check("выключенные не копятся", len(c.get("/api/now").json()["events"]), was)
            c.post("/api/settings", json={"notifications": True})

            # лента не растёт: окно спрашивает её каждые двадцать секунд
            for i in range(EVENTS_KEEP + 5):
                asyncio.run(service.call(lambda s, n=i: s.push(f"раз {n}", "текст")))
            check("лента не длиннее предела",
                  len(c.get("/api/now").json()["events"]), EVENTS_KEEP)

            # --- мастер первого запуска -----------------------------------
            check("мастер не показан", c.get("/api/now").json()["shell"]["onboarded"], False)
            check("закрывается", c.post("/api/settings", json={"onboarded": True}).json()["onboarded"], True)
            check("и лёг на диск", Settings(tmp / "settings.json").onboarded, True)
            check("больше не здоровается", c.get("/api/now").json()["shell"]["onboarded"], True)

            # уведомление не должно ронять фарм
            asyncio.run(service.call(lambda s: setattr(s, "notifier", _boom)))
            check("падение сглотнуто", asyncio.run(service.call(lambda s: s.push("а", "б"))), False)

            # --- тема ---------------------------------------------------
            check("по умолчанию как в системе", c.get("/api/settings").json()["theme"], "system")
            check("тёмная выбирается",
                  c.post("/api/settings", json={"theme": "dark"}).json()["theme"], "dark")
            check("и легла на диск", Settings(tmp / "settings.json").theme, "dark")
            # значение приходит из JSON по HTTP — сюда может прилететь что угодно
            check("чужая тема не пролезает",
                  c.post("/api/settings", json={"theme": "лиловая"}).json()["theme"], "dark")
            c.post("/api/settings", json={"theme": "system"})

            # --- данные оболочки -----------------------------------------
            shell = c.get("/api/now").json()["shell"]
            check("оболочка едет вместе с «сейчас»", sorted(shell), [
                "counts", "deadline", "onboarded", "server_mode", "shift", "telegram", "theme",
                "traffic", "twitch", "update", "update_ready", "update_url",
                "version"])
            # плашка об обновлении: сравнение версий делает служба, и своя
            # версия за новую не считается
            check("без ответа сервера плашки нет", shell["update"], "")
            asyncio.run(service.call(lambda s: setattr(s, "latest_version", vahta_version)))
            check("своя версия — не обновление",
                  c.get("/api/now").json()["shell"]["update"], "")
            asyncio.run(service.call(lambda s: setattr(s, "latest_version", "99.0.0")))
            check("новее — плашка есть",
                  c.get("/api/now").json()["shell"]["update"], "99.0.0")
            asyncio.run(service.call(lambda s: setattr(s, "latest_version", "мусор")))
            check("мусор не показываем",
                  c.get("/api/now").json()["shell"]["update"], "")
            asyncio.run(service.call(lambda s: setattr(s, "latest_version", "")))
            check("счётчик игр из базы", shell["counts"]["games"], 1)
            check("твич виден и в оболочке", shell["twitch"], "tester")
            check("телеграма нет", shell["telegram"], False)
            check("версия отдана", shell["version"], vahta_version)
            check("эфира нет — смены нет", shell["shift"], None)

            # трафик считается по телам запросов и ответов
            asyncio.run(service.call(lambda s: s.tw.meter.add(120 * 1024)))
            check("трафик виден", c.get("/api/now").json()["shell"]["traffic"] > 0, True)

            laps_seen = c.get("/api/now").json()["note"]

        # --- перезапуск веба фарм не трогает ---------------------------------
        time.sleep(0.3)
        with TestClient(web.api, base_url="http://127.0.0.1:8420") as c2:
            after = c2.get("/api/now").json()["note"]
            left = [r["name"] for r in c2.get("/api/watchlist").json()]
        check("веб перезапустился, а фарм шёл", after != laps_seen, True)
        check("служба всё ещё жива", service._thread.is_alive(), True)
        check("данные пережили перезапуск", left, ["Rust"])

        # --- без сети служба всё равно поднимается ---------------------------
        # Проверка токена ходит на Twitch, и раньше её отказ ронял запуск
        # целиком. Теперь это сетевая часть, отдельная от подъёма.
        async def probe(s):
            s.tw = OfflineTwitch()  # type: ignore[assignment]
            await s._begin()  # автопилот создастся, но крутиться начнёт позже
            return s.live.note

        check("без сети служба живёт", asyncio.run(service.call(probe)), "нет связи с Twitch")
        check("и поток не умер", service._thread.is_alive(), True)
    finally:
        thread_before = service._thread
        service.stop()
        # A final tick may finish while stop() joins the service thread.
        # Only activity after stop() returns would violate its contract.
        laps_before = service.laps

    time.sleep(0.2)
    check("поток остановлен", service._thread is None, True)
    check("остановленный поток действительно завершился", thread_before.is_alive(), False)
    check("фарм больше не крутится", service.laps, laps_before)
    # что база закрыта, проверять отдельно нечем: незакрытое соединение и
    # закрытое дают одну и ту же ProgrammingError из чужого потока. Зато
    # незакрытое держит файл — и удаление папки ниже упадёт с WinError 32.

from datetime import timedelta  # noqa: E402

from vahta import drops as dr  # noqa: E402
from vahta.service import SHIFT_GAP  # noqa: E402

# --- пауза -------------------------------------------------------------------
# «на паузе 16 мин» — это про потерянный эфир, поэтому продление паузы начало
# не сдвигает: иначе счётчик обнулялся бы от каждого нажатия
rest = Service()
check("сначала паузы нет", rest.paused_since, None)
first = rest.pause(timedelta(hours=1))
began = rest.paused_since
rest.pause(timedelta(hours=2))
check("продление начало не сдвинуло", rest.paused_since, began)
check("а срок сдвинулся", rest.paused_until > first, True)
check("на паузе так и написано", rest.live.note, "на паузе")
rest.resume()
check("сняли — забыли и начало", (rest.paused_since, rest.paused_until), (None, None))
# Надпись должна смениться сразу. Автопилот спит до полуминуты, а потом ещё
# уходит в обход вотчлиста на минуты — всё это время экран показывал бы «на
# паузе» при снятой паузе, и это читается как «фарм не запускается»
check("и надпись не осталась от паузы", rest.live.note == "на паузе", False)


# --- сон, который можно прервать ---------------------------------------------
# Автопилот спал жёстко: после «фармить нечего» — десять минут. За это время
# человек успевал снять паузу и добавить игру, а «Вахта» досыпала свой срок,
# и выглядело это как «фарм не запускается». Ксен на живой 0.3.1 так и решил:
# поставил паузу и запустил заново.
naps = Service()


async def _nap(seconds: float, after: float | None = None) -> float:
    from time import monotonic
    # каждый asyncio.run — свой цикл, а Event привязан к тому, где его ждали;
    # в бою он один и заводится в `_boot`, здесь сбрасываем сами
    naps.nudge = None
    if after is not None:
        asyncio.get_running_loop().call_later(after, naps.wake)
    started = monotonic()
    await naps.rest(seconds)
    return monotonic() - started


# Допуск в 20 мс — не небрежность: таймер Windows тикает раз в 15 мс, и сон
# на 0.4 честно измеряется как 0.399. Без допуска проверка падает раз в десяток
# прогонов, а тест, падающий случайно, учит не смотреть на упавшие тесты
check("без просьбы спит свой срок", asyncio.run(_nap(0.4)) >= 0.38, True)
check("а по просьбе просыпается сразу", asyncio.run(_nap(30, after=0.2)) < 2, True)
# снятая пауза — это и есть просьба: причина спать пропала
naps.paused_until = dr.now() + timedelta(hours=1)


async def _resumed() -> float:
    from time import monotonic
    naps.nudge = None
    asyncio.get_running_loop().call_later(0.2, naps.resume)
    started = monotonic()
    await naps.rest(30)
    return monotonic() - started


check("снятая пауза будит автопилот", asyncio.run(_resumed()) < 2, True)

# --- длительность смены ------------------------------------------------------
# Смена живёт, пока Twitch засчитывает минуты, и обрывается сама, когда они
# пропали. Так надёжнее, чем расставлять сбросы по веткам автопилота: новая
# ветка однажды забудется, а этот счётчик заметит и её.
lone = Service()
check("до эфира смены нет", lone.shift_seconds(), None)

lone.count_watched()
check("первая засчитанная минута начинает смену", lone.shift_seconds(), 0)

lone.shift_since = dr.now() - timedelta(hours=3, minutes=40)
check("считается от начала", lone.shift_seconds() // 60, 220)

# минуты перестали приходить: пауза, потерянный канал, упавшая сеть
lone.shift_tick = dr.now() - SHIFT_GAP - timedelta(seconds=1)
check("пропали засчитанные — смена оборвана", lone.shift_seconds(), None)

lone.count_watched()
check("вернулись — считаем заново", lone.shift_seconds(), 0)

# короткий провал сменой не считается: между дропами канал переезжает
lone.shift_since = dr.now() - timedelta(hours=1)
lone.shift_tick = dr.now() - timedelta(seconds=90)
check("пропуск одного пинга обрывом не считается", lone.shift_seconds() // 60, 60)

# --- автопилот: кому окно и когда пересобирать план ---------------------------
# Ради этого: обходчик не отдавал управление, пока по игре есть живой канал.
# План от 02:55 не пересобирался семь часов, Arena Breakout с дедлайном
# 18 августа держала окно, а отсрочка THE FINALS истекла в 04:18 и осталась
# незамеченной. Теперь обходчик отдаёт решение ломтями, и выбор делает план.


def _plan_of(*games: str):
    """План из одной задачи на игру, в заданном порядке."""
    from vahta import drops as dr
    from vahta import planner

    now = dr.now()
    tasks = [
        planner.Task(
            game_id=name, game=name, campaign_id=f"c-{name}", campaign=f"{name} drops",
            minutes=60, release=now, deadline=now + timedelta(hours=10 + i),
            availability=1.0, confident=True, priority=i + 1,
        )
        for i, name in enumerate(games)
    ]
    blocks = [planner.Block(t, now, now + timedelta(hours=1), 60) for t in tasks]
    return planner.Plan(tasks, blocks, [], now, now + timedelta(days=14))


def _drive(outcomes: list[str], plan, replan_after: float | None = None):
    """Прогнать автопилот по заданным исходам обходчика.

    Возвращает (какие игры смотрели, сколько раз пересобирался план).
    """
    from vahta import service as svc
    from vahta import watcher as w

    app = Service()
    app.store = StubStore()
    app.live = Live()
    watched: list[str] = []
    replans = 0

    # вторым аргументом автопилот передаёт «куда дошёл обход» — подделке
    # он не нужен, но принять его она обязана
    async def fake_replan(_app, _on_game=None):
        nonlocal replans
        replans += 1
        return plan

    async def fake_watch(tw, game, **kw):
        watched.append(game)
        if not outcomes:
            app.stopping = True  # так автопилот выпускает CancelledError наружу
            raise asyncio.CancelledError
        return outcomes.pop(0)

    было = svc.replan, w.watch, svc.REPLAN_AFTER
    svc.replan, w.watch = fake_replan, fake_watch
    if replan_after is not None:
        svc.REPLAN_AFTER = replan_after
    try:
        asyncio.run(svc.autopilot(app))
    except asyncio.CancelledError:
        pass
    finally:
        svc.replan, w.watch, svc.REPLAN_AFTER = было
    return watched, replans, app


class StubStore:
    def box_art(self, game_id):
        return ""


from vahta import watcher as _w  # noqa: E402

# Ломоть кончился — решаем заново по тому же плану, без обхода каталога:
# он лезет в сеть на минуту, а за двадцать минут меняется только «кто в эфире»
seen, replans, app = _drive([_w.SLICE, _w.SLICE, _w.SLICE], _plan_of("A", "B"))
check("ломоть не заставляет пересобирать план", replans, 1)
check("и окно остаётся у головы очереди", seen, ["A"] * 4)

# Нет канала — окно уходит следующему, а игра получает метку тишины
seen, replans, app = _drive([_w.NO_CHANNEL, _w.SLICE], _plan_of("A", "B"))
check("без канала окно уходит следующей задаче", seen[:2], ["A", "B"])
check("а игра без канала помечена тихой", "A" in app.quiet, True)

# Забранный дроп — повод пересобрать: остатки кампаний изменились
seen, replans, app = _drive([_w.NOTHING], _plan_of("A", "B"))
check("не-ломоть пересобирает план", replans, 2)

# Состарившийся план собирается заново, даже если ломти идут один за другим:
# иначе не подхватываются истёкшие отсрочки и открывшиеся кампании.
# Порог отрицательный, а не нулевой: `time.monotonic()` в Windows тикает раз
# в 15 мс, а поддельный обходчик возвращается мгновенно — с нулём возраст плана
# выходил ровно 0.0 и порог не срабатывал
seen, replans, app = _drive([_w.SLICE, _w.SLICE], _plan_of("A"), replan_after=-1)
check("состарившийся план пересобирается", replans, 3)

# --- обход не удваивается ----------------------------------------------------
# При запуске обход заказывают двое: автопилот и окно, спрашивающее `/api/plan`,
# пока плана ещё нет. В логе это две одинаковые строки «Обход: …» подряд и
# двойной трафик к Twitch на каждом запуске
def _double_replan() -> tuple[int, int]:
    было = _svc.replan
    счёт = 0

    async def медленный(app, on_game=None):
        nonlocal счёт
        счёт += 1
        await asyncio.sleep(0.05)  # обход идёт долго — за это время придёт второй
        return "план"

    async def гонка():
        app = Service.__new__(Service)
        app._replan_job = None
        оба = await asyncio.gather(app.replan(), app.replan())
        # а после того, как обход кончился, следующий заказ — уже новый
        снова = await app.replan()
        return оба, снова

    _svc.replan = медленный
    try:
        (оба, снова) = asyncio.run(гонка())
    finally:
        _svc.replan = было
    return счёт, len([x for x in [*оба, снова] if x == "план"])


обходов, планов = _double_replan()
check("два одновременных заказа — один обход, третий позже — свой", обходов, 2)
check("и оба заказчика получили план", планов, 3)

# --- отложенные кампании -----------------------------------------------------
# Строка «Twitch не отдаёт дроп» предлагала «проверить привязку» всегда, даже
# когда аккаунт привязан и в «Каталоге» так и написано: причина одна, а лечить
# зовут другое. Ссылку даём, только если привязки правда нет.
from types import SimpleNamespace  # noqa: E402


def _stuck_row(linked: bool):
    campaign = SimpleNamespace(
        id="c1", name="OWCS MSC Day 3", remaining_minutes=60, ends_at=None,
        account_linked=linked, account_link_url="https://account.battle.net/connections",
    )
    service = SimpleNamespace(
        statuses=[SimpleNamespace(name="Overwatch", game_id="515025",
                                  campaigns=[campaign])],
        store=SimpleNamespace(
            held=lambda: {"c1": {"until": "2026-07-31T21:51", "reason": "Twitch не отдаёт дроп"}},
            box_art=lambda _: "",
        ),
    )
    return web._stuck(service)[0]


check("привязанной кампании ссылку не предлагаем", _stuck_row(True)["link"], "")
check("а непривязанной — предлагаем", _stuck_row(False)["link"],
      "https://account.battle.net/connections")
check("причина при этом не теряется", _stuck_row(True)["reason"], "Twitch не отдаёт дроп")

# --- что показываем, когда фарм споткнулся ------------------------------------
# 8 августа 2026 экран четыре часа показывал «ошибка: [Errno 11004] getaddrinfo
# failed». Ошибка была не наша: DNS-фильтр отдавал spade.twitch.tv как 0.0.0.0,
# и решало дело имя домена, которого в надписи не было
_blocked = httpx.ConnectError("[Errno 11004] getaddrinfo failed")
_blocked._request = httpx.Request("POST", "https://spade.twitch.tv/track")
check("домен, который не открылся, виден на экране",
      _svc._note(_blocked), "spade.twitch.tv не открывается")
_wrapped = TwitchError("Сеть недоступна: …")   # так его заворачивает `gql`
_wrapped.__cause__ = _blocked
check("сетевой отказ из gql домен не теряет",
      _svc._note(_wrapped), "spade.twitch.tv не открывается")
check("а сырой errno на экран не попадает вовсе",
      "Errno" in _svc._note(httpx.ConnectError("[Errno 11004] getaddrinfo failed")), False)

# --- трафик ------------------------------------------------------------------
meter = Meter()
check("молчит, пока нечего считать", meter.per_minute(), 0.0)
meter.add(-5)
check("мусор не считается", meter.total, 0)
meter.add(60 * 1024)
check("байты копятся", meter.total, 61440)
# знаменатель не меньше минуты: иначе первые секунды дают мегабайты в минуту
check("сразу после старта — не больше, чем набежало", round(meter.per_minute()), 60)

print(f"\n{ok} проверок прошло, {fail} упало")
sys.exit(1 if fail else 0)
