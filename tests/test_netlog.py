"""NetLog: httpx's request lines in the journal, rewritten so no secret reaches the log (app/netlog.py)."""
from __future__ import annotations

import logging

import httpx
import pytest

from app import logbook
from app.netlog import NetLog, is_local


def _record(method: str, url: str, status: int) -> logging.LogRecord:
    return logging.LogRecord("httpx", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s %d %s"',
                             (method, httpx.URL(url), "HTTP/1.1", status, "OK"), None)


@pytest.fixture
def net():
    n = NetLog()
    yield n, n._filter


def test_internet_request_logged_without_query(net):
    _, f = net
    r = _record("GET", "https://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks&api_key=abc123secret", 200)
    assert f.filter(r)
    assert r.name == "hub.net"
    line = r.getMessage()
    assert line == "GET ws.audioscrobbler.com/2.0/ -> 200"
    assert "abc123secret" not in line


def test_calendar_secret_address_redacted(net):
    _, f = net
    r = _record("GET", "https://calendar.google.com/calendar/ical/someone%40example.com/private-0123456789abcdef/basic.ics", 200)
    assert f.filter(r)
    line = r.getMessage()
    assert "private-0123" not in line and "example.com" not in line


def test_learned_secret_redacted(net):
    _, f = net
    logbook.learn(["tok-very-secret-value"])
    r = _record("GET", "https://example.org/feeds/tok-very-secret-value/rss", 200)
    assert f.filter(r)
    assert "tok-very-secret-value" not in r.getMessage()


def test_local_and_polling_counted_not_logged(net):
    n, f = net
    assert not f.filter(_record("GET", "http://192.168.0.70:8680/stats", 200))
    assert not f.filter(_record("GET", "http://127.0.0.1:14564/api/get-devices", 500))      # local failures: counted
    assert not f.filter(_record("GET", "http://probe-1.local:8123/api/states", 200))
    assert not f.filter(_record("GET", "http://100.127.118.96:8080/", 200))
    assert not f.filter(_record("GET", "https://api.spotify.com/v1/me/player", 200))
    assert f.filter(_record("GET", "https://api.spotify.com/v1/me/player", 429))            # unless it fails
    s = n.summary()
    assert s["calls"] == {"192.168.0.70:8680": 1, "127.0.0.1:14564": 1, "probe-1.local:8123": 1,
                          "100.127.118.96:8080": 1, "api.spotify.com": 2}
    assert s["failed"] == {"127.0.0.1:14564": 1, "api.spotify.com": 1}


def test_summary_line_and_reset(net, caplog):
    n, f = net
    for _ in range(3):
        f.filter(_record("GET", "https://musicbrainz.org/ws/2/artist", 200))
    f.filter(_record("GET", "https://api.discogs.com/x", 503))
    with caplog.at_level(logging.INFO, logger="hub.net"):
        line = n.flush()
    assert line and "Calls in the last 0 min: 4." in line
    assert "musicbrainz.org 3" in line and "api.discogs.com 1 (1 failed)" in line
    assert n.flush() is None                                       # counts start again


def test_other_httpx_info_stays_quiet(net):
    _, f = net
    other = logging.LogRecord("httpx", logging.INFO, __file__, 1, "something else", None, None)
    assert not f.filter(other)


def test_is_local():
    assert is_local("localhost") and is_local("nzxt") and is_local("10.1.2.3") and is_local("::1")
    assert not is_local("api.spotify.com") and not is_local("8.8.8.8")


def test_lifespan_turns_it_on_and_off(owner_client):
    assert isinstance(owner_client.app.state.netlog, NetLog)
    assert logging.getLogger("httpx").level == logging.INFO
