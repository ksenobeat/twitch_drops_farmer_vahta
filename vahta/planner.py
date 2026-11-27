"""Расписание фарма.

Фармить можно только одну игру за раз, у кампаний свои окна и дедлайны —
это классическая однопроцессорная задача с прерываниями, временами появления
и дедлайнами. В учебном виде для неё оптимален EDF (алгоритм Хорна): всегда
работать над тем, у чего дедлайн ближе.

Но у нас очередь строится **не по дедлайну, а по запасу** (`_order`, `_slack`):
эфирные минуты приходят со своей скоростью, и «догнать потом» получается
не всегда — редкую кампанию нельзя отсмотреть быстрее, чем идёт эфир по игре.
Значит ей надо начинать раньше, даже если дедлайн у неё дальше. Цель — забрать
все дропы, а не соблюсти порядок дедлайнов.

Главная поправка к учебной задаче: прогресс капает только когда живой стример
с дропсами в эфире, а по нишевой игре это шесть часов в сутки, а не двадцать
четыре. Отсюда две разные величины, и путать их нельзя:

* **просмотр занимает календарь один к одному.** Смотреть можно только одно
  за раз, и минута просмотра стоит минуты календаря — здесь машина честно одна.
* **ожидание эфира календарь не занимает вовсе.** Пока по игре никто не стримит,
  «Вахта» смотрит другую; ровно это делает обходчик (`NO_CHANNEL` →
  `Service.quiet`).

Поэтому задача не владеет отрезком календаря длиной `минуты / доступность`,
а **набирает свои минуты со скоростью не быстрее доступности эфира**, деля
календарь с остальными: сумма скоростей не больше единицы. Редкая кампания
тянется неделю, но занимает в этой неделе свои восемь процентов, а не всю.

Первая же живая установка показала, чего стоила прежняя модель: Overwatch
с 600 минутами эфира при доступности 8% забирал 7579 минут календаря — пятеро
суток одним блоком, — и League, Arena и Marvel Rivals вставали в очередь
за ним, на 4–5 августа. Служба при этом вела себя правильно (нет канала — окно
уходит следующей задаче), а человек видел план и читал его как «Вахта не хочет
фармить то, что кончается сегодня».

Чего модель не знает: совпадают ли эфиры двух игр по времени. Считается, что
свободного календаря хватает всем, пока сумма скоростей не больше единицы, —
то есть эфиры ложатся в промежутки друг друга. Иногда они всё же наложатся, и
тогда план чуть оптимистичен; прежняя модель ошибалась в другую сторону и
в разы.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import drops as dr
from .scanner import GameStatus
from .store import Store

# меньше этого числа замеров — статистике доступности верить рано
MIN_SAMPLES = 8
# Недосмотренный хвост меньше этого считается досмотренным. Полминуты, а не
# машинный эпсилон: границы окон приходят от Twitch, а «сейчас» берётся с часов,
# и они расходятся на микросекунды. С эпсилоном такой хвост становился
# конфликтом «не хватает 0 мин» — настоящим для программы и бессмысленным для
# человека.
TOLERANCE = 0.5
DEFAULT_HORIZON = timedelta(days=14)


@dataclass
class Task:
    """Одна кампания как единица работы."""

    game_id: str
    game: str
    campaign_id: str
    campaign: str
    minutes: int  # эфирных минут нужно досмотреть
    release: datetime
    deadline: datetime
    availability: float  # доля времени, когда по игре есть эфир с дропсами
    confident: bool  # хватает ли замеров, чтобы верить оценке
    priority: int  # место в вотчлисте
    # каналы, на которых кампания капает; пусто — годится любой по игре.
    # Нужны, чтобы понять, идут ли две кампании одной игры одновременно
    channels: tuple[str, ...] = ()
    # Сколько ещё смотреть до каждой незабранной вехи, по возрастанию. Дропы —
    # вехи одного потока просмотра, поэтому «не влезает» почти всегда значит
    # «не влезает последняя», а не «всё впустую»: за оставшееся время дропы
    # с ближними вехами заберутся как ни в чём не бывало
    milestones: tuple[int, ...] = ()

    @property
    def air_share(self) -> float:
        """С какой скоростью задача может набирать минуты.

        Это и есть доля времени с эфиром: за календарный час кампания при 8%
        наберёт около пяти минут, и быстрее — никак, сколько бы свободного
        календаря вокруг ни было.

        Ноль был бы приговором, а он чаще значит «замеров мало», чем «стримов
        не будет»; пол в один процент оставляет такой игре место в плане, пусть
        и в самом хвосте.
        """
        return min(max(self.availability, 0.01), 1.0)

    def shares_air_with(self, other: "Task") -> bool:
        """Идут ли эти две кампании от одного просмотра.

        Twitch засчитывает минуту всем кампаниям игры, которым подходит канал:
        час на Marvel Rivals двигает и «Season 9 Twitch Drops», и «Ignite MSF
        2026 Day 1» разом. Кампания без списка каналов ловится на любом стриме
        по игре, со списком — только на своих, поэтому две ограниченные считаем
        совместимыми, лишь когда списки пересекаются.
        """
        if self.game_id != other.game_id or self.campaign_id == other.campaign_id:
            return False
        if not self.channels or not other.channels:
            return True
        return bool(set(self.channels) & set(other.channels))


@dataclass
class Block:
    """Отрезок, на котором кампания набирает минуты.

    Блоки разных кампаний **пересекаются**, и это не ошибка: пока по одной игре
    нет эфира, «Вахта» смотрит другую, и обе продвигаются в одни и те же сутки.
    `share` — какая доля календаря на этом отрезке уходит именно на эту
    кампанию, а `minutes` — сколько эфирных минут из этого выйдет.
    """

    task: Task
    start: datetime
    end: datetime
    minutes: float = 0.0  # сколько эфирных минут здесь ожидается

    @property
    def span(self) -> float:
        """Календарных минут от начала до конца."""
        return (self.end - self.start).total_seconds() / 60

    @property
    def share(self) -> float:
        """Какая доля календаря на этом отрезке уходит на эту кампанию."""
        return self.minutes / self.span if self.span else 0.0


@dataclass
class Shortfall:
    """Задача, которая не помещается в свой дедлайн **целиком**.

    Это сведение, а не задача для человека. Дропы — вехи одного потока
    просмотра, и «не хватает 21 минуты» означает ровно одно: последнюю веху
    не возьмём. Ближние возьмём как обычно, и отказаться от кампании было бы
    строго хуже, чем не делать ничего.
    """

    task: Task
    missing_minutes: float  # эфирных минут не хватает
    competitors: list[str] = field(default_factory=list)

    @property
    def earned(self) -> float:
        """Сколько эфирных минут по этой кампании всё-таки наберётся."""
        return max(0.0, self.task.minutes - self.missing_minutes)

    @property
    def reachable(self) -> int:
        """Сколько дропов успеем забрать, несмотря на нехватку."""
        return sum(1 for m in self.task.milestones if m <= self.earned + TOLERANCE)

    @property
    def total(self) -> int:
        return len(self.task.milestones)

    @property
    def empty(self) -> bool:
        """Не успеваем вообще ничего — вот это уже повод что-то решать."""
        return self.total > 0 and self.reachable == 0


@dataclass
class Plan:
    tasks: list[Task]
    blocks: list[Block]
    shortfalls: list[Shortfall]
    starts_at: datetime
    ends_at: datetime

    @property
    def feasible(self) -> bool:
        return not self.shortfalls

    @property
    def unconfident(self) -> list[Task]:
        return [t for t in self.tasks if not t.confident]


def waiting_reason(game_id: str, store: Store) -> str:
    """Почему по игре сейчас нельзя фармить вовсе. Пусто — можно.

    Ноль доступности при сорока замерах — это не скорость, а приговор. Считать
    его скоростью и планировать «по проценту в сутки» значит выдумывать работу,
    которой не будет: у Overwatch пол в 1% превращался в 14 минут в сутки шесть
    дней подряд и в конфликт «не хватает 8 ч 50 м» на ровном месте.

    Приговор снимается сам: появится канал — обход это увидит, доступность
    перестанет быть нулём, и следующая пересборка вернёт кампании в план.
    Задержка — до получаса, столько же живёт весь остальной план.
    """
    if store.sample_count(game_id) < MIN_SAMPLES:
        return ""  # замеров мало, верить рано — планируем как обычно
    if store.availability(game_id) > 0:
        return ""
    # Разница между «эфира нет» и «эфир есть, но не тот» — это единственное,
    # что человек может здесь сделать: во втором случае помогает подождать
    # нужного стримера, в первом — ничего
    if store.air(game_id) > 0:
        return "идёт только на своих каналах, и их нет в эфире"
    return "по игре сейчас не стримят с дропсами"


def make_tasks(
    statuses: list[GameStatus], store: Store, now: datetime | None = None
) -> list[Task]:
    """Превратить кампании вотчлиста в задачи планировщика."""
    now = now or dr.now()
    skipped = store.skipped()
    # отложенные обходчиком: Twitch по ним считает время, но дроп не отдаёт,
    # и отдавать им окно — значит жечь часы впустую
    held = set(store.held())
    tasks: list[Task] = []
    for priority, status in enumerate(statuses, start=1):
        if waiting_reason(status.game_id, store):
            continue  # фармить нельзя вовсе — не выдумываем работу, см. выше
        share = store.availability(status.game_id)
        samples = store.sample_count(status.game_id)
        confident = samples >= MIN_SAMPLES
        for campaign in status.farmable:
            if campaign.id in skipped:
                continue  # от этой кампании отказались при разборе конфликта
            if campaign.id in held:
                continue  # отложена до срока, см. `Store.hold`
            if campaign.remaining_minutes <= 0 or campaign.ends_at is None:
                continue
            if campaign.ends_at <= now:
                continue
            tasks.append(
                Task(
                    game_id=status.game_id,
                    game=status.name,
                    campaign_id=campaign.id,
                    campaign=campaign.name,
                    minutes=campaign.remaining_minutes,
                    release=max(now, campaign.starts_at or now),
                    deadline=campaign.ends_at,
                    # без данных считаем эфир постоянным, но помечаем оценку
                    availability=share if confident else 1.0,
                    confident=confident,
                    channels=tuple(campaign.allowed_channels),
                    milestones=tuple(
                        sorted(
                            max(0, drop.required_minutes - drop.current_minutes)
                            for drop in campaign.drops
                            if not drop.claimed
                        )
                    ),
                    priority=priority,
                )
            )
    return tasks


def _slack(task: Task, left: dict[str, float], moment: datetime) -> float:
    """Сколько календаря останется, если делать только эту задачу и прямо сейчас.

    Дедлайн минус то календарное время, которое нужно на остаток при её
    доступности эфира. Ноль — впритык, меньше нуля — уже не успеваем.
    """
    need = left[task.campaign_id] / task.air_share
    return (task.deadline - moment).total_seconds() / 60 - need


def _order(ready: list[Task], left: dict[str, float], moment: datetime) -> list[Task]:
    """Кого пускать к календарю раньше: у кого меньше запаса.

    Не по дедлайну, и это важно. EDF оптимален, когда работать можно в любую
    минуту, — но эфирные минуты приходят со своей скоростью, и «догнать потом»
    получается не всегда: редкую кампанию нельзя отсмотреть быстрее, чем идёт
    эфир по игре. Дедлайн сам по себе этого не знает, а запас знает.

    Пример, на котором видна разница. Кампания A: дедлайн через два часа, час
    работы, эфир круглосуточный — запас час. Кампания B: дедлайн через десять
    часов, 300 эфирных минут при доступности 0.5, то есть нужны все десять
    часов — запас ноль. По дедлайну первой идёт A, забирает календарь целиком,
    и B опаздывает на полчаса. По запасу первой идёт B, они делят календарь
    пополам — и успевают обе. Цель — забрать все дропы, а не соблюсти порядок.

    Ничью разрешает вотчлист.
    """
    return sorted(ready, key=lambda t: (_slack(t, left, moment), t.priority))


def _shares(ready: list[Task], order: list[Task]) -> tuple[dict[str, float], set[str]]:
    """Кому какая доля календаря достаётся, пока расклад не изменился.

    Календарь один — смотреть можно только одну игру за раз, поэтому сумма
    долей не больше единицы; кто раньше в очереди, тот и берёт своё первым.
    Быстрее своей доступности эфира задача набирать не может, и как раз поэтому
    редкой кампании остаётся всего её процент, а не весь календарь.

    Возвращает ещё и тех, кому эфира не досталось совсем или досталось меньше,
    чем позволяла игра: это будущие виновники и пострадавшие в разборе
    конфликта.
    """
    shares: dict[str, float] = {}
    taken: dict[str, float] = {}  # сколько эфира игры уже разобрано
    free = 1.0
    for task in order:
        if task.campaign_id in shares:
            continue  # уже капает тем же просмотром, что кампания выше
        # Эфир игры нельзя продать дважды: две кампании с разными каналами
        # от одного просмотра не двигаются, значит делят те же проценты
        room = task.air_share - taken.get(task.game_id, 0.0)
        share = min(room, free)
        if share > 0:
            shares[task.campaign_id] = share
            taken[task.game_id] = taken.get(task.game_id, 0.0) + share
            free -= share
            # Тот же просмотр двигает и остальные кампании этой игры: минута,
            # засчитанная Twitch, идёт всем, кому подходит канал, и второй раз
            # календаря не стоит. Без этого план считал одну и ту же работу
            # по нескольку раз и выдумывал конфликты
            for other in ready:
                if other.campaign_id not in shares and task.shares_air_with(other):
                    shares[other.campaign_id] = share
        if free <= 0:
            break
    starved = {
        t.campaign_id for t in ready if shares.get(t.campaign_id, 0.0) < t.air_share - 1e-9
    }
    return shares, starved


def _walk(tasks, left: dict[str, float], moment, end, rank, blocks=None, blame=None):
    """Разложить оставшуюся работу от `moment` до `end`.

    `left` меняется на месте — вызывающий передаёт копию, если она нужна целой.
    `rank(ready, moment)` выстраивает очередь на календарь. `blocks` и `blame`,
    если переданы, наполняются отрезками и долгами; прогонам вперёд не нужны
    ни то, ни другое.
    """
    tail: dict[str, Block] = {}  # последний блок кампании — чтобы склеивать
    while moment < end:
        ready = [
            t
            for t in tasks
            if t.release <= moment and t.deadline > moment and left[t.campaign_id] > TOLERANCE
        ]
        if not ready:
            # ничего не готово — прыгаем к ближайшему появлению
            later = [
                t.release for t in tasks if t.release > moment and left[t.campaign_id] > TOLERANCE
            ]
            if not later:
                break
            moment = min(later)
            continue

        order = rank(ready, moment)
        shares, starved = _shares(ready, order)

        # держим расклад до ближайшего события, которое может его сменить:
        # кто-то появился, у кого-то вышел срок или кто-то дописал свои минуты
        events = [t.release for t in tasks if t.release > moment]
        events += [t.deadline for t in tasks if t.deadline > moment]
        span = ((min([*events, end])) - moment).total_seconds() / 60
        done = [left[cid] / share for cid, share in shares.items() if share > 0]
        if done:
            span = min(span, min(done))
        if span <= 0 or not shares:
            moment = min([*events, end])
            continue

        finish = moment + timedelta(minutes=span)
        for task in order:
            share = shares.get(task.campaign_id, 0.0)
            if share <= 0:
                continue
            got = span * share
            left[task.campaign_id] = max(0.0, left[task.campaign_id] - got)
            if blocks is None:
                continue
            last = tail.get(task.campaign_id)
            # Склеиваем соседние куски одной задачи, даже если доля изменилась:
            # доля меняется каждый раз, когда рядом кто-то закончил, и без
            # склейки в плане копились обрезки по минуте
            if last is not None and last.end == moment:
                last.end = finish
                last.minutes += got
            else:
                block = Block(task, moment, finish, got)
                blocks.append(block)
                tail[task.campaign_id] = block
        if blame is not None:
            _blame(blame, ready, shares, starved, span)
        moment = finish
    return left


def _blame(blame, ready, shares, starved, span: float) -> None:
    """Записать, кто отнял календарь у тех, кому эфира не досталось.

    Долг считается в календарных минутах: столько чужого просмотра прошло, пока
    задача стояла. Кампании, которые идут от того же просмотра, виновными быть
    не могут — они не отнимают, а едут вместе.
    """
    if not starved:
        return
    by_id = {t.campaign_id: t for t in ready}
    for campaign_id in starved:
        task = by_id[campaign_id]
        owed = blame.setdefault(campaign_id, {})
        for rival_id, share in shares.items():
            rival = by_id[rival_id]
            if rival_id == campaign_id or share <= 0 or task.shares_air_with(rival):
                continue
            owed[rival_id] = owed.get(rival_id, 0.0) + span * share


def _cost(left: dict[str, float]) -> tuple[int, float]:
    """Во что обошлось расписание: сколько кампаний не поместилось и на сколько."""
    rest = [value for value in left.values() if value > TOLERANCE]
    return len(rest), sum(rest)


def _queue(ready: list[Task], left, moment, lifted: frozenset[str]) -> list[Task]:
    """Очередь на календарь: поднятые вотчлистом впереди, остальные по запасу.

    `lifted` — кампании, которым разрешено идти по порядку вотчлиста, потому что
    это никому не стоило дедлайна (см. `_lifted`). Внутри группы спор решает
    приоритет, а при равном приоритете — дедлайн: у одной игры кампаний бывает
    несколько, и приоритет у них общий.
    """
    up = sorted(
        (t for t in ready if t.campaign_id in lifted),
        key=lambda t: (t.priority, t.deadline),
    )
    rest = [t for t in ready if t.campaign_id not in lifted]
    return [*up, *_order(rest, left, moment)]


def _cost_with(tasks, left, moment, end, lifted: frozenset[str]) -> tuple[int, float]:
    """Во что обойдётся весь остаток плана при таком наборе поднятых."""
    trial = dict(left)
    return _cost(
        _walk(
            tasks,
            trial,
            moment,
            end,
            lambda ready, at: _queue(ready, trial, at, lifted),
        )
    )


def _lifted(tasks, left, moment, end) -> frozenset[str]:
    """Кого можно пустить вперёд по вотчлисту, не отняв ни у кого дедлайна.

    Правило то же, что было («порядок забирает окно, пока это никому не стоит
    дедлайна»), но теперь оно про **всю** очередь, а не про её голову. Раньше
    приоритет решал только первую позицию, а доли календаря раздаются по всей
    очереди — то есть перетащить игру на второе место значило ничего не изменить,
    и обещание экрана «Мои игры» держалось лишь для первой строки.

    Считается **один раз на пересборку**, а не на каждом шаге раскладки, и это
    принципиально: ответ на вопрос «можно ли соблюсти порядок бесплатно» —
    свойство вотчлиста и набора кампаний, а не текущей минуты. По шагам это
    стоило почти секунды на сорока кампаниях, здесь — десятков миллисекунд.

    Проверка честная: цена считается тем же порядком, каким расписание потом
    и строится (`_queue`), и сравнивается с ценой чистого порядка по запасу.
    Не хуже — поднимаем. Значит человек, перетащивший строку, получает порядок,
    но не получает потерянную кампанию: выдумать конфликт своими руками
    программа не может.
    """
    plain = _cost_with(tasks, left, moment, end, frozenset())
    everyone = frozenset(t.campaign_id for t in tasks)
    # Сначала примеряем весь порядок вотчлиста разом: обычно он никому ничего
    # не стоит, и тогда торговаться за каждую кампанию незачем — один прогон
    if _cost_with(tasks, left, moment, end, everyone) <= plain:
        return everyone

    lifted: frozenset[str] = frozenset()
    for candidate in sorted(tasks, key=lambda t: (t.priority, t.deadline)):
        trial = lifted | {candidate.campaign_id}
        if _cost_with(tasks, left, moment, end, trial) <= plain:
            lifted = trial
    return lifted


def build(tasks: list[Task], horizon: timedelta = DEFAULT_HORIZON) -> Plan:
    """Построить расписание и найти, что не помещается.

    Очередь на календарь возглавляет приоритет вотчлиста, пока это никому
    не стоит дедлайна, — см. `_lifted`. Где стоит, решает запас: у кого меньше
    всех, тот и раньше.
    """
    now = dr.now()
    end = now + horizon
    if not tasks:
        return Plan([], [], [], now, end)

    # считаем в эфирных минутах — в тех самых, которые Twitch засчитывает
    left: dict[str, float] = {t.campaign_id: float(t.minutes) for t in tasks}
    by_id = {t.campaign_id: t for t in tasks}
    blocks: list[Block] = []
    blame: dict[str, dict[str, float]] = {}

    # Кому можно вперёд по вотчлисту — решается один раз на весь прогон:
    # это свойство порядка и набора кампаний, а не текущей минуты
    lifted = _lifted(tasks, left, now, end)

    _walk(
        tasks,
        left,
        now,
        end,
        lambda ready, moment: _queue(ready, left, moment, lifted),
        blocks,
        blame,
    )

    shortfalls = []
    for campaign_id, rest in left.items():
        if rest <= TOLERANCE:
            continue
        task = by_id[campaign_id]
        # За горизонтом мы просто не смотрели: у такой кампании есть время,
        # о котором план ничего не знает, и объявлять её конфликтом — выдумывать
        if task.deadline > end:
            continue
        shortfalls.append(
            Shortfall(
                task=task,
                missing_minutes=rest,
                competitors=_competitors(blame.get(campaign_id, {}), by_id),
            )
        )
    shortfalls.sort(key=lambda s: s.task.deadline)
    return Plan(tasks, blocks, shortfalls, now, end)




def _competitors(owed: dict[str, float], by_id: dict[str, Task]) -> list[str]:
    """Кто отнял календарь у задачи, которой не хватило эфира.

    Раньше соперником считался всякий, чей блок пересёкся с окном задачи. Пока
    блоки шли встык, это работало; теперь они пересекаются почти все — эфир
    делится, а не занимается целиком, — и такой признак назвал бы соперником
    каждого, в том числе редкую кампанию, которая идёт неделю и никому
    не мешает. Считаем настоящий долг: сколько чужого просмотра прошло именно
    в те минуты, когда задача из-за него стояла. Самый крупный должник первым:
    его же первым и предложит разбор конфликта.
    """
    names: list[str] = []
    for campaign_id, minutes in sorted(owed.items(), key=lambda pair: -pair[1]):
        name = by_id[campaign_id].campaign
        if minutes > TOLERANCE and name not in names:
            names.append(name)
    return names


@dataclass
class Option:
    """Одна сторона вилки «что важнее».

    Раньше это был «вариант разбора» с подписью «Отказаться от X»: называлась
    только потеря, и сравнить её было не с чем. А выбор здесь всегда об одном —
    какая из двух кампаний важнее, — и понятен он только когда у обеих сторон
    видна цена. Поэтому тут не слова, а числа: сколько дропов возьмёт наша,
    если выбрать её, сколько возьмёт, если не выбрать, и сколько потеряет
    соперник. Словами это одевают экран и бот, каждый по-своему.
    """

    # кого отключаем, если выбрали нашу сторону
    drop_campaign_id: str
    drop_campaign: str
    drop_game: str
    # наша кампания: дропов станет / было бы / всего
    gain: int
    keep: int
    total: int
    # соперник: дропов у него сейчас / всего у него
    rival_keeps: int
    rival_total: int


def _outcome(plan: Plan, campaign_id: str) -> tuple[int, float]:
    """Что выйдет по кампании в этом расписании: (сколько дропов, сколько не хватит)."""
    gap = next((s for s in plan.shortfalls if s.task.campaign_id == campaign_id), None)
    if gap is not None:
        return gap.reachable, gap.missing_minutes
    task = next((t for t in plan.tasks if t.campaign_id == campaign_id), None)
    return (len(task.milestones) if task else 0), 0.0


def _helps(shortfall: Shortfall, trial: Plan) -> bool:
    """Стало ли лучше нашей кампании в этом расписании.

    Мерка — забранные дропы, а не сэкономленные минуты: «не хватает на 5 минут
    меньше» человеку ничего не даёт, если веха всё равно не взята. Но когда
    данных о вехах нет вовсе (а такое бывает у только что открывшейся кампании),
    мерить всё же надо — иначе проверка молча признаёт бесполезным любой выбор.
    """
    drops, missing = _outcome(trial, shortfall.task.campaign_id)
    if shortfall.task.milestones:
        return drops > shortfall.reachable
    return missing < shortfall.missing_minutes - TOLERANCE


def resolutions(shortfall: Shortfall, plan: Plan) -> list[Option]:
    """Варианты разбора — только те, что правда меняют исход.

    Раньше здесь перечислялись все соперники плюс «отказаться от себя», и цена
    бралась с потолка. На живых данных это выглядело так: у Overwatch пять
    кнопок, четыре из них «отказаться от <кампании Marvel Rivals>» — а все
    четыре кампании Marvel идут от одного просмотра и держат один и тот же
    календарь, так что отказ от одной не освобождает ни минуты. Пятая
    предлагала отказаться от самого Overwatch, то есть от двух дропов из трёх,
    которые и так были бы забраны.

    Поэтому каждая сторона теперь проверяется пересчётом: строим расписание без
    кандидата и смотрим, стало ли дропов больше. Не стало — стороны нет.
    Не осталось ни одной — значит решать нечего, и экрану сказать нечего:
    что-то успеем, что-то нет.

    Отказа «от себя» здесь больше нет как отдельного варианта: он и был
    той самой кнопкой «сделай хуже» — кампания, у которой не влезает последняя
    веха, при отказе теряет и всё остальное. Оставить как есть — это не вариант
    в списке, а то, что случится, если ничего не нажимать; экран так и говорит.
    """
    ours = shortfall.task.campaign_id
    options: list[Option] = []

    rivals = [
        t
        for t in plan.tasks
        if t.campaign in shortfall.competitors and t.campaign_id != ours
    ]
    for rival in sorted(rivals, key=lambda t: t.priority):
        trial = build([t for t in plan.tasks if t.campaign_id != rival.campaign_id])
        if not _helps(shortfall, trial):
            continue  # ничего не меняет — молчим
        drops, _ = _outcome(trial, ours)
        options.append(
            Option(
                drop_campaign_id=rival.campaign_id,
                drop_campaign=rival.campaign,
                drop_game=rival.game,
                gain=drops,
                keep=shortfall.reachable,
                total=shortfall.total,
                # что теряет соперник: отключённая кампания больше не фармится,
                # то есть все её незабранные дропы — и это надо назвать вслух
                rival_keeps=_outcome(plan, rival.campaign_id)[0],
                rival_total=len(rival.milestones),
            )
        )
    return options
