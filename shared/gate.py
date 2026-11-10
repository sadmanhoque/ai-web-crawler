"""Ingest gate: decides what happens to a partner page at discovery time.

    index   -> publish to the indexer stream (t0, depth full)
    archive -> straight to the vault (t3): no vectors, text in page_archives
    skip    -> not ingested at all (boilerplate, thin, unchanged, duplicate)

Pure function, shared by the feed watcher (build order 3) and the backfill
job (build order 6). DB lookups (known hash, duplicate-of) happen in the
caller; the gate itself is trivially unit-testable.
"""
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from pydantic import BaseModel
from .tiers import as_utc

# substring match on the lowercased URL; heuristic, same style as fetch.SKIP_PATTERNS
BOILERPLATE = ("/tag/", "/tags/", "/author/", "/authors/", "/category/", "/categories/",
               "/topic/", "/topics/", "/archive", "/search", "/tag-search", "/comments",
               "/replytocom", "/share/", "/wp-json", "/cdn-cgi", "/feed")
PAGINATION = re.compile(r"/page/\d+|/p/\d+(/|$)")


class GateDecision(BaseModel):
    action: str      # "index" | "archive" | "skip"
    reason: str


def ingest_gate(*, url: str, content_hash: str, published: datetime | None = None,
                words: int = 0, known_hash: str | None = None,
                duplicate_of: str | None = None, cutoff_days: int = 365,
                min_words: int | None = None, now: datetime | None = None) -> GateDecision:
    """Check order: unchanged -> boilerplate -> thin -> age -> duplicate.
    `min_words=None` disables the thin check (feed items are summaries by
    design; only full-page crawls get the floor). Unknown publication date
    is treated as fresh (safe default: index now, archive later via heat)."""
    now = as_utc(now) or datetime.now(timezone.utc)

    if known_hash and known_hash == content_hash:
        return GateDecision(action="skip", reason="unchanged")

    low = url.lower()
    if any(p in low for p in BOILERPLATE) or PAGINATION.search(urlparse(url).path.lower()):
        return GateDecision(action="skip", reason="boilerplate path")

    if min_words is not None and words < min_words:
        return GateDecision(action="skip", reason=f"too thin ({words} words < {min_words})")

    pub = as_utc(published)
    if pub is not None:
        age_days = (now - pub).total_seconds() / 86400
        if age_days > cutoff_days:
            return GateDecision(action="archive",
                                reason=f"age {age_days:.0f}d > cutoff {cutoff_days}d")

    if duplicate_of:
        return GateDecision(action="skip", reason=f"duplicate of {duplicate_of}")

    return GateDecision(action="index", reason="ok")
