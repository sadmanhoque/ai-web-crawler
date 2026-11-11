#!/usr/bin/env python3
"""Standalone tests for the tier bot (no pytest, no redis, no network:
fetch and publish are stubbed; the decision logic is shared/tiers.py):

    python3 tests/test_tierbot.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_DB = tempfile.mktemp(suffix=".db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.messages import DemoteMsg, PromoteMsg, Section
from shared.tiers import Tier, as_utc
from crawler import tierbot
from crawler.db import (Base, PageArchive, PageStat, Partner, SessionLocal,
                        Site, engine)

Base.metadata.create_all(engine)
NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=30)

LEDE = Section(heading="Introduction", heading_tag="h1", text="lede body " * 20)
BODY = Section(heading="Details", heading_tag="h2", text="detail body " * 20)
HEADLINE = "Title. H1 text. meta description"


def fake_fetch(url):
    if "dead" in url:
        raise RuntimeError("boom")
    return "Title", HEADLINE, [LEDE, BODY]


def check(name, cond, extra=None):
    assert cond, f"FAIL: {name} {'' if cond else extra}"
    print(f"ok  {name}")


def tiers():
    db = SessionLocal()
    rows = {p.url: (p.tier, as_utc(p.last_tier_change), p.heat) for p in db.query(PageStat).all()}
    db.close()
    return rows


def main():
    # --- seed: one enabled partner site + one non-partner site ---
    db = SessionLocal()
    site = Site(domain="p.test", start_url="https://p.test/", interval_hours=24)
    other = Site(domain="o.test", start_url="https://o.test/", interval_hours=24)
    db.add_all([site, other])
    db.flush()
    db.add(Partner(domain="p.test", profile="docs", site_id=site.id, feed_type="crawl"))
    db.add_all([
        PageStat(url="https://p.test/u1", site_id=site.id, tier="t0", hits=10,
                 last_accessed_at=NOW, last_tier_change=OLD),          # t0 -> t1
        PageStat(url="https://p.test/u2", site_id=site.id, tier="t2", hits=1,
                 last_accessed_at=OLD, last_tier_change=OLD),          # t2 -> t3
        PageStat(url="https://p.test/u3", site_id=site.id, tier="t3", hits=15,
                 last_accessed_at=NOW),                                # t3 -> t2
        PageStat(url="https://p.test/u4", site_id=site.id, tier="t0", hits=1,
                 last_accessed_at=OLD, last_tier_change=NOW),          # dwell blocks
        PageStat(url="https://p.test/u5", site_id=site.id, tier="t1", hits=70,
                 last_accessed_at=NOW, last_tier_change=NOW),          # promote (dwell exempt)
        PageStat(url="https://p.test/dead", site_id=site.id, tier="t0", hits=1,
                 last_accessed_at=OLD, last_tier_change=OLD),          # fetch fails
        PageStat(url="https://o.test/x", site_id=other.id, tier="t0", hits=5,
                 last_accessed_at=NOW),                                # non-partner
    ])
    db.commit()
    db.close()

    # --- stub publish: capture + snapshot archive state AT publish time ---
    tierbot._fetch = fake_fetch
    captured, archived_at_publish = [], []

    def rec_publish(r, m):
        captured.append(m)
        with SessionLocal() as s:
            archived_at_publish.append(s.get(PageArchive, m.url) is not None)
    tierbot.publish = rec_publish

    # --- _sections_for: tier table ---
    check("t0 keeps all sections", len(tierbot._sections_for(HEADLINE, "T", [LEDE, BODY], Tier.t0)) == 2)
    check("t1 keeps lede only", tierbot._sections_for(HEADLINE, "T", [LEDE, BODY], Tier.t1) == [LEDE])
    stub = tierbot._sections_for(HEADLINE, "T", [LEDE, BODY], Tier.t2)
    check("t2 is one stub (headline text)",
          len(stub) == 1 and stub[0].text == HEADLINE and stub[0].heading == "T", stub)

    # --- pass 1 ---
    stats = tierbot.evaluate_pass(None, now=NOW)
    check("pass 1 stats",
          stats == {"evaluated": 6, "hold": 1, "promoted": 2, "demoted": 2, "fetch_failed": 1},
          stats)

    t = tiers()
    check("u1 demoted t0 -> t1", t["https://p.test/u1"][0] == "t1", t)
    check("u2 demoted t2 -> t3", t["https://p.test/u2"][0] == "t3", t)
    check("u3 promoted t3 -> t2", t["https://p.test/u3"][0] == "t2", t)
    check("u4 held by min dwell", t["https://p.test/u4"][0] == "t0", t)
    check("u5 promoted t1 -> t0 (dwell exempt)", t["https://p.test/u5"][0] == "t0", t)
    check("dead held (fetch failed)", t["https://p.test/dead"][0] == "t0", t)
    check("transitions stamped last_tier_change",
          t["https://p.test/u1"][1] is not None and t["https://p.test/u5"][1] is not None, t)
    check("held page untouched (no stamp)", t["https://p.test/u4"][1] == NOW, t)
    check("non-partner page never evaluated", "https://o.test/x" in t
          and t["https://o.test/x"][0] == "t0" and stats["evaluated"] == 6, stats)

    db = SessionLocal()
    arch = db.get(PageArchive, "https://p.test/u2")
    check("u2 archived to page_archives (full text)",
          arch is not None and "lede body" in arch.text and "detail body" in arch.text and arch.tier == "t3")
    check("u3 promotion wrote no archive",
          db.get(PageArchive, "https://p.test/u3") is None)
    db.close()

    check("4 messages published", len(captured) == 4, captured)
    by_url = {m.url: m for m in captured}
    check("u2 demote is a DemoteMsg to t3",
          isinstance(by_url["https://p.test/u2"], DemoteMsg) and by_url["https://p.test/u2"].to_tier is Tier.t3)
    check("u1 demote is delete-then-reembed (PromoteMsg) to t1 with lede only",
          isinstance(by_url["https://p.test/u1"], PromoteMsg)
          and by_url["https://p.test/u1"].to_tier is Tier.t1
          and by_url["https://p.test/u1"].sections == [LEDE])
    check("u3 promote carries the t2 stub",
          isinstance(by_url["https://p.test/u3"], PromoteMsg)
          and by_url["https://p.test/u3"].to_tier is Tier.t2
          and by_url["https://p.test/u3"].sections[0].text == HEADLINE)
    check("u5 promote carries full sections",
          by_url["https://p.test/u5"].sections == [LEDE, BODY])
    demote_i = [i for i, m in enumerate(captured) if isinstance(m, DemoteMsg)]
    check("archive exists BEFORE the demote message is published",
          all(archived_at_publish[i] for i in demote_i), archived_at_publish)

    # --- pass 2: quiet except the dead page ---
    n = len(captured)
    stats2 = tierbot.evaluate_pass(None, now=NOW)
    check("pass 2 stats",
          stats2 == {"evaluated": 6, "hold": 5, "promoted": 0, "demoted": 0, "fetch_failed": 1},
          stats2)
    check("pass 2 published nothing", len(captured) == n, captured[n:])
    check("dead page heat still refreshed", tiers()["https://p.test/dead"][2] > 0)

    # --- manual transitions (tierctl path): multi-step jump + no-op ---
    db = SessionLocal()
    ps, p = db.get(PageStat, "https://p.test/u3"), db.query(Partner).first()  # currently t2
    assert ps is not None and p is not None
    result = tierbot.transition_page(db=db, r=None, ps=ps, site=site, partner=p,
                                     to_tier=Tier.t0, score=15)
    db.close()
    check("manual jump t2 -> t0", result == "promoted"
          and tiers()["https://p.test/u3"][0] == "t0", result)
    manual_msgs = [m for m in captured[n:] if m.url == "https://p.test/u3"]
    check("manual jump re-embeds full sections",
          manual_msgs and manual_msgs[-1].sections == [LEDE, BODY], manual_msgs)
    db = SessionLocal()
    ps = db.get(PageStat, "https://p.test/u3")
    assert ps is not None
    result = tierbot.transition_page(db=db, r=None, ps=ps, site=site, partner=p,
                                     to_tier=Tier.t0, score=15)
    db.close()
    check("manual no-op on same tier", result == "hold", result)

    # --- restore (build order 7): vaulted page back from the archive ---
    db = SessionLocal()
    p = db.query(Partner).first()
    assert p is not None
    # u2 was demoted to t3 in pass 1; the archive now carries title + sections
    arch = db.get(PageArchive, "https://p.test/u2")
    assert arch is not None and arch.sections and arch.title == "Title"
    n = len(captured)
    result = tierbot.restore_page(db=db, r=None, url="https://p.test/u2", to_tier=Tier.t0)
    db.close()
    check("restore returns restored", result == "restored", result)
    rm = captured[n:]
    check("restore publishes a PromoteMsg to t0",
          len(rm) == 1 and isinstance(rm[0], PromoteMsg) and rm[0].to_tier is Tier.t0, rm)
    check("restore re-embeds the ARCHIVED sections (no live fetch)",
          rm[0].sections == [LEDE, BODY] and rm[0].title == "Title", rm)
    check("restore re-creates page_stats at t0",
          tiers()["https://p.test/u2"][0] == "t0", tiers())
    db = SessionLocal()
    check("archive row kept as the cold copy",
          db.get(PageArchive, "https://p.test/u2") is not None)
    n = len(captured)
    result = tierbot.restore_page(db=db, r=None, url="https://p.test/u2", to_tier=Tier.t1)
    db.close()
    check("restore --tier t1 keeps the lede only",
          result == "restored" and tiers()["https://p.test/u2"][0] == "t1"
          and captured[-1].sections == [LEDE])
    db = SessionLocal()
    check("restore of an un-archived url -> no_archive",
          tierbot.restore_page(db=db, r=None, url="https://p.test/nope") == "no_archive")
    db.close()
    stats3 = tierbot.evaluate_pass(None, now=NOW)
    check("restored page held by dwell on the next pass",
          tiers()["https://p.test/u2"][0] == "t1"
          and stats3 == {"evaluated": 6, "hold": 5, "promoted": 0, "demoted": 0,
                         "fetch_failed": 1}, (tiers(), stats3))

    # --- tier_events log (build order 8): audit trail of every applied action ---
    from crawler.db import TierEvent
    db = SessionLocal()
    evs = db.query(TierEvent).order_by(TierEvent.id).all()
    db.close()
    by_url: dict[str, list] = {}
    for e in evs:
        by_url.setdefault(e.url, []).append(e)
    check("7 events: 4 pass-1 transitions + 1 manual jump + 2 restores",
          len(evs) == 7, [ (e.url, e.kind) for e in evs ])
    u1 = by_url["https://p.test/u1"][0]
    check("u1: demote t0 -> t1, via auto, dwell ~720h (30 days in t0)",
          u1.kind == "demote" and u1.via == "auto" and u1.from_tier == "t0"
          and u1.to_tier == "t1" and abs(u1.dwell_hours - 720.0) < 1,
          (u1.kind, u1.from_tier, u1.to_tier, u1.dwell_hours))
    check("u2: demote to t3 then two restores, restore latency recorded",
          [e.kind for e in by_url["https://p.test/u2"]] == ["demote", "restore", "restore"]
          and by_url["https://p.test/u2"][0].to_tier == "t3"
          and all(e.dwell_hours is not None and e.dwell_hours > 0
                  for e in by_url["https://p.test/u2"] if e.kind == "restore"),
          [(e.kind, e.to_tier, e.dwell_hours) for e in by_url["https://p.test/u2"]])
    check("u3: promote t3 -> t2 then manual jump t2 -> t0, in order",
          [(e.kind, e.from_tier, e.to_tier) for e in by_url["https://p.test/u3"]]
          == [("promote", "t3", "t2"), ("promote", "t2", "t0")],
          [(e.kind, e.from_tier, e.to_tier) for e in by_url["https://p.test/u3"]])
    check("hold / fetch_failed / no-op log nothing",
          "https://p.test/u4" not in by_url and "https://p.test/dead" not in by_url,
          list(by_url))

    os.remove(_DB)
    print("all tierbot tests passed")


if __name__ == "__main__":
    main()
