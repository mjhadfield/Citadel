"""The one connection to the goxlr-utility daemon on this PC, shared by the Audio page (app/audio.py) and the header's
volume buttons (app/volume.py). Plan: ~/Projects/HomeLab/audio.

The daemon (goxlr-utility, http://localhost:14564) only listens on this PC, and the hub runs here too, so the hub talks
to it and browsers talk to the hub: that's what lets the Audio page work from a phone without opening the daemon to the
network. The utility's own web page is left exactly as it is, as the fallback.

- **Status over a websocket.** One socket is held while the hub runs: it asks `GetStatus`, then the daemon pushes a
  JSON patch on every change (a slider here, a physical fader, the utility's own page). Rather than apply patches, a
  patch just means "ask again": at most every `REFRESH_S`, so a fader sweep coalesces. The latest status is kept here;
  when it changes, SSE `audio` tells the Audio page to re-read (throttled the same way), and `volume` too if Line out or
  Line in moved, so the header buttons follow the hardware.
- **Commands over HTTP** (`POST /api/command`), one at a time, as `goxlr-client --use-http` does. When the socket is
  down, a command is followed by an HTTP status read so pages still update.
- **Reconnects** with backoff (1 s doubling to 30 s). While it's down, `status()` falls back to one HTTP read.
- Off entirely when `HUB_GOXLR_URL` is unset (tests, scratch copies, `HUB_NO_FETCH`), so nothing here can touch the
  real speakers; a UI check points it at `tools/fake_goxlr.py`.

Command names and argument shapes were checked against the goxlr-utility v1.2.4 source (table in
HomeLab/audio/README.md).
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any, Callable

import httpx

from . import notify

log = logging.getLogger(__name__)

REFRESH_S = 0.2                 # at most five status reads (and SSE nudges) a second while things are moving
BACKOFF_MIN_S, BACKOFF_MAX_S = 1.0, 30.0
REPLY_TIMEOUT_S = 3.0
VOLUME_CHANNELS = ("LineOut", "LineIn")   # the header's buttons (app/volume.py)


class Unavailable(Exception):
    """The GoXLR daemon didn't answer, refused a command, or has no GoXLR."""


def to_raw(pct: float) -> int:
    """0–100 % → the daemon's 0–255."""
    return max(0, min(255, round(pct * 255 / 100)))


def to_pct(raw: int) -> int:
    return max(0, min(100, round(raw * 100 / 255)))


