#!/usr/bin/env python3
"""
Generates an RSS feed for NHK NEWS WEB EASY (https://news.web.nhk/news/easy/).

Background: Since October 2025, NHK has required an authorization flow
that sets cookies before accessing `news-list.json`.
For users abroad, there is an official "abroad" profile for this purpose.
This script replicates the flow (similar to the RSSHub fix, commit 15c703c) and writes `feed.xml`.

Usage: python nhk_easy_rss.py [output_file] (Default: feed.xml)
Dependency: curl_cffi (pip install curl_cffi)

Note: We use curl_cffi instead of plain `requests` because NHK's WAF
rejects clients with a non-browser TLS fingerprint (HTTP 403).
curl_cffi impersonates a real Chrome handshake.
"""

import html
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from curl_cffi import requests

BASE = "https://news.web.nhk"
LIST_URL = f"{BASE}/news/easy/news-list.json"
EASY_URL = f"{BASE}/news/easy/"
JST = timezone(timedelta(hours=9))

BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.8,de;q=0.6",
    "Referer": EASY_URL,
}

MAX_NEW_ARTICLE_FETCHES = 0


def _raise_with_body(resp) -> None:
    """Like raise_for_status(), but includes a snippet of the response body
    so WAF block pages are visible in the Actions log."""
    if resp.status_code >= 400:
        snippet = (resp.text or "")[:300].replace("\n", " ")
        raise RuntimeError(f"HTTP {resp.status_code} for {resp.url} - body: {snippet}")


def _cookies_from_response(resp) -> str:
    """Return all Set-Cookie headers of a response as 'name=value; ...'."""
    parts = []
    for header in resp.headers.get_list("set-cookie"):
        parts.append(header.split(";")[0])
    return "; ".join(parts)


def authorize(session: requests.Session) -> str:
    """
    Complete the authorization flow for users outside Japan. 
    Returns the cookie header string that must be included
    with all subsequent requests.
    """
    r1 = session.get(
        f"{BASE}/tix/build_authorize",
        params={
            "idp": "a-alaz",
            "profileType": "abroad",
            "redirect_uri": EASY_URL,
            "entity": "none",
            "area": "130",
            "pref": "13",
            "jisx0402": "13101",
            "postal": "1000001",
        },
        allow_redirects=False,
        timeout=30,
    )
    _raise_with_body(r1)
    build_cookie = _cookies_from_response(r1)
    loc1 = r1.headers.get("Location")
    if not loc1:
        raise RuntimeError("build_authorize did not return a redirect: Has the flow changed?")

    r2 = session.get(loc1, allow_redirects=False, timeout=30)
    loc2 = r2.headers.get("Location")
    if not loc2:
        raise RuntimeError("authorize did not return a redirect: Has the flow changed?")

    r3 = session.get(
        loc2,
        headers={"Cookie": build_cookie},
        allow_redirects=False,
        timeout=30,
    )
    idp_cookie = _cookies_from_response(r3)

    return "; ".join(c for c in (build_cookie, idp_cookie) if c)


def fetch_news_list(session: requests.Session, cookie: str) -> list:
    resp = session.get(LIST_URL, headers={"Cookie": cookie}, timeout=30)
    _raise_with_body(resp)
    # Die Datei beginnt traditionell mit einem UTF-8-BOM
    return __import__("json").loads(resp.content.decode("utf-8-sig"))


def fetch_article_body(session: requests.Session, cookie: str, url: str) -> str:
    """Load the article HTML and extract the text (with furigana) from .article-body."""
    try:
        resp = session.get(url, headers={"Cookie": cookie}, timeout=30)
        _raise_with_body(resp)
        m = re.search(
            r'<[^>]+class="[^"]*article-body[^"]*"[^>]*>(.*?)</(?:div|section|article)>',
            resp.text,
            re.DOTALL,
        )
        return m.group(1).strip() if m else ""
    except Exception as exc:  # Volltext ist optional – Feed soll trotzdem entstehen
        print(f"  Warning: Full text not loaded ({url}): {exc}", file=sys.stderr)
        return ""


def load_existing_descriptions(feed_path: Path) -> dict:
    """guid -> description from an earlier feed.xml to save on requests."""
    known = {}
    if not feed_path.exists():
        return known
    try:
        tree = ET.parse(feed_path)
        for item in tree.iter("item"):
            guid = item.findtext("guid")
            desc = item.findtext("description")
            if guid and desc:
                known[guid] = desc
    except ET.ParseError:
        pass
    return known


def rfc822(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")


def build_feed(items: list) -> str:
    now = rfc822(datetime.now(JST))
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        "<title>NHK NEWS WEB EASY</title>",
        f"<link>{EASY_URL}</link>",
        "<description>やさしい日本語で書いたニュース (inoffizieller Feed)</description>",
        "<language>ja</language>",
        f"<lastBuildDate>{now}</lastBuildDate>",
    ]
    for it in items:
        parts += [
            "<item>",
            f"<title>{html.escape(it['title'])}</title>",
            f"<link>{it['link']}</link>",
            f"<guid isPermaLink=\"false\">{html.escape(it['guid'])}</guid>",
            f"<pubDate>{it['pubDate']}</pubDate>",
            f"<description>{html.escape(it['description'])}</description>",
            "</item>",
        ]
    parts += ["</channel>", "</rss>"]
    return "\n".join(parts)


def main() -> None:
    out_path = Path(sys.argv[1] if len(sys.argv) > 1 else "feed.xml")

    session = requests.Session(impersonate="chrome")
    session.headers.update(BROWSER_HEADERS)

    print("Authorization (Profil: abroad) ...")
    cookie = authorize(session)

    print("Load news-list.json ...")
    data = fetch_news_list(session, cookie)
    dates = data[0]

    known_descriptions = load_existing_descriptions(out_path)
    new_fetches = 0

    items = []
    for date_key in sorted(dates.keys(), reverse=True):
        for art in dates[date_key]:
            news_id = art["news_id"]
            link = f"{EASY_URL}{news_id}/{news_id}.html"
            title = art.get("title") or re.sub(r"<[^>]+>", "", art.get("title_with_ruby", ""))

            try:
                pub = datetime.strptime(
                    art["news_prearranged_time"], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=JST)
            except (KeyError, ValueError):
                pub = datetime.strptime(date_key, "%Y-%m-%d").replace(tzinfo=JST)

            if news_id in known_descriptions:
                description = known_descriptions[news_id]
            else:
                description = ""
                img = art.get("news_easy_image_uri") or art.get("news_web_image_uri")
                if img and img.startswith("http"):
                    description += f'<img src="{img}"/><br/>'
                elif img:
                    description += f'<img src="{EASY_URL}{news_id}/{img}"/><br/>'
                if new_fetches < MAX_NEW_ARTICLE_FETCHES:
                    body = fetch_article_body(session, cookie, link)
                    if body:
                        description += body
                        new_fetches += 1
                if not description:
                    description = html.escape(title)

            items.append(
                {
                    "title": title,
                    "link": link,
                    "guid": news_id,
                    "pubDate": rfc822(pub),
                    "description": description,
                }
            )

    out_path.write_text(build_feed(items), encoding="utf-8")
    print(f"OK: {len(items)} Article -> {out_path}")


if __name__ == "__main__":
    main()
