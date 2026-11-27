"""Локальный веб-интерфейс — тонкий клиент над службой.

Отдаёт страницу и превращает состояние службы в JSON. Ничего своего не хранит
и ничем не владеет: фарм, планирование и данные живут в `service.py`, в своём
потоке. Поэтому веб можно уронить, перезапустить или не поднимать вовсе —
вахта продолжается.

Наружу не смотрит и авторизации не требует: слушает только localhost.

**Правило**: `store` и `tw` из этого модуля не трогаются напрямую. Всё, что их
касается, оформляется функцией от службы и уходит в `svc.call(...)` — она
выполнит её в потоке службы. Иначе sqlite3 упрётся в чужой поток, а Twitch —
в чужой цикл событий.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import httpx
from urllib.parse import urlsplit
from datetime import timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import __version__
from . import runtime
from . import autostart
from . import drops as dr
from . import planner
from . import releases
from . import scanner
from . import telegram
from . import update as upd
from .api import LoginRequired, TwitchError
from .paths import DATA_DIR, STATIC_DIR
from .service import Live, Service, await_login
from .settings import THEMES

log = logging.getLogger("vahta")

PAGE = STATIC_DIR / "index.html"

_service: Service | None = None
remote_access = None


def attach(service: Service) -> None:
    """Подключить веб к уже поднятой службе. Владеет ею тот, кто её поднял."""
    global _service
    _service = service


def svc() -> Service:
    if _service is None:
        raise RuntimeError("служба не подключена: вызови web.attach(service)")
    return _service


def _iso(moment) -> str:
    return moment.astimezone().isoformat(timespec="minutes")


api = FastAPI(title="Вахта")


@api.middleware("http")
async def local_requests(request: Request, call_next):
    """Отсекать чужие сайты и DNS rebinding; native IPC не шлёт Origin."""
    if runtime.SERVER_MODE:
        if remote_access is None:
            return JSONResponse({"error": "Серверный доступ не настроен"}, 503)
        return await remote_access.handle(request, call_next)
    if request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return JSONResponse({"error": "Недопустимый адрес сервера"}, 403)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        source = origin if origin is not None else referer
        expected = urlsplit(str(request.base_url))
        try:
            parsed = urlsplit(source) if source is not None else None
            same = parsed is None or (
                parsed.scheme == expected.scheme
                and parsed.hostname == expected.hostname
                and (parsed.port or 80) == (expected.port or 80)
            )
        except ValueError:
            same = False
        if not same or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"error": "Запрос с другого сайта запрещён"}, 403)
    return await call_next(request)


@api.get("/")
async def index() -> FileResponse:
    return FileResponse(PAGE)


@api.get("/healthz")
async def health():
    try:
        healthy = await asyncio.wait_for(svc().call(lambda s: not s.stopping), 5)
    except (RuntimeError, TimeoutError):
        healthy = False
    return JSONResponse({"ok": healthy}, 200 if healthy else 503)


def _shell_payload(s: Service) -> dict:
    """Данные оболочки: сайдбар, статусная строка, тема.

    Едут вместе с `/api/now`, а не своим запросом: опрос и так идёт раз
    в двадцать секунд, а два опроса вместо одного ничего не добавляют.
    """
    current = s.plan
    deadlines = [t.deadline for t in current.tasks] if current else []
    return {
        # секунды, а не готовая строка: часы в сайдбаре идут сами, между
        # опросами, иначе «03:40» стояло бы по двадцать секунд
        "shift": s.shift_seconds(),
        "traffic": round(s.tw.meter.per_minute(), 1),
        "deadline": _iso(min(deadlines)) if deadlines else "",
        "counts": {
            "sched": len(current.shortfalls) if current else 0,
            "games": len(s.store.watchlist()),
            # каталог показываем тот, что уже скачан: лезть в сеть ради
            # счётчика в меню — не то, за чем в меню смотрят
            "cat": sum(1 for item in s.catalog if item.active),
            # награды, которых человек ещё не видел
            "inv": len(s.store.unseen_rewards()),
        },
        "twitch": s.tw.login_name or "",
        "telegram": bool(s.bot_job and not s.bot_job.done()),
        "theme": s.settings.theme if s.settings else "system",
        # мастер первого запуска: показывать ли его. Едет тут же, потому что
        # решается это в первый же опрос, до того как человек куда-то нажал
        "onboarded": s.settings.onboarded if s.settings else True,
        "version": __version__,
        "server_mode": runtime.SERVER_MODE,
        # последний релиз на GitHub. Сравниваем здесь, а не на странице:
        # «новее» — это про версии, а не про строки, и знать об этом
        # достаточно одному месту
        "update": s.latest_version if not runtime.SERVER_MODE and _newer(s.latest_version) else "",
        # куда ведёт плашка: страница релиза, где лежат инсталлер и заметки
        "update_url": s.release_page if _newer(s.latest_version) else "",
        # обновление уже скачано и сверено — ждёт паузы в фарме. Нужно и плашке
        # (иначе человек жмёт на неё, хотя жать не надо), и «Настройкам»:
        # без этого «ставятся сами» ничем не отличается от «ничего не происходит»
        "update_ready": s.update_ready is not None,
    }


def _newer(version: str) -> bool:
    """Версия с сервера новее нашей? Мусор и равенство — это «нет»."""
    def parts(text: str) -> list[int]:
        return [int(bit) for bit in text.split(".") if bit.isdigit()]

    try:
        return bool(version) and parts(version) > parts(__version__)
    except (AttributeError, ValueError):
        return False


def _claimed_today(s: Service) -> list:
    """Забранное с местной полуночи. В базе время в UTC — переводим границу."""
    midnight = dr.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    since = midnight.astimezone(timezone.utc).isoformat(timespec="seconds")
    return [
        {
            "game": row["game_name"],
            "game_id": row["game_id"],
            "box_art": s.store.box_art(row["game_id"]),
            "drop": row["drop_name"],
            "at": row["claimed_at"],
        }
        for row in s.store.claims(since=since)
    ]


@api.get("/api/now")
async def now() -> JSONResponse:
    def go(s: Service) -> dict:
        return {
            **s.live.payload(),
            "paused_since": _iso(s.paused_since) if s.paused_since else "",
            "paused_until": _iso(s.paused_until) if s.paused_until else "",
            "today": _claimed_today(s),
            # когда построен нынешний план. Экран держит план в кэше и сам
            # перечитывает его только после действий человека, а служба
            # пересчитывает его и без него — после забранного дропа и по
            # старению. Без этой отметки «График» показывал вчерашний план:
            # Hearthstone доели в 23:53, а он висел в графике блоком 23:02→23:50,
            # потому что страницу открыли в 23:02 и с тех пор не трогали
            "plan_at": _iso(s.plan.starts_at) if s.plan else "",
            # лента уведомлений: окно показывает своим тостом то, чего ещё
            # не видело, и само помнит, где остановилось
            "events": s.events,
            "shell": _shell_payload(s),
        }

    return JSONResponse(await svc().call(go))


async def _plan_payload(s: Service) -> dict:
    return _plan_view(s, await _current_plan(s))


async def _current_plan(s: Service) -> planner.Plan:
    """Готовый план, а нет — построенный. Не вышло построить — пустой.

    Пятисотый отсюда ронял весь первый запуск, а не одно «Расписание»: опрос
    интерфейса идёт за планом сразу после `/api/now`, и на чистой установке —
    без входа в Twitch — обход бросал `LoginRequired`. Опрос падал на этом
    запросе целиком: шапка не рисовалась, мастер первого запуска не
    открывался, а внизу окна стояло «нет связи со службой», хотя служба
    работала. Человек получал пустой экран вместо приветствия.

    Сеть ловится наравне со входом: план строится обходом Twitch, а экран
    не должен зависеть от того, дошёл ли обход. Пустой план — честный ответ,
    планировать пока нечего; автопилот тем временем повторяет обход сам
    и покажет причину надписью в «Сейчас».
    """
    if s.plan is not None:
        return s.plan
    try:
        return await s.replan()
    except LoginRequired:
        return planner.build([])
    except Exception as exc:
        log.warning("План не построился: %s: %s", type(exc).__name__, exc)
        return planner.build([])


def _plan_view(s: Service, current: planner.Plan) -> dict:
    """План в виде, который понимает интерфейс. План передаётся явно: тем же
    видом отдаётся и предпросмотр порядка, который ещё не применён."""
    return {
        "starts_at": _iso(current.starts_at),
        "ends_at": _iso(current.ends_at),
        "feasible": current.feasible,
        "blocks": [
            {
                "game": b.task.game,
                "game_id": b.task.game_id,
                "box_art": s.store.box_art(b.task.game_id),
                "campaign": b.task.campaign,
                "start": _iso(b.start),
                "end": _iso(b.end),
                # эфирных минут ожидается — не длина отрезка: блоки кампаний
                # пересекаются, потому что эфир делится, а не резервируется
                "minutes": round(b.minutes),
                "share": round(b.share, 3),
            }
            for b in current.blocks
        ],
        "tasks": [
            {
                "game": t.game,
                "game_id": t.game_id,
                "box_art": s.store.box_art(t.game_id),
                "campaign": t.campaign,
                "minutes": t.minutes,
                "release": _iso(t.release),
                "deadline": _iso(t.deadline),
                "availability": round(t.availability, 3),
                "confident": t.confident,
            }
            for t in current.tasks
        ],
        "shortfalls": [
            {
                "game": sf.task.game,
                "campaign": sf.task.campaign,
                "deadline": _iso(sf.task.deadline),
                "missing": round(sf.missing_minutes),
                # «Не влезает» — это про последнюю веху, а не про всю кампанию:
                # дропы с ближними вехами заберутся как обычно
                "reachable": sf.reachable,
                "drops": sf.total,
                "competitors": sf.competitors,
                # Стороны вилки «что важнее»: числами, а не словами. Слова —
                # дело экрана и бота, у них разные поверхности и разная длина,
                # а числа должны быть одни
                "options": [
                    {
                        "drop_id": o.drop_campaign_id,
                        "drop_campaign": o.drop_campaign,
                        "drop_game": o.drop_game,
                        "gain": o.gain,
                        "keep": o.keep,
                        "total": o.total,
                        "rival_keeps": o.rival_keeps,
                        "rival_total": o.rival_total,
                    }
                    for o in planner.resolutions(sf, current)
                ],
            }
            for sf in current.shortfalls
        ],
        "unconfident": sorted({t.game for t in current.unconfident}),
        # то, что планировщик в работу не берёт, но человеку видеть нужно.
        # Когда фарм без привязки включён, такие кампании в плане — и вторым
        # списком, «вне плана», их показывать уже нельзя
        "unlinked": [] if s.settings and s.settings.farm_unlinked else _aside(s, linked=False),
        "done": _aside(s, linked=None, finished=True),
        "waiting": _waiting(s),
        "stuck": _stuck(s),
        # игры, по которым сейчас нет канала с нужной кампанией: их окно
        # пропущено, и «Расписание» не должно рисовать по ним «идёт»
        "quiet": sorted(s.quiet_games()),
        "refused": _refused(s),
    }


def _refused(s: Service) -> list:
    """Кампании, от которых отказались при разборе конфликта.

    Отказ — единственное решение, которое принимает человек, и до сих пор оно
    было невидимым и необратимым: кампания пропадала из плана совсем, а вернуть
    её было нечем. Показываем и даём вернуть.
    """
    skipped = s.store.skipped()
    if not skipped:
        return []
    moment = dr.now()
    out = []
    for status in s.statuses:
        for item in status.campaigns:
            if item.id not in skipped or not item.ends_at or item.ends_at <= moment:
                continue
            out.append(
                {
                    "id": item.id,
                    "game": status.name,
                    "game_id": status.game_id,
                    "box_art": s.store.box_art(status.game_id),
                    "campaign": item.name,
                    "minutes": item.remaining_minutes,
                    "deadline": _iso(item.ends_at),
                }
            )
    return out


def _stuck(s: Service) -> list:
    """Кампании, отложенные обходчиком: время идёт, а дроп Twitch не отдаёт.

    Их нет в плане — окно ушло другой кампании, — но и пропасть с экрана они
    не должны: с точки зрения человека это самое странное, что может случиться
    («смотрю уже час, а ничего не падает»), и объяснение должно быть на виду.
    """
    held = s.store.held()
    if not held:
        return []
    out = []
    for status in s.statuses:
        for item in status.campaigns:
            hold = held.get(item.id)
            if not hold:
                continue
            out.append(
                {
                    "game": status.name,
                    "game_id": status.game_id,
                    "box_art": s.store.box_art(status.game_id),
                    "campaign": item.name,
                    "minutes": item.remaining_minutes,
                    "deadline": _iso(item.ends_at) if item.ends_at else "",
                    "until": hold["until"],
                    "reason": hold["reason"],
                    # Ссылка на привязку — только когда привязки правда нет.
                    # Раньше она стояла всегда, и строка получалась такая:
                    # причина «Twitch не отдаёт дроп», а предлагаемое действие —
                    # «проверить привязку», хотя аккаунт привязан и в «Каталоге»
                    # так и написано. Человеку в этот момент и без того непонятно,
                    # что происходит; отправлять его чинить исправное — худшее,
                    # что можно предложить
                    "link": "" if item.account_linked else item.account_link_url,
                }
            )
    return out


def _waiting(s: Service) -> list:
    """Кампании, которые фармить сейчас нельзя вовсе, с причиной.

    Планировщик их не берёт: ноль доступности при сорока замерах — приговор,
    а не скорость, и планировать «по проценту в сутки» значит выдумывать работу.
    Но пропавшая из интерфейса кампания читается как «Вахта её не заметила»,
    поэтому она видна строкой с причиной — той самой, которая говорит, есть ли
    смысл ждать: «идёт только на своих каналах» ждать стоит, «не стримят
    с дропсами» — нет.
    """
    moment = dr.now()
    out = []
    for status in s.statuses:
        reason = planner.waiting_reason(status.game_id, s.store)
        if not reason:
            continue
        for item in status.campaigns:
            # кампания без привязки ждёт эфира только если её вообще фармят
            if not item.account_linked and not status.unlinked:
                continue
            if item.remaining_minutes <= 0:
                continue
            if not item.ends_at or item.ends_at <= moment:
                continue
            out.append(
                {
                    "game": status.name,
                    "game_id": status.game_id,
                    "box_art": s.store.box_art(status.game_id),
                    "campaign": item.name,
                    "minutes": item.remaining_minutes,
                    "deadline": _iso(item.ends_at),
                    "reason": reason,
                }
            )
    return out


def _aside(s: Service, *, linked: bool | None, finished: bool = False) -> list:
    """Кампании вне плана: без привязки аккаунта либо уже закрытые.

    Планировщик их отбрасывает — первым прогресс, скорее всего, не идёт вовсе,
    вторым идти некуда. Но пропавшая из интерфейса кампания читается как
    «не заметил её», поэтому отдаём отдельными списками.

    `linked=None` — привязка неважна: закрытая кампания закрыта независимо
    от того, привязан игровой аккаунт или нет.
    """
    moment = dr.now()
    out = []
    for status in s.statuses:
        for item in status.campaigns:
            if linked is not None and item.account_linked is not linked:
                continue
            if (item.remaining_minutes <= 0) is not finished:
                continue
            if not item.ends_at or item.ends_at <= moment:
                continue
            out.append(
                {
                    "game": status.name,
                    "game_id": status.game_id,
                    "box_art": s.store.box_art(status.game_id),
                    "campaign": item.name,
                    "minutes": item.remaining_minutes,
                    "deadline": _iso(item.ends_at),
                    "link": item.account_link_url,
                }
            )
    return out


@api.get("/api/plan")
async def plan() -> JSONResponse:
    return JSONResponse(await svc().call(_plan_payload))


@api.post("/api/replan")
async def do_replan() -> JSONResponse:
    async def go(s: Service) -> dict:
        # Та же оговорка, что и в `_current_plan`: нажать «Пересчитать» можно
        # и без входа в Twitch — экран этого не запрещает. Пятисотый в ответ
        # выглядел бы как сломанная кнопка, хотя сломан только вход
        try:
            await s.replan()
        except LoginRequired:
            pass
        except Exception as exc:
            log.warning("Пересчёт не удался: %s: %s", type(exc).__name__, exc)
        return _plan_view(s, s.plan or planner.build([]))

    return JSONResponse(await svc().call(go))


class PauseIn(BaseModel):
    """Насколько встать. По умолчанию час — столько же, сколько у `/pause` в боте."""

    minutes: int = 60


@api.post("/api/pause")
async def pause(body: PauseIn) -> JSONResponse:
    # сутки сверху: пауза — это «отойду», а не «выключил». Забытая на неделю
    # пауза выглядела бы как сломанный фарм
    span = timedelta(minutes=max(1, min(body.minutes, 24 * 60)))
    until = await svc().call(lambda s: s.pause(span))
    return JSONResponse({"until": _iso(until)})


@api.post("/api/resume")
async def resume() -> JSONResponse:
    await svc().call(lambda s: s.resume())
    return JSONResponse({"ok": True})


@api.post("/api/decide/{campaign_id}")
async def decide(campaign_id: str) -> JSONResponse:
    """Отказаться от кампании — так разрешается любой конфликт."""

    async def go(s: Service) -> dict:
        s.store.skip(campaign_id)
        await s.replan()
        return {"ok": True}

    return JSONResponse(await svc().call(go))


@api.delete("/api/decide/{campaign_id}")
async def undecide(campaign_id: str) -> JSONResponse:
    """Вернуть кампанию, от которой отказались. Отказ должен быть обратим."""

    def go(s: Service) -> dict:
        returned = s.store.unskip(campaign_id)
        if returned:
            # пересобираем план из последнего обхода, не трогая сеть: вернуть
            # кампанию — это про наше же решение, у Twitch ничего не менялось
            s.plan = planner.build(planner.make_tasks(s.statuses, s.store))
        return {"returned": returned}

    return JSONResponse(await svc().call(go))


@api.get("/api/watchlist")
async def watchlist() -> JSONResponse:
    def rows(s: Service) -> list:
        out = []
        for row in s.store.watchlist():
            share = s.store.availability(row["game_id"])
            out.append(
                {
                    "game_id": row["game_id"],
                    "name": row["game_name"],
                    "box_art": row["box_art"] or "",
                    "priority": row["priority"],
                    # нужен «сбросить порядок»: сброс возвращает порядок
                    # добавления, а не алфавит — алфавит тут ничего не значит
                    "added": row["added_at"],
                    "availability": None if share < 0 else round(share, 3),
                    "samples": s.store.sample_count(row["game_id"]),
                }
            )
        return out

    return JSONResponse(await svc().call(rows))


class GameIn(BaseModel):
    game: str


@api.post("/api/watchlist")
async def watchlist_add(body: GameIn) -> JSONResponse:
    async def go(s: Service) -> tuple[dict, int]:
        game_id, name, slug = await scanner.resolve_game(s.tw, body.game)
        if not game_id:
            return {"error": f"Twitch не знает игру «{body.game}»"}, 404
        added = s.store.watch_add(game_id, name, slug)
        if added:
            # автопилот мог спать над пустым планом ещё десять минут: добавили
            # игру — значит причина спать пропала
            s.wake()
        return {"added": added, "name": name}, 200

    payload, status = await svc().call(go)
    return JSONResponse(payload, status)


@api.delete("/api/watchlist/{game_id}")
async def watchlist_remove(game_id: str) -> JSONResponse:
    return JSONResponse(
        {"removed": await svc().call(lambda s: s.store.watch_remove(game_id))}
    )


@api.post("/api/watchlist/{game_id}/move/{position}")
async def watchlist_move(game_id: str, position: int) -> JSONResponse:
    await svc().call(lambda s: s.store.watch_move(game_id, position))
    return JSONResponse({"ok": True})


@api.get("/api/watchlist/preview")
async def watchlist_preview() -> JSONResponse:
    """Каким стал бы график, если пересчитать его с нынешним порядком.

    Сеть не трогаем и ничего не публикуем: берём последний обход вотчлиста и
    строим план заново. Порядок к этому моменту уже сохранён — панель «что
    меняет порядок» показывает цену кнопки «Применить», а не гипотезу.

    Отдаётся ровно тот же вид, что у `/api/plan`, — чтобы интерфейс считал
    запас по обоим планам одной и той же меркой.
    """

    def go(s: Service) -> dict:
        order = {row["game_id"]: row["priority"] for row in s.store.watchlist()}
        statuses = sorted(s.statuses, key=lambda st: order.get(st.game_id, 1 << 30))
        return _plan_view(s, planner.build(planner.make_tasks(statuses, s.store)))

    return JSONResponse(await svc().call(go))


@api.get("/api/games")
async def games_hints() -> JSONResponse:
    """Названия игр для подсказки при вводе — из своей базы, а не из сети.

    Складываем два источника. В базе лежит каталог Twitch, приехавший со
    сборкой (около четырёх тысяч категорий), плюс всё, что «Вахта» видела сама.
    В каталоге дропсов, наоборот, только сегодняшние кампании — зато они всегда
    свежие и там бывают игры, которых в сборке ещё нет.

    Список отдаётся целиком: подсказку по буквам браузер делает сам, а ручка
    «дай по префиксу» означала бы запрос на каждое нажатие. Сети это не требует
    вовсе — на то и заводилась база; каталог берём только тот, что уже скачан.
    """

    def go(s: Service) -> dict:
        names = set(s.store.game_names())
        names.update(item.game for item in s.catalog if item.active and item.game)
        return {"games": sorted(names, key=str.casefold)}

    return JSONResponse(await svc().call(go))


@api.get("/api/campaigns")
async def campaigns() -> JSONResponse:
    """Весь каталог кампаний Twitch — чтобы было видно, что можно подключить."""

    async def go(s: Service) -> dict:
        stale = dr.now() - s.catalog_at > timedelta(minutes=3) if s.catalog_at else True
        if stale:
            s.catalog = await dr.list_campaigns(s.tw)
            s.catalog_at = dr.now()

        watched = {row["game_id"] for row in s.store.watchlist()}
        items = [
            {
                "id": item.id,
                "name": item.name,
                "game": item.game,
                "game_id": item.game_id,
                "box_art": item.box_art,
                "publisher": item.publisher,
                "starts_at": _iso(item.starts_at) if item.starts_at else "",
                "ends_at": _iso(item.ends_at) if item.ends_at else "",
                "linked": item.account_linked,
                "link_url": item.account_link_url,
                "details_url": item.details_url,
                "watched": item.game_id in watched,
            }
            for item in s.catalog
            if item.active
        ]
        items.sort(key=lambda x: (not x["linked"], x["game"].casefold()))
        # время обхода — под сноску «последний обход 21:32»: она отвечает на
        # вопрос «данные свежие или это то, что было час назад»
        return {"campaigns": items, "total": len(items), "at": _iso(s.catalog_at)}

    return JSONResponse(await svc().call(go))


def _twitch_payload(s: Service) -> dict:
    pending = None
    if s.pending_login:
        pending = {
            "user_code": s.pending_login["user_code"],
            "verification_uri": s.pending_login["verification_uri"],
            # чтобы экран мог сказать «код живёт до 21:39», а не «десять минут
            # от какого-то момента»
            "expires_at": _iso(s.pending_login["expires_at"]),
        }
    return {
        "login": s.tw.login_name or "",
        "user_id": s.tw.user_id or "",
        "pending": pending,
        "error": s.login_error,
    }


@api.get("/api/twitch")
async def twitch_status() -> JSONResponse:
    return JSONResponse(await svc().call(_twitch_payload))


@api.post("/api/twitch/login")
async def twitch_login() -> JSONResponse:
    """Начать вход: Twitch выдаёт код, пользователь вводит его на сайте."""

    async def go(s: Service) -> dict:
        if s.pending_login:
            return _twitch_payload(s)
        try:
            device = await s.tw.begin_device_login()
        except (TwitchError, httpx.HTTPError) as exc:
            s.login_error = (str(exc) if isinstance(exc, TwitchError)
                             else "Не удалось связаться с Twitch. Попробуй ещё раз позже.")
            return _twitch_payload(s)
        # срок жизни кода считаем один раз здесь: `await_login` держит свой
        # дедлайн, но он живёт в задаче, а payload читается раньше неё
        device["expires_at"] = dr.now() + timedelta(seconds=device["expires_in"])
        s.pending_login = device
        s.login_error = ""
        s.login_job = asyncio.create_task(await_login(s, device))
        s.jobs.append(s.login_job)
        return _twitch_payload(s)

    return JSONResponse(await svc().call(go))


@api.post("/api/twitch/logout")
async def twitch_logout() -> JSONResponse:
    async def go(s: Service) -> dict:
        s.pending_login = None
        if s.login_job and not s.login_job.done():
            s.login_job.cancel()
            await asyncio.gather(s.login_job, return_exceptions=True)
        s.login_job = None
        s.tw.forget()
        if s.current and not s.current.done():
            s.current.cancel()
        s.live = Live(note="нужен вход в Twitch")
        return {"ok": True}

    return JSONResponse(await svc().call(go))


@api.get("/api/telegram")
async def telegram_status() -> JSONResponse:
    """Состояние бота. Токен наружу не отдаём — только его хвост."""

    def go(s: Service) -> dict:
        assert s.tg_config
        return {
            "configured": bool(s.tg_config.token),
            "hint": s.tg_config.hint,
            "owner": s.tg_config.chat_id,
            "username": s.bot.username if s.bot else "",
            "running": bool(s.bot_job and not s.bot_job.done()),
        }

    return JSONResponse(await svc().call(go))


class TokenIn(BaseModel):
    token: str


@api.post("/api/telegram")
async def telegram_set(body: TokenIn) -> JSONResponse:
    async def go(s: Service) -> tuple[dict, int]:
        assert s.tg_config
        me = await telegram.verify(body.token)
        if not me:
            return {"error": "Telegram не принял этот токен"}, 400
        s.tg_config.token = body.token.strip()
        s.tg_config.chat_id = None  # новый бот — заново назначаем владельца
        s.tg_config.save()
        await s.start_bot()
        return {"username": me.get("username", "")}, 200

    payload, status = await svc().call(go)
    return JSONResponse(payload, status)


@api.delete("/api/telegram")
async def telegram_forget() -> JSONResponse:
    async def go(s: Service) -> dict:
        assert s.tg_config
        await s.stop_bot()
        s.tg_config.forget()
        return {"ok": True}

    return JSONResponse(await svc().call(go))


@api.post("/api/update/check")
async def update_check() -> JSONResponse:
    """Спросить GitHub про свежую версию прямо сейчас — кнопкой из «Настроек».

    Обычно это делает часовой круг `update.loop`, но ждать его, когда
    обновление вышло только что, незачем. Заодно сразу качаем: ставить всё равно на границе
    ломтя, а качать в этот момент — значит его пропустить.
    """

    if runtime.SERVER_MODE:
        return JSONResponse({"message": "Сервер обновляется развёртыванием контейнера"}, 409)

    async def go(s: Service) -> dict:
        if not await releases.check(s):
            return {"ok": False, "message": "GitHub не ответил — "
                    "подробности в vahta.log"}
        if not _newer(s.latest_version):
            return {"ok": True, "message": f"Свежее нет, у тебя {__version__}"}
        if not s.update_url or not s.update_sha256:
            return {"ok": True, "message": f"Вышла {s.latest_version}, но в релизе "
                    "нет инсталлера с хэшем — обновиться придётся вручную"}
        if s.update_ready is None:
            s.update_ready = await upd.prepare(s)
        if s.update_ready is None:
            return {"ok": False, "message": f"Версия {s.latest_version} есть, "
                    "но скачать её не вышло — подробности в vahta.log"}
        action = ("встанет в ближайшую паузу в фарме"
                  if s.settings is None or s.settings.auto_update
                  else "можно поставить кнопкой «Поставить сейчас»")
        return {"ok": True, "message": f"Версия {s.latest_version} скачана "
                f"и проверена — {action}"}

    return JSONResponse(await svc().call(go))


@api.post("/api/update/install")
async def update_install() -> JSONResponse:
    """Поставить скачанное прямо сейчас, не дожидаясь паузы в фарме.

    Момент выбирает автопилот — но человек, нажавший кнопку, уже согласился
    потерять недосчитанную минуту, и спорить с ним незачем.
    """

    if runtime.SERVER_MODE:
        return JSONResponse({"message": "Сервер обновляется развёртыванием контейнера"}, 409)

    def go(s: Service) -> dict:
        setup = s.update_ready
        if setup is None or not setup.exists():
            return {"ok": False, "message": "Скачанного обновления нет"}
        if not upd.install(setup, s.update_sha256):
            return {"ok": False, "message": "Инсталлер не запустился — "
                    "поставь вручную, файл в data/updates"}
        # снимаем только после удачного запуска: не вышло — попробуем ещё
        s.update_ready = None
        return {"ok": True, "message": "Инсталлер запущен — «Вахта» закроется "
                "и вернётся уже обновлённой"}

    return JSONResponse(await svc().call(go))


class SettingsIn(BaseModel):
    """Меняем только то, что прислали: интерфейс шлёт один переключатель."""

    notifications: bool | None = None
    close_to_tray: bool | None = None
    window_on_autostart: bool | None = None
    autostart: bool | None = None
    onboarded: bool | None = None
    farm_unlinked: bool | None = None
    auto_update: bool | None = None
    theme: str | None = None


def _settings_payload(s: Service) -> dict:
    assert s.settings
    return {
        **s.settings.payload(),
        "server_mode": runtime.SERVER_MODE,
        # автозапуск живёт не в настройках, а в реестре: он и есть источник
        # правды, и переспросить его дешевле, чем держать вторую копию
        "autostart": autostart.is_enabled(),
        # без трея уведомления показывать нечем, а окном никто не владеет
        "available": s.notifier is not None,
        "windowed": s.open_ui is not None,
    }


@api.get("/api/settings")
async def settings_get() -> JSONResponse:
    return JSONResponse(await svc().call(_settings_payload))


@api.post("/api/settings")
async def settings_set(body: SettingsIn) -> JSONResponse:
    def go(s: Service) -> dict:
        assert s.settings
        for name, value in body.model_dump(exclude_none=True).items():
            if runtime.SERVER_MODE and name in {"autostart", "auto_update", "notifications",
                                               "close_to_tray", "window_on_autostart"}:
                continue
            if name == "theme" and value not in THEMES:
                continue  # чужое значение темы молча пропускаем
            if name == "autostart":
                autostart.set_enabled(value)  # это реестр, а не файл настроек
                continue
            setattr(s.settings, name, value)
        s.settings.save()
        return _settings_payload(s)

    return JSONResponse(await svc().call(go))


@api.post("/api/window/show")
async def window_show() -> JSONResponse:
    """Показать окно. Сюда стучится второй запуск, прежде чем уйти."""
    shown = await svc().call(lambda s: bool(s.open_ui) and (s.open_ui() or True))
    return JSONResponse({"shown": bool(shown)})


@api.post("/api/quit")
async def quit_app() -> JSONResponse:
    """Выйти совсем. Сюда стучится инсталлер перед обновлением.

    То же самое, что «Выход» в трее, — не отдельный путь выключения: второй
    однажды разошёлся бы с первым и, скажем, не сохранил бы размер окна.
    Обновление ставится поверх запущенной «Вахты», и попросить её выйти самой
    честнее, чем прибить процесс: она успевает закрыть базу и запомнить окно.
    """
    left = await svc().call(lambda s: bool(s.quit_ui) and (s.quit_ui() or True))
    return JSONResponse({"quitting": bool(left)})


@api.post("/api/settings/test")
async def settings_test() -> JSONResponse:
    """Показать пробное уведомление.

    Нужна не для красоты: тост могут глушить «Фокусировка внимания», полный
    экран или системные настройки, и без кнопки человек об этом не узнает.
    """
    shown = await svc().call(
        lambda s: s.push("Вахта на связи", "Уведомления работают. Так будут выглядеть дропы.")
    )
    return JSONResponse({"shown": shown})


INVENTORY_FRESH = timedelta(minutes=3)


async def _inventory(s: Service) -> dr.Inventory:
    """Инвентарь Twitch, не чаще раза в несколько минут, и сразу в базу.

    Кэш в памяти — как у каталога, чтобы не ходить в сеть на каждое нажатие.
    Запись в базу — потому что Twitch помнит инвентарь плохо и недолго, а без
    сети или с протухшим токеном не отдаёт вовсе: показывать в такой момент
    пустоту вместо того, что у человека есть, нельзя.
    """
    if s.inv is None or s.inv_at is None or dr.now() - s.inv_at > INVENTORY_FRESH:
        s.inv = await dr.inventory(s.tw)
        s.inv_at = dr.now()
        s.store.remember_rewards(s.inv.rewards)
    return s.inv


def _rewards(s: Service) -> list:
    """Награды своей копией, свежие сверху, с игрой там, где она известна.

    Читаются из базы, а не из ответа Twitch, и это нарочно: свою копию видно
    всегда, а живой инвентарь — только когда есть сеть и живой токен. Обновляет
    копию `_inventory`, дальше экрану всё равно, чем она обновлена.
    """
    by_benefit = s.store.games_by_benefit()
    out = []
    for row in s.store.rewards():
        game_id, game = by_benefit.get(row["id"], ("", ""))
        # вписанное руками сильнее подставленного: человек знает, из какой
        # кампании награда, а мы — только про те кампании, что видели сами
        by_hand = (row["game_note"] or "").strip()
        if by_hand:
            game, game_id = by_hand, ""
        out.append(
            {
                "id": row["id"],
                "name": row["name"],
                "image": row["image"],
                "at": row["awarded_at"] or "",
                "by_hand": bool(by_hand),
                # за всё время, а не за кампанию: «Twitch Shards ×12» — это все
                # недели вместе, и обещать обратное нельзя
                "count": row["count"],
                "linked": bool(row["linked"]),
                "link_url": row["link_url"],
                "game": game,
                "game_id": game_id,
                # «этого ты ещё не видел»: снимается, когда экран открыт
                "new": not row["seen"],
            }
        )
    return out


@api.get("/api/inventory")
async def inventory() -> JSONResponse:
    """Что у тебя есть (Twitch) и сколько это стоило (мы).

    Два источника в одном экране, потому что ни один не знает всего: в инвентаре
    Twitch нет ни часов, ни доли эфира, а в нашей базе — только то, что забрала
    сама «Вахта». У Ксена это ноль записей при 137 наградах, и до сих пор экран
    показывал именно ноль.
    """

    async def go(s: Service) -> tuple[dict, int]:
        # Освежить копию — попытка, а не условие: нет сети, нет токена, Twitch
        # лёг — экран всё равно покажет то, что у человека есть
        stale = ""
        try:
            await _inventory(s)
        except LoginRequired:
            stale = "нужен вход в Twitch"
        except Exception as exc:
            stale = f"{type(exc).__name__}: {exc}"
            log.warning("Инвентарь не обновился, показываю свою копию: %s", stale)
        rewards = _rewards(s)
        totals = s.store.totals()
        games = [
            {
                "game_id": row["id"],
                "name": row["name"],
                "box_art": row["box_art"] or "",
                "drops": row["drops"],
                "minutes": row["minutes"],
                "campaigns": row["campaigns"],
                "claims": [
                    {
                        "name": claim["drop_name"],
                        "campaign": claim["campaign_name"],
                        "at": claim["claimed_at"],
                        "minutes": claim["required_minutes"],
                    }
                    for claim in s.store.claims(row["id"])
                ],
            }
            for row in s.store.games()
        ]
        return {
            "totals": {
                "drops": totals["drops"],
                "games": totals["games"],
                "minutes": totals["minutes"],
                "minutes_week": totals["minutes_week"],
                "rewards": len(rewards),
                "new": sum(item["new"] for item in rewards),
            },
            "games": games,
            "rewards": rewards,
            "at": _iso(s.inv_at) if s.inv_at else "",
            # непустое — копия своя, а живой инвентарь спросить не удалось
            "stale": stale,
        }, 200

    payload, status = await svc().call(go)
    return JSONResponse(payload, status)


class RewardGameIn(BaseModel):
    game: str = ""


@api.post("/api/inventory/{reward_id}/game")
async def inventory_name_game(reward_id: str, body: RewardGameIn) -> JSONResponse:
    """Вписать игру награде руками. Пустая строка возвращает автоматику.

    Twitch про игру у наград молчит (`game: null` у всех до единой), а наша
    подстановка добивает только те, что из знакомых кампаний. Остальное знает
    человек — и его пометка живёт своим столбцом, поэтому обход её не сотрёт.
    """

    def go(s: Service) -> tuple[dict, int]:
        if not s.store.name_reward_game(reward_id, body.game):
            return {"ok": False, "message": "Такой награды нет"}, 404
        # Что теперь стоит в строке: стёртая пометка возвращает подстановку,
        # а какая она — знает только эта сторона. Иначе экран показывал бы
        # прочерк там, где игра на самом деле известна
        by_hand = body.game.strip()
        game = by_hand or s.store.games_by_benefit().get(reward_id, ("", ""))[1]
        return {"ok": True, "game": game, "by_hand": bool(by_hand)}, 200

    payload, status = await svc().call(go)
    return JSONResponse(payload, status)


@api.post("/api/inventory/export")
async def inventory_export() -> JSONResponse:
    """Инвентарь файлом — по строке на награду.

    Точка с запятой и BOM не от хорошей жизни: Excel в русской локали читает
    запятую как разделитель дробной части и складывает всю строку в одну
    ячейку, а без BOM — показывает кириллицу кракозябрами. Обе беды видны
    только у пользователя, поэтому решены здесь.

    Файл кладётся в `data/`: это единственная папка, куда программа заведомо
    может писать (при установке в Program Files она уезжает в профиль), и она
    же открывается соседней кнопкой.
    """

    async def go(s: Service) -> tuple[dict, int]:
        rows = _rewards(s)
        path = DATA_DIR / f"inventory-{dr.now():%Y-%m-%d}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            out = csv.writer(f, delimiter=";")
            out.writerow(["Получена", "Награда", "Игра", "Всего раз", "Аккаунт привязан"])
            for item in rows:
                out.writerow(
                    [
                        item["at"],
                        item["name"],
                        item["game"],
                        item["count"],
                        "да" if item["linked"] else "нет",
                    ]
                )
        return {"name": path.name, "rows": len(rows)}, 200

    payload, status = await svc().call(go)
    if runtime.SERVER_MODE and status == 200:
        return FileResponse(DATA_DIR / payload["name"], filename=payload["name"], media_type="text/csv")
    return JSONResponse(payload, status)


@api.post("/api/inventory/seen")
async def inventory_seen() -> JSONResponse:
    """Экран открыт — новое перестало быть новым.

    Отдельной ручкой, а не побочным действием чтения: перерисовка экрана
    случается и сама (нажали на игру), а «увидел» — событие человека.
    """
    return JSONResponse({"seen": await svc().call(lambda s: s.store.mark_rewards_seen())})


@api.get("/api/inventory/games")
async def inventory_games() -> JSONResponse:
    """Игры, по которым аккаунт уже что-то фармит. Для мастера первого запуска.

    `dropCampaignsInProgress[].game` отдаёт `{id, slug, name, boxArtURL}` —
    ровно то, что нужно вотчлисту, и без единого лишнего запроса. Человек,
    который уже что-то фармил, ждёт свои игры на месте, а не поиск по каталогу.

    Беда отдаётся полем, а не пятисотым: мастеру надо сказать человеку, что
    случилось — «нужен вход» и «Twitch не ответил» лечатся по-разному, а молчание
    выглядит как «игр нет», хотя их восемь.
    """

    async def go(s: Service) -> dict:
        try:
            inv = await _inventory(s)
        except LoginRequired:
            return {"games": [], "needs_login": True}
        except Exception as exc:
            log.warning("Игры из инвентаря не пришли: %s: %s", type(exc).__name__, exc)
            return {"games": [], "error": f"{type(exc).__name__}: {exc}"}
        watched = {row["game_id"] for row in s.store.watchlist()}
        games: dict[str, dict] = {}
        for camp in inv.campaigns:
            if not camp.game_id:
                continue
            item = games.setdefault(
                camp.game_id,
                {
                    "game_id": camp.game_id,
                    "name": camp.game,
                    "slug": camp.game_slug,
                    "box_art": camp.box_art,
                    "campaigns": 0,
                    "watched": camp.game_id in watched,
                },
            )
            item["campaigns"] += 1
        return {"games": sorted(games.values(), key=lambda g: g["name"].casefold())}

    return JSONResponse(await svc().call(go))


class GamesIn(BaseModel):
    game_ids: list[str]


@api.post("/api/watchlist/inventory")
async def watchlist_add_inventory(body: GamesIn) -> JSONResponse:
    """Добавить в вотчлист игры из инвентаря — те, что человек отметил.

    Молча не добавляем ни одной: вотчлист — это его выбор, а не наша догадка.
    Поэтому ручка принимает список, а не «возьми всё, что нашлось».
    """

    async def go(s: Service) -> tuple[dict, int]:
        try:
            inv = await _inventory(s)
        except LoginRequired:
            return {"added": 0, "needs_login": True}, 200
        except Exception as exc:
            log.warning("Игры из инвентаря не добавились: %s", exc)
            return {"added": 0, "error": f"{type(exc).__name__}: {exc}"}, 200
        known = {
            camp.game_id: camp for camp in inv.campaigns if camp.game_id
        }
        wanted = [known[gid] for gid in body.game_ids if gid in known]
        added = 0
        for camp in wanted:
            # slug инвентарь отдаёт всегда, но без него вотчлист бесполезен:
            # именно по нему обход ищет живые каналы. Пустой — спросим Twitch
            slug = camp.game_slug
            if not slug:
                _, _, slug = await scanner.resolve_game(s.tw, camp.game)
            if s.store.watch_add(camp.game_id, camp.game, slug):
                added += 1
        if added:
            s.wake()  # см. `/api/watchlist`: спать больше не над чем
        return {"added": added}, 200

    payload, status = await svc().call(go)
    return JSONResponse(payload, status)


@api.post("/api/data/open")
async def data_open() -> JSONResponse:
    """Открыть папку с данными — то же, что пункт в трее, но из окна."""
    if runtime.SERVER_MODE:
        return JSONResponse({"error": "Папка данных находится на сервере"}, 409)
    os.startfile(DATA_DIR)  # noqa: S606 — своя же папка с данными
    return JSONResponse({"ok": True})


def serve(host: str = "127.0.0.1", port: int = 8420) -> None:
    """Поднять только веб. Служба к этому моменту уже должна быть подключена."""
    import uvicorn

    uvicorn.run(api, host=host, port=port, log_level="warning")