class GoXLR:
    def __init__(self, url: str | None, transport: httpx.AsyncBaseTransport | None = None,
                 connect: Callable[..., Any] | None = None):
        self.url = (url or "").rstrip("/")
        self._transport = transport
        self._connect = connect                   # tests pass a fake websocket factory
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()               # commands go one at a time, in the order they came
        self._task: asyncio.Task | None = None
        self._ws: Any = None
        self._ids = itertools.count(1)
        self._waiting: dict[int, asyncio.Future] = {}
        self._dirty = asyncio.Event()
        self._last: dict[str, Any] | None = None  # the daemon's whole status, as it last sent it
        self.live = False                         # the websocket is up and has sent a status

    @property
    def configured(self) -> bool:
        return bool(self.url)

    # ---- lifecycle ----------------------------------------------------------------------------------

    async def start(self) -> None:
        if self.configured and self._task is None:
            self._task = asyncio.create_task(self._run(), name="goxlr-socket")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._client:
            await self._client.aclose()
            self._client = None

    # ---- what the rest of Citadel uses --------------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        """The daemon's whole status: the socket's latest, or one HTTP read when the socket is down."""
        if self.live and self._last is not None:
            return self._last
        return await self._http_status()

    async def fresh_mixer(self) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """As `mixer()`, but read now over HTTP rather than from the socket's latest (which can trail by a moment)."""
        st = await self._http_status()
        mixers = st.get("mixers") or {}
        if not mixers:
            raise Unavailable("no GoXLR connected")
        serial, mixer = next(iter(mixers.items()))
        return serial, mixer, st

    async def mixer(self) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """(serial, that GoXLR's status, the whole status) for the first GoXLR. Unavailable if none."""
        st = await self.status()
        mixers = st.get("mixers") or {}
        if not mixers:
            raise Unavailable("no GoXLR connected")
        serial, mixer = next(iter(mixers.items()))
        return serial, mixer, st

    async def command(self, serial: str, cmd: dict[str, Any]) -> None:
        """Send one command ({Name: args}). Unavailable if the daemon is down or refuses it."""
        if not self.configured:
            raise Unavailable("no GoXLR daemon configured")
        async with self._lock:
            try:
                r = await self._http().post("/api/command", json={"Command": [serial, cmd]})
                r.raise_for_status()
                answer = r.json()
            except (httpx.HTTPError, ValueError) as e:
                raise Unavailable(f"GoXLR daemon: {e}") from e
        if answer != "Ok":
            raise Unavailable(f"GoXLR daemon refused {next(iter(cmd))}: {answer}")
        if not self.live:                         # no socket to tell us: read the result back so pages update
            try:
                self._changed(await self._http_status())
            except Unavailable:
                pass

    async def mic_level(self, serial: str) -> float:
        """The microphone's live level (the daemon's own scale), asked over the socket."""
        if not self.live:
            raise Unavailable("GoXLR daemon not connected")
        answer = await self._ask({"GetMicLevel": serial})
        if not isinstance(answer, dict) or "MicLevel" not in answer:
            raise Unavailable(f"GoXLR daemon: {answer}")
        return float(answer["MicLevel"])

    # ---- HTTP ---------------------------------------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.url, timeout=httpx.Timeout(3.0, connect=1.0), transport=self._transport)
        return self._client

    async def _http_status(self) -> dict[str, Any]:
        if not self.configured:
            raise Unavailable("no GoXLR daemon configured")
        try:
            r = await self._http().get("/api/get-devices")
            r.raise_for_status()
            st = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise Unavailable(f"GoXLR daemon: {e}") from e
        if not isinstance(st, dict):
            raise Unavailable("GoXLR daemon: unexpected status")
        return st

    # ---- the websocket ------------------------------------------------------------------------------

    def _ws_url(self) -> str:
        return ("ws" + self.url[4:] if self.url.startswith("http") else self.url) + "/api/websocket"

    async def _open(self) -> Any:
        if self._connect:
            return await self._connect(self._ws_url())
        from websockets.asyncio.client import connect
        return await connect(self._ws_url(), max_size=8 * 1024 * 1024, open_timeout=3)

    async def _run(self) -> None:
        delay = BACKOFF_MIN_S
        while True:
            try:
                self._ws = await self._open()
                reader = asyncio.create_task(self._read(self._ws), name="goxlr-read")
                try:
                    self._changed(await self._get_status())
                    self.live = True
                    delay = BACKOFF_MIN_S
                    log.info("GoXLR daemon connected")
                    while not reader.done():
                        dirty = asyncio.create_task(self._dirty.wait())
                        await asyncio.wait({reader, dirty}, return_when=asyncio.FIRST_COMPLETED)
                        dirty.cancel()
                        if reader.done():
                            break
                        await asyncio.sleep(REFRESH_S)       # let a burst of patches settle into one read
                        self._dirty.clear()
                        self._changed(await self._get_status())
                    reader.result()                           # raises what ended the read loop
                finally:
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
            except asyncio.CancelledError:
                await self._drop()
                raise
            except Exception as e:                             # noqa: BLE001 - any failure means reconnect later
                was = self.live
                await self._drop()
                if was:
                    log.warning("GoXLR daemon connection lost: %s", e)
                    notify.audio()
                await asyncio.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX_S)

    async def _drop(self) -> None:
        self.live = False
        for fut in self._waiting.values():
            if not fut.done():
                fut.set_exception(Unavailable("GoXLR daemon connection closed"))
        self._waiting.clear()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:                                  # noqa: BLE001 - already gone
                pass

    async def _read(self, ws: Any) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            data = msg.get("data") if isinstance(msg, dict) else None
            if isinstance(data, dict) and "Patch" in data:
                self._dirty.set()
                continue
            fut = self._waiting.pop(msg.get("id"), None) if isinstance(msg, dict) else None
            if fut and not fut.done():
                fut.set_result(data)
        raise ConnectionError("GoXLR daemon closed the connection")

    async def _ask(self, data: Any) -> Any:
        ws = self._ws
        if ws is None:
            raise Unavailable("GoXLR daemon not connected")
        mid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._waiting[mid] = fut
        try:
            await ws.send(json.dumps({"id": mid, "data": data}))
            return await asyncio.wait_for(fut, REPLY_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            raise Unavailable("GoXLR daemon didn't answer") from e
        finally:
            self._waiting.pop(mid, None)

    async def _get_status(self) -> dict[str, Any]:
        answer = await self._ask("GetStatus")
        if not isinstance(answer, dict) or not isinstance(answer.get("Status"), dict):
            raise Unavailable(f"GoXLR daemon: {answer}")
        return answer["Status"]

    def _changed(self, st: dict[str, Any]) -> None:
        """Keep the new status; tell pages if anything they show moved."""
        old, self._last = self._last, st
        if old == st:
            return
        notify.audio()
        if _volumes(old) != _volumes(st):
            notify.volume()


def _volumes(st: dict[str, Any] | None) -> tuple:
    if not st:
        return ()
    mixers = st.get("mixers") or {}
    if not mixers:
        return ()
    vols = (next(iter(mixers.values())).get("levels") or {}).get("volumes") or {}
    return tuple(vols.get(c) for c in VOLUME_CHANNELS)
