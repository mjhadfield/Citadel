"""The header's two volume buttons: the GoXLR's Line out and Vinyl (the turntable on its Line in).

They replace Mike's two Plasma desktop widgets (com.mike.goxlrline{out,in}slider) and talk to the same thing: the
goxlr-utility daemon on this PC, through its local HTTP API (`http://localhost:14564`, the one `goxlr-client
--use-http` uses). Volumes are 0–255 inside the daemon and percentages here, as `goxlr-client volume` takes them.

- **Mute** is volume 0, as in the widgets (neither channel sits on a fader, so there's no mute button to press). The
  level to come back to is kept in `widget_settings` key `volume` (`restore`), updated whenever a level is set here.
- **Line out's slider is curved** like its widget (`curve` 2.2): the slider's position p (0–100) is volume
  100·(p/100)^(1/2.2), so most of the travel fine-tunes the top end. The browser does the mapping; the API speaks volume.
- **Muted at startup**: once per boot (the kernel's boot id, also kept in the setting) both channels drop to 0 as soon
  as the daemon answers, their levels kept to come back to. A hub restart within the same boot leaves them alone.

The daemon connection itself is `app/goxlr.py` (shared with the Audio page): it holds the websocket that tells every
device when Line out or Line in moved, even from the hardware or the utility's own page.

Only the live hub talks to the daemon: `HUB_GOXLR_URL` defaults to nothing under `HUB_NO_FETCH=1` (tests and scratch
copies), so a scratch instance can never mute the real speakers.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import db, notify
from .goxlr import GoXLR, Unavailable, to_pct, to_raw   # noqa: F401 - Unavailable/to_* are this module's API too

log = logging.getLogger(__name__)

KEY = "volume"                       # widget_settings: {"boot": <boot id muted for>, "restore": {channel: volume}}
DEFAULT_RESTORE = 50
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
BOOT_WAIT_S, BOOT_POLL_S = 600, 3    # how long to wait for the daemon after a boot, and how often to ask


@dataclass(frozen=True)
class Channel:
    name: str
    goxlr: str                        # the daemon's channel name
    curve: float = 1.0


CHANNELS: dict[str, Channel] = {
    "lineout": Channel("Line out", "LineOut", curve=2.2),
    "vinyl": Channel("Vinyl", "LineIn"),
}


def boot_id() -> str:
    try:
        return BOOT_ID.read_text().strip()
    except OSError:
        return ""


class Volume:
    def __init__(self, url: str | None = None, transport: httpx.AsyncBaseTransport | None = None, goxlr: GoXLR | None = None):
        self._own = goxlr is None                 # made its own client (tests): close it on stop
        self.gx = goxlr or GoXLR(url, transport)
        self._lock = asyncio.Lock()   # a read-then-set (mute, unmute) isn't split by another one
        self._task: asyncio.Task | None = None

    @property
    def configured(self) -> bool:
        return self.gx.configured

    async def start(self) -> None:
        if self.configured:
            self._task = asyncio.create_task(self.mute_for_boot(), name="volume-boot-mute")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._own:
            await self.gx.stop()

    # ---- the daemon ----------------------------------------------------------------------------------

    async def _status(self) -> tuple[str, dict[str, int]]:
        """The GoXLR's serial and its channel volumes (0–255)."""
        serial, mixer, _ = await self.gx.mixer()
        return serial, (mixer.get("levels") or {}).get("volumes") or {}

    async def _set_raw(self, serial: str, channel: Channel, raw: int) -> None:
        await self.gx.command(serial, {"SetVolume": [channel.goxlr, raw]})

    # ---- what the header shows ----------------------------------------------------------------------

    @staticmethod
    def _settings() -> dict[str, Any]:
        return db.get_settings(KEY, {"boot": "", "restore": {}})

    def _remember(self, cid: str, volume: int) -> None:
        s = self._settings()
        if volume > 0 and s["restore"].get(cid) != volume:
            s["restore"] = {**s["restore"], cid: volume}
            db.put_settings(KEY, s)

    def _view(self, volumes: dict[str, int] | None) -> dict[str, Any]:
        restore = self._settings()["restore"]
        chans = []
        for cid, ch in CHANNELS.items():
            vol = to_pct(volumes.get(ch.goxlr, 0)) if volumes is not None else None
            chans.append({"id": cid, "name": ch.name, "curve": ch.curve, "volume": vol, "muted": vol == 0,
                          "restore": int(restore.get(cid) or DEFAULT_RESTORE)})
        return {"configured": self.configured, "available": volumes is not None, "channels": chans}

    async def view(self) -> dict[str, Any]:
        try:
            _, volumes = await self._status()
        except Unavailable:
            volumes = None
        return self._view(volumes)

    async def set(self, cid: str, volume: int) -> dict[str, Any]:
        ch = CHANNELS[cid]
        async with self._lock:
            serial, volumes = await self._status()
            await self._set_raw(serial, ch, to_raw(volume))
            volumes = {**volumes, ch.goxlr: to_raw(volume)}
        self._remember(cid, volume)
        notify.volume()
        return self._view(volumes)

    async def mute(self, cid: str, muted: bool) -> dict[str, Any]:
        ch = CHANNELS[cid]
        async with self._lock:
            serial, volumes = await self._status()
            now = to_pct(volumes.get(ch.goxlr, 0))
            if muted:
                self._remember(cid, now)
                target = 0
            else:
                target = now or int(self._settings()["restore"].get(cid) or DEFAULT_RESTORE)
            await self._set_raw(serial, ch, to_raw(target))
            volumes = {**volumes, ch.goxlr: to_raw(target)}
        notify.volume()
        return self._view(volumes)

    async def mute_for_boot(self, boot: str | None = None) -> bool:
        """Mute both channels once per boot, as soon as the daemon answers. True if it muted them this time."""
        boot = boot if boot is not None else boot_id()
        if not boot or self._settings()["boot"] == boot:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BOOT_WAIT_S
        while True:
            try:
                async with self._lock:
                    serial, volumes = await self._status()
                    for cid, ch in CHANNELS.items():
                        self._remember(cid, to_pct(volumes.get(ch.goxlr, 0)))
                        if volumes.get(ch.goxlr, 0):
                            await self._set_raw(serial, ch, 0)
                break
            except Unavailable as e:
                if loop.time() >= deadline:
                    log.warning("couldn't mute the GoXLR at startup: %s", e)
                    break
                await asyncio.sleep(BOOT_POLL_S)
        db.put_settings(KEY, {**self._settings(), "boot": boot})
        log.info("GoXLR line out and vinyl muted for this boot")
        notify.volume()
        return True
