#!/usr/bin/env python3
"""Backfill tests: stubbed crawler (no network), stubbed publish.

    python3 tests/test_backfill.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_DB = os.path.join(tempfile.gettempdir(), f"backfill_test_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bs4 import BeautifulSoup

import crawler.backfill as backfill
import crawler.feedwatcher as fw
from crawler.db import Base, PageArchive, PageStat, Partner, SessionLocal, engine

Base.metadata.create_all(engine)

NOW = datetime.now(timezone.utc)
OLD_ISO = (NOW - timedelta(days=700)).strftime("%Y-%m-%dT%H:%M:%SZ")
LEDE = (" " + "containers orchestration kubernetes pods scheduling deployment "
        .strip() * 12)


def article(title, published=None):
    meta = f'<meta property="article:published_time" content="{published}">' \
        if published else ""
    return (f"<html><head><title>{title}</title>{meta}</head><body>"
            f"<h1>{title}</h1><p>{LEDE}</p></body></html>")


PAGES = {
    "/fresh": article("Fresh article"),
    "/old": article("Old article", published=OLD_ISO),
    "/thin": "<html><head><title>Thin</title></head><body><h1>Thin</h1>"
             "<p>a short body that is far too thin</p></body></html>",
    "/tag/x": article("Tag listing"),
}


class FakeCrawler:
    def __init__(self, site):
        self.site = site

    def pages(self):
        for url in PAGES:
            yield url, BeautifulSoup(PAGES[url], "html.parser")


fw.SiteCrawler = FakeCrawler
captured = []
fw.publish = lambda r, m: captured.append(m)


def check(name, cond, extra=None):
    assert cond, f"FAIL: {name} {'' if cond else extra}"
    print(f"ok  {name}")


db = SessionLocal()
# partner only — the site row is linked lazily by the real _site_for path
db.add(Partner(domain="p.test", profile="docs", feed_type="crawl"))
db.commit()
db.close()

stats = backfill.run_backfill("p.test", None)
check("gate split: 1 index / 1 archive / 2 skip",
      stats == {"index": 1, "archive": 1, "skip": 2}, stats)
check("one PageMsg, for the fresh page",
      len(captured) == 1 and captured[0].url == "/fresh", captured)
db = SessionLocal()
arch = db.get(PageArchive, "/old")
check("old page archived at t3 with its text",
      arch is not None and arch.tier == "t3" and "containers" in arch.text, arch)
check("old page NOT indexed (no page_stats row)",
      db.get(PageStat, "/old") is None)
ps = db.get(PageStat, "/fresh")
check("fresh page indexed at t0 with a dedup hash",
      ps is not None and ps.tier == "t0" and ps.content_hash, ps)
db.close()

# idempotent re-run: everything the gate already knows is skipped
captured.clear()
stats2 = backfill.run_backfill("p.test", None)
check("re-run is a no-op", stats2 == {"index": 0, "archive": 0, "skip": 4}, stats2)
check("re-run publishes nothing", len(captured) == 0, captured)

db = SessionLocal()
p = db.query(Partner).filter(Partner.domain == "p.test").first()
assert p is not None
check("poll clock seeded (feed watcher takes over)", p.last_poll_at is not None, p)
check("last_status records the backfill", (p.last_status or "").startswith("backfill ok"),
      p.last_status)
check("site row linked lazily", p.site_id is not None)
db.close()

try:
    backfill.run_backfill("nobody.test", None)
    check("unknown domain raises LookupError", False)
except LookupError:
    check("unknown domain raises LookupError", True)

os.remove(_DB)
print("\nbackfill tests passed")
