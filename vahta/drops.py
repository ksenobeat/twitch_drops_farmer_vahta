"""Кампании, дропы и их прогресс."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic

from . import constants as c
from .api import LoginRequired, TemporaryTwitchError, Twitch, TwitchError

log = logging.getLogger("vahta")


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Drop:
    id: str
    name: str
    required_minutes: int
    current_minutes: int = 0
    claimed: bool = False
    claim_id: str | None = None
    benefits: list[str] = field(default_factory=list)
    # id награды за дроп: по нему награда из инвентаря сходится с дропом, а
    # заодно — с игрой, потому что у самой награды `game` всегда пустой
    benefit_ids: list[str] = field(default_factory=list)
    # окно дропа. Нужно, чтобы отличить награду, полученную по нему, от такой же
    # награды за прошлую неделю: `totalCount` в инвентаре — за всё время
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    # награда пришла сама, без клейма (`distributionType: DIRECT_ENTITLEMENT`).
    # Считается забранным, но забрали его не мы
    granted: bool = False

    @property
    def done(self) -> bool:
        return self.current_minutes >= self.required_minutes

    @property
    def claimable(self) -> bool:
        return not self.claimed and self.claim_id is not None

    def __str__(self) -> str:
        mark = "~" if self.granted else "+" if self.claimed else " "
        return f"[{mark}] {self.name} — {self.current_minutes}/{self.required_minutes} мин"


@dataclass
class Campaign:
    id: str
    name: str
    game: str
    game_id: str
    starts_at: datetime | None
    ends_at: datetime | None
    account_linked: bool
    drops: list[Drop] = field(default_factory=list)
    allowed_channels: list[str] = field(default_factory=list)
    account_link_url: str = ""
    box_art: str = ""
    # slug каталога: он есть и в инвентаре, и в деталях кампании, а иначе его
    # приходится добывать отдельным запросом (`DirectoryGameRedirect`)
    game_slug: str = ""
    publisher: str = ""
    details_url: str = ""

    @property
    def active(self) -> bool:
        moment = now()
        return (
            (self.starts_at is None or self.starts_at <= moment)
            and (self.ends_at is None or moment < self.ends_at)
        )

    @property
    def remaining_minutes(self) -> int:
        """Сколько ещё минут смотреть, чтобы закрыть кампанию целиком.

        Прогресс внутри кампании общий: смотришь один поток, а дропы —
        это вехи на нём (60, 120, 180 минут). Поэтому работа определяется
        самой дальней вехой, а не суммой остатков.
        """
        pending = [d for d in self.drops if not d.claimed]
        if not pending:
            return 0
        return max(max(0, d.required_minutes - d.current_minutes) for d in pending)

    def __str__(self) -> str:
        # Twitch отдаёт время в UTC, показываем в местном
        ends = self.ends_at.astimezone().strftime("%d.%m %H:%M") if self.ends_at else "?"
        got = sum(d.claimed for d in self.drops)
        return f"{self.game} — {self.name} (до {ends}, {got}/{len(self.drops)})"


def _parse_drops(raw: list[dict] | None) -> list[Drop]:
    drops = []
    for d in raw or []:
        state = d.get("self") or {}
        awards = [e["benefit"] for e in (d.get("benefitEdges") or []) if e.get("benefit")]
        drops.append(
            Drop(
                id=d["id"],
                name=d["name"],
                required_minutes=d["requiredMinutesWatched"],
                current_minutes=state.get("currentMinutesWatched") or 0,
                claimed=bool(state.get("isClaimed")),
                claim_id=state.get("dropInstanceID"),
                benefits=[b["name"] for b in awards],
                benefit_ids=[b["id"] for b in awards if b.get("id")],
                starts_at=_ts(d.get("startAt")),
                ends_at=_ts(d.get("endAt")),
            )
        )
    return drops


def _parse_campaign(raw: dict) -> Campaign:
    game = raw.get("game") or {}
    allow = raw.get("allow") or {}
    return Campaign(
        id=raw["id"],
        name=raw["name"],
        game=game.get("displayName") or game.get("name") or "?",
        game_id=str(game.get("id") or ""),
        starts_at=_ts(raw.get("startAt")),
        ends_at=_ts(raw.get("endAt")),
        account_linked=bool((raw.get("self") or {}).get("isAccountConnected")),
        drops=_parse_drops(raw.get("timeBasedDrops")),
        allowed_channels=[ch["name"] for ch in (allow.get("channels") or [])],
        account_link_url=raw.get("accountLinkURL") or "",
        box_art=game.get("boxArtURL") or "",
        game_slug=game.get("slug") or "",
        publisher=(raw.get("owner") or {}).get("name") or "",
        details_url=raw.get("detailsURL") or "",
    )


async def list_campaigns(tw: Twitch) -> list[Campaign]:
    """Все кампании, которые Twitch показывает этому аккаунту (без деталей)."""
    data = await tw.gql(c.campaigns())
    user = data.get("currentUser")
    if not user:
        raise LoginRequired("Twitch не подтвердил аккаунт для каталога кампаний")
    raw = user.get("dropCampaigns")
    if raw is None:
        raise TwitchError("Twitch не отдаёт каталог кампаний для этого входа. Фарм не запущен; требуется совместимый способ авторизации.")
    return [_parse_campaign(item) for item in raw]


class CampaignUnavailable(LookupError):
    pass


async def campaign_details(tw: Twitch, campaign_id: str) -> Campaign:
    """Полная кампания: дропы, требования, разрешённые каналы."""
    if not tw.user_id:
        raise LoginRequired("Нужен ID аккаунта Twitch для деталей кампании")
    data = await tw.gql(c.campaign_details(campaign_id, tw.user_id))
    if data.get("user") is None:
        raise TwitchError("Twitch не вернул аккаунт для запроса деталей кампании")
    raw = (data.get("user") or {}).get("dropCampaign")
    if not raw:
        raise CampaignUnavailable(f"Кампания {campaign_id} не найдена")
    return _parse_campaign(raw)


async def active_campaign_details(tw: Twitch, item: Campaign) -> Campaign | None:
    try:
        full = await campaign_details(tw, item.id)
    except CampaignUnavailable:
        return None
    return full if full.game_id == item.game_id and full.active else None


@dataclass
class Reward:
    """Награда из инвентаря: всё, что аккаунт когда-либо получил.

    К игре не привязана — Twitch отдаёт `game: null` у всех до единой. Зато
    `id` совпадает с id benefit'а кампании, и через него награда сходится
    и с дропом, и с игрой, если кампанию мы когда-нибудь видели.
    """

    id: str
    name: str
    image: str = ""
    awarded_at: datetime | None = None
    # за всё время, а не за кампанию: «Twitch Shards ×12» — это все недели вместе
    count: int = 1
    linked: bool = False
    link_url: str = ""


@dataclass
class Inventory:
    """Обе ветки ответа `Inventory`, а не одна.

    Раньше читалась только первая, и из-за этого «Вахта» не знала про дропы,
    которые пришли сами: у них нечего клеймить, и по кампаниям их не видно.
    """

    campaigns: list[Campaign] = field(default_factory=list)
    rewards: list[Reward] = field(default_factory=list)


FARM_CATALOG_MAX_AGE = 30 * 60


async def farming_campaigns(
    tw: Twitch, inv: Inventory, wanted: Callable[[Campaign], bool],
) -> list[Campaign]:
    """Discover campaigns, keeping confirmed work going during catalog timeouts."""
    owner = tw.user_id
    try:
        catalog = await list_campaigns(tw)
    except TemporaryTwitchError:
        if not owner or tw.user_id != owner:
            raise
        snapshot = tw.farm_catalog
        known = {}
        if snapshot and snapshot[0] == owner and monotonic() - snapshot[1] < FARM_CATALOG_MAX_AGE:
            known = {c.id: c for c in snapshot[2] if c.active and wanted(c)}
        ongoing = [c for c in inv.campaigns if c.active and wanted(c) and c.remaining_minutes > 0]
        for campaign in ongoing:
            known.setdefault(campaign.id, campaign)
        if not known:
            raise
        log.warning("Тайм-аут каталога Twitch. Проверяю детали %d известных кампаний", len(known))
        return list(known.values())
    if owner and tw.user_id == owner:
        tw.farm_catalog = (owner, monotonic(), catalog)
    return [c for c in catalog if c.active and wanted(c)]


def _parse_reward(raw: dict) -> Reward:
    return Reward(
        id=raw["id"],
        name=raw.get("name") or "?",
        image=raw.get("imageURL") or "",
        awarded_at=_ts(raw.get("lastAwardedAt")),
        count=raw.get("totalCount") or 1,
        linked=bool(raw.get("isConnected")),
        link_url=raw.get("requiredAccountLink") or "",
    )


def _within(moment: datetime | None, since: datetime | None, until: datetime | None) -> bool:
    """Награда получена внутри окна. Без момента выдачи судить не о чем."""
    if moment is None:
        return False
    return (since is None or moment >= since) and (until is None or moment < until)


def mark_granted(campaigns: list[Campaign], rewards: list[Reward]) -> list[Drop]:
    """Пометить забранными дропы, награда за которые уже пришла сама.

    Дроп может прийти без клейма: у benefit'а бывает
    `distributionType: DIRECT_ENTITLEMENT`, и тогда забирать нечего — награда
    приходит сама. Пока «Вахта» этого не понимала, она принимала такой дроп
    за отказ Twitch: трижды писала «дроп готов, но не отдают» и откладывала
    кампанию на час. С THE FINALS так вышло восемь кругов подряд по 266/240.

    Сверяем по окну дропа, а не по одному id награды: `totalCount` в инвентаре
    за всё время, и та же награда за прошлую неделю пометила бы дроп забранным
    зря. У Ксена ровно такой случай — «Tactical Stockpile» выдан 18 июля, а окно
    нынешнего дропа началось 24-го.

    Вторая ловушка дороже первой. Одна и та же награда бывает на нескольких
    вехах одной кампании — у Stalzone `CUSTOM_ID_twitch_shards` стоит на 150,
    300, 510 и 600 минутах, — а `lastAwardedAt` один, за последнюю выдачу.
    Значит одна полученная награда закрыла бы разом все четыре вехи, включая
    неотсмотренные, и «Вахта» ушла бы с кампании, не добрав дропы. Поэтому
    делённую награду принимаем в доказательство только там, где по нашим
    же минутам веха и так закрыта: сказать «не знаю» дешевле, чем потерять дроп.
    """
    awarded = {item.id: item.awarded_at for item in rewards if item.awarded_at}
    if not awarded:
        return []
    found = []
    for camp in campaigns:
        shared = {
            benefit
            for benefit in awarded
            if sum(benefit in drop.benefit_ids for drop in camp.drops) > 1
        }
        for drop in camp.drops:
            if drop.claimed or not drop.benefit_ids:
                continue
            if not drop.done and shared.intersection(drop.benefit_ids):
                continue
            since = drop.starts_at or camp.starts_at
            until = drop.ends_at or camp.ends_at
            if all(_within(awarded.get(b), since, until) for b in drop.benefit_ids):
                drop.claimed = drop.granted = True
                found.append(drop)
    return found


async def inventory(tw: Twitch) -> Inventory:
    """Инвентарь целиком: кампании в процессе и все полученные награды.

    Кампании — единственный источник реального прогресса, награды — единственный
    источник правды про дропы, которые пришли без клейма.
    """
    data = await tw.gql(c.inventory())
    # Без принятого токена Twitch не ругается, а отдаёт `currentUser: null`
    # с кодом 200. Считать это пустым инвентарём нельзя: такой ответ однажды
    # лёг в кэш на три минуты, и мастер первого запуска показал «игр нет» через
    # секунду после входа. Сбои Twitch сюда не попадают — они приходят
    # ошибками и пятисотыми, их ловит `gql`
    if data.get("currentUser") is None:
        raise LoginRequired("Twitch не признал аккаунт: инвентарь спрашивать некому")
    inv = data["currentUser"].get("inventory")
    if inv is None:
        raise TwitchError("Twitch не вернул инвентарь аккаунта")
    result = Inventory(
        campaigns=[_parse_campaign(item) for item in inv.get("dropCampaignsInProgress") or []],
        rewards=[
            _parse_reward(item) for item in inv.get("gameEventDrops") or [] if item.get("id")
        ],
    )
    mark_granted(result.campaigns, result.rewards)
    return result


FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)
# для сортировок по времени, которого нет: награда без `lastAwardedAt` должна
# оказаться в конце списка «свежие сверху», а не в начале
LONG_AGO = datetime.min.replace(tzinfo=timezone.utc)


async def merge_progress(
    tw: Twitch, campaigns: list[Campaign], inv: Inventory | None = None
) -> None:
    """Проставить кампаниям реальный прогресс.

    DropCampaignDetails отдаёт требования, но не то, сколько уже отсмотрено —
    это есть только в инвентаре. Он же закрывает вехи, награда за которые
    пришла сама: такой кампании в `dropCampaignsInProgress` может уже не быть,
    а награда в `gameEventDrops` остаётся навсегда.

    `inv` — уже прочитанный инвентарь. Обход вотчлиста читает его один раз
    на все игры: инвентарь один на аккаунт, а запросов было по одному на игру.
    """
    inv = inv or await inventory(tw)
    known: dict[str, Drop] = {
        drop.id: drop for camp in inv.campaigns for drop in camp.drops
    }
    for camp in campaigns:
        for drop in camp.drops:
            if (actual := known.get(drop.id)) is not None:
                drop.current_minutes = actual.current_minutes
                drop.claimed = actual.claimed
                drop.claim_id = actual.claim_id
                drop.granted = actual.granted
    # Про каждую такую веху в лог не пишем: `merge_progress` зовётся на каждую
    # игру каждого обхода, и девять «пришёл сам» превращались в тридцать шесть
    # строк за пять минут, в которых не видно ничего другого. Вслух об этом
    # говорит `_watch_channel` — один раз, когда веха закрылась при нас
    if granted := mark_granted(campaigns, inv.rewards):
        log.debug("Пришли сами, забирать нечего: %d", len(granted))


async def find_campaigns(tw: Twitch, game: str, store=None) -> list[Campaign]:
    """Все активные кампании по игре, с деталями, раньше кончающиеся — первыми.

    По одной игре Twitch спокойно крутит несколько кампаний сразу, и какая из них
    доступна — зависит от того, какой канал сейчас в эфире. Поэтому выбирать
    кампанию до выбора канала нельзя.
    """
    needle = game.casefold().strip()
    inv = await inventory(tw)
    matches = await farming_campaigns(tw, inv, lambda item: item.game.casefold() == needle)
    detailed = []
    for item in matches:
        full = await active_campaign_details(tw, item)
        if full is not None:
            detailed.append(full)
    if store:
        for camp in detailed:
            store.restore_progress(camp)
    await merge_progress(tw, detailed, inv)
    if store:
        for camp in detailed:
            store.restore_progress(camp)
            store.remember_progress(camp)
    detailed.sort(key=lambda x: x.ends_at or FAR_FUTURE)
    return detailed


async def claim(tw: Twitch, drop: Drop) -> bool:
    if not drop.claim_id:
        return False
    data = await tw.gql(c.claim_drop(drop.claim_id))
    ok = bool((data.get("claimDropRewards") or {}).get("status"))
    if ok:
        drop.claimed = True
        log.debug("Забрал дроп: %s", drop.name)
    return ok


async def claim_pending(tw: Twitch) -> list[tuple[Campaign, Drop]]:
    """Забрать всё готовое, что накопилось. Возвращает забранное."""
    taken: list[tuple[Campaign, Drop]] = []
    for camp in (await inventory(tw)).campaigns:
        for drop in camp.drops:
            if drop.claimable and await claim(tw, drop):
                taken.append((camp, drop))
    return taken
