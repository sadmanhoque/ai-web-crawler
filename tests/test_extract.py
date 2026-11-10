#!/usr/bin/env python3
"""Standalone tests for shared.extract date extraction + the missing-</head>
guard (no pytest needed):

    python3 tests/test_extract.py
"""
import os
import sys
from datetime import datetime, timezone
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.extract import extract_published, extract_sections, parse_dt

BODY = "<p>" + " ".join(["word"] * 30) + "</p>"


def check(name, cond, extra=None):
    assert cond, f"FAIL: {name} {'' if cond else extra}"
    print(f"ok  {name}")


def main():
    # --- parse_dt ---
    dt = parse_dt("2026-11-08T12:30:00Z")
    check("iso Z", dt == datetime(2026, 11, 8, 12, 30, tzinfo=timezone.utc), dt)
    dt = parse_dt("Tue, 08 Nov 2026 12:30:00 GMT")
    check("rfc822", dt == datetime(2026, 11, 8, 12, 30, tzinfo=timezone.utc), dt)
    dt = parse_dt("2026-11-08")
    check("naive assumed utc", dt == datetime(2026, 11, 8, tzinfo=timezone.utc), dt)
    check("garbage -> None", parse_dt("not a date") is None)
    check("empty -> None", parse_dt("") is None)

    # --- extract_published sources ---
    soup = BeautifulSoup(
        '<html><head><title>T</title>'
        '<meta property="article:published_time" content="2026-01-02T03:04:05Z">'
        f'<script type="application/ld+json">[{{"datePublished": "2020-01-01"}}]</script>'
        "</head><body><h1>H</h1>" + BODY + "</body></html>", "html.parser")
    check("meta wins", extract_published(soup) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))

    soup = BeautifulSoup(
        '<html><head><script type="application/ld+json">'
        '{"@graph": [{"@type": "NewsArticle", "datePublished": "2025-05-05T00:00:00Z"}]}'
        "</script></head><body><h1>H</h1>" + BODY + "</body></html>", "html.parser")
    check("json-ld @graph", extract_published(soup) == datetime(2025, 5, 5, tzinfo=timezone.utc))

    soup = BeautifulSoup(
        "<html><body><h1>H</h1><time datetime='2024-07-08T09:10:11Z'>x</time>" + BODY +
        "</body></html>", "html.parser")
    check("time tag", extract_published(soup) == datetime(2024, 7, 8, 9, 10, 11, tzinfo=timezone.utc))

    soup = BeautifulSoup("<html><body><h1>H</h1>" + BODY + "</body></html>", "html.parser")
    check("no date -> None", extract_published(soup) is None)

    # --- sections: normal page ---
    soup = BeautifulSoup("<html><head><title>T</title><script>var x=1;</script></head>"
                         "<body><h1>Heading</h1>" + BODY + "</body></html>", "html.parser")
    secs = extract_sections(soup)
    check("normal page keeps body", len(secs) == 1 and secs[0].heading == "Heading",
          [(s.heading, s.text[:30]) for s in secs])

    # --- sections: missing </head> must not eat the body ---
    soup = BeautifulSoup("<html><head><title>T</title>"
                         f"<meta property='article:published_time' content='2026-01-01T00:00:00Z'>"
                         "<body><h1>Heading</h1>" + BODY + "</body></html>", "html.parser")
    secs = extract_sections(soup)
    check("missing </head> keeps body", len(secs) == 1 and secs[0].heading == "Heading",
          [(s.heading, s.text[:30]) for s in secs])

    print("all extract tests passed")


if __name__ == "__main__":
    main()
