"""Проверка бота без сети: разбор команд, кнопки конфликта, пауза."""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vahta import drops as dr
from vahta import planner
from vahta.planner import Task, build
from vahta.store import Store
from vahta.telegram import Bot, Config, parse_span

NOW = dr.now()


class FakeApp:
    """Приложение-заглушка: боту от него нужны store, plan, live и пауза."""

    def __init__(self, store: Store):
        self.store = store
        self.tw = None
        self.plan = None
        self.paused_until = None
        self.live = type("L", (), {"game": "", "campaign": "", "channel": "",
                                   "viewers": 0, "current": 0, "required": 0,
                                   "note": "простой"})()

    def pause(self, span):
        self.paused_until = dr.now() + span
        return self.paused_until

    def resume(self):
        self.paused_until = None

    async def replan(self):
        return self.plan


def task(name: str, minutes: int, deadline_h: float, priority: int = 1,
         milestones: tuple[int, ...] = ()) -> Task:
    return Task(game_id=name, game=name, campaign_id=f"id-{name}", campaign=name,
                minutes=minutes, release=NOW, deadline=NOW + timedelta(hours=deadline_h),
                availability=1.0, confident=True, priority=priority,
                milestones=milestones)


def check(label: str, condition: bool) -> bool:
    print(f"  {'ок  ' if condition else 'ПЛОХО'}  {label}")
    return condition


