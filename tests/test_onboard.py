#!/usr/bin/env python3
"""Onboarding trigger test: a partner row appears in the database via plain
SQL (the "other means" — no API, no script, no ORM), and the RUNNING feed
watcher picks it up, auto-creates the sites row, runs the crawl through the
ingest gate, and the tiering system then acts on the indexed pages.

    python3 tests/test_onboard.py
"""
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

_DB = os.path.join(tempfile.gettempdir(), f"onboard_test_{os.getpid()}.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bs4 import BeautifulSoup

import crawler.feedwatcher as fw
import crawler.tierbot as tierbot
from crawler.db import Base, PageArchive, PageStat, Partner, SessionLocal, engine
from shared.messages import Section
from sqlalchemy import text

Base.metadata.create_all(engine)

# --- stubbed crawler (no network) + stubbed publish (no redis writes) ------
NOW = datetime.now(timezone.utc)
OLD_ISO = (NOW - timedelta(days=700)).strftime("%Y-%m-%dT%H:%M:%SZ")
LEDE = (" " + "partners contract content licensing revenue sharing visits "
        .strip() * 12)


def article(title, published=None):
    meta = f'<meta property="article:published_time" content="{published}">' \
        if published else ""
    return (f"<html><head><title>{title}</title>{meta}</head><body>"
            f"<h1>{title}</h1><p>{LEDE}</p></body></html>")


PAGES = {
    "/fresh": article("Fresh partner article"),
    "/old": article("Old partner article", published=OLD_ISO),
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
page_msgs, tier_msgs = [], []
fw.publish = lambda r, m: page_msgs.append(m)
tierbot.publish = lambda r, m: tier_msgs.append(m)
LEDE_S = Section(heading="Le", heading_tag="h2", text="lede text " * 10)
BODY_S = Section(heading="Body", heading_tag="h2", text="body text " * 20)
tierbot._fetch = lambda url: ("Fresh partner article", "headline text", [LEDE_S, BODY_S])


def check(name, cond, extra=None):
    assert cond, f"FAIL: {name} {'' if cond else extra}"
    print(f"ok  {name}")


# --- 1. the crawler is already running (the real feed worker loop) ---------
stop = threading.Event()
_real_wait = stop.wait
stop.wait = lambda timeout=None: _real_wait(0.3)     # fast idle spin for the test
threading.Thread(target=fw.feed_worker_loop, args=(stop,), daemon=True).start()
time.sleep(0.7)                                       # let it spin once on an empty DB

# --- 2. "other means": the partner row lands by plain SQL, minimal insert --
db = SessionLocal()
db.execute(text("INSERT INTO partners (domain) VALUES ('p.test')"))
db.commit()
db.close()

# wait for the running loop to claim + poll it (timeout = real failure)
deadline = time.time() + 15
p = None
while time.time() < deadline:
    db = SessionLocal()
    p = db.query(Partner).filter(Partner.domain == "p.test").first()
    status = p.last_status if p else None
    db.close()
    if status and (status.startswith("poll ok") or status.startswith("error")):
        break
    time.sleep(0.2)
p = p or None
assert p is not None and (p.last_status or "").startswith("poll ok"), \
    f"partner never polled ok: {p.last_status if p else 'no partner row'}"
print("ok  running feed watcher picked up the SQL-inserted partner")

# --- 3. what the poll produced ---------------------------------------------
check("gate split in last_status: 1 index / 1 archive / 2 skip",
      p.last_status == "poll ok: {'index': 1, 'archive': 1, 'skip': 2}", p.last_status)

db = SessionLocal()
site = db.execute(text("SELECT * FROM sites WHERE domain = 'p.test'")).mappings().first()
check("sites row auto-created (source=partner, start_url derived from domain)",
      site is not None and site["source"] == "partner"
      and site["start_url"] == "https://p.test/" and site["enabled"], dict(site) if site else None)
check("partner.site_id linked to the auto-created site",
      p.site_id is not None and p.site_id == site["id"], (p.site_id, site["id"] if site else None))
check("bare insert got every DB default (30/365/{}/enabled)",
      p.poll_interval_minutes == 30 and p.backfill_cutoff_days == 365
      and p.tier_config == {} and p.enabled and p.profile == "general"
      and p.feed_type == "crawl" and p.created_at is not None, p)

ps = db.get(PageStat, "/fresh")
check("fresh page indexed at t0 with hash + title (keyword leg can find it)",
      ps is not None and ps.tier == "t0" and ps.content_hash and ps.title, ps)
check("old page archived at t3, NOT indexed",
      db.get(PageArchive, "/old") is not None and db.get(PageStat, "/old") is None)
check("one PageMsg for the fresh page (collection + profile from partner row)",
      len(page_msgs) == 1 and page_msgs[0].url == "/fresh"
      and page_msgs[0].profile == "general" and page_msgs[0].collection, page_msgs)
db.close()

# --- 4. the tiered storage system acts on what the crawl produced ----------
fast = os.path.join(tempfile.gettempdir(), f"onboard_fast_{os.getpid()}.yaml")
with open(fast, "w") as f:
    f.write("config:\n  - type: defaults\n    half_life_days: 0.02\n"
            "    min_dwell_days: 0.001\n    t0_floor: 20\n    t1_floor: 5\n"
            "    t2_floor: 1\n    t2_promote: 10\n    t1_promote: 25\n    t0_promote: 60\n")
os.environ["TIERS_FILE"] = fast

# /hits-equivalent: 25 opens of the fresh page (the app's /hits logic, inlined)
T0 = datetime.now(timezone.utc)
db = SessionLocal()
ps = db.get(PageStat, "/fresh")
assert ps is not None
ps.hits, ps.last_accessed_at = 25, T0
db.commit()
db.close()

s1 = tierbot.evaluate_pass(None, now=T0 + timedelta(minutes=1))
db.close()                                   # close before the next pass: a live
db = SessionLocal()                          # SQLite read session would keep the
ps = db.get(PageStat, "/fresh")              # old snapshot
assert ps is not None
check("hot page held at t0 by the real tier bot (heat 25 >= floor 20)",
      ps.tier == "t0" and s1.get("hold", 0) >= 1, (ps.tier, s1))
db.close()

s2 = tierbot.evaluate_pass(None, now=T0 + timedelta(minutes=60))
db = SessionLocal()
ps = db.get(PageStat, "/fresh")
assert ps is not None
check("same page demoted to t1 once the (fast) decay drops heat below the floor",
      ps.tier == "t1" and s2.get("demoted", 0) == 1, (ps.tier, s2))
check("demote published the indexer message",
      len(tier_msgs) == 1 and getattr(tier_msgs[0], "url", None) == "/fresh", tier_msgs)
db.close()

stop.set()
os.remove(_DB)
os.remove(fast)
print("\nonboard trigger tests passed")
