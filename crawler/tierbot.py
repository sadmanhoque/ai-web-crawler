"""Tier bot: scheduled heat evaluation for partner pages. Computes heat,
decides transitions via shared/tiers.py, publishes the transition messages
and keeps page_stats current. See docs/tiering.md (build order 4).

Runs as one thread inside the crawler app. tierctl's dry-run calls the same
evaluate_pass logic, so what the CLI shows is what the bot does.

Per transition (docs/tiering.md, "Service boundaries"):
  1. fetch the page — the archive text / re-embed sections must come from a
     live copy
  2. demote to t3: write page_archives BEFORE publishing the delete — a
     crash in between leaves the page both archived and indexed, which is
     harmless (the next pass re-demotes; every step is idempotent)
  3. publish DemoteMsg (t3: remove the points) or PromoteMsg (delete the
     old points, re-embed the provided sections at the new tier — also the
     mechanism for demotions that keep vectors, t0->t1 and t1->t2)
  4. update page_stats (tier, heat, last_tier_change), committed after the
     message is out; a crash before the commit just re-runs the move next
     pass
A failed fetch skips the transition: the page keeps its current tier, the
safe direction both ways (warm pages stay findable, cold pages stay cheap).
"""
import hashlib
import logging
import os
import threading
import requests
from bs4 import BeautifulSoup
from shared.config import load_profiles
from shared.extract import extract_headline, extract_sections
from shared.messages import DemoteMsg, PromoteMsg, Section, get_redis, publish
from shared.tiers import TIER_ORDER, Tier, heat, target_tier, tier_config_for
from .db import PageStat, Partner, SessionLocal, Site, archive_page, utcnow
from .fetch import TIMEOUT, UA

log = logging.getLogger("tierbot")
EVAL_INTERVAL_MIN = int(os.getenv("TIER_EVAL_MINUTES", "60"))


def _collection_for(partner: Partner) -> str:
    return partner.collection or load_profiles()[partner.profile].collection


def _fetch(url: str) -> tuple[str, str, list[Section]]:
    """(title, headline text, sections) for a live page. Raises on failure."""
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    resp.raise_for_status()
    if "text/html" not in resp.headers.get("content-type", "").lower():
        raise ValueError(f"not text/html: {url}")
    soup = BeautifulSoup(resp.text, "html.parser")
    title, headline = extract_headline(soup)      # before extract_sections mutates the soup
    sections = extract_sections(soup)
    if not any(s.text.strip() for s in sections):
        raise ValueError(f"no extractable sections: {url}")
    return title or url, headline or title, sections


def _sections_for(headline: str, title: str, sections: list[Section], to_tier: Tier) -> list[Section]:
    """What the target tier keeps (tier table in docs/tiering.md):
    t0 all sections, t1 the lede/summary section, t2 one stub
    (title + h1 + meta description, i.e. the extracted headline text)."""
    if to_tier is Tier.t0:
        return sections
    if to_tier is Tier.t1:
        return sections[:1]
    return [Section(heading=title, heading_tag="h1", text=headline)]


def transition_page(*, db, r, ps: PageStat, site: Site, partner: Partner,
                    to_tier: Tier, score: float, now=None) -> str:
    """Move one page to `to_tier` end to end: fetch, (archive), publish,
    page_stats update. The bot's decision is always one step; the manual CLI
    may jump several. Commits the stats update itself. Returns 'demoted',
    'promoted', 'hold' or 'fetch_failed'."""
    now = now or utcnow()
    cur = Tier(ps.tier)
    if to_tier is cur:
        ps.heat = score
        db.commit()
        return "hold"
    try:
        title, headline, sections = _fetch(ps.url)
    except Exception as e:
        log.warning("transition %s -> %s skipped (fetch failed: %s); keeping %s",
                    ps.url, to_tier.value, e, cur.value)
        ps.heat = score
        db.commit()
        return "fetch_failed"
    if to_tier is Tier.t3:
        text = "\n".join(s.text for s in sections)
        archive_page(ps.url, site.id, text,
                     hashlib.sha256(text.encode()).hexdigest())     # archive BEFORE delete
        publish(r, DemoteMsg(site_id=site.id, collection=_collection_for(partner),
                             url=ps.url, to_tier=to_tier))
    else:
        publish(r, PromoteMsg(site_id=site.id, domain=site.domain,
                              profile=partner.profile, collection=_collection_for(partner),
                              url=ps.url, title=title, to_tier=to_tier,
                              sections=_sections_for(headline, title, sections, to_tier)))
    ps.tier, ps.heat, ps.last_tier_change, ps.updated_at = to_tier.value, score, now, now
    db.commit()
    return "demoted" if TIER_ORDER.index(to_tier) > TIER_ORDER.index(cur) else "promoted"


def evaluate_pass(r, now=None, partner_domain: str | None = None) -> dict:
    """One scheduled pass over every page of every enabled partner: compute
    heat, decide, apply transitions. Also the engine behind tierctl dry-run
    (which inspects the same rows without applying)."""
    now = now or utcnow()
    stats = {"evaluated": 0, "hold": 0, "promoted": 0, "demoted": 0, "fetch_failed": 0}
    with SessionLocal() as db:
        q = (db.query(PageStat, Site, Partner)
             .join(Site, Site.id == PageStat.site_id)
             .join(Partner, Partner.site_id == Site.id)
             .filter(Partner.enabled))
        if partner_domain:
            q = q.filter(Partner.domain == partner_domain)
        for ps, site, p in q.all():
            stats["evaluated"] += 1
            cfg = tier_config_for(p)
            score = heat(ps.hits, ps.impressions, ps.last_accessed_at,
                         cfg.half_life_days, cfg.view_weight, cfg.impression_weight, now=now)
            current = Tier(ps.tier)
            target, reason = target_tier(score, current, cfg, ps.last_tier_change, now=now)
            ps.heat = score
            if target is current:
                stats["hold"] += 1
                continue
            result = transition_page(db=db, r=r, ps=ps, site=site, partner=p,
                                     to_tier=target, score=score, now=now)
            stats[result] += 1
            log.info("%s %s (%s)", ps.url, result, reason)
        db.commit()
    return stats


def tier_worker_loop(stop: threading.Event):
    log.info("tier bot started (eval every %d min)", EVAL_INTERVAL_MIN)
    while not stop.is_set():
        try:
            stats = evaluate_pass(get_redis())
            if stats["evaluated"]:
                log.info("tier pass: %s", stats)
        except Exception:
            log.exception("tier pass failed")
        stop.wait(EVAL_INTERVAL_MIN * 60)
