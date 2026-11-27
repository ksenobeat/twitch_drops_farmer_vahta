"""Константы Twitch API.

Client ID, хэши persisted-запросов и формат watch-события подсмотрены в
DevilXD/TwitchDropsMiner — это приватный API, документации по нему нет.
Когда Twitch что-то ломает, чинить нужно в первую очередь здесь.
"""

from __future__ import annotations

# Клиент официального Android-приложения: только он поддерживает device code flow
CLIENT_ID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"
# Новый вход через TV device flow; старые сохранённые Android-токены остаются
# привязаны к прежнему CLIENT_ID.
DEVICE_CLIENT_ID = "ue6666qo983tsx6so1t0vnawi233wa"
DEVICE_CLIENT_URL = "https://android.tv.twitch.tv"
CLIENT_URL = "https://www.twitch.tv"
USER_AGENT = (
    "Dalvik/2.1.0 (Linux; U; Android 16; SM-S911B Build/TP1A.220624.014) "
    "tv.twitch.android.app/25.3.0/2503006"
)
# Страницу канала отдают по-человечески только браузеру
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)

GQL_URL = "https://gql.twitch.tv/gql"
DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"

# Twitch засчитывает не более одной минуты просмотра в минуту, шлём чуть чаще
WATCH_INTERVAL = 59.0


def playback_token(login: str) -> dict:
    return op(
        "PlaybackAccessToken",
        "ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9",
        isLive=True, isVod=False, login=login, platform="web", playerType="site", vodID="",
    )


def op(name: str, sha256: str, /, **variables) -> dict:
    """Собрать persisted-запрос в формате, который ждёт gql.twitch.tv.

    Первые два параметра позиционные: у некоторых запросов переменная
    называется 'name' и иначе перекрывала бы имя операции.
    """
    query: dict = {
        "operationName": name,
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": sha256}},
    }
    if variables:
        query["variables"] = variables
    return query


# Хэши привязаны к версии схемы Twitch. Если запрос начал отвечать
# PersistedQueryNotFound — хэш протух, надо обновить.
def campaigns() -> dict:
    """Все кампании, доступные аккаунту."""
    return op(
        "ViewerDropsDashboard",
        "5a4da2ab3d5b47c9f9ce864e727b2cb346af1e3ea8b897fe8f704a97ff017619",
        fetchRewardCampaigns=False,
    )


def campaign_details(campaign_id: str, user_id: str) -> dict:
    """Дропы, требования и список разрешённых каналов одной кампании."""
    return op(
        "DropCampaignDetails",
        "039277bf98f3130929262cc7c6efd9c141ca3749cb6dca442fc8ead9a53f77c1",
        dropID=campaign_id,
        # Twitch now resolves this legacy-named variable as a numeric user ID.
        # A login name returns user:null even with a valid authenticated session.
        channelLogin=str(user_id),
    )


def inventory() -> dict:
    """Кампании в процессе — здесь лежит реальный прогресс и claim id."""
    return op(
        "Inventory",
        "d86775d0ef16a63a33ad52e80eaff963b2d5b72fada7c991504a57496e1d8e4b",
        fetchRewardCampaigns=False,
    )


def current_drop(channel_id: str) -> dict:
    """Прогресс дропа на канале, который смотрим прямо сейчас."""
    return op(
        "DropCurrentSessionContext",
        "4d06b702d25d652afb9ef835d2a550031f1cf762b193523a92166f40ea3d142b",
        channelID=str(channel_id),
        channelLogin="",
    )


def claim_drop(drop_instance_id: str) -> dict:
    return op(
        "DropsPage_ClaimDropRewards",
        "a455deea71bdc9015b78eb49f4acfbce8baa7ccbedd28e549bb025bd0f751930",
        input={"dropInstanceID": drop_instance_id},
    )


def stream_info(login: str) -> dict:
    return op(
        "VideoPlayerStreamInfoOverlayChannel",
        "198492e0857f6aedead9665c81c5a06d67b25b58034649687124083ff288597d",
        channel=login,
    )


def available_drops(channel_id: str) -> dict:
    """Кампании, по которым капает на этом канале. Пусто — дропсы выключены."""
    return op(
        "DropsHighlightService_AvailableDrops",
        "782dad0f032942260171d2d80a654f88bdd0c5a9dddc392e9bc92218a0f42d20",
        channelID=str(channel_id),
    )


def slug_redirect(game_name: str) -> dict:
    """Название игры -> slug для каталога."""
    return op(
        "DirectoryGameRedirect",
        "1f0300090caceec51f33c5e20647aceff9017f740f223c3c532ba6fa59f6b6cc",
        name=game_name,
    )


def game_directory(slug: str, limit: int = 30) -> dict:
    """Живые каналы по игре."""
    return op(
        "DirectoryPage_Game",
        "86bcceb4e8b1a51256ff8eed8bd8aae4acacf80d737efe904f84f3aeadf8cafd",
        slug=slug,
        limit=limit,
        imageWidth=50,
        includeCostreaming=False,
        sortTypeIsRecency=False,
        options={
            "broadcasterLanguages": [],
            "freeformTags": None,
            "includeRestricted": ["SUB_ONLY_LIVE"],
            "recommendationsContext": {"platform": "web"},
            "sort": "RELEVANCE",
            # тот же фильтр, что у Twitch в адресе `?filter=drops`. Он про
            # **настройку канала**, а не про нужную нам кампанию: замер 31 июля
            # на сотне каналов дал Minecraft 100 -> 19, Just Chatting 100 -> 62,
            # а Zenless Zone Zero и Overwatch — 100 -> 100, потому что там
            # дропсы включены у всех и идёт просто другая кампания.
            # Значит это предварительный отсев, а не ответ: кампанию всё равно
            # проверяет `available_drops`. Но отсев даровой (та же переменная
            # в том же запросе), а канал с выключенными дропсами не даст дроп
            # никогда — на Minecraft это восемьдесят сэкономленных проверок
            "systemFilters": ["DROPS_ENABLED"],
            "tags": [],
            "requestID": "JIRA-VXP-2397",
        },
    )
