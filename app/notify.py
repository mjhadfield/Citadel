"""Change notifications for the event stream.

Events are *triggers only* — they carry at most an id, never data. Every client re-fetches its own,
role-filtered state when told something changed. That keeps a TV (kiosk) subscriber from ever receiving,
say, calendar events it isn't allowed to show, even though it shares the same stream.
"""
from __future__ import annotations

from .bus import bus


def widget(widget_id: str) -> None:
    bus.publish("widget", {"id": widget_id})


def projects() -> None:
    bus.publish("projects", {})


def health() -> None:
    bus.publish("health", {})


def config() -> None:
    """Links/projects/settings were edited: screens should refresh."""
    bus.publish("config", {})


def volume() -> None:
    """A GoXLR volume changed (the header's volume buttons): every device re-reads them."""
    bus.publish("volume", {})


def audio() -> None:
    """The GoXLR's state changed (app/goxlr.py): the Audio page re-reads it."""
    bus.publish("audio", {})
