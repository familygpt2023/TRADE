#!/usr/bin/env python3
"""Collect FinancialJuice headlines from the public RSS feed.

Writes new headlines (since the last run) to reports/latest.json and appends a
readable digest to reports/YYYY-MM-DD.md. Summarising is left to the reader
(e.g. a scheduled Claude task reading reports/latest.json).

Usage:
    python fj_news.py              # fetch, de-duplicate, write reports
    python fj_news.py --self-test  # offline check of the parser (no network)
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from bs4 import BeautifulSoup

FEED_URL = "https://www.financialjuice.com/feed.ashx?xy=rss"
HOME_URL = "https://www.financialjuice.com/home"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "seen.json"
REPORT_DIR = ROOT / "reports"
MAX_SEEN = 3000

# Cheap relevance tags for Nasdaq-100 futures; extend as you like.
TAGS = {
    "fed": r"\b(fed|fomc|powell|rate (cut|hike)|dot plot)\b",
    "inflation": r"\b(cpi|pce|ppi|inflation)\b",
    "jobs": r"\b(nfp|payrolls?|jobless|unemployment|jolts)\b",
    "yields": r"\b(yields?|treasur(y|ies)|10-?year|2-?year)\b",
    "tech": r"\b(nasdaq|nvidia|nvda|apple|aapl|microsoft|msft|tesla|tsla|meta|amazon|alphabet|google|semis?)\b",
    "oil": r"\b(oil|crude|wti|brent|opec)\b",
    "geopolitics": r"\b(war|missile|sanctions?|tariffs?|ceasefire|china|russia|iran|israel)\b",
    "usd": r"\b(dollar|dxy|usd|yen|boj)\b",
}


def _make_item(guid, title, link, published):
    title = re.sub(r"^FinancialJuice:\s*", "", title.strip(), flags=re.I)
    return {
        "id": guid or link or title,
        "title": title,
        "link": link,
        "published": published.isoformat() if published else None,
        "tags": [k for k, rx in TAGS.items() if re.search(rx, title, re.I)],
    }


def parse_feed(xml_bytes):
    """Parse the RSS feed with BeautifulSoup. Newest first."""
    soup = BeautifulSoup(xml_bytes, "xml")
    items = []
    for it in soup.find_all("item"):
        def txt(tag):
            el = it.find(tag)
            return el.get_text(strip=True) if el else ""

        title = txt("title")
        if not title:
            continue
        try:
            published = parsedate_to_datetime(txt("pubDate")).astimezone(timezone.utc)
        except (TypeError, ValueError):
            published = None
        items.append(_make_item(txt("guid"), title, txt("link"), published))
    items.sort(key=lambda x: x["published"] or "", reverse=True)
    return items


def parse_home_html(html):
    """Best-effort fallback: read headline elements from the home page HTML.

    The live page loads its headlines with JavaScript, so plain HTML may contain
    none. Selectors are guesses; adjust after inspecting the real markup.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen, items = set(), []
    for el in soup.select('[class*="headline"], [class*="news-title"], [class*="infoTitle"]'):
        title = el.get_text(" ", strip=True)
        if len(title) < 15 or title in seen:
            continue
        seen.add(title)
        a = el.find("a") or el.find_parent("a")
        link = a["href"] if a and a.has_attr("href") else ""
        items.append(_make_item(link or title, title, link, None))
    return items


def fetch_url(url, retries=3):
    import requests

    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200 and r.content:
                return r.content
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)
        time.sleep(2 * attempt)
    raise RuntimeError(f"could not fetch {url}: {last}")


def load_seen():
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except (OSError, ValueError):
        return set()


def save_seen(seen_ids):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(seen_ids)[-MAX_SEEN:]))


def write_reports(new_items):
    REPORT_DIR.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    (REPORT_DIR / "latest.json").write_text(
        json.dumps(
            {"generated_utc": now.isoformat(), "count": len(new_items), "items": new_items},
            indent=2,
            ensure_ascii=False,
        )
    )
    day_file = REPORT_DIR / f"{now:%Y-%m-%d}.md"
    lines = [f"\n## {now:%H:%M} UTC - {len(new_items)} new headlines\n"]
    for it in new_items:
        ts = (it["published"] or "")[11:16]
        tags = f" `{','.join(it['tags'])}`" if it["tags"] else ""
        lines.append(f"- {ts} {it['title']}{tags}")
    with day_file.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def self_test():
    sample = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>FJ</title>
    <item><title>FinancialJuice: FED'S POWELL: NO RATE CUT YET, YIELDS JUMP</title>
      <link>https://www.financialjuice.com/news/1</link><guid>1</guid>
      <pubDate>Wed, 04 Nov 2026 10:01:00 GMT</pubDate></item>
    <item><title>NVDA EARNINGS BEAT, NASDAQ FUTURES RISE</title>
      <link>https://www.financialjuice.com/news/2</link><guid>2</guid>
      <pubDate>Wed, 04 Nov 2026 10:05:00 GMT</pubDate></item>
    </channel></rss>"""
    items = parse_feed(sample)
    assert len(items) == 2, items
    assert items[0]["id"] == "2" and items[1]["title"].startswith("FED'S POWELL"), items
    assert {"fed", "yields"} <= set(items[1]["tags"]), items[1]
    assert {"tech"} <= set(items[0]["tags"]), items[0]
    print("self-test OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    try:
        items = parse_feed(fetch_url(FEED_URL))
    except Exception as e:  # network block, bad XML, etc.
        print(f"RSS failed ({e}); trying home page HTML", file=sys.stderr)
        try:
            items = parse_home_html(fetch_url(HOME_URL).decode("utf-8", "replace"))
        except Exception as e2:
            print(f"ERROR: {e2}", file=sys.stderr)
            sys.exit(2)
        if not items:
            print("ERROR: home page had no headlines (likely JavaScript-rendered)", file=sys.stderr)
            sys.exit(2)

    seen = load_seen()
    new_items = [i for i in items if i["id"] not in seen]
    if new_items:
        write_reports(new_items)
        save_seen(seen | {i["id"] for i in new_items})
    print(f"{len(items)} in feed, {len(new_items)} new")


if __name__ == "__main__":
    main()
