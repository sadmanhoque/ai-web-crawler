"""Claims due sites (FOR UPDATE SKIP LOCKED => safe with N replicas), crawls them,
decides depth per page, and publishes pages to the indexer stream."""
import hashlib
import logging
import threading
import uuid
from datetime import timedelta
from sqlalchemy import func, select
from shared.config import load_profiles
from shared.extract import extract_headline, extract_sections
from shared.messages import CloseMsg, Depth, PageMsg, Section, get_redis, publish
from . import policy
from .db import PageStat, SessionLocal, Site, utcnow
from .fetch import SiteCrawler
from .registry import compute_next
from .summarize import summarize

log = logging.getLogger("scheduler")
LEASE = timedelta(hours=2)      # if a pod dies mid-crawl, the site is retried after the lease


def claim_due() -> Site | None:
    with SessionLocal() as s:
        now = utcnow()
        site = s.execute(select(Site).where(Site.enabled, Site.next_run_at <= now)
                         .order_by(Site.next_run_at).limit(1).with_for_update(skip_locked=True)
                         ).scalar_one_or_none()
        if site:
            site.next_run_at, site.last_status = now + LEASE, "running"
            s.commit()
        return site


def _refresh_score(site: Site):
    with SessionLocal() as s:
        total = s.execute(select(func.coalesce(func.sum(PageStat.hits), 0))
                          .where(PageStat.site_id == site.id)).scalar_one()
        site.visit_score = max(policy.rank_score(site.rank), policy.hit_score(total))
        s.merge(site)
        s.commit()


def _page_stat(url: str) -> tuple[int, str | None, str | None]:
    with SessionLocal() as s:
        ps = s.get(PageStat, url)
        return (ps.hits, ps.content_hash, ps.summary) if ps else (0, None, None)


def _save_summary(url: str, site_id: int, h: str, summary: str):
    with SessionLocal() as s:
        ps = s.get(PageStat, url) or PageStat(url=url, site_id=site_id, hits=0)
        ps.content_hash, ps.summary, ps.updated_at = h, summary, utcnow()
        s.merge(ps)
        s.commit()


def run_site(site: Site):
    prof = load_profiles()[site.profile]
    collection = site.collection_override or prof.collection
    _refresh_score(site)
    crawl_id, total, r = uuid.uuid4().hex, 0, get_redis()
    crawler = SiteCrawler(site)
    for url, soup in crawler.pages():
        hits, old_hash, old_summary = _page_stat(url)
        depth = policy.page_depth(site, url, hits)
        title, headline = extract_headline(soup)            # must run before extract_sections
        if depth == Depth.headline:
            sections = [Section(heading=title or url, heading_tag="h1", text=headline)]
        else:
            sections = extract_sections(soup)
            if depth == Depth.summary:
                body = "\n".join(x.text for x in sections)
                h = hashlib.sha256(body.encode()).hexdigest()
                summ = old_summary if (h == old_hash and old_summary) else summarize(title, body)
                if h != old_hash:
                    _save_summary(url, site.id, h, summ)
                sections = [Section(heading=title or url, heading_tag="h1", text=summ)]
        sections = [x for x in sections if x.text.strip()]
        if not sections:
            continue
        publish(r, PageMsg(crawl_id=crawl_id, site_id=site.id, domain=site.domain, profile=site.profile,
                           collection=collection, url=url, title=title or url, depth=depth,
                           sections=sections))
        total += 1
    publish(r, CloseMsg(crawl_id=crawl_id, site_id=site.id, collection=collection, total=total,
                        complete=crawler.complete and total > 0))
    _finish(site.id, f"ok: {total} pages, complete={crawler.complete}", success=True)


def _finish(site_id: int, status: str, success: bool):
    with SessionLocal() as s:
        site = s.get(Site, site_id)
        now = utcnow()
        site.last_status = status[:255]
        if success:
            site.last_run_at, site.next_run_at = now, compute_next(site, now)
        else:
            site.next_run_at = now + timedelta(hours=1)       # retry in an hour
        s.commit()


def worker_loop(stop: threading.Event):
    while not stop.is_set():
        site = claim_due()
        if not site:
            stop.wait(30)
            continue
        try:
            log.info("crawling %s", site.domain)
            run_site(site)
        except Exception as e:
            log.exception("crawl failed: %s", site.domain)
            _finish(site.id, f"error: {e}", success=False)
