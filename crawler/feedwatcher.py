"""Feed watcher: polls partner feeds (crawl / rss / api), runs each item through
the ingest gate, and either publishes it to the indexer stream (t0) or archives
it straight to the vault (t3). See docs/tiering.md.

Runs as extra threads inside the crawler app (FEED_WORKERS). It never
publishes CloseMsg: for partner pages, deletion is the tier bot's job, and a
'complete' crawl would wipe points the gate intentionally left unindexed.
Stale partner points (e.g. pages a partner removed) are reclaimed by heat
decay -> demote, not by crawl finalization.
"""
import hashlib
import logging
import os
import threading
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from sqlalchemy import or_, select
from shared.config import load_profiles
from shared.extract import extract_headline, extract_published, extract_sections, parse_dt
from shared.gate import ingest_gate
from shared.messages import Depth, PageMsg, Section, get_redis, publish
from .db import PageArchive, PageStat, Partner, SessionLocal, Site, archive_page, utcnow
from .fetch import SiteCrawler, TIMEOUT, UA
from .registry import upsert_site

log = logging.getLogger("feedwatcher")
LEASE = timedelta(minutes=10)        # a dead pod re-claims after this
GATE_MIN_WORDS = int(os.getenv("GATE_MIN_WORDS", "60"))   # thin floor, full-page crawls only


def claim_due_partner() -> Partner | None:
    with SessionLocal() as s:
        now = utcnow()
        p = s.execute(select(Partner).where(
            Partner.enabled,
            or_(Partner.last_poll_at.is_(None), Partner.last_poll_at <= now),
        ).order_by(Partner.last_poll_at.asc().nulls_first()).limit(1)
            .with_for_update(skip_locked=True)).scalar_one_or_none()
        if p:
            p.last_poll_at, p.last_status = now + LEASE, "polling"
            s.commit()
        return p


def _site_for(partner: Partner) -> Site:
    """Link the partner to its crawler Site row, creating it on first poll."""
    with SessionLocal() as s:
        p = s.get(Partner, partner.id)
        if p is None:
            raise LookupError(f"partner id {partner.id} not found")
        if p.site_id is None:
            site = upsert_site(s, p.domain,
                               create={"profile": p.profile, "source": "partner"})
            p.site_id = site.id
            s.commit()
            partner.site_id = p.site_id  # keep the caller's object in sync
        site = s.get(Site, p.site_id)
        assert site is not None
        return site


def _known_state(url: str) -> tuple[str | None, bool]:
    """(content_hash, already_archived) for dedup across polls."""
    with SessionLocal() as s:
        ps = s.get(PageStat, url)
        if ps:
            return ps.content_hash, False
        pa = s.get(PageArchive, url)
        return (pa.content_hash, True) if pa else (None, False)


def _duplicate_of(content_hash: str, url: str) -> str | None:
    """Another URL already holding this exact content (syndicated copies)."""
    with SessionLocal() as s:
        other = s.execute(select(PageStat.url).where(
            PageStat.content_hash == content_hash, PageStat.url != url).limit(1)
        ).scalar_one_or_none()
        if other:
            return other
        return s.execute(select(PageArchive.url).where(
            PageArchive.content_hash == content_hash, PageArchive.url != url).limit(1)
        ).scalar_one_or_none()


def _touch_page_stat(url: str, site_id: int, content_hash: str):
    """Immediate dedup marker: the page is 'known' from this poll on, even
    before the indexer or the first hit touches it."""
    with SessionLocal() as s:
        ps = s.get(PageStat, url) or PageStat(url=url, site_id=site_id, hits=0)
        ps.content_hash, ps.updated_at = content_hash, utcnow()
        s.merge(ps)
        s.commit()


def _finish(partner_id: int, status: str, success: bool):
    with SessionLocal() as s:
        p = s.get(Partner, partner_id)
        if p is None:                                # partner deleted mid-poll
            return
        p.last_status = status[:255]
        p.last_poll_at = utcnow() + (timedelta(minutes=p.poll_interval_minutes) if success
                                     else timedelta(minutes=5))
        s.commit()


def handle_item(*, partner: Partner, site: Site, r, url: str, title: str, text: str,
                published, min_words: int | None, sections: list[Section] | None = None) -> str:
    """Gate one discovered item and act. Returns the action for stats."""
    if not url or not text.strip():
        return "skip"
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    known_hash, already_archived = _known_state(url)
    if already_archived:
        return "skip"                                  # vaulted on an earlier poll
    words = len(text.split())
    dup = None if known_hash == content_hash else _duplicate_of(content_hash, url)
    d = ingest_gate(url=url, content_hash=content_hash, published=published, words=words,
                    known_hash=known_hash, duplicate_of=dup,
                    cutoff_days=partner.backfill_cutoff_days, min_words=min_words)
    if d.action == "index":
        publish(r, PageMsg(
            crawl_id=f"feed:{partner.id}:{uuid.uuid4().hex[:8]}", site_id=site.id,
            domain=site.domain, profile=partner.profile,
            collection=partner.collection or load_profiles()[partner.profile].collection,
            url=url, title=title or url, depth=Depth.full,
            sections=sections or [Section(heading=title or url, heading_tag="h1", text=text)]))
        _touch_page_stat(url, site.id, content_hash)
    elif d.action == "archive":
        archive_page(url, site.id, text, content_hash)
    log.debug("%s -> %s (%s)", url, d.action, d.reason)
    return d.action