async def run() -> bool:
    ok = True
    tmp = Path(__file__).with_name("_tg_test.db")
    tmp.unlink(missing_ok=True)
    store = Store(tmp)
    app = FakeApp(store)
    bot = Bot(app, Config(Path(__file__).with_name("_tg_test.json")))
    await bot.close()

    # разбор длительности паузы
    ok &= check("«2ч» -> два часа", parse_span("2ч") == timedelta(hours=2))
    ok &= check("«30м» -> тридцать минут", parse_span("30м") == timedelta(minutes=30))
    ok &= check("«90m» -> девяносто минут", parse_span("90m") == timedelta(minutes=90))
    ok &= check("мусор -> ничего", parse_span("завтра") is None)

    # команды
    ok &= check("/help перечисляет команды", "/status" in await bot.handle("/help"))
    ok &= check("незнакомая команда не падает", "Не знаю" in await bot.handle("/чтото"))
    ok &= check("/status без фарма честен", "Не фармит" in await bot.handle("/status"))
    ok &= check("/watchlist на пустом", "пуст" in await bot.handle("/watchlist"))
    ok &= check("/history на пустом", "Всего" in await bot.handle("/history"))

    # пауза
    await bot.handle("/pause 2ч")
    ok &= check("пауза выставлена", app.paused_until is not None)
    ok &= check("/status показывает паузу", "паузе" in await bot.handle("/status"))
    await bot.handle("/pause off")
    ok &= check("пауза снята", app.paused_until is None)

    # Нехватка: кнопки должны отключать конкретную кампанию — и появляться
    # только когда отказ правда что-то меняет, иначе это кнопка «сделай хуже»
    app.plan = build([task("A", 90, 2, priority=1), task("B", 90, 2, priority=2)])
    gap = app.plan.shortfalls[0]
    options = planner.resolutions(gap, app.plan)
    ok &= check("у каждой кнопки есть жертва",
                all(o.drop_campaign_id for o in options))
    ok &= check("есть вариант, жертвующий соперником",
                any(o.drop_campaign_id != gap.task.campaign_id for o in options))

    # Кампания, у которой не влезает только последняя веха, кнопок не даёт:
    # два дропа из трёх и так будут забраны
    lone = build([task("тесная", 140, 1.7, milestones=(30, 60, 140))])
    ok &= check("«не влезает целиком» решать нечем",
                planner.resolutions(lone.shortfalls[0], lone) == [])

    said: list = []

    async def catch(text, buttons=None):
        said.append((text, buttons))

    bot.say = catch
    await bot.conflict(lone.shortfalls[0], lone)
    ok &= check("но сказать об этом надо — без кнопок «решай»",
                len(said) == 1 and "не целиком" in said[0][0].lower()
                and "2 из 3" in said[0][0] and not said[0][1])

    # А где выбор есть — это вилка «что важнее», и у обеих сторон видна цена:
    # что получим, что было бы без выбора и что потеряет соперник
    said.clear()
    pair = build([task("A", 90, 2, priority=1, milestones=(30, 90)),
                  task("B", 90, 2, priority=2, milestones=(30, 90))])
    gap = pair.shortfalls[0]
    await bot.conflict(gap, pair)
    text, keys = said[0]
    ok &= check("вопрос поставлен вилкой", "Что важнее?" in text)
    ok &= check("названо, что получим и чего лишимся",
                "2 из 2 вместо 1" in text and "перестанем фармить" in text.lower())
    ok &= check("и что будет, если ничего не нажимать",
                "Оставить как есть" in text)
    ok &= check("кнопка на каждую сторону плюс «как есть»", len(keys) == 2)
    # Отказ «от себя» и был той самой кнопкой «сделай хуже»: кампания, у которой
    # не влезает последняя веха, при отказе теряет и всё остальное
    ok &= check("кнопка жертвует соперником, а не собой",
                keys[0][0]["callback_data"].startswith("drop:")
                and keys[0][0]["callback_data"] != f"drop:{gap.task.campaign_id}")
    # Подпись называла победителя, а он у всех вариантов один: в чате выходили
    # две одинаковые кнопки, делающие разное. Различает варианты жертва
    ok &= check("подпись называет жертву, а не победителя",
                gap.task.game not in keys[0][0]["text"]
                and all(o.drop_game in keys[n][0]["text"]
                        for n, o in enumerate(planner.resolutions(gap, pair))))

    # нажатие кнопки реально исключает кампанию из плана
    store.skip(options[0].drop_campaign_id)
    ok &= check("отказ записан", options[0].drop_campaign_id in store.skipped())
    ok &= check("отмена отказа работает", store.unskip(options[0].drop_campaign_id))
    ok &= check("после отмены список пуст", not store.skipped())

    # длина callback_data должна влезать в лимит Telegram (64 байта)
    ok &= check("callback_data влезает в лимит",
                all(len(f"drop:{o.drop_campaign_id}".encode()) <= 64 for o in options))

    ok &= check_noise(bot)

    store.close()
    tmp.unlink(missing_ok=True)
    Path(str(tmp) + "-wal").unlink(missing_ok=True)
    Path(str(tmp) + "-shm").unlink(missing_ok=True)
    return ok


def check_noise(bot) -> bool:
    """Об одной и той же беде говорят один раз, а не по строке на попытку.

    Ради этого: в ночь на 30 июля `api.telegram.org` отвалился, и лог получил
    под тридцать строк `Bad Gateway` в одну секунду — читать его стало нечем,
    а разбор той ночи как раз шёл по логу. Отказ Telegram при этом не был
    исключением, поэтому пауза не срабатывала и цикл долбил заново без остановки.
    """
    import logging

    seen: list[str] = []

    class Catch(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    catcher = Catch()
    log = logging.getLogger("vahta")
    log.addHandler(catcher)
    try:
        for _ in range(30):
            bot._trouble("getUpdates", "Bad Gateway")
        got = check("тридцать одинаковых отказов — одна строка", len(seen) == 1)
        bot._recovered()
        got &= check("а когда всё кончилось, сказано сколько их было",
                     len(seen) == 2 and "ещё 29 раз" in seen[1])

        seen.clear()
        bot._trouble("getUpdates", "Bad Gateway")
        bot._trouble("sendMessage", "Too Many Requests")
        got &= check("другая беда — своя строка", len(seen) == 2)
    finally:
        log.removeHandler(catcher)
    return got


def main() -> int:
    ok = asyncio.run(run())
    print("\nВсё сошлось" if ok else "\nЕсть расхождения")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
