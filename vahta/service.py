"""Вахта как служба: фарм, планирование и состояние, независимые от интерфейса.

Раньше всё это жило в жизненном цикле веб-приложения: состояние в `App`, фарм
стартовал в `lifespan`. Из-за этого трей был обязан поднимать uvicorn, а любая
возня с интерфейсом задевала фарм.

Теперь у службы **свой цикл событий в своём потоке**. Веб-слой — тонкий клиент
над ней: он может упасть, перезапуститься или не подниматься вовсе, а вахта
продолжается.

Отсюда одно правило, которое нельзя нарушать: **`store` и `tw` трогаются только
из потока службы**. Соединение sqlite3 привязано к создавшему его потоку, а
`Twitch` держит asyncio-клиента, привязанного к этому циклу. Всё, что снаружи, —
через `call()`.
"""

from __future__ import annotations

import asyncio
import errno
import inspect
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic

import httpx

from . import drops as dr
from . import games
from . import planner
from . import scanner
from . import telegram
from . import update as upd
from . import watcher as w
from .api import LoginRequired, Twitch, TwitchError
from .paths import DATA_DIR, DB_PATH
from .settings import Settings
from .store import Store

log = logging.getLogger("vahta")

BOOT_TIMEOUT = 60
STOP_TIMEOUT = 15

# Засчитанные минуты приходят раз в WATCH_INTERVAL, то есть примерно раз
# в минуту. Пропали на три — эфира больше нет, смена оборвалась.
SHIFT_GAP = timedelta(minutes=3)

# Пределы Shell_NotifyIcon: заголовок 63 символа, текст 255. Длиннее Windows
# не покажет, а обрезать лучше самим, чем гадать, где оборвалось.
TITLE_LIMIT = 63
TEXT_LIMIT = 255

# Сколько последних уведомлений помнить для тостов в окне. Окно спрашивает
# службу раз в двадцать секунд, тост живёт четыре — десятка хватает с запасом,
# а лента не растёт за сутки работы.
EVENTS_KEEP = 10

# Сколько не предлагать игру, по которой только что не нашлось канала
# с нужной кампанией. Стримеры появляются не за секунду, но и ждать их,
# держа окно, незачем — за это время можно отфармить соседнюю кампанию
QUIET_FOR = 300

# Через сколько план считается устаревшим и собирается заново. Пересборка —
# это обход каталога, около минуты работы по сети, поэтому не на каждый ломоть:
# внутри получаса меняется только «кто сейчас в эфире», а это видно и так,
# по готовому плану. Зато без пересборки не подхватываются истёкшие отсрочки
# (в ночь на 30 июля THE FINALS ждала своего срока в 04:18 и не дождалась),
# открывшиеся кампании и сгоревшие дедлайны.
REPLAN_AFTER = 1800


@dataclass
class Live:
    """Что происходит прямо сейчас — для вкладки «Сейчас»."""

    game: str = ""
    campaign: str = ""
    channel: str = ""
    viewers: int = 0
    current: int = 0
    required: int = 0
    box_art: str = ""
    note: str = "простой"
    # какой по счёту дроп кампании сейчас идёт и сколько их всего: прогресс
    # внутри кампании общий, дропы — вехи одного потока просмотра
    drop: int = 0
    drops: int = 0

    def payload(self) -> dict:
        return {
            "game": self.game,
            "campaign": self.campaign,
            "channel": self.channel,
            "viewers": self.viewers,
            "current": self.current,
            "required": self.required,
            "boxArt": self.box_art,
            "note": self.note,
            "drop": self.drop,
            "drops": self.drops,
            "farming": bool(self.game),
        }


