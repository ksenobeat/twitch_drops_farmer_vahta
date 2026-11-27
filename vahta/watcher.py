"""Выбор канала и эмуляция просмотра.

Плейлист HLS опрашивается каждые 20 секунд, сегменты проверяются через HEAD.
Видео не скачивается. Минуты подтверждаются только ответами Twitch.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic

from . import constants as c
from . import drops as dr
from .api import Twitch, TwitchError
from .store import Store
from .playback import Playback

log = logging.getLogger("vahta")

# Чем кончился просмотр канала
CLAIMED, STALLED, OVER = "claimed", "stalled", "over"
# счётчик стоял: канал тот же, а минуты не капают — стоит попробовать другой
STALE = "stale"
# чем кончился заход по игре целиком
NO_CHANNEL, NOTHING = "no-channel", "nothing"
# ломоть времени вышел: пусть автопилот решит заново, кому теперь окно
SLICE = "slice"

# Сколько держать одну игру, прежде чем отдать решение автопилоту. Раньше
# обходчик не отдавал его вовсе, пока по игре был живой канал, — и в ночь
# на 30 июля Arena Breakout с дедлайном 18 августа продержала окно семь часов,
# пока сгорали кампании, кончавшиеся в тот же день.
#
# Двадцать минут, потому что платим мы не временем, а запросами: переключение
# теряет только недосчитанную минуту, что была в полёте (накопленное живёт
# на стороне Twitch), и теряет её лишь когда цель правда меняется. Если наверху
# очереди всё та же игра, мы просто продолжаем.
SLICE_MINUTES = 20

# Сколько раз подряд терпеть «время досмотрено, а дроп не отдали» и на сколько
# откладывать такую кампанию. Пара минут — это нормальная задержка инвентаря
# Twitch; дальше начинается то, что чинить нам нечем, и час ожидания дешевле,
# чем час просмотра впустую: слот тем временем достаётся другой кампании.
STALL_LIMIT = 3
STALL_HOLD_MINUTES = 60

# Сколько тиков подряд терпеть стоящий счётчик при живом и здоровом стриме.
# Twitch засчитывает минуту в минуту, но одиночные пропуски бывают, поэтому
# не один и не два. Ушедший стример, сменённая игра и перезапущенный эфир сюда
# не относятся — они видны сразу, потому что состояние стрима переспрашивается
# каждый круг; здесь остаётся только «стрим идёт, а Twitch не считает».
STALE_LIMIT = 5

# Сколько кругов ждать, пока Twitch переведёт дроп-сессию на наш канал.
# Сессия у аккаунта одна (см. `current_progress`), и после смены канала она
# ещё минуту-другую показывает прежний — это норма, а не беда.
#
# Ноль терпения обходился дорого: в ночь на 31 июля «Вахта» уходила с канала
# через секунду после захода, потому что спрашивала сессию сразу после первого
# пинга, когда открыться она ещё не успела. В логе это 239 заходов на каналы
# за двадцать часов и 193 сессии, не давшие ни одной засчитанной минуты.
ADOPT_LIMIT = 3


@dataclass
class Channel:
    id: str
    login: str
    display_name: str
    viewers: int = 0


@dataclass
class Stream:
    channel: Channel
    broadcast_id: str
    game: str
    game_id: str


async def game_slug(tw: Twitch, game: str) -> str:
    data = await tw.gql(c.slug_redirect(game))
    node = data.get("game")
    if not node:
        raise LookupError(f"Twitch не знает игру «{game}»")
    return node["slug"]


async def live_channels(tw: Twitch, slug: str, limit: int = 30) -> list[Channel]:
    """Живые каналы по игре, в порядке выдачи каталога."""
    data = await tw.gql(c.game_directory(slug, limit))
    edges = ((data.get("game") or {}).get("streams") or {}).get("edges") or []
    result = []
    for edge in edges:
        node = edge.get("node") or {}
        caster = node.get("broadcaster") or {}
        if not caster.get("login"):
            continue
        result.append(
            Channel(
                id=str(caster["id"]),
                login=caster["login"],
                display_name=caster.get("displayName") or caster["login"],
                viewers=node.get("viewersCount") or 0,
            )
        )
    return result


# Сколько каналов из списка кампании проверять поодиночке — и это запасной
# путь, а не основной: список бывает на 744 канала, и спрашивать про каждый
# «ты в эфире?» ради ответа «нет» — то самое, чего делать не надо.
MAX_ALLOWED_PROBES = 25

# Сколько живых каналов брать из каталога игры. Сотня, а не тридцать: этим же
# списком теперь ищутся каналы кампаний со списком, и чем он длиннее, тем реже
# приходится опрашивать логины поодиночке.
DIRECTORY_LIMIT = 100


async def channel_campaigns(tw: Twitch, channel: Channel) -> set[str] | None:
    """id кампаний, по которым на этом канале сейчас капает."""
    data = await tw.gql(c.available_drops(channel.id))
    node = data.get("channel")
    if not isinstance(node, dict) or "viewerDropCampaigns" not in node:
        raise TwitchError("Twitch не вернул проверку кампаний канала")
    campaigns = node["viewerDropCampaigns"]
    if campaigns is None:
        return None  # unavailable check, not an authoritative empty list
    return {item["id"] for item in campaigns if item.get("id")}


async def pick_channel(
    tw: Twitch, slug: str, campaigns: list[dr.Campaign], avoid: set[str] = frozenset()
) -> tuple[Channel, dr.Campaign] | None:
    """Канал + кампания, по которой на нём реально капает.

    Из доступного берём кампанию, которая кончается раньше — она рискует
    больше остальных. `avoid` — логины, на которых счёт только что стоял:
    выдача каталога стабильна, и без этого мы возвращались бы на тот же канал
    круг за кругом.
    """
    if not campaigns:
        return None
    by_id = {item.id: item for item in campaigns}
    game = campaigns[0].game
    skip = {login.casefold() for login in avoid}

    # Каталог спрашивается один раз и служит обоим случаям. Раньше кампании
    # со списком разбирались до него — по одному запросу на логин, — и когда
    # из списка не было в эфире никого, это стоило двадцати пяти запросов
    # ради ответа «нет». В ночном логе такое случалось 210 раз.
    live = await live_channels(tw, slug, DIRECTORY_LIMIT)
    listed = [ch for ch in live if ch.login.casefold() not in skip]

    # Канал из списка кампании, который прямо сейчас в эфире по этой игре.
    # Спрашивать про него `available_drops` незачем: список — это и есть
    # разрешение, а каталог уже сказал, что канал живой и в нужной категории.
    for camp in campaigns:
        if not camp.allowed_channels:
            continue
        allowed = {login.casefold() for login in camp.allowed_channels}
        for channel in listed:
            if channel.login.casefold() in allowed:
                return channel, camp

    # Кампании без списка: тут канал разрешён любой, но капает не на каждом —
    # по замеру 31 июля дропсы шли на 8–10 каналов из первых десяти выдачи.
    # Один запрос на кандидата дешевле трёх минут просмотра впустую, поэтому
    # проверка остаётся; обычно она же и последняя — первый кандидат подходит.
    open_ids = {item.id for item in campaigns if not item.allowed_channels}
    if open_ids:
        for channel in listed:
            available = await channel_campaigns(tw, channel)
            if available is None:
                # Directory is filtered by DROPS_ENABLED and this game's slug.
                # Only unrestricted campaigns qualify; _watch_channel verifies
                # the actual channel/drop session and abandons stalled streams.
                best = min(open_ids, key=lambda i: by_id[i].ends_at or dr.FAR_FUTURE)
                log.warning("Twitch не вернул кампании канала %s; проверяю прогресс через Drops-каталог", channel.login)
                return channel, by_id[best]
            hits = available & open_ids
            if hits:
                best = min(hits, key=lambda i: by_id[i].ends_at or dr.FAR_FUTURE)
                return channel, by_id[best]

    # Каталог показывает не всех: канал из списка кампании бывает мелким
    # и в выдачу не попадает. Тогда — и только тогда — спрашиваем поодиночке
    for camp in campaigns:
        for login in camp.allowed_channels[:MAX_ALLOWED_PROBES]:
            if login.casefold() in skip:
                continue
            stream = await stream_of(tw, login)
            if stream is None:
                continue
            available = await channel_campaigns(tw, stream.channel)
            if available is not None and camp.id in available:
                return stream.channel, camp

    if not open_ids:
        log.warning("Все кампании по «%s» ограничены каналами, никто не в эфире", game)
    else:
        log.warning(
            "Живые каналы по «%s» есть, но ни на одном не идёт нужная кампания", game
        )
    return None


async def stream_of(tw: Twitch, login: str) -> Stream | None:
    data = await tw.gql(c.stream_info(login))
    user = data.get("user") or {}
    live = user.get("stream")
    if not live:
        return None
    settings = user.get("broadcastSettings") or {}
    game = settings.get("game") or {}
    return Stream(
        channel=Channel(
            id=str(user["id"]),
            login=login,
            display_name=user.get("displayName") or login,
            viewers=live.get("viewersCount") or 0,
        ),
        broadcast_id=str(live["id"]),
        game=game.get("displayName") or game.get("name") or "",
        game_id=str(game.get("id") or ""),
    )


async def current_progress(tw: Twitch, channel_id: str) -> tuple[str, int, int, str] | None:
    """(id дропа, текущие минуты, требуется минут, **чей это канал**).

    Twitch отдаёт объект сессии даже когда ничего не засчитывает — тогда он
    пустой (dropID == ""). Это и означает «прогресс не идёт».

    **Сессия у аккаунта одна на все каналы, и `channelID` в запросе на ответ
    не влияет.** Проверено 31 июля 2026 на живом аккаунте: три разных id
    (наш канал, чужой канал, заведомо несуществующий) дают один и тот же
    ответ — сессию того канала, который Twitch считает сейчас. Поэтому
    прочитанные минуты могут оказаться чужими, и последним элементом едет
    хозяин сессии: без него «Вахта» принимала чужой стоящий счётчик за свой.
    """
    data = await tw.gql(c.current_drop(channel_id))
    session = (data.get("currentUser") or {}).get("dropCurrentSession") or {}
    if not session.get("dropID"):
        return None
    return (
        session["dropID"],
        session.get("currentMinutesWatched") or 0,
        session.get("requiredMinutesWatched") or 0,
        str((session.get("channel") or {}).get("id") or ""),
    )


async def _watch_channel(
    tw: Twitch,
    channel: Channel,
    campaign: dr.Campaign,
    store: Store | None = None,
    on_tick=None,
    on_claim=None,
    until: float | None = None,
) -> str:
    """Держать канал, пока он жив и по нему идёт прогресс.

    `until` — момент (по `monotonic`), после которого ломоть кончился и решать,
    кому окно, должен снова автопилот.

    Возвращает, чем кончилось: `CLAIMED` — дроп забран, `STALLED` — время
    досмотрено, но Twitch дроп не отдал, `STALE` — счётчик стоял или нас тут
    не считают, `SLICE` — вышел ломоть времени, `OVER` — пора пересмотреть
    выбор по другой причине (стрим упал, сменил игру).

    **Минуты принимаются, только если дроп-сессия про наш канал.** Сессия
    у аккаунта одна на все каналы (`current_progress`), поэтому на канале,
    который нас не считает, виден чужой счётчик — стоящий, потому что того
    канала мы не смотрим. Ночь на 31 июля прошла ровно так: 239 заходов,
    193 сессии без единой засчитанной минуты и 212 минут за двадцать часов.
    """
    stream = await stream_of(tw, channel.login)
    if stream is None:
        return OVER
    playback = Playback(tw, channel.login)
    log.info(
        "Смотрю %s (%d зрителей) — %s",
        channel.display_name,
        channel.viewers,
        campaign.name,
    )
    session = store.session_start(campaign, channel.login) if store else 0
    earned_from: int | None = None
    seen: int | None = None  # что счётчик показывал в прошлый тик
    stale = 0
    adopt = 0  # кругов подряд, что сессия дропа не про нас
    наши_вехи = {d.id for d in campaign.drops}

    while True:
        if not await playback.tick(stream.broadcast_id):
            log.warning("HLS-просмотр не подтверждён, меняю канал")
            return OVER

        progress = await current_progress(tw, channel.id)
        # пустой хозяин — Twitch не назвал канал; считаем сессию своей, чтобы
        # не спорить с ним об умолчаниях
        свой_канал = progress is not None and progress[3] in ("", str(channel.id))
        # Второй вопрос того же рода: та ли это кампания. Канал совпасть может,
        # а веха в сессии оказаться от совсем другой игры — 31 июля на канале
        # Emongg (Overwatch) приехала веха SMITE 2 на 720 минут при 1405
        # набранных, и «Вахта» показала «1405 / 720», решила, что дроп готов,
        # не нашла его в инвентаре и отложила OWCS MSC Day 3 на час с причиной
        # «Twitch не отдаёт дроп». У самой кампании вехи 60/120/180/240/300,
        # и первая так и осталась незабранной.
        # Вех своей кампании мы можем не знать (обход не дошёл) — тогда
        # не спорим, иначе отказ был бы построен на нашем незнании
        своя_веха = not наши_вехи or (progress is not None and progress[0] in наши_вехи)
        mine = свой_канал and своя_веха

        if not mine:
            # Сессия дропа у аккаунта одна на все каналы, и сейчас она не наша:
            # либо Twitch ещё не успел её перевести (норма первые минуты),
            # либо на этом канале нас не считают вовсе. Чужие минуты читать
            # как свои нельзя — именно так «Вахта» ночью сидела на канале,
            # который ей ничего не давал, и видела чужой стоящий счётчик.
            adopt += 1
            if adopt >= ADOPT_LIMIT:
                if progress is None:
                    log.warning(
                        "Twitch не открыл дроп-сессию за %d мин — меняю канал (%s). "
                        "Так бывает, когда аккаунт смотрит Twitch где-то ещё или "
                        "когда кампания на канале только что кончилась",
                        adopt, channel.display_name,
                    )
                elif not свой_канал:
                    log.warning(
                        "Засчитывают не нам: дроп-сессия на другом канале уже %d мин, "
                        "а смотрим мы %s — меняю канал",
                        adopt, channel.display_name,
                    )
                else:
                    log.warning(
                        "Засчитывают не в ту кампанию: на %s уже %d мин идёт чужая "
                        "веха, а не «%s» — меняю канал",
                        channel.display_name, adopt, campaign.name,
                    )
                return STALE
        else:
            adopt = 0
            drop_id, current, required = progress[:3]
            if store:
                for drop in campaign.drops:
                    if drop.id == drop_id:
                        drop.current_minutes = max(drop.current_minutes, current)
                store.remember_progress(campaign)
            # Пишем в лог только когда счётчик двинулся. Одинаковая строчка раз
            # в минуту — это 700 строк за ночь, в которых не видно главного:
            # стоит счётчик или идёт. Простой теперь называет себя сам, ниже
            if seen is not None and current <= seen:
                stale += 1
            else:
                stale = 0
                log.info("Прогресс: %d/%d мин — %s", current, required, campaign.name)
            seen = current
            if on_tick:
                on_tick(campaign, channel, current, required)

            # в базу пишем минуты, которые Twitch реально засчитал, а не по часам:
            # первое чтение возвращает уже накопленный прогресс, он не наш
            if store:
                if earned_from is None:
                    earned_from = current
                elif current > earned_from:
                    store.session_tick(session, current - earned_from)
                    earned_from = current

            outcome = await _milestone(
                tw, campaign, drop_id, current, required, store, on_claim
            )
            if outcome is not None:
                return outcome

            if stale >= STALE_LIMIT:
                # Раз состояние стрима переспрашивается каждый круг, здесь уже
                # известно, что эфир идёт, игра та же и номер тот же, а сессия
                # дропа — наша. Значит остаётся единственная причина
                log.warning(
                    "Счёт стоит на %d/%d мин уже %d мин: стрим идёт, а Twitch "
                    "не засчитывает — меняю канал (%s)",
                    current, required, stale, channel.display_name,
                )
                return STALE

        if until is not None and monotonic() >= until:
            # Ломоть кончился. Уходим на границе минуты, а не посреди неё:
            # недосчитанная минута — единственное, что теряется на переключении
            return SLICE

        await playback.wait(c.WATCH_INTERVAL, stream.broadcast_id)

        # Пинг — это односторонняя аналитика: он ничего не возвращает и после
        # ухода стримера продолжает приниматься как ни в чём не бывало.
        # Единственный способ знать, что эфир жив, — спросить, и спрашивать
        # надо каждый круг, а не один раз при выборе канала. Один запрос
        # в минуту рядом с обходом каталога, который делает их сотни.
        fresh = await stream_of(tw, channel.login)
        if fresh is None:
            log.info("%s ушёл из эфира — меняю канал", channel.display_name)
            return OVER
        if fresh.game_id != stream.game_id:
            log.info(
                "%s сменил игру на «%s» — меняю канал",
                channel.display_name,
                fresh.game or "?",
            )
            return OVER
        if fresh.broadcast_id != stream.broadcast_id:
            # Перезапуск эфира — не повод уходить: канал тот же и кампания та
            # же, просто пинги с прежним номером больше не считаются. Раньше
            # это выглядело как вставший счётчик, и хороший канал терялся
            log.info(
                "%s перезапустил эфир — продолжаю с новым номером",
                channel.display_name,
            )
        stream = fresh
        # зрителей показывает окно, и до этого число стояло с момента выбора
        channel.viewers = fresh.channel.viewers


async def _milestone(
    tw: Twitch,
    campaign: dr.Campaign,
    drop_id: str,
    current: int,
    required: int,
    store: Store | None,
    on_claim,
) -> str | None:
    """Веха досмотрена — забрать дроп. None, если смотреть ещё есть что.

    Вынесено из `_watch_channel` не ради красоты: цикл там оброс проверкой
    «а нам ли засчитывают», и вместе с этим куском он перестал помещаться
    в голову целиком.
    """
    if current < required or required <= 0:
        return None

    # claim id появляется в инвентаре не мгновенно
    await asyncio.sleep(5)
    taken = await dr.claim_pending(tw)
    for camp, drop in taken:
        log.info("Забрал: %s (%s)", drop.name, camp.game)
        if store:
            store.record_claim(camp, drop)
        if on_claim:
            on_claim(camp, drop)
    if not taken:
        # Забирать бывает нечего по хорошей причине: награда пришла сама
        # (`DIRECT_ENTITLEMENT`). Спрашиваем инвентарь ещё раз — теперь
        # вместе с наградами, — и если веха закрыта ими, дроп получен,
        # а не потерян. Запрос здесь не дорог: он случается раз
        # на досмотренную веху, а не раз в минуту
        await dr.merge_progress(tw, [campaign])
        done = next((d for d in campaign.drops if d.id == drop_id), None)
        if done is not None and done.claimed:
            # Молчать о таком дропе нельзя: для человека он ничем
            # не отличается от забранного, кроме того, что мы его
            # не забирали, — а часы на него ушли наши
            log.info("«%s»: дроп пришёл сам, без клейма", done.name)
            if store:
                store.record_claim(campaign, done)
            if on_claim:
                on_claim(campaign, done)
            return CLAIMED
        log.warning("Дроп готов, но Twitch пока не отдаёт его — заберу позже")
        return STALLED
    return CLAIMED


async def watch(
    tw: Twitch,
    game: str,
    store: Store | None = None,
    on_tick=None,
    on_claim=None,
    minutes: float | None = SLICE_MINUTES,
    unlinked: bool = False,
) -> str:
    """Фармить игру ломтём в `minutes` минут, а потом отдать решение автопилоту.

    Один канал за раз. После каждого дропа выбор кампании и канала
    пересматривается — по одной игре кампаний обычно несколько, и доступна
    из них та, которую крутит живой стример.

    Ломоть нужен потому, что выбор «кому окно» принимает не обходчик, а план:
    очередь строится по дедлайну и вотчлисту, и правильное поведение получается
    само, если решение принимается регулярно. Без ломтя обходчик держал игру,
    пока по ней есть живой канал, — то есть чем популярнее игра, тем прочнее
    она блокировала остальные. `None` — держать без ограничения (для `cli`).

    `unlinked` — фармить и кампании без привязанного игрового аккаунта. По
    умолчанию нет, см. отказ ниже.
    """
    announced: set[str] = set()
    # сколько раз подряд кампания досмотрена, но дроп не выдан
    stalled: dict[str, int] = {}
    # каналы, на которых счёт стоял: пробуем других, прежде чем возвращаться
    avoid: set[str] = set()
    until = monotonic() + minutes * 60 if minutes else None
    slug = await game_slug(tw, game)

    while True:
        found = [
            item for item in await dr.find_campaigns(tw, game, store=store) if item.remaining_minutes > 0
        ]
        if not found:
            log.info("По «%s» фармить нечего", game)
            return NOTHING

        # Кампанию без привязанного игрового аккаунта по умолчанию не берём:
        # один раз мы видели, что Twitch по такой не открывает дроп-сессию
        # вовсе. Но это одно наблюдение против документации Twitch («claim
        # Drop Rewards before linking their account») и против 21 награды
        # в инвентаре Ксена без привязки — поэтому выбор оставлен человеку,
        # а не зашит. Отложенные пропускаем по причине попроще: время идёт,
        # а дроп не отдают.
        held = store.held() if store else {}
        campaigns = [
            item
            for item in found
            if (item.account_linked or unlinked) and item.id not in held
        ]
        for item in found:
            if item.id in announced:
                continue
            announced.add(item.id)
            if item.account_linked or unlinked:
                link = "" if item.account_linked else " (без привязки, как и просили)"
                log.info(
                    "Кампания: %s — осталось %d мин%s", item, item.remaining_minutes, link
                )
            else:
                log.warning(
                    "Пропускаю «%s»: игровой аккаунт не привязан. Предмет выдаётся "
                    "только привязавшимся, а идёт ли без привязки прогресс — "
                    "не проверено; включить такие кампании можно в настройках. "
                    "Привязать: %s",
                    item.name,
                    item.account_link_url or "https://www.twitch.tv/drops/campaigns",
                )

        if not campaigns:
            if any(item.id in held for item in found):
                log.info("По «%s» всё отложено — вернусь, когда выйдет срок", game)
            else:
                log.error("Все кампании по «%s» требуют привязки игрового аккаунта", game)
            return NOTHING

        if store:
            for item in campaigns:
                store.remember(item)

        picked = await pick_channel(tw, slug, campaigns, avoid)
        if picked is None and avoid:
            # Может быть, дело было не в канале: Twitch случается не считает
            # никому. Отказаться фармить вовсе — хуже, чем вернуться к тому,
            # у кого в прошлый раз не пошло.
            log.info("Все каналы по «%s» уже подводили — пробую снова с начала", game)
            avoid.clear()
            picked = await pick_channel(tw, slug, campaigns)
        if store:
            store.record_uptime(campaigns[0].game_id, picked is not None)
        if picked is None:
            # Раньше здесь спали пять минут и пробовали ту же игру снова. Это
            # и был простой на ровном месте: пока по «Assassin's Creed» никто
            # не стримил с дропсами, THE FINALS с живыми каналами ждала своей
            # очереди. Уходим сразу — окно отдаст автопилот следующей задаче,
            # а к этой вернётся, когда канал появится.
            log.info("По «%s» сейчас нет подходящего канала", game)
            return NO_CHANNEL

        started = monotonic()
        outcome = await _watch_channel(
            tw, *picked, store=store, on_tick=on_tick, on_claim=on_claim, until=until
        )
        if outcome == SLICE:
            return SLICE

        # «Время досмотрено, а дроп не отдали» — это норма минуту-другую, пока
        # обновляется инвентарь Twitch. Но если повторяется, смотреть дальше
        # бессмысленно: счётчик просмотра растёт, забирать нечего. Так и было
        # с THE FINALS — восемь кругов подряд по 266/240 мин.
        campaign = picked[1]
        if outcome == STALE:
            # Не «канал плохой», а «здесь не пошло». Круг спустя список
            # обнулится, и мы вернёмся, если больше идти некуда
            avoid.add(picked[0].login)
        if outcome == STALLED:
            stalled[campaign.id] = stalled.get(campaign.id, 0) + 1
            if stalled[campaign.id] >= STALL_LIMIT and store:
                until = store.hold(
                    campaign.id, STALL_HOLD_MINUTES, "Twitch не отдаёт дроп"
                )
                # Совет про привязку — только когда привязки правда нет.
                # Он стоял в этой строке всегда, и 31 июля она вышла такой:
                # «Twitch дроп не отдаёт… стоит проверить привязку» — при том,
                # что аккаунт привязан, а дроп не отдавался потому, что веха
                # в сессии была вообще от другой игры. Совет чинить исправное
                # уводит от настоящей причины, а не помогает
                log.error(
                    "«%s»: время досмотрено, но Twitch дроп не отдаёт (%d раза подряд). "
                    "Откладываю до %s, окно отдам другой кампании.%s",
                    campaign.name,
                    stalled[campaign.id],
                    until,
                    "" if campaign.account_linked else
                    " Стоит проверить привязку игрового аккаунта на сайте издателя",
                )
                return NOTHING
        else:
            stalled.pop(campaign.id, None)

        if monotonic() - started < 30:
            # канал отвалился сразу — не крутиться вхолостую
            await asyncio.sleep(60)
