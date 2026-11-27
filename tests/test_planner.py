"""Проверка планировщика на случаях, где ответ известен руками."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vahta import drops as dr
from vahta.planner import TOLERANCE, Task, build, resolutions

NOW = dr.now()

# Сравнивать минуты машинным эпсилоном нельзя — по той же причине, по которой
# TOLERANCE появился в самом планировщике: NOW берётся при импорте, а build
# спрашивает часы заново, и окно оказывается на доли секунды короче номинала.
# Проверка «не хватает ровно часа» с допуском в 60 микросекунд падала примерно
# в каждом шестом прогоне.


def task(name: str, minutes: int, deadline_h: float, *, release_h: float = 0,
         availability: float = 1.0, priority: int = 1, game: str | None = None,
         channels: tuple[str, ...] = (),
         milestones: tuple[int, ...] = ()) -> Task:
    return Task(
        milestones=milestones,
        game_id=game or name,
        game=game or name,
        campaign_id=name,
        campaign=name,
        channels=channels,
        minutes=minutes,
        release=NOW + timedelta(hours=release_h),
        deadline=NOW + timedelta(hours=deadline_h),
        availability=availability,
        confident=True,
        priority=priority,
    )


def check(label: str, condition: bool) -> bool:
    print(f"  {'ок  ' if condition else 'ПЛОХО'}  {label}")
    return condition


def air(plan, campaign: str) -> float:
    """Эфирных минут, которые план отвёл кампании."""
    return sum(b.minutes for b in plan.blocks if b.task.campaign == campaign)


def calendar(plan) -> float:
    """Календарных минут от начала плана до конца последнего блока."""
    if not plan.blocks:
        return 0.0
    return (max(b.end for b in plan.blocks) - plan.starts_at).total_seconds() / 60


def finish(plan, campaign: str):
    ends = [b.end for b in plan.blocks if b.task.campaign == campaign]
    return max(ends) if ends else None


def main() -> int:
    ok = True

    # Одна задача, времени вагон
    plan = build([task("A", minutes=60, deadline_h=10)])
    ok &= check("одна задача помещается", plan.feasible)
    ok &= check("на неё отведён ровно час эфира", abs(air(plan, "A") - 60) < TOLERANCE)
    ok &= check("и ровно час календаря", abs(calendar(plan) - 60) < TOLERANCE)

    # Две задачи, суммарно ровно впритык
    plan = build([task("A", 60, 2), task("B", 60, 2)])
    ok &= check("две по часу в два часа помещаются", plan.feasible)

    # Две задачи, работы больше, чем времени
    plan = build([task("A", 90, 2), task("B", 90, 2)])
    ok &= check("три часа работы в два часа не влезают", not plan.feasible)
    ok &= check("не хватает ровно часа", abs(sum(s.missing_minutes for s in plan.shortfalls) - 60) < TOLERANCE)

    # EDF: срочная задача идёт первой, даже если добавлена второй
    plan = build([task("долгая", 60, 10), task("срочная", 60, 2)])
    ok &= check("первой стоит срочная", plan.blocks[0].task.campaign == "срочная")
    ok &= check("обе успевают", plan.feasible)

    # Прерывание: срочная появляется позже и вытесняет долгую
    plan = build([task("долгая", 120, 12), task("срочная", 60, 4, release_h=1)])
    names = [b.task.campaign for b in plan.blocks]
    ok &= check("долгая прерывается срочной", names == ["долгая", "срочная", "долгая"])
    ok &= check("обе успевают", plan.feasible)

    # --- эфир делится, календарь не резервируется -----------------------------
    # Ради этого модель и переписана. Просмотр занимает календарь один к одному,
    # а ожидание эфира — не занимает вовсе: пока по редкой игре никто не
    # стримит, «Вахта» смотрит другую. Раньше редкая кампания забирала календарь
    # целиком (600 мин при 8% эфира — это пятеро суток), и всё остальное
    # вставало в очередь за ней, на пятый день.

    редкая = task("редкая", 600, 24 * 7, availability=0.08, priority=1)
    частая = task("частая", 120, 24 * 7, availability=1.0, priority=2)
    plan = build([редкая, частая])
    ok &= check("редкая кампания не отбирает календарь у остальных", plan.feasible)
    ok &= check("обе начинаются сразу, а не по очереди",
                len({b.start for b in plan.blocks if b.start == plan.starts_at}) == 1
                and {b.task.campaign for b in plan.blocks if b.start == plan.starts_at}
                == {"редкая", "частая"})
    fast = finish(plan, "частая")
    ok &= check("частая заканчивается в первые часы, а не на пятый день",
                fast is not None and (fast - plan.starts_at) < timedelta(hours=3))
    slow = finish(plan, "редкая")
    ok &= check("редкая всё равно тянется по эфиру (600 мин при 8% — пятеро суток)",
                slow is not None and abs((slow - plan.starts_at).total_seconds() / 60 - 7500) < 60)
    ok &= check("каждой отведено ровно её эфирное время",
                abs(air(plan, "редкая") - 600) < TOLERANCE
                and abs(air(plan, "частая") - 120) < TOLERANCE)

    # Быстрее своего эфира задача набирать не может, даже когда соперников нет
    plan = build([task("редкая", 60, 3, availability=0.5)])
    ok &= check("час эфира при 50% растянулся на два часа календаря",
                abs(calendar(plan) - 120) < TOLERANCE)
    ok &= check("но работы в нём ровно час", abs(air(plan, "редкая") - 60) < TOLERANCE)

    plan = build([task("редкая", 60, 1.5, availability=0.5)])
    ok &= check("при 50% эфира час в полтора часа не влезает", not plan.feasible)

    # Календарного времени хватает, эфирного — нет
    plan = build([task("A", 300, 10, availability=0.3)])
    ok &= check("календарь обманывает, эфир — нет", not plan.feasible)

    # Календарь один: две игры с постоянным эфиром идут по очереди, а не вместе
    plan = build([task("A", 60, 5, game="rivals"), task("B", 60, 5, game="finals")])
    ok &= check("два постоянных эфира делят календарь, а не удваивают его",
                abs(calendar(plan) - 120) < TOLERANCE)

    # Эфир одной игры нельзя продать дважды: кампании с разными каналами
    # не идут от одного просмотра, значит делят те же 50% между собой
    plan = build([
        task("A", 30, 1.5, game="rivals", availability=0.5, channels=("alpha",)),
        task("B", 30, 1.5, game="rivals", availability=0.5, channels=("beta",)),
    ])
    ok &= check("эфир игры не делится на двоих сверх самого себя", not plan.feasible)

    # --- очередь по запасу, а не по дедлайну ----------------------------------
    # Главная цель — забрать все дропы, а не соблюсти порядок дедлайнов. Эфирные
    # минуты приходят со своей скоростью, и «догнать потом» получается не всегда:
    # редкую кампанию нельзя отсмотреть быстрее, чем идёт эфир. Дедлайн этого
    # не знает, запас знает.
    #
    # A: дедлайн через 2 ч, час работы, эфир круглосуточный — запас час.
    # B: дедлайн через 10 ч, 300 эфирных минут при 0.5, то есть нужны все
    # десять часов — запас ноль. По дедлайну первой шла бы A, забирала календарь
    # целиком, и B опаздывала на полчаса. По запасу они делят его пополам.
    plan = build([task("частая", 60, 2, availability=1.0),
                  task("редкая", 300, 10, availability=0.5)])
    ok &= check("по запасу успевают обе, хотя по дедлайну — нет", plan.feasible)
    ok &= check("и обе идут с самого начала",
                {b.task.campaign for b in plan.blocks if b.start == plan.starts_at}
                == {"частая", "редкая"})

    # Но запас не отменяет дедлайн там, где тот и есть запас: при равной
    # доступности порядок по запасу совпадает с порядком по дедлайну
    plan = build([task("поздняя", 60, 9), task("ранняя", 60, 3)])
    ok &= check("при равном эфире решает дедлайн, как и раньше",
                plan.blocks[0].task.campaign == "ранняя")

    # --- приоритет вотчлиста -------------------------------------------------
    # Порядок в вотчлисте решает, кому достанется окно, — но только пока это
    # никому не стоит дедлайна. Иначе работает EDF, который для этого оптимален.

    # Приоритет решает не только голову очереди. Запас поставил бы эти три
    # наоборот (у первой дедлайн дальше всех), вотчлист просит прямой порядок,
    # и это никому ничего не стоит — значит соблюдается целиком. Раньше
    # приоритет решал только первую строку, а доли раздаются по всей очереди,
    # то есть перетащить игру на второе место значило ничего не изменить
    plan = build([task("первая", 60, 30, priority=1),
                  task("вторая", 60, 20, priority=2),
                  task("третья", 60, 10, priority=3)])
    ok &= check("порядок вотчлиста соблюдается целиком, а не только в голове",
                [b.task.campaign for b in plan.blocks] == ["первая", "вторая", "третья"])
    ok &= check("и никто не опоздал", plan.feasible)

    # А если соблюсти порядок целиком нельзя, вперёд пускают только тех, кто
    # никому не мешает: «впритык» идёт первой, хотя стоит в списке последней
    plan = build([task("первая", 60, 30, priority=1),
                  task("вторая", 60, 20, priority=2),
                  task("впритык", 60, 1, priority=3)])
    ok &= check("кто стоит впритык, тот идёт первым независимо от списка",
                plan.blocks[0].task.campaign == "впритык")
    ok &= check("и снова никто не опоздал", plan.feasible)

    # Обе успевают в любом порядке: побеждает приоритет, а не дедлайн
    plan = build([task("любимая", 60, 10, priority=1), task("срочная", 60, 5, priority=2)])
    ok &= check("при запасе окно берёт первая по списку",
                plan.blocks[0].task.campaign == "любимая")
    ok &= check("и никто не опоздал", plan.feasible)

    # Тот же расклад, но времени ровно на двоих: уступка стоила бы дедлайна
    plan = build([task("любимая", 60, 10, priority=1), task("срочная", 60, 1, priority=2)])
    ok &= check("когда уступка стоит дедлайна, окно берёт срочная",
                plan.blocks[0].task.campaign == "срочная")
    ok &= check("и снова никто не опоздал", plan.feasible)

    # Приоритет не «сильнее», а «не хуже»: конфликт из ничего не создаётся
    plan = build([task("любимая", 120, 20, priority=1), task("срочная", 60, 1, priority=2)])
    ok &= check("вперёд не пускают, если это ломает дедлайн",
                plan.blocks[0].task.campaign == "срочная")
    ok &= check("обе всё равно помещаются", plan.feasible)

    # Равный приоритет бывает: у одной игры несколько кампаний. Спор решает дедлайн
    plan = build([task("поздняя", 60, 9, priority=3), task("ранняя", 60, 3, priority=3)])
    ok &= check("при равном приоритете спор решает дедлайн",
                plan.blocks[0].task.campaign == "ранняя")

    # Когда всё уже не помещается, приоритет решает, кто из двух уцелеет
    plan = build([task("любимая", 120, 2, priority=1), task("прочая", 120, 2, priority=2)])
    ok &= check("в перегрузке первая по списку забирает окно",
                plan.blocks[0].task.campaign == "любимая")
    ok &= check("и кто-то всё равно не помещается", not plan.feasible)

    # Задача, которая ещё не началась, не мешает считать
    plan = build([task("поздняя", 60, 20, release_h=10), task("ранняя", 60, 5)])
    ok &= check("обе успевают при разнесённых окнах", plan.feasible)
    ok &= check("первой идёт ранняя", plan.blocks[0].task.campaign == "ранняя")

    # Нехватка называет соперника и предлагает выбор — когда он что-то меняет.
    # Вехи здесь настоящие: у живой кампании они есть всегда, а вилка без них
    # не может назвать цену
    plan = build([task("A", 90, 2, priority=1, milestones=(30, 90)),
                  task("B", 90, 2, priority=2, milestones=(30, 90))])
    ok &= check("в нехватке указан соперник",
                all(s.competitors for s in plan.shortfalls))
    options = resolutions(plan.shortfalls[0], plan)
    ok &= check("предложена сторона вилки", len(options) >= 1)
    ok &= check("каждая сторона знает, кем жертвует",
                all(o.drop_campaign_id and o.drop_game for o in options))
    # У вилки должна быть цена с двух сторон, иначе выбирать не из чего:
    # что получим, что было бы без выбора и что потеряет соперник
    ok &= check("и цену с двух сторон",
                all(o.gain > o.keep and o.rival_total >= 0 for o in options))
    print("      вилка:", "; ".join(
        f"важнее «{plan.shortfalls[0].task.campaign}» ({o.gain} из {o.total} "
        f"вместо {o.keep}) ценой «{o.drop_campaign}»" for o in options))

    # --- «не влезает» ≠ «нечего фармить» -------------------------------------
    # Дропы это вехи одного потока просмотра. «Не хватает 21 минуты» означает
    # «последнюю веху не возьмём», а не «всё впустую»: за оставшееся время
    # ближние вехи заберутся как обычно. Отказ от такой кампании — добровольная
    # потеря почти забранных дропов, и предлагать его нельзя.
    tight = task("тесная", 140, 1.7, milestones=(30, 60, 140))
    plan = build([tight])
    gap = plan.shortfalls[0]
    ok &= check("не влезает только последняя веха", gap.reachable == 2 and gap.total == 3)
    ok &= check("и это не «ничего не успеваем»", not gap.empty)
    ok &= check("отказаться от себя не предлагают: два дропа и так наши",
                resolutions(gap, plan) == [])

    # А вот когда не успеваем даже первую веху — решение и правда есть
    hopeless = task("безнадёжная", 300, 0.5, priority=2, milestones=(120, 300))
    plan = build([hopeless])
    gap = plan.shortfalls[0]
    ok &= check("ни одной вехи — это «не успеваем»", gap.empty and gap.reachable == 0)

    # Отказ от соперника предлагается, только если он правда спасает дроп.
    # Кампании одной игры идут от одного просмотра: отказ от одной не освобождает
    # ни минуты, потому что вторая занимает то же место. Ровно это и было видно
    # на живых данных — у Overwatch четыре кнопки «отказаться от Marvel Rivals»
    plan = build([
        task("своя", 90, 2, game="rivals", priority=1),
        task("сестра", 90, 2, game="rivals", priority=1),
        task("чужая", 140, 2, priority=2, milestones=(30, 140)),
    ])
    gap = next(s for s in plan.shortfalls if s.task.campaign == "чужая")
    ok &= check("отказ от одной из двух кампаний одной игры не предлагается",
                all("своя" not in o.label and "сестра" not in o.label
                    for o in resolutions(gap, plan)))

    # Соперник — тот, кто правда отнял календарь, а не всякий, чей блок рядом.
    # Блоки теперь пересекаются почти все: редкая кампания идёт неделю, но
    # календаря на ней восемь процентов, и виновником чужой нехватки она не была
    plan = build([
        task("редкая", 300, 24 * 7, availability=0.05, priority=1),
        task("тесная", 240, 2, availability=1.0, priority=2),
    ])
    gaps = {s.task.campaign: s for s in plan.shortfalls}
    ok &= check("не помещается именно тесная", set(gaps) == {"тесная"})
    ok &= check("редкая в соперники не записана",
                "редкая" not in gaps["тесная"].competitors)

    # Один просмотр двигает все кампании игры, которым подходит канал:
    # час на Marvel Rivals идёт и в «Season 9», и в «Ignite MSF». Раньше план
    # считал эту работу дважды и выдумывал конфликт на ровном месте.
    # Порознь 90 + 90 в два часа не помещаются, вместе — помещаются
    plan = build([task("A", 90, 2, game="rivals"), task("B", 90, 2, game="rivals")])
    ok &= check("две кампании одной игры идут от одного просмотра", plan.feasible)
    ok &= check("и занимают полтора часа календаря, а не три",
                abs(calendar(plan) - 90) < TOLERANCE)
    ok &= check("при этом каждой засчитаны свои 90 минут",
                abs(air(plan, "A") - 90) < TOLERANCE and abs(air(plan, "B") - 90) < TOLERANCE)

    # У кампаний со своими списками каналов общего просмотра не выходит,
    # если списки не пересекаются
    plan = build([
        task("A", 90, 2, game="rivals", channels=("alpha",)),
        task("B", 90, 2, game="rivals", channels=("beta",)),
    ])
    ok &= check("разные каналы — работа отдельная", not plan.feasible)
    plan = build([
        task("A", 90, 2, game="rivals", channels=("alpha", "beta")),
        task("B", 90, 2, game="rivals", channels=("beta",)),
    ])
    ok &= check("общий канал — снова вместе", plan.feasible)

    # разные игры одновременно не смотрятся
    plan = build([task("A", 90, 2, game="rivals"), task("B", 90, 2, game="finals")])
    ok &= check("разные игры делят время как раньше", not plan.feasible)

    # Дальше горизонта план не смотрел — и конфликтов оттуда не выдумывает
    plan = build([task("длинная", 600, 24 * 30, availability=0.02)])
    ok &= check("кампания с дедлайном за горизонтом не считается конфликтом",
                plan.feasible)

    ok &= check_tasks()
    ok &= check_speed()

    print("\nВсё сошлось" if ok else "\nЕсть расхождения")
    return 0 if ok else 1


def check_speed() -> bool:
    """Прогоны вперёд не должны стоить заметного времени.

    `_lifted` примеряет порядок вотчлиста прогонами вперёд, и на живом
    вотчлисте это десятки кампаний. Считается он один раз на прогон, а не
    на каждом шаге раскладки: по шагам то же самое стоило 828 мс на сорока
    кампаниях вместо нынешних шести. Проверять дешевле, чем однажды обнаружить
    пересчёт, который держит службу минуту.
    """
    from time import perf_counter

    many = [
        task(f"C{i}", 120 + i, 24 + i, availability=0.1 + (i % 9) / 10,
             priority=1 + i // 3, game=f"g{i // 3}")
        for i in range(40)
    ]
    started = perf_counter()
    plan = build(many)
    spent = (perf_counter() - started) * 1000
    print(f"      сорок кампаний: {spent:.0f} мс, блоков {len(plan.blocks)}")
    return check("сорок кампаний считаются быстрее секунды", spent < 1000)


def check_waiting(store, status) -> bool:
    """Кампания, которую фармить нельзя вовсе, в план не берётся.

    Ради этого: у Overwatch эфир круглосуточный, а обе кампании идут только
    на своих каналах, и те не стримят. Замер писал одно число на оба факта,
    доступность выходила 0, пол в 1% превращал её в 14 минут в сутки — и план
    шесть дней подряд обещал фарм, которого не будет, плюс конфликт
    «не хватает 8 ч 50 м» на ровном месте.
    """
    import tempfile

    from vahta.planner import MIN_SAMPLES, make_tasks, waiting_reason

    game = status.game_id
    got = check("пока замеров мало, планируем как обычно",
                waiting_reason(game, store) == "")

    # эфир есть, а кампанию поймать не на чем — «стримят, но не то»
    for _ in range(MIN_SAMPLES):
        store.record_uptime(game, False, True)
    got &= check("«эфир есть, но кампания идёт не там» названо своим именем",
                 "своих каналах" in waiting_reason(game, store))
    got &= check("и в план такая кампания не берётся", make_tasks([status], store) == [])

    # по игре вообще не стримят — причина другая, и ждать её бессмысленно
    from vahta.store import Store

    with tempfile.TemporaryDirectory() as raw:
        other = Store(Path(raw) / "vahta.db")
        try:
            for _ in range(MIN_SAMPLES):
                other.record_uptime(game, False, False)
            got &= check("«по игре не стримят» — это другая причина",
                         "не стримят" in waiting_reason(game, other))
        finally:
            other.close()

    # канал появился — приговор снимается сам, без чьей-либо помощи
    store.record_uptime(game, True, True)
    got &= check("появился канал — кампания вернулась в план",
                 len(make_tasks([status], store)) == 1)
    return got


def check_tasks() -> bool:
    """Что планировщик вообще берёт в работу.

    Отложенные кампании он брать не должен: обходчик откладывает те, по которым
    Twitch считает время, но дроп не отдаёт, — отдавать им окно значит жечь часы
    впустую (так и было с THE FINALS: восемь кругов подряд по 266/240 мин).
    """
    import tempfile

    from vahta import scanner
    from vahta.planner import make_tasks
    from vahta.store import Store

    campaign = dr.Campaign(
        id="camp-1", name="S11 LAUNCH DROPS", game="THE FINALS", game_id="1",
        starts_at=NOW - timedelta(hours=1), ends_at=NOW + timedelta(hours=10),
        account_linked=True,
        drops=[dr.Drop(id="d1", name="Charm", required_minutes=240)],
    )
    status = scanner.GameStatus(
        game_id="1", name="THE FINALS", campaigns=[campaign], available=True, fresh=[]
    )

    with tempfile.TemporaryDirectory() as raw:
        store = Store(Path(raw) / "vahta.db")
        try:
            got = check("кампания попадает в план", len(make_tasks([status], store)) == 1)
            got &= check_waiting(store, status)
            store.hold(campaign.id, 60, "Twitch не отдаёт дроп")
            got &= check("отложенная не попадает", make_tasks([status], store) == [])
            store.unhold(campaign.id)
            got &= check("снятая с ожидания возвращается",
                         len(make_tasks([status], store)) == 1)
            store.skip(campaign.id)
            got &= check("а от чего отказались — не возвращается",
                         make_tasks([status], store) == [])
        finally:
            store.close()
    return got


if __name__ == "__main__":
    raise SystemExit(main())