class Service:
    """Живёт в своём потоке со своим циклом событий.

    Снаружи есть ровно три вещи: `start()`, `stop()` и `call()`. Всё остальное —
    внутренности, к которым обращаются либо сам автопилот, либо Telegram-бот:
    оба крутятся в этом же цикле, и им можно напрямую.
    """

    def __init__(self) -> None:
        self.tw: Twitch = None  # type: ignore[assignment]
        self.store: Store = None  # type: ignore[assignment]
        self.settings: Settings | None = None
        # Куда показывать уведомления. Ставит тот, у кого есть чем: у трея это
        # иконка Windows, у `cli serve` — никто, и тогда их просто нет.
        self.notifier = None
        # Чем открыть окно. Ставит трей; через это второй запуск просит первый
        # показаться, а сама служба окном не владеет и не должна.
        self.open_ui = None
        # Чем выйти совсем. Тоже ставит трей — это его же «Выход». Нужно
        # инсталлеру: обновление ставится поверх запущенной «Вахты», и попросить
        # её выйти по-человечески лучше, чем просить об этом человека или
        # прибивать процесс. Нет трея (`cli serve`) — нет и выхода по просьбе.
        self.quit_ui = None
        self.live = Live()
        self.plan: planner.Plan | None = None
        # последний обход вотчлиста целиком. Планировщик берёт из него только
        # то, что можно фармить, а интерфейсу нужно и остальное: кампании без
        # привязанного игрового аккаунта и уже закрытые. Молча выброшенные,
        # они выглядели бы как «Вахта их не заметила»
        self.statuses: list = []
        self.bot: telegram.Bot | None = None
        self.tg_config: telegram.Config | None = None
        self.bot_job: asyncio.Task | None = None
        self.jobs: list[asyncio.Task] = []
        self.current: asyncio.Task | None = None  # текущий фарм, его же снимаем на паузе
        self.paused_until: datetime | None = None
        self.paused_since: datetime | None = None  # «вахта на паузе 16 мин»
        # идущий обход, если он идёт: чтобы второй заказчик ждал его, а не
        # начинал свой. Гасится вместе со службой — см. `stop()`
        self._replan_job: asyncio.Task | None = None
        self.stopping = False
        self.pending_login: dict | None = None  # незавершённый вход по коду
        self.login_job: asyncio.Task | None = None
        self.login_error = ""
        self.catalog: list = []
        self.catalog_at: datetime | None = None
        # инвентарь Twitch: кампании в процессе и все полученные награды. Живёт
        # тут по той же причине, что каталог, — за ним ходят и экран «Инвентарь»,
        # и мастер первого запуска, а меняется он куда реже, чем его спрашивают
        self.inv: dr.Inventory | None = None
        self.inv_at: datetime | None = None
        self.announced: set[str] = set()  # о каких конфликтах уже сказали
        # о каких наградах уже сказали: и те, что забрали сами, и те, что
        # заметил обход. Одна награда — одно уведомление, иначе повтор читается
        # как сбой. Знание на текущий запуск: «новое» на экране живёт в базе
        self.announced_rewards: set[str] = set()
        # игры, по которым только что не нашлось канала с нужной кампанией:
        # game_id → до какого момента их не предлагать. Знание на ближайшие
        # минуты, переживать перезапуск ему незачем — но экрану его видно надо,
        # иначе «Расписание» показывает «идёт» на том, что сейчас пропущено
        self.quiet: dict[str, float] = {}
        # лента уведомлений для тостов в окне: окно спрашивает её вместе
        # с остальным опросом и показывает то, чего ещё не видело
        self.events: list[dict] = []
        self._event_id = 0
        # где лежат данные; в тестах подменяется на временную папку
        self.data_dir = DATA_DIR
        # что нашлось в последнем релизе на GitHub (`releases.check`): версия
        # для плашки, страница релиза и инсталлер с хэшем. Ничего из этого
        # не обязательно: пустой адрес значит «только плашка»
        self.latest_version = ""
        self.release_page = ""
        self.update_url = ""
        self.update_sha256 = ""
        # база игр: что лежит в релизах и что уже лежит у нас
        self.games_built = ""
        self.games_url = ""
        self.games_sha256 = ""
        self.games_have = ""
        # скачанное и сверенное по хэшу обновление, ждущее удобного момента.
        # Момент выбирает автопилот — на границе ломтя, когда просмотр не идёт
        self.update_ready: Path | None = None
        self.shift_since: datetime | None = None  # когда начался нынешний эфир
        self.shift_tick: datetime | None = None  # когда Twitch последний раз засчитал

        # Чем разбудить автопилот, когда он спит между попытками: снятая пауза,
        # добавленная игра. Иначе он досыпает свои десять минут, что бы
        # ни случилось, — а человек в это время смотрит на «фармить нечего».
        #
        # Заводится не здесь, а в `_boot`: `asyncio.Event` привязывается
        # к циклу, в котором его впервые ждут, а `__init__` идёт в чужом потоке,
        # до того как цикл службы вообще появился
        self.nudge: asyncio.Event | None = None

        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._boot_error: BaseException | None = None

    # --- жизненный цикл ------------------------------------------------------

    def start(self) -> None:
        """Поднять службу и дождаться, пока она готова принимать запросы."""
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="vahta-service", daemon=True)
        self._thread.start()
        if not self._ready.wait(BOOT_TIMEOUT):
            raise RuntimeError("служба не поднялась за отведённое время")
        if self._boot_error:
            raise self._boot_error

    def stop(self) -> None:
        if not self.loop or not self._thread:
            return
        self.stopping = True
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(STOP_TIMEOUT)
        self._thread = None

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._boot())
        except BaseException as exc:  # noqa: BLE001 — пробрасываем в start()
            self._boot_error = exc
            self._ready.set()
            self.loop.close()
            return
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            self.stopping = True
            self.loop.run_until_complete(self._shutdown())
            self.loop.close()

    async def _boot(self) -> None:
        """Только то, что делается мгновенно и без сети.

        `start()` ждёт именно эту часть, а её ждёт трей. Проверка токена ходит
        на Twitch с двадцатисекундным таймаутом — держать на ней запуск значит
        не показать иконку, пока не ответит интернет, а без интернета не
        показать вовсе.
        """
        self.nudge = asyncio.Event()  # см. `__init__`: цикл появился только здесь
        self.tw = Twitch(DATA_DIR)
        await self.tw.__aenter__()
        self.store = Store(DB_PATH)
        games.влить(self.store)
        self.games_have = games.собрана()
        self.settings = Settings(DATA_DIR / "settings.json")
        self.tg_config = telegram.Config(DATA_DIR / "telegram.json")
        self.live = Live(note="проверяю вход в Twitch")
        self.jobs.append(asyncio.create_task(self._begin()))
        log.info("Служба поднята")

    async def _begin(self) -> None:
        """Всё, что требует сети. Крутится уже после того, как служба поднялась."""
        try:
            await self.tw.ensure_login(interactive=False)
        except LoginRequired:
            # служба должна работать и без токена — вход делается в интерфейсе
            log.info("Twitch: входа нет, жду авторизации через интерфейс")
            self.live = Live(note="нужен вход в Twitch")
        except Exception as exc:
            # нет сети, Twitch лёг, DPI — не повод не запускаться. Автопилот
            # повторит сам, а в интерфейсе будет видно, что происходит.
            log.warning("Twitch недоступен при старте: %s: %s", type(exc).__name__, exc)
            self.live = Live(note="нет связи с Twitch")

        if self.tg_config and self.tg_config.token:
            await self.start_bot()
        else:
            log.info("Telegram не настроен — токен можно вставить на вкладке «Настройки»")

        self.jobs.append(asyncio.create_task(autopilot(self)))
        # качает обновление заранее, ставит его не сама: момент выбирает
        # автопилот, когда просмотр не идёт
        self.jobs.append(asyncio.create_task(upd.loop(self)))

    async def _shutdown(self) -> None:
        pending = [job for job in [*self.jobs, self.current, self._replan_job]
                   if job and not job.done()]
        for job in pending:
            job.cancel()
        if pending:
            # дать снятым задачам доиграть отмену: иначе `watch` не успеет
            # дописать сессию, а мы уже закроем базу под ним
            await asyncio.gather(*pending, return_exceptions=True)
        await self.stop_bot()
        if self.store:
            self.store.close()
        if self.tw:
            await self.tw.__aexit__(None, None, None)
        log.info("Служба остановлена")

    # --- мост из чужих потоков ----------------------------------------------

    async def call(self, fn):
        """Выполнить `fn(service)` в потоке службы и дождаться результата.

        Единственная законная дверь снаружи. `fn` может быть и обычной
        функцией, и корутиной — awaitable дожидается здесь же.
        """
        if not self.loop:
            raise RuntimeError("служба не запущена")

        async def run():
            result = fn(self)
            if inspect.isawaitable(result):
                result = await result
            return result

        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(run(), self.loop))

    # --- состояние -----------------------------------------------------------

    async def start_bot(self) -> None:
        """Поднять бота на текущем токене. Уже запущенного сначала гасим."""
        await self.stop_bot()
        if not self.tg_config or not self.tg_config.token:
            return
        self.bot = telegram.Bot(self, self.tg_config)
        self.bot_job = asyncio.create_task(self.bot.run())

    async def stop_bot(self) -> None:
        if self.bot_job:
            self.bot_job.cancel()
            self.bot_job = None
        if self.bot:
            await self.bot.close()
            self.bot = None

    async def rest(self, seconds: float) -> None:
        """Поспать, но проснуться, если причина спать пропала.

        Автопилот спал жёстко: после «фармить нечего» — десять минут. За это
        время человек успевал снять паузу, добавить игру, ввести код — а «Вахта»
        продолжала спать, и выглядело это как «фарм не запускается». Ксен
        на живой 0.3.1 так и поступил: поставил паузу и запустил заново.
        """
        if self.nudge is None:
            self.nudge = asyncio.Event()
        self.nudge.clear()
        try:
            await asyncio.wait_for(self.nudge.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    def wake(self) -> None:
        """Разбудить автопилот. Зовётся только из потока службы — как и всё.

        Спит он не всегда, поэтому будильник может звонить в пустоту: это
        нормально и ничего не значит.
        """
        if self.nudge is not None:
            self.nudge.set()

    def quiet_games(self) -> dict[str, float]:
        """Игры без подходящего канала прямо сейчас. Просроченные забываем."""
        now = monotonic()
        self.quiet = {game: until for game, until in self.quiet.items() if until > now}
        return self.quiet

    def pause(self, span: timedelta) -> datetime:
        # продление паузы начало не сдвигает: «на паузе 16 мин» — это про то,
        # сколько эфира уже потеряно, а не про то, когда нажали в последний раз
        self.paused_since = self.paused_since or dr.now()
        self.paused_until = dr.now() + span
        if self.current and not self.current.done():
            self.current.cancel()
        self.live = Live(note="на паузе")
        return self.paused_until

    def resume(self) -> None:
        self.paused_until = self.paused_since = None
        # надпись меняем сразу, не дожидаясь автопилота: тот спит до полуминуты,
        # а потом ещё уходит в обход вотчлиста — всё это время экран показывал бы
        # «на паузе», хотя пауза уже снята
        self.live = Live(note="возвращаюсь")
        self.wake()

    async def replan(self, on_game=None) -> planner.Plan:
        """Пересобрать план. Второй одновременный заказ ждёт первый.

        Обход — это полминуты запросов к Twitch, и при запуске его заказывают
        сразу двое: автопилот, которому нужен план, чтобы работать, и окно,
        которое спрашивает `/api/plan`, пока плана ещё нет. В логе это две
        одинаковые строки «Обход: …» подряд, а у Twitch — двойной трафик
        на каждом запуске «Вахты».

        Ждать чужой обход, а не начинать свой, здесь всегда правильно: обход
        не зависит от того, кто его попросил, и результат у него один на всех —
        `app.plan`. Разница только в `on_game`, которым автопилот показывает
        ход обхода на экране; присоединившийся к чужому обходу его не получит
        и просто дождётся плана.
        """
        job = self._replan_job
        if job is None or job.done():
            job = self._replan_job = asyncio.create_task(replan(self, on_game))
        return await job

    def count_watched(self) -> None:
        """Twitch засчитал ещё минуту. Отсюда и считается длительность смены."""
        moment = dr.now()
        if self.shift_tick is None or moment - self.shift_tick > SHIFT_GAP:
            self.shift_since = moment
        self.shift_tick = moment

    def shift_seconds(self) -> int | None:
        """Сколько идёт нынешняя смена без обрывов. `None` — эфира нет.

        Смена жива, пока приходят засчитанные минуты, и обрывается сама, если
        они пропали: пауза, потерянный канал, упавшая сеть, кончившийся токен.
        Считать по одному этому признаку надёжнее, чем расставлять сбросы по
        всем веткам автопилота — новая ветка однажды забудется, а этот
        счётчик не заметит только того, чего и не было.
        """
        if not self.shift_since or not self.shift_tick:
            return None
        if dr.now() - self.shift_tick > SHIFT_GAP:
            return None
        return int((dr.now() - self.shift_since).total_seconds())

    async def tell(self, coro_factory) -> None:
        """Сказать в Telegram, если он настроен, и не упасть, если он молчит."""
        if not self.bot:
            return
        try:
            await coro_factory(self.bot)
        except Exception as exc:
            log.warning("Telegram не принял сообщение: %s", exc)

    def push(self, title: str, text: str) -> bool:
        """Показать уведомление Windows. Не вышло — и ладно.

        Уведомление никогда не должно ронять фарм: его может не быть кому
        показать (запуск без трея), его может подавить система (фокусировка
        внимания, полный экран), а на не-Windows его нет вовсе.

        Здесь же копится лента событий для тостов в окне: `push` — единственное
        место, через которое проходит всё, о чём стоит сказать вслух, и второй
        такой список однажды разошёлся бы с этим.
        """
        if not self.settings or not self.settings.notifications:
            return False
        self._event_id += 1
        self.events.append({"id": self._event_id, "title": title, "text": text})
        del self.events[:-EVENTS_KEEP]
        if not self.notifier:
            return False
        try:
            self.notifier(title[:TITLE_LIMIT], text[:TEXT_LIMIT])
            return True
        except Exception as exc:
            log.warning("Уведомление не показалось: %s: %s", type(exc).__name__, exc)
            return False


def _when(moment) -> str:
    return moment.astimezone().strftime("%d.%m %H:%M") if moment else "?"


async def replan(app: Service, on_game=None) -> planner.Plan:
    unlinked = bool(app.settings and app.settings.farm_unlinked)
    # Инвентарь читается здесь, один раз на весь обход: он один на аккаунт,
    # а запрашивался по разу на игру. Заодно это единственное место, где рядом
    # есть и он, и база, и кому рассказать о новых наградах
    inv = await dr.inventory(app.tw)
    # Twitch can close the viewing session before the final tick reaches us.
    # A completed drop then has no minutes left to schedule, so _milestone
    # will never collect it. Sweep the inventory even when the plan is empty.
    collected = False
    for campaign in inv.campaigns:
        for drop in campaign.drops:
            if not drop.claimable:
                continue
            if not await dr.claim(app.tw, drop):
                raise TwitchError("Twitch пока не подтвердил получение готового дропа")
            # Persist each success immediately: a later claim or refresh can
            # fail, and already collected rewards must survive that failure.
            app.store.record_claim(campaign, drop)
            app.store.remember_progress(campaign)
            log.info("Забрал из инвентаря: %s (%s)", drop.name, campaign.game)
            collected = True
    if collected:
        inv = await dr.inventory(app.tw)
    app.inv, app.inv_at = inv, dr.now()
    await announce_rewards(app, app.store.remember_rewards(inv.rewards))
    statuses = await scanner.scan(app.tw, app.store, unlinked, inv, on_game)
    app.statuses = statuses
    for status in statuses:
        for fresh in status.fresh:
            await app.tell(lambda bot, s=status, f=fresh: bot.new_campaign(s.name, f))
            app.push(
                "Кампания открылась",
                f"{status.name} — {fresh.name}\nУже в плане, до {_when(fresh.ends_at)}",
            )
    tasks = planner.make_tasks(statuses, app.store)
    app.plan = planner.build(tasks)
    for gap in app.plan.shortfalls:
        await app.tell(lambda bot, g=gap: bot.conflict(g, app.plan))
        # план пересматривается после каждого дропа, а нехватка та же самая —
        # без этого одно и то же уведомление приходило бы весь день
        if gap.task.campaign_id in app.announced:
            continue
        app.announced.add(gap.task.campaign_id)
        # «Не влезает целиком» — это сведение, а не задача: дропы вехи одного
        # потока просмотра, и ближние заберутся как обычно. Требовать за это
        # к экрану значит учить не доверять уведомлениям. Зовём только когда
        # не успеваем вообще ничего — вот там решение и правда есть
        if gap.empty:
            app.push(
                "Кампания не успевает",
                f"{gap.task.game}: {gap.task.campaign}\n"
                f"Ни один дроп не успеет до дедлайна — разбери во вкладке «Сейчас»",
            )
        else:
            app.push(
                "Влезает не целиком",
                f"{gap.task.game}: {gap.task.campaign}\n"
                f"Успеем {gap.reachable} из {gap.total} — последний не влезает",
            )
    return app.plan


async def announce_rewards(app: Service, fresh: list[str]) -> None:
    """Сказать о наградах, которые появились сами.

    «Сами» — потому что о тех, что мы забрали или получили при своём просмотре,
    уже сказано в `claimed`: второе уведомление о том же предмете читается как
    сбой, а не как забота. Такие id складываются в `announced_rewards`, и здесь
    мы их пропускаем.

    Одно уведомление на обход, а не по одному на награду: инвентарь Twitch
    случается наполняет задним числом, и тогда их приходит пачка.
    """
    unheard = [item for item in fresh if item not in app.announced_rewards]
    if not unheard:
        return
    app.announced_rewards.update(unheard)
    by_id = {item.id: item for item in (app.inv.rewards if app.inv else [])}
    names = [by_id[item].name for item in unheard if item in by_id]
    if not names:
        return
    if len(names) == 1:
        app.push("Новая награда", f"{names[0]}\nПоявилась в инвентаре Twitch")
    else:
        app.push(
            "Новые награды",
            f"{len(names)} шт.: {', '.join(names[:3])}"
            + ("…" if len(names) > 3 else "")
            + "\nПоявились в инвентаре Twitch",
        )
    await app.tell(lambda bot: bot.new_rewards(names))


def _note(exc: Exception) -> str:
    """Во что превратить упавший запрос на экране «Сейчас».

    Сырой текст исключения там читается как отказ программы, хотя почти всегда
    это сеть. Хуже того, он молчит о единственном, что решает дело, — **о том,
    какой домен не открылся**: 8 августа 2026 у Ксена `gql.twitch.tv` работал,
    а `spade.twitch.tv` его же DNS-фильтр отдавал как `0.0.0.0` (домен
    телеметрии есть в блок-листах трекеров). Обход шёл, каналы находились,
    фарм не двигался ни на минуту. Поэтому домен выносим в надпись, а подробности
    остаются в логе.
    """
    if (isinstance(exc, sqlite3.Error)
            and (getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL
                 or "database or disk is full" in str(exc).lower())):
        return "на диске ВАХТЫ закончилось место"
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "на диске ВАХТЫ закончилось место"
    if isinstance(exc, sqlite3.Error):
        return "не удалось записать или прочитать данные ВАХТЫ"
    # `gql` заворачивает сетевой отказ в свой «Сеть недоступна: …», и домен
    # там уже не разобрать — берём исходное исключение, оно приложено причиной
    if isinstance(exc, TwitchError) and isinstance(exc.__cause__, httpx.TransportError):
        exc = exc.__cause__
    if isinstance(exc, httpx.TransportError):
        try:
            host = exc.request.url.host
        except RuntimeError:  # httpx не привязал запрос к исключению
            host = ""
        return f"{host} не открывается" if host else "нет связи с Twitch"
    if isinstance(exc, (TwitchError, httpx.HTTPError)):
        return "Twitch отвечает ошибкой"
    return "внутренняя ошибка ВАХТЫ"


async def autopilot(app: Service) -> None:
    """Фармить то, что стоит первым в плане, и пересматривать после каждого дропа."""
    last_error = ""
    error_alert = telegram.FarmErrorAlert()
    progress_alert = telegram.FarmProgressAlert()
    while True:
        try:
            # Скачанное обновление ждёт именно этой точки: сюда автопилот
            # приходит между ломтями, после забранного дропа и на паузе —
            # то есть тогда, когда просмотр не идёт и терять нечего. Ставить
            # его посреди ломтя значило бы отнять у человека минуты, которые
            # Twitch не вернёт.
            if setup := upd.take(app):
                app.live = Live(note="ставлю обновление")
                if upd.install(setup, app.update_sha256):
                    # дальше инсталлер сам попросит нас выйти (`--quit`)
                    # и поднимет обратно уже новую версию
                    await app.rest(60)
                    continue

            if app.paused_until and dr.now() < app.paused_until:
                progress_alert.reset()
                app.live = Live(note="на паузе")
                await app.rest(30)
                continue
            app.paused_until = app.paused_since = None

            # Обход вотчлиста — это сеть: десяток игр, сотня кампаний, минуты
            # работы. Пока он идёт, `live` держит прошлую надпись, и экран
            # показывает то, чего уже нет: после паузы — «на паузе», на первом
            # запуске — «проверяю вход в Twitch». Человек читает это как
            # «фарм не запускается», и он прав: по экрану так и есть.
            # Куда дошёл обход, а не просто «обхожу»: на пустой базе первый
            # обход идёт минутами (детали всех кампаний неизвестны), и одна
            # неподвижная надпись всё это время читается как «зависла».
            # Ксен на живой 0.3.1 так и решил — поставил паузу и запустил
            # заново, хотя обход шёл своим чередом
            app.live = Live(note="обхожу список игр")
            plan = await app.replan(
                lambda number, total, game: setattr(
                    app, "live", Live(note=f"обхожу список: {game} · {number} из {total}")
                ),
            )
            built = monotonic()
            # Пауза, нажатая во время обхода, раньше терялась: `pause()` снимает
            # текущий фарм, а его в это время нет, и внутренний цикл о паузе
            # не спрашивал — «Вахта» уходила фармить, хотя её попросили стоять
            if app.paused_until and dr.now() < app.paused_until:
                continue
            if not plan.blocks:
                progress_alert.reset()
                last_error = ""
                error_alert.clear()
                app.live = Live(note="фармить нечего")
                await app.rest(600)
                continue

            def tick(campaign: dr.Campaign, channel, current: int, required: int) -> None:
                progress_alert.observe((campaign.id, required), current)
                nonlocal last_error
                last_error = ""
                error_alert.clear()
                app.live.campaign = campaign.name
                app.live.channel = channel.display_name
                app.live.viewers = channel.viewers
                app.live.current = current
                app.live.required = required
                app.live.note = "в эфире"
                # забранные вехи + текущая: «дроп 3 из 4»
                app.live.drops = len(campaign.drops)
                app.live.drop = min(len(campaign.drops),
                                    sum(d.claimed for d in campaign.drops) + 1)
                app.count_watched()

            def claimed(campaign: dr.Campaign, drop: dr.Drop) -> None:
                # об этой награде сказано здесь и сейчас — обход про неё
                # промолчит, когда увидит её в инвентаре
                app.announced_rewards.update(drop.benefit_ids)
                asyncio.create_task(app.tell(lambda bot: bot.claimed(campaign, drop)))
                done = sum(d.claimed for d in campaign.drops)
                app.push(
                    # Дроп мог прийти сам, без клейма: сказать «забран» про то,
                    # чего мы не забирали, — значит соврать в мелочи, которую
                    # человек однажды заметит в инвентаре
                    "Дроп пришёл сам" if drop.granted else "Дроп забран",
                    f"{drop.name}\n{campaign.game} — {done} из {len(campaign.drops)} "
                    "по кампании",
                )

            # Идём по плану сверху вниз, пока не найдём задачу, которую можно
            # фармить прямо сейчас. По игре, где только что не нашлось канала
            # с нужной кампанией, ждать смысла нет: окно достанется следующей,
            # а к этой вернёмся, когда кто-нибудь выйдет в эфир. Пересчёт при
            # этом не трогаем — он лезет в сеть на минуты, а план у нас свежий.
            #
            # Обходчик отдаёт управление ломтями (`w.SLICE`), и это здесь
            # главное: выбор «кому окно» принимает план, а не тот, кому окно
            # досталось в прошлый раз. Пока обходчик решения не возвращал, игра
            # с живым каналом держала его часами — тем прочнее, чем популярнее.
            while True:
                if app.paused_until and dr.now() < app.paused_until:
                    break  # паузу могли нажать между ломтями — здесь она и видна
                quiet = app.quiet_games()
                head = next(
                    (b.task for b in plan.blocks if b.task.game_id not in quiet), None
                )
                if head is None:
                    progress_alert.reset()
                    app.live = Live(note="жду подходящий канал")
                    await app.rest(60)
                    break

                # Игра та же — не сбрасываем состояние: иначе «Сейчас» каждый
                # ломоть мигал бы «ищу канал» с нулевой полосой прогресса
                if app.live.game != head.game:
                    app.live = Live(
                        game=head.game,
                        campaign=head.campaign,
                        box_art=app.store.box_art(head.game_id),
                        note="ищу канал",
                    )
                app.current = asyncio.create_task(
                    w.watch(
                        app.tw,
                        head.game,
                        store=app.store,
                        on_tick=tick,
                        on_claim=claimed,
                        unlinked=bool(app.settings and app.settings.farm_unlinked),
                    )
                )
                watching = app.current
                progress_alert.start_watch()
                try:
                    while not watching.done():
                        await asyncio.wait({watching}, timeout=60)
                        if not watching.done():
                            await progress_alert.check(app.bot)
                    outcome = await watching
                    if outcome == w.SLICE:
                        await progress_alert.check(app.bot)
                finally:
                    progress_alert.end_watch()
                last_error = ""
                error_alert.clear()
                if outcome == w.NO_CHANNEL:
                    app.quiet[head.game_id] = monotonic() + QUIET_FOR
                    continue
                if outcome != w.SLICE:
                    progress_alert.reset()
                    break  # дроп забран, кампания отложена, фармить нечего
                if monotonic() - built > REPLAN_AFTER:
                    break  # план состарился: отсрочки, новые кампании, дедлайны
        except asyncio.CancelledError:
            if app.stopping:
                raise
            log.info("Фарм прерван (пауза или смена плана)")
        except LoginRequired:
            # цикл крутится каждые 15 секунд, а сказать надо один раз —
            # признак того, что уже сказали, и есть текущая пометка
            if last_error != "нужен вход в Twitch":
                app.push(
                    "Нужен вход в Twitch",
                    "Токен больше не действует. Фарм остановлен до входа "
                    "во вкладке «Настройки».",
                )
            last_error = "нужен вход в Twitch"
            app.live = Live(note=last_error)
            await error_alert.notify(app.bot, last_error)
            await asyncio.sleep(15)
        except Exception as exc:
            log.error("Автопилот споткнулся: %s: %s", type(exc).__name__, exc)
            note = _note(exc)
            # Сказать вслух — но один раз, а не каждые две минуты. 8 августа
            # 2026 фарм простоял четыре часа с надписью «ошибка: [Errno 11004]
            # getaddrinfo failed»: сырой errno не говорит человеку ничего,
            # а молча стоять — тем более
            if last_error != note:
                app.push("Фарм остановлен", note)
            last_error = note
            app.live = Live(note=note)
            await error_alert.notify(app.bot, note)
            await asyncio.sleep(120)


async def await_login(app: Service, device: dict) -> None:
    """Дождаться, пока код введут на сайте Twitch."""
    deadline = dr.now() + timedelta(seconds=device["expires_in"])
    try:
        while dr.now() < deadline:
            await asyncio.sleep(device["interval"])
            if app.pending_login is not device:
                return
            if await app.tw.poll_device_login(device):
                if app.pending_login is not device:
                    return
                await app.tw.finish_login()
                app.pending_login = None
                log.info("Twitch: вошёл как %s", app.tw.login_name)
                return
        app.login_error = "Код истёк, начни заново"
    except Exception as exc:
        if app.pending_login is device:
            app.login_error = str(exc)
        log.error("Вход в Twitch не удался: %s", exc)
    finally:
        if app.pending_login is device:
            app.pending_login = None
