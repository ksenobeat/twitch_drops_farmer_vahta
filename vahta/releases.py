"""Откуда «Вахта» узнаёт о новой версии и свежей базе игр: релизы на GitHub.

Своего сервера у проекта нет. Всё, что раньше приходило в ответ на суточный
пинг, теперь лежит в открытых релизах репозитория:

- **Последний релиз** (`/releases/latest`) — версия из тега (`v0.6.0` → `0.6.0`)
  и инсталлер `Vahta-Setup-<версия>.exe` среди его файлов.
- **База игр** — файл `games-ГГГГ-ММ-ДД.json.gz`. Ищется и в последнем релизе,
  и в отдельном релизе с тегом `games`: базу обновляют чаще, чем выходят версии,
  и перевыпускать ради неё приложение незачем. Берётся самая свежая по дате
  в имени.

Хэш берётся из поля `digest`, которое GitHub сам считает для каждого
загруженного файла. Это проверка целостности, а не подпись: от битой закачки
и подменённого зеркала спасает, от взломанного репозитория — нет. Настоящая
защита здесь — HTTPS и то, что адрес приходит с api.github.com.

Что отправляется: обычный GET без токена, без идентификатора установки и без
данных о человеке. GitHub видит IP и версию в User-Agent — как у любой
программы, которая проверяет обновления. Выключенное «Обновлять
автоматически» не спрашивает ничего, кроме как по кнопке «Проверить».

Репозиторий можно сменить переменной `VAHTA_UPDATE_REPO` (`владелец/имя`) —
для форков, которые раздают свои сборки.
"""

from __future__ import annotations

import logging
import os
import re

import httpx

from . import __version__

log = logging.getLogger("vahta")

REPO = os.environ.get("VAHTA_UPDATE_REPO", "ksenobeat/twitch_drops_farmer_vahta")
API = "https://api.github.com"
GAMES_TAG = "games"
TIMEOUT = 15

SETUP = re.compile(r"^Vahta-Setup-[\w.\-]+\.exe$", re.IGNORECASE)
GAMES = re.compile(r"^games-(\d{4}-\d{2}-\d{2})\.json\.gz$")


def _sha256(asset: dict) -> str:
    digest = str(asset.get("digest") or "")
    algo, _, value = digest.partition(":")
    if algo.lower() == "sha256" and re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return value.lower()
    return ""


def _games_asset(release: dict) -> tuple[str, str, str] | None:
    """(дата сборки, адрес, sha256) самой свежей базы игр в релизе."""
    best = None
    for asset in release.get("assets") or []:
        found = GAMES.match(str(asset.get("name") or ""))
        sha = _sha256(asset)
        url = str(asset.get("browser_download_url") or "")
        if found and sha and url and (best is None or found[1] > best[0]):
            best = (found[1], url, sha)
    return best


def apply(service, latest: dict | None, games_release: dict | None) -> None:
    """Разложить ответы GitHub по полям службы. Мусор в ответе — пустые поля."""
    latest = latest if isinstance(latest, dict) else {}
    tag = str(latest.get("tag_name") or "")
    version = tag[1:] if tag[:1] in "vV" else tag
    if latest.get("draft") or latest.get("prerelease"):
        version = ""
    service.latest_version = version
    service.release_page = str(latest.get("html_url") or "") if version else ""
    service.update_url = service.update_sha256 = ""
    for asset in (latest.get("assets") or []) if version else []:
        sha = _sha256(asset)
        url = str(asset.get("browser_download_url") or "")
        if SETUP.match(str(asset.get("name") or "")) and sha and url:
            service.update_url, service.update_sha256 = url, sha
            break

    candidates = [
        found for found in (
            _games_asset(latest),
            _games_asset(games_release if isinstance(games_release, dict) else {}),
        ) if found
    ]
    if candidates:
        built, url, sha = max(candidates)
        service.games_built, service.games_url, service.games_sha256 = built, url, sha
    else:
        service.games_built = service.games_url = service.games_sha256 = ""


async def _get(client: httpx.AsyncClient, path: str) -> dict | None:
    answer = await client.get(f"{API}/repos/{REPO}{path}")
    if answer.status_code == 404:
        return None  # релизов ещё нет или нет релиза с базой игр — не ошибка
    answer.raise_for_status()
    data = answer.json()
    return data if isinstance(data, dict) else None


async def check(service) -> bool:
    """Спросить GitHub. True — ответ получен (даже если свежего ничего нет).

    Сеть упала, GitHub ответил лимитом — поля остаются какими были: прежний
    ответ лучше пустого, а «Вахта» работает и без него.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"Vahta/{__version__}",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers,
                                     follow_redirects=True) as client:
            latest = await _get(client, "/releases/latest")
            games_release = await _get(client, f"/releases/tags/{GAMES_TAG}")
    except Exception as exc:  # проверка обновлений не должна ничего ломать
        log.warning("GitHub не ответил про релизы: %s: %s", type(exc).__name__, exc)
        return False
    apply(service, latest, games_release)
    if service.latest_version:
        log.info("Последний релиз на GitHub: %s", service.latest_version)
    return True
