"""The Guardian's "Most viewed", news and sport only, top 5 — the second Most read tile on the News page's front page.

The Guardian's site fills its Most viewed box from a small JSON endpoint (api.nextgen.guardianapps.co.uk, whose
robots.txt allows it): ten stories, each with its headline, address, summary, picture, publish time and "pillar"
(news, sport, opinion, culture, lifestyle). Only news and sport are kept, so this sits beside the BBC's list rather than
filling up with reviews and recipes. That often leaves fewer than five, so the list is topped up from the same
endpoint's "in UK news" and "in World news" rankings, taking turns; the result is ranked 1–5. Two requests an hour.

Everything else — movement, peaks, storage as source 'guardian-most-read' — is mostread.py's.
"""
from __future__ import annotations

from datetime import datetime
from itertools import chain, zip_longest
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import news
from .mostread import TOP, MostReadWidget

# Each answers with two rankings: the whole site's, then the section's.
URLS = ("https://api.nextgen.guardianapps.co.uk/most-read/uk-news.json?dcr=true",
        "https://api.nextgen.guardianapps.co.uk/most-read/world.json?dcr=true")
PILLARS = ("news", "sport")


def _published(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _trails(data: Any, tab: int) -> list[dict[str, Any]]:
    tabs = data.get("tabs") if isinstance(data, dict) else None
    trails = (tabs[tab] or {}).get("trails") if isinstance(tabs, list) and len(tabs) > tab and isinstance(tabs[tab], dict) else None
    return [t for t in trails if isinstance(t, dict)] if isinstance(trails, list) else []


def parse(pages: list[Any]) -> list[dict[str, Any]]:
    """[{rank, title, url, summary, image, published}] — up to 5: the site-wide ranking's news and sport first, then
    the section rankings' (UK, World, UK, …). Raises ValueError if the site-wide ranking can't be found."""
    overall = _trails(pages[0], 0) if pages else []
    if not overall:
        raise ValueError("found no stories in the Guardian's Most viewed list — the feed may have changed")
    sections = [t for t in chain.from_iterable(zip_longest(*(_trails(p, 1) for p in pages))) if t]
    out, seen = [], set()
    for t in overall + sections:
        if t.get("pillar") not in PILLARS:
            continue
        url, title = str(t.get("url") or ""), news.clean(t.get("headline") or t.get("linkText"))
        if urlsplit(url).hostname != "www.theguardian.com" or not title or url in seen or news.is_media(url):
            continue
        seen.add(url)
        summary = news.clean(t.get("trailText"))
        image = str(t.get("image") or "")
        out.append({"rank": len(out) + 1, "title": title[:300], "url": url,
                    "summary": summary[:237].rsplit(" ", 1)[0] + "…" if len(summary) > 240 else summary,
                    "image": image if image.startswith("https://i.guim.co.uk/") else "", "published": _published(t.get("webPublicationDate"))})
        if len(out) == TOP:
            break
    return out


class GuardianReadWidget(MostReadWidget):
    id = "guardianread"
    title = "Guardian Most read"
    url = URLS[0]

    async def download(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        pages = [(await self.get(client, url)).json() for url in URLS]
        return parse(pages)


widget = GuardianReadWidget()
