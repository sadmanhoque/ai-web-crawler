#!/usr/bin/env python3
"""Standalone tests for tiering metrics (build order 8): the tier_events
audit log, record_event, and tierctl metrics/transitions. No redis, no
network (fetch + publish stubbed):

    python3 tests/test_metrics.py
"""
import argparse
import contextlib
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_DB = tempfile.mktemp(suffix=".db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "cli"))

from shared.messages import Section
from shared.tiers import Tier
from crawler import tierbot
from crawler.db import (Base, PageArchive, PageStat, Partner, SessionLocal,
                        Site, TierEvent, engine, record_event)

Base.metadata.create_all(engine)
NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=30)

LEDE = Section(heading="Intro", heading_tag="h1", text="lede text " * 20)
BODY = Section(heading="Deep", heading_tag="h2", text="deep text " * 20)


def fake_fetch(url):
    return "Fake Title", "Headline text", [LEDE, BODY]


def check(name, cond, extra=None):
    assert cond, f"FAIL: {name} {'' if cond else extra}"
    print(f"ok  {name}")


def main():
    tierbot._fetch = fake_fetch
    tierbot.publish = lambda r, m: None

    # --- seed: two partners, a spread across the tiers ---------------------
    db = SessionLocal()
    sa = Site(domain="a.test", start_url="https://a.test/", interval_hours=24)
    sb = Site(domain="b.test", start_url="https://b.test/", interval_hours=24)
    db.add_all([sa, sb])
    db.flush()
    db.add(Partner(domain="a.test", profile="docs", site_id=sa.id, feed_type="crawl"))
    db.add(Partner(domain="b.test", profile="news", site_id=sb.id, feed_type="rss"))
    db.add_all([
        # a.test: 2 t0, 1 t1, 1 t2, 1 t3  (page for the live transitions below)
        PageStat(url="https://a.test/hot1", site_id=sa.id, tier="t0"),
        PageStat(url="https://a.test/hot2", site_id=sa.id, tier="t0"),
        PageStat(url="https://a.test/warm", site_id=sa.id, tier="t1"),
        PageStat(url="https://a.test/cold", site_id=sa.id, tier="t2"),
        PageStat(url="https://a.test/vaulted", site_id=sa.id, tier="t3"),
        # b.test: 1 t0, 2 t3
        PageStat(url="https://b.test/one", site_id=sb.id, tier="t0"),
        PageStat(url="https://b.test/v1", site_id=sb.id, tier="t3"),
        PageStat(url="https://b.test/v2", site_id=sb.id, tier="t3"),
    ])
    # a.test/vaulted: a real archived copy, archived 20 days ago
    arch = PageArchive(url="https://a.test/vaulted", site_id=sa.id,
                       text="deep text " * 20, title="Fake Title",
                       sections=[{"heading": "Deep", "heading_tag": "h2",
                                  "text": "deep text " * 20}],
                       archived_at=NOW - timedelta(days=20))
    db.add(arch)
    db.commit()
    db.close()

    # --- the real machinery writes the log ----------------------------------
    db = SessionLocal()
    ps = db.get(PageStat, "https://a.test/hot1")
    partner = db.query(Partner).filter(Partner.domain == "a.test").first()
    site = db.get(Site, sa.id)
    assert ps is not None and partner is not None and site is not None
    ps.last_tier_change = OLD
    db.commit()
    result = tierbot.transition_page(db=db, r=None, ps=ps, site=site, partner=partner,
                                     to_tier=Tier.t1, score=1.0)
    db.close()
    check("transition applied", result == "demoted", result)
    db = SessionLocal()
    tierbot.restore_page(db=db, r=None, url="https://a.test/vaulted", to_tier=Tier.t2,
                         via="manual")
    db.close()
    # ingest-time archiving (the feedwatcher's path) is the same helper:
    record_event("https://b.test/old-post", sb.id, "archive", to_tier="t3")
    db = SessionLocal()
    db.add(PageStat(url="https://b.test/old-post", site_id=sb.id, tier="t3"))
    db.commit()
    db.close()

    db = SessionLocal()
    evs = db.query(TierEvent).order_by(TierEvent.id).all()
    db.close()
    check("3 events: demote (auto) + restore (manual) + archive",
          [(e.kind, e.via) for e in evs]
          == [("demote", "auto"), ("restore", "manual"), ("archive", "auto")],
          [(e.kind, e.via) for e in evs])
    check("demote event: a.test hot1 t0 -> t1 with 720h dwell",
          evs[0].from_tier == "t0" and evs[0].to_tier == "t1"
          and evs[0].dwell_hours is not None and abs(evs[0].dwell_hours - 720.0) < 1,
          (evs[0].from_tier, evs[0].to_tier, evs[0].dwell_hours))
    check("restore latency = time in the vault (~20 days)",
          evs[1].kind == "restore" and evs[1].to_tier == "t2"
          and abs(evs[1].dwell_hours - 480.0) < 1, evs[1].dwell_hours)
    check("archive event: straight to t3, no from_tier, no dwell",
          evs[2].to_tier == "t3" and evs[2].from_tier is None
          and evs[2].dwell_hours is None, (evs[2].from_tier, evs[2].to_tier))

    # --- tierctl metrics (in-process, stdout captured) -----------------------
    import tierctl  # type: ignore[import]
    out_all = io.StringIO()
    with contextlib.redirect_stdout(out_all):
        tierctl.cmd_metrics(argparse.Namespace(partner=None, since="30d"))
    out_all = out_all.getvalue()
    check("metrics: pages per tier table has both partners + TOTAL",
          "pages per tier (current, all partners)" in out_all
          and "a.test" in out_all and "b.test" in out_all and "TOTAL" in out_all,
          out_all)
    a_line = next(l for l in out_all.splitlines() if l.strip().startswith("a.test"))
    check("metrics: a.test row is 1 t0 / 2 t1 / 2 t2 / 0 t3 (hot1 demoted, vaulted restored to t2)",
          a_line.split()[:6] == ["a.test", "1", "2", "2", "0", "5"], a_line)
    b_line = next(l for l in out_all.splitlines() if l.strip().startswith("b.test"))
    check("metrics: b.test row is 1 t0 / 0 t1 / 0 t2 / 3 t3",
          b_line.split()[:6] == ["b.test", "1", "0", "0", "3", "4"], b_line)
    check("metrics: storage per tier section", "storage per tier (current):" in out_all, out_all)
    check("metrics: transition breakdown auto vs manual",
          "demote" in out_all and "manual 1" in out_all
          and "(at ingest, straight to the vault)" in out_all, out_all)
    check("metrics: events-per-day rate", "events/day" in out_all, out_all)
    check("metrics: restore latency min/median/max",
          "restore latency (last 30d" in out_all
          and "n=1  min 20.0d  median 20.0d  max 20.0d" in out_all, out_all)
    check("metrics: vault depth + ages", "vault (current, page_archives):" in out_all
          and "20.0d" in out_all, out_all)

    # partner filter
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        tierctl.cmd_metrics(argparse.Namespace(partner="a.test", since="30d"))
    out_a = buf.getvalue()
    check("metrics --partner: only a.test shown",
          "a.test" in out_a and "b.test" not in out_a, out_a)
    check("metrics --partner: event window scoped to the partner",
          "restore" in out_a and "archive" not in out_a.split("restore latency")[0], out_a)

    # --- tierctl transitions reads the event log ----------------------------
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        tierctl.cmd_transitions(argparse.Namespace(since="30d"))
    out_t = buf.getvalue()
    check("transitions: lists events with kind/from->to/via/domain",
          "demote   t0 -> t1  auto   a.test" in out_t
          and "restore  t3 -> t2  manual a.test" in out_t
          and "archive  - -> t3  auto   b.test" in out_t, out_t)
    # the window actually filters: a 60-day-old event is outside 30d, inside 90d
    record_event("https://b.test/ancient", sb.id, "demote", from_tier="t1",
                 to_tier="t2", via="auto", ts=NOW - timedelta(days=60))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        tierctl.cmd_transitions(argparse.Namespace(since="30d"))
    out_30 = buf.getvalue()
    check("transitions: 60d-old event excluded from the 30d window",
          "ancient" not in out_30, out_30)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        tierctl.cmd_transitions(argparse.Namespace(since="90d"))
    out_90 = buf.getvalue()
    check("transitions: 90d window includes it, newest first",
          "ancient" in out_90
          and out_90.splitlines()[0].endswith("https://b.test/old-post"), out_90)

    os.remove(_DB)
    print("\nmetrics tests passed")


if __name__ == "__main__":
    main()
