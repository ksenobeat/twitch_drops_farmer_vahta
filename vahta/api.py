"""HTTP-клиент к Twitch: авторизация по device code flow и GQL-запросы."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections import deque
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

import httpx

from . import constants as c

if TYPE_CHECKING:
    from .drops import Campaign

log = logging.getLogger("vahta")

TRAFFIC_WINDOW = 600.0  # за десять минут средняя перестаёт скакать


class TwitchError(RuntimeError):
    pass


class TemporaryTwitchError(TwitchError):
    """A retryable failure reported by the upstream service."""


class LoginRequired(TwitchError):
    pass


class Meter:
    """Сколько байт «Вахта» прогоняет через сеть.

    Считаются тела запросов и ответов по `Content-Length`: заголовки, TLS и
    служебное сюда не попадают, поэтому число всегда чуть меньше того, что
    покажет системный монитор. Точность тут и не нужна — цифра отвечает на
    один вопрос, и он у всех первый: видео не качается, фарм стоит копейки.
    """

    def __init__(self, window: float = TRAFFIC_WINDOW) -> None:
        self.window = window
        self.total = 0
        self._recent: deque[tuple[float, int]] = deque()

    def add(self, size: int) -> None:
        if size <= 0:
            return
        self.total += size
        self._recent.append((monotonic(), size))
        self._forget()

    def _forget(self) -> None:
        edge = monotonic() - self.window
        while self._recent and self._recent[0][0] < edge:
            self._recent.popleft()

    def per_minute(self) -> float:
        """Килобайт в минуту за последние минуты.

        Знаменатель не меньше минуты нарочно: сразу после запуска окно длиной
        в пару секунд превратило бы сотню килобайт в мегабайты в минуту.
        """
        self._forget()
        if not self._recent:
            return 0.0
        span = max(60.0, monotonic() - self._recent[0][0])
        return sum(size for _, size in self._recent) / span * 60 / 1024


class Twitch:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._auth_path = data_dir / "auth.json"
        self._client: httpx.AsyncClient | None = None
        self.access_token: str | None = None
        self.client_id: str = c.CLIENT_ID
        self.user_id: str | None = None
        self.login_name: str | None = None
        self.device_id: str = secrets.token_hex(16)
        self.session_id: str = secrets.token_hex(16)
        self.meter = Meter()
        self.farm_catalog: tuple[str, float, list[Campaign]] | None = None

    # ---------- жизненный цикл ----------

    async def __aenter__(self) -> "Twitch":
        self._client = httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            # счётчик трафика вешаем сюда, а не на каждый вызов: мимо клиента
            # «Вахта» в сеть не ходит, и забыть пометить новый запрос нельзя
            event_hooks={"request": [self._measure_out], "response": [self._measure_in]},
        )
        self._load_auth()
        return self

    async def _measure_out(self, request: httpx.Request) -> None:
        self.meter.add(len(request.content))

    async def _measure_in(self, response: httpx.Response) -> None:
        """Тело ещё не прочитано — длину берём из заголовка.

        Читать его здесь нельзя: httpx отдаёт тело вызывающему потоком, и
        прочитанное из хука до него уже не дойдёт.
        """
        # HEAD advertises the media size but transfers no media body.
        if response.request.method == "HEAD":
            return
        length = response.headers.get("content-length") or ""
        self.meter.add(int(length) if length.isdigit() else 0)

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Twitch используется вне 'async with'")
        return self._client

    # ---------- хранение токена ----------

    def _load_auth(self) -> None:
        if not self._auth_path.exists():
            return
        data = json.loads(self._auth_path.read_text("utf8"))
        self.access_token = data.get("access_token")
        self.user_id = data.get("user_id")
        self.login_name = data.get("login")
        self.device_id = data.get("device_id", self.device_id)
        self.client_id = data.get("client_id", c.CLIENT_ID)

    def _save_auth(self) -> None:
        self._auth_path.write_text(
            json.dumps(
                {
                    "access_token": self.access_token,
                    "user_id": self.user_id,
                    "login": self.login_name,
                    "device_id": self.device_id,
                    "client_id": self.client_id,
                },
                indent=2,
            ),
            "utf8",
        )
        # токен = полный доступ к аккаунту, прячем от других пользователей машины
        try:
            self._auth_path.chmod(0o600)
        except OSError:
            pass

    def forget(self) -> None:
        self.farm_catalog = None
        self._auth_path.unlink(missing_ok=True)
        self.access_token = self.user_id = self.login_name = None

    # ---------- заголовки ----------

    def _headers(self, *, gql: bool = False) -> dict:
        headers = {
            "Accept": "*/*",
            "Accept-Language": "en-US",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Client-Id": self.client_id,
            "User-Agent": c.USER_AGENT,
            "X-Device-Id": self.device_id,
        }
        if gql:
            headers["Client-Session-Id"] = self.session_id
            headers["Origin"] = c.CLIENT_URL
            headers["Referer"] = c.CLIENT_URL
            if self.access_token:
                headers["Authorization"] = f"OAuth {self.access_token}"
        return headers

    # ---------- авторизация ----------

    async def ensure_login(self, interactive: bool = True) -> None:
        """Проверить сохранённый токен, при необходимости пройти device flow."""
        if self.access_token and await self._validate():
            return
        if not self.access_token:
            log.info("Сохранённого токена нет")
        else:
            log.warning("Сохранённый токен недействителен")
        if not interactive:
            raise LoginRequired("Нужна авторизация: запусти 'vahta login'")
        await self._device_login()
        await self.finish_login()

    async def _validate(self) -> bool:
        r = await self.http.get(
            c.VALIDATE_URL, headers={"Authorization": f"OAuth {self.access_token}"}
        )
        if r.status_code == 401:
            return False
        r.raise_for_status()
        data = r.json()
        if data.get("client_id") != self.client_id:
            log.warning("Токен выдан другому клиенту, нужна повторная авторизация")
            return False
        self.user_id = str(data["user_id"])
        self.login_name = data["login"]
        return True

    def _device_headers(self) -> dict:
        headers = self._headers()
        headers["Host"] = "id.twitch.tv"
        headers["Client-Id"] = c.DEVICE_CLIENT_ID
        headers["Origin"] = c.DEVICE_CLIENT_URL
        headers["Referer"] = c.DEVICE_CLIENT_URL
        headers["Accept"] = "application/json"
        return headers

    async def begin_device_login(self) -> dict:
        """Запросить код. Дальше пользователь вводит его на сайте Twitch."""
        r = await self.http.post(
            c.DEVICE_URL,
            headers=self._device_headers(),
            data={"client_id": c.DEVICE_CLIENT_ID, "scopes": ""},
        )
        if r.is_error:
            raise TwitchError(f"Twitch не выдал код входа (HTTP {r.status_code}). Попробуй ещё раз позже.")
        return r.json()

    async def poll_device_login(self, device: dict) -> bool:
        """Одна попытка забрать токен. False — пользователь ещё не подтвердил."""
        r = await self.http.post(
            c.TOKEN_URL,
            headers=self._device_headers(),
            data={
                "client_id": c.DEVICE_CLIENT_ID,
                "device_code": device["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        if r.status_code == 400:
            # authorization_pending — код ещё не ввели
            return False
        r.raise_for_status()
        self.access_token = r.json()["access_token"]
        self.client_id = c.DEVICE_CLIENT_ID
        return True

    async def finish_login(self) -> None:
        """Проверить полученный токен и сохранить его."""
        if not await self._validate():
            raise TwitchError("Авторизация прошла, но токен не проходит валидацию")
        self._save_auth()

    async def _device_login(self) -> None:
        device = await self.begin_device_login()
        print()
        print("  Открой:  ", device["verification_uri"])
        print("  Код:     ", device["user_code"])
        print()
        print("  Жду подтверждения...", flush=True)

        deadline = asyncio.get_running_loop().time() + device["expires_in"]
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(device["interval"])
            if await self.poll_device_login(device):
                return
        raise TwitchError("Код истёк, начни авторизацию заново")

    # ---------- GQL ----------

    # Twitch регулярно отдаёт кратковременные сбои; на них нужно повторить,
    # а не считать запрос провалившимся
    TRANSIENT = ("service timeout", "service error", "internal server error")

    async def gql(self, query: dict, attempts: int = 3) -> dict:
        delay = 2.0
        for attempt in range(attempts):
            try:
                return await self._gql_once(query)
            except TemporaryTwitchError as exc:
                if attempt == attempts - 1:
                    raise
                log.debug("Повтор %s: %s", query["operationName"], exc)
            except httpx.TransportError as exc:
                if attempt == attempts - 1:
                    raise TwitchError(f"Сеть недоступна: {exc}") from exc
            await asyncio.sleep(delay)
            delay *= 2
        raise TwitchError("недостижимо")  # pragma: no cover

    async def _gql_once(self, query: dict) -> dict:
        r = await self.http.post(c.GQL_URL, headers=self._headers(gql=True), json=query)
        if r.status_code == 401:
            raise LoginRequired("Twitch отклонил токен")
        if r.status_code >= 500:
            raise TemporaryTwitchError(f"GQL {query['operationName']}: internal server error")
        r.raise_for_status()
        data = r.json()
        if errors := data.get("errors"):
            messages = list(dict.fromkeys(e.get("message") or "?" for e in errors))
            transient = self.TRANSIENT
            if query["operationName"] in {"Inventory", "ViewerDropsDashboard"}:
                transient += ("context deadline exceeded",)
            retryable = all(any(term in msg.lower() for term in transient) for msg in messages)
            error = TemporaryTwitchError if retryable else TwitchError
            raise error(f"GQL {query['operationName']}: {'; '.join(messages)}")
        if data.get("data") is None:
            raise TwitchError(f"GQL {query['operationName']}: пустой ответ")
        return data["data"]
