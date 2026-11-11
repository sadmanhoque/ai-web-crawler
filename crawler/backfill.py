"""Backfill: one-shot onboarding crawl for a new partner (build order 6).

The partner row is created manually (curated set); this job crawls the whole
site once, through the exact same ingest gate the feed watcher uses
(crawler/feedwatcher.poll_crawl -> shared/gate.ingest_gate):

  * recent pages  -> PageMsg at t0 (indexed)
  * old pages     -> straight to the t3 vault (metadata + archived text),
                     older than the partner's backfill_cutoff_days
  * thin / boilerplate / duplicate pages -> skipped (never indexed)

Idempotent: the gate's unchanged-hash / duplicate / already-archived checks
make a re-run a no-op, so it is safe to re-point a partner and crawl again.
When the crawl finishes the feed watcher takes over: the partner's poll
clock is seeded to now, so it does not immediately re-crawl the site.

Usage (crawler side, indexer + redis running):
    python -m crawler.backfill --domain example.com [--max-pages N]
"""
import argparse
import logging
import sys

from shared.messages import get_redis
from .db import Partner, SessionLocal, utcnow
from .feedwatcher import GATE_MIN_WORDS, _site_for, poll_crawl

log = logging.getLogger("backfill")


def run_backfill(domain: str, r, *, max_pages: int | None = None) -> dict:
    """Crawl one partner's site once, through the ingest gate.
    Returns gate stats {"index": n, "archive": n, "skip": n}."""
    with SessionLocal() as s:
        p = s.query(Partner).filter(Partner.domain == domain).first()
        if p is None or not p.enabled:
            raise LookupError(f"no enabled partner for domain {domain!r} "
                              f"(onboard the partner row first)")
    site = _site_for(p)
    if max_pages:
        site.max_pages = max_pages
    log.info("backfill %s: crawling up to %d pages (thin floor %d words, "
             "cutoff %dd)", domain, site.max_pages, GATE_MIN_WORDS,
             p.backfill_cutoff_days)
    stats = poll_crawl(p, site, r)
    with SessionLocal() as s:
        p2 = s.get(Partner, p.id)
        if p2 is not None:                  # partner deleted mid-crawl
            p2.last_poll_at = utcnow()      # feed watcher takes over from now
            p2.last_status = f"backfill ok: {stats}"[:255]
            s.commit()
    return stats


def main():
    ap = argparse.ArgumentParser(
        description="one-shot onboarding crawl for a partner (build order 6)")
    ap.add_argument("--domain", required=True, help="partner domain")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="cap on pages fetched (default: the site's max_pages)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        stats = run_backfill(args.domain, get_redis(), max_pages=args.max_pages)
    except LookupError as e:
        sys.exit(str(e))
    print(f"backfill {args.domain}: {stats}")
    print("feed watcher takes over from here (last_poll_at seeded)")


if __name__ == "__main__":
    main()
