"""Low-bandwidth HLS viewing; only Twitch inventory confirms earned minutes."""
from __future__ import annotations

import asyncio
import logging
from time import monotonic
from urllib.parse import urljoin, urlsplit

import httpx

from . import constants as c
from .api import Twitch, TwitchError, LoginRequired

log = logging.getLogger("vahta")
POLL_SECONDS = 20.0


def playlist_urls(body: str, base: str, *, master: bool = False) -> list[str]:
    """Read tagged HLS entries, never error bodies or arbitrary URI lines."""
    if not body.lstrip().startswith("#EXTM3U"):
        return []
    tag = "#EXT-X-STREAM-INF:" if master else "#EXTINF:"
    pending = False
    result = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if line.startswith(tag):
                pending = True
            continue
        if not pending:
            return []
        try:
            url = urljoin(base, line)
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            if (parsed.scheme != "https" or parsed.username or parsed.password
                    or not (host.endswith(".ttvnw.net") or host.endswith(".twitch.tv"))):
                return []
        except ValueError:
            return []
        result.append(url)
        pending = False
    return [] if pending else result


class Playback:
    def __init__(self, tw: Twitch, login: str):
        self.tw = tw
        self.login = login
        self.url: str | None = None
        self.broadcast: str | None = None

    async def _playlist(self) -> str | None:
        data = await self.tw.gql(c.playback_token(self.login))
        token = data.get("streamPlaybackAccessToken") or {}
        if not all(isinstance(token.get(k), str) and token[k] for k in ("value", "signature")):
            return None
        base = f"https://usher.ttvnw.net/api/channel/hls/{self.login}.m3u8"
        response = await self.tw.http.get(base, params={
            "sig": token["signature"], "token": token["value"],
        }, timeout=10)
        if response.status_code != 200:
            return None
        urls = playlist_urls(response.text, base, master=True)
        return urls[-1] if urls else None

    async def tick(self, broadcast: str) -> bool:
        if broadcast != self.broadcast:
            self.broadcast, self.url = broadcast, None
        try:
            async with asyncio.timeout(18):
                if self.url is None:
                    self.url = await self._playlist()
                if self.url is None:
                    return False
                response = await self.tw.http.get(
                    self.url, headers={"Connection": "close"}, timeout=8,
                )
                if response.status_code != 200:
                    self.url = None
                    return False
                segments = playlist_urls(response.text, self.url)
                if not segments:
                    self.url = None
                    return False
                # HEAD never downloads media. Bound fan-out and total duration.
                results = await asyncio.gather(*(
                    self.tw.http.head(url, headers={"Connection": "close"}, timeout=8)
                    for url in segments[-20:]
                ), return_exceptions=True)
                accepted = any(isinstance(r, httpx.Response) and r.status_code == 200
                               for r in results)
                if not accepted:
                    self.url = None
                return accepted
        except LoginRequired:
            raise
        except (httpx.HTTPError, TwitchError, TimeoutError):
            # Signed URLs contain playback credentials: never log exception text.
            self.url = None
            log.warning("HLS недоступен для %s; повторю с новым плейлистом", self.login)
            return False

    async def wait(self, seconds: float, broadcast: str) -> None:
        """Keep HLS alive between minute-spaced progress checks."""
        deadline = monotonic() + seconds
        while (remaining := deadline - monotonic()) > POLL_SECONDS:
            await asyncio.sleep(POLL_SECONDS)
            await self.tick(broadcast)
        await asyncio.sleep(max(0, deadline - monotonic()))
