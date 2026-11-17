"""A polite MusicBrainz client. MusicBrainz allows ~1 request/second for anonymous clients and returns 503 when it
is busy (we saw this while testing), so every send goes through one lock + minimum spacing, and busy responses
back off (outside the lock) and retry. All widgets share the single instance so their combined rate still respects the limit."""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx

from . import __version__, config

BASE = "https://musicbrainz.org/ws/2/"
MIN_GAP = 1.1          # seconds between requests
ATTEMPTS = 6


def backoff(attempt: int, retry_after: str | None) -> float:
    """MusicBrainz sheds a share of everyone's traffic when it's loaded (503, `x-ratelimit-who: search-shed` or `global`,
    `Retry-After: 0`) — not aimed at us, so a quick retry usually gets through. Honour a real Retry-After, else 1.5 s doubling
    to 20 s with a little jitter: about a minute in all before giving up."""
    try:
        if retry_after and float(retry_after) > 0:
            return min(60.0, float(retry_after))
    except ValueError:
        pass
    return min(20.0, 1.5 * 2 ** attempt) + random.uniform(0, 0.5)


class MusicBrainzBusy(RuntimeError):
    pass


class Client:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(headers={"User-Agent": f"Citadel/{__version__} ({config.CONTACT})", "Accept": "application/json"}, timeout=30)
        return self._http

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        for attempt in range(ATTEMPTS):
            async with self._lock:                                       # the lock only paces the sends ...
                wait = self._last + MIN_GAP - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                resp = await self._client().get(BASE + path, params={**params, "fmt": "json"})
                self._last = time.monotonic()
            if resp.status_code in (429, 503):                           # ... so a back-off here doesn't hold everyone else up
                await asyncio.sleep(backoff(attempt, resp.headers.get("retry-after")))
                continue
            resp.raise_for_status()
            return resp.json()
        raise MusicBrainzBusy("MusicBrainz is busy — will try again next sync")

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None


client = Client()


async def resolve_artist_id(name: str) -> str | None:
    """Look an artist up by name. Only an exact (case-insensitive) name match with a high score is accepted —
    a wrong artist is worse than none."""
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    data = await client.get("artist", query=f'artist:"{escaped}"', limit=5)
    for a in data.get("artists", []):
        if a.get("name", "").casefold() == name.casefold() and int(a.get("score", 0)) >= 90:
            return a["id"]
    return None