# --- adapters ---------------------------------------------------------------

def _strip_html(s: str) -> str:
    return BeautifulSoup(s or "", "html.parser").get_text(" ", strip=True)


def parse_feed(root: ET.Element) -> list[tuple[str, str, str, datetime | None]]:
    """RSS 2.0 + Atom -> (url, title, text, published). Best effort; items
    without a usable link or body are dropped."""
    ns = {"atom": "http://www.w3.org/2005/Atom", "c": "http://purl.org/rss/1.0/modules/content/"}
    items: list[tuple[str, str, str, datetime | None]] = []
    if root.tag.endswith("rss"):
        for it in root.iter("item"):
            link = (it.findtext("link") or "").strip()
            body = it.findtext("c:encoded", namespaces=ns) or it.findtext("description") or ""
            items.append((link, it.findtext("title") or "", _strip_html(body),
                          parse_dt(it.findtext("pubDate") or "")))
    else:
        for it in root.findall("atom:entry", ns):
            link_el = it.find("atom:link[@rel='alternate']", ns) or it.find("atom:link", ns)
            body = (it.findtext("atom:content", namespaces=ns)
                    or it.findtext("atom:summary", namespaces=ns) or "")
            pub = (it.findtext("atom:published", namespaces=ns)
                   or it.findtext("atom:updated", namespaces=ns) or "")
            items.append(((link_el.get("href") if link_el is not None else "") or "",
                          it.findtext("atom:title", namespaces=ns) or "", _strip_html(body),
                          parse_dt(pub)))
    return [(u, t, x, p) for u, t, x, p in items if u and x.strip()]


def poll_crawl(partner: Partner, site: Site, r) -> dict:
    """BFS the partner site; every page goes through the gate with the thin
    floor (full text is available). New/changed pages index at t0, old ones
    archive to t3, boilerplate is skipped."""
    stats = {"index": 0, "archive": 0, "skip": 0}
    crawler = SiteCrawler(site)
    for url, soup in crawler.pages():
        title, _ = extract_headline(soup)              # before extract_sections (mutates soup)
        published = extract_published(soup)            #  "  "
        sections = [x for x in extract_sections(soup) if x.text.strip()]
        if not sections:
            stats["skip"] += 1
            continue
        stats[handle_item(partner=partner, site=site, r=r, url=url, title=title or url,
                          text="\n".join(x.text for x in sections), published=published,
                          min_words=GATE_MIN_WORDS, sections=sections)] += 1
    return stats


def poll_rss(partner: Partner, site: Site, r) -> dict:
    """RSS/Atom feed. No thin check: feed items are summaries by design, and
    the text they carry is what gets indexed (full-text fetch on demand is the
    tier bot's reactivation job)."""
    if not partner.feed_url:
        raise ValueError(f"partner {partner.domain} has no feed_url for rss")
    resp = requests.get(partner.feed_url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    resp.raise_for_status()
    stats = {"index": 0, "archive": 0, "skip": 0}
    for url, title, text, pub in parse_feed(ET.fromstring(resp.content)):
        stats[handle_item(partner=partner, site=site, r=r, url=url, title=title,
                          text=text, published=pub, min_words=None)] += 1
    return stats


def poll_api(partner: Partner, site: Site, r) -> dict:
    """Generic JSON feed: a list of items, or {"items": [...]}. Per item:
    url|link, title, published|publishedAt|pubDate, content|summary|description."""
    if not partner.feed_url:
        raise ValueError(f"partner {partner.domain} has no feed_url for api")
    resp = requests.get(partner.feed_url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("API feed must be a list (or an object with an 'items' list)")
    stats = {"index": 0, "archive": 0, "skip": 0}
    for it in items:
        if not isinstance(it, dict):
            continue
        url = str(it.get("url") or it.get("link") or "").strip()
        text = _strip_html(str(it.get("content") or it.get("summary")
                               or it.get("description") or ""))
        if not url or not text:
            stats["skip"] += 1
            continue
        pub_raw = it.get("published") or it.get("publishedAt") or it.get("pubDate")
        stats[handle_item(partner=partner, site=site, r=r, url=url,
                          title=str(it.get("title") or url), text=text,
                          published=parse_dt(str(pub_raw)) if pub_raw else None,
                          min_words=None)] += 1
    return stats


ADAPTERS = {"crawl": poll_crawl, "rss": poll_rss, "api": poll_api}


def poll_partner(partner: Partner) -> str:
    site = _site_for(partner)
    r = get_redis()
    stats = ADAPTERS[partner.feed_type](partner, site, r)
    return f"poll ok: {stats}"


def feed_worker_loop(stop: threading.Event):
    while not stop.is_set():
        p = claim_due_partner()
        if not p:
            stop.wait(15)
            continue
        try:
            log.info("polling partner %s (%s)", p.domain, p.feed_type)
            _finish(p.id, poll_partner(p), success=True)
        except Exception as e:
            log.exception("partner poll failed: %s", p.domain)
            _finish(p.id, f"error: {e}", success=False)
