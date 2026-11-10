#!/usr/bin/env python3
"""Standalone tests for shared.gate (no pytest needed):

    python3 tests/test_gate.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.gate import GateDecision, ingest_gate

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)
H = "abc123"


def check(name, actual, expected):
    assert actual == expected, f"{name}: expected {expected!r}, got {actual!r}"
    print(f"ok  {name}")


def main():
    g = lambda **kw: ingest_gate(now=NOW, **kw)

    # --- default: index ---
    d = g(url="https://x.test/news/story", content_hash=H, published=NOW - timedelta(days=1),
          words=800)
    check("fresh page indexes", d, GateDecision(action="index", reason="ok"))

    # --- unchanged (known hash matches) ---
    d = g(url="https://x.test/news/story", content_hash=H, known_hash=H,
          published=NOW - timedelta(days=1), words=800)
    check("unchanged skips", d.action, "skip")

    # changed content re-indexes even if old
    d = g(url="https://x.test/news/story", content_hash="new", known_hash=H,
          published=NOW - timedelta(days=1), words=800)
    check("changed content re-indexes", d.action, "index")

    # --- boilerplate paths ---
    for url in ("https://x.test/tag/kubernetes", "https://x.test/author/jane",
                "https://x.test/2020/01/page/2", "https://x.test/search?q=pods",
                "https://x.test/category/engineering"):
        d = g(url=url, content_hash=H, words=800)
        check(f"boilerplate skips: {url.split('.test')[1]}", d.action, "skip")

    # a content path that merely *contains* a keyword still indexes
    d = g(url="https://x.test/homepage-of-the-day", content_hash=H, words=800)
    check("non-boilerplate 'page' path indexes", d.action, "index")

    # --- thin pages (crawl floor) ---
    d = g(url="https://x.test/news/stub", content_hash=H, words=20, min_words=60)
    check("thin page skips", d.action, "skip")
    d = g(url="https://x.test/news/stub", content_hash=H, words=20, min_words=None)
    check("feed items skip the thin check", d.action, "index")

    # --- age cutoff ---
    d = g(url="https://x.test/news/old", content_hash=H, published=NOW - timedelta(days=400),
          words=800, cutoff_days=365)
    check("older than cutoff archives", d.action, "archive")
    d = g(url="https://x.test/news/old", content_hash=H, published=NOW - timedelta(days=364),
          words=800, cutoff_days=365)
    check("just under cutoff indexes", d.action, "index")

    # --- unknown publication date is safe (index) ---
    d = g(url="https://x.test/news/undated", content_hash=H, published=None, words=800)
    check("unknown date indexes", d.action, "index")

    # naive published datetime (e.g. from a feed parser) does not crash
    d = g(url="https://x.test/news/naive", content_hash=H,
          published=datetime(2025, 1, 1), words=800)
    check("naive date archives by age", d.action, "archive")

    # --- duplicates (syndication) ---
    d = g(url="https://x.test/news/copy", content_hash=H, words=800,
          duplicate_of="https://y.test/original")
    check("duplicate skips", d.action, "skip")

    print("all gate tests passed")


if __name__ == "__main__":
    main()
