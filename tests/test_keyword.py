#!/usr/bin/env python3
"""Standalone tests for the keyword fallback (search/keyword.py, build
order 7). No network, no redis — one SQLite DB:

    python3 tests/test_keyword.py
"""
import os
import sys
import tempfile

_DB = tempfile.mktemp(suffix=".db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from crawler.db import Base, PageArchive, PageStat, SessionLocal, Site, engine
Base.metadata.create_all(engine)
from search.keyword import keyword_search


def check(name, cond, extra=None):
    assert cond, f"FAIL: {name} {'' if cond else extra}"
    print(f"ok  {name}")


db = SessionLocal()
site = Site(domain="p.test", start_url="https://p.test/", interval_hours=24)
other = Site(domain="n.test", start_url="https://n.test/", interval_hours=24)
db.add_all([site, other])
db.flush()
db.add_all([
    PageStat(url="https://p.test/kubernetes-scheduling-guide", site_id=site.id,
             title="Kubernetes scheduling guide", tier="t0", heat=5.0),
    PageStat(url="https://p.test/hpa", site_id=site.id,
             title="Kubernetes horizontal pod autoscaling", tier="t1", heat=0.5),
    # restored page: row in BOTH tables (cold copy lingers) -> dedup
    PageStat(url="https://p.test/2019/network-policies", site_id=site.id,
             title="Kubernetes network policies", tier="t0", heat=4.0),
    PageStat(url="https://n.test/kubernetes-in-practice", site_id=other.id,
             title="Kubernetes in practice", tier="t0", heat=9.0),          # non-partner
    PageArchive(url="https://p.test/2019/network-policies", site_id=site.id,
                title="Kubernetes network policies (archived copy)",
                text="kubernetes networking deep dive"),
    PageArchive(url="https://p.test/2018/pod-security", site_id=site.id,
                title=None, text="pod security standards kubernetes"),      # legacy: no title
])
db.commit()
db.close()

res = keyword_search("kubernetes")
urls = [r["url"] for r in res]
check("finds indexed AND vaulted pages (score, then heat, then url)",
      urls == ["https://n.test/kubernetes-in-practice",     # url+title 5.0, heat 9
               "https://p.test/kubernetes-scheduling-guide",  # url+title 5.0, heat 5
               "https://p.test/2019/network-policies",    # title 2.0, heat 4 (dedup: indexed)
               "https://p.test/hpa",                      # title 2.0, heat 0.5
               "https://p.test/2018/pod-security"],       # body 1.0
      urls)
check("url+title match outranks title-only outranks body-only",
      res[0]["score"] > res[2]["score"] > res[4]["score"], res)
vault = [r for r in res if r["source"] == "vault"]
check("vaulted page flagged as vault with a snippet around the match",
      len(vault) == 1 and vault[0]["url"] == "https://p.test/2018/pod-security"
      and "kubernetes" in vault[0]["snippet"], vault)
check("legacy archive without title still found by body",
      any(r["url"] == "https://p.test/2018/pod-security" for r in res), res)

# dedup: the restored page appears once, as the indexed row
res2 = keyword_search("network policies")
np = [r for r in res2 if r["url"] == "https://p.test/2019/network-policies"]
check("indexed+archived url reported once, indexed wins",
      len(np) == 1 and np[0]["source"] == "indexed" and np[0]["tier"] == "t0", res2)

check("partner filter", [r["url"] for r in keyword_search("kubernetes",
      partner="p.test")] == [r["url"] for r in res if "p.test" in r["url"]])
check("no match -> empty", keyword_search("zzzqqq") == [])
check("terms shorter than 2 chars ignored", keyword_search("a b k") == [])
check("limit respected", len(keyword_search("kubernetes", limit=2)) == 2)
check("title-term match (no url overlap)",
      keyword_search("autoscaling")[0]["url"] == "https://p.test/hpa")

os.remove(_DB)
print("\nkeyword tests passed")
