#!/usr/bin/env python3
"""Smoke-test fixture partner: a local HTTP server whose pages have
CONTROLLED publication dates and content, so the ingest gate and the tier
bot can be driven through every path deterministically. Used by
docs/smoke-testing.md.

    python3 scripts/smoke_server.py            # http://127.0.0.1:8801
    python3 scripts/smoke_server.py --port 9000

Pages (dates relative to today):
    /           landing, links to every article (the crawl adapter's entry)
    /hot        published today, long article        -> gate: index at t0
    /fresh      published today, second long article -> gate: index at t0
    /warm       published 30 days ago, long article  -> gate: index at t0
    /old        published 700 days ago               -> gate: straight to t3
                                                        (older than the default
                                                         backfill_cutoff_days=365)
    /thin       published today, ~25 words           -> gate: skip (thin floor 60)
    /tag/legacy boilerplate path                     -> gate: skip (path rule)
    /feed       RSS of hot/warm/old                  -> for feed_type=rss polls

Every page has a unique body and title (the dedupe hash gate would skip
identical bodies), and an article:published_time meta tag that
shared/extract.extract_published reads.
"""
import argparse
import socketserver
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from xml.sax.saxutils import escape

NOW = datetime.now(timezone.utc)
DAY = timedelta(days=1)

# (slug, published-offset-days, title, body sentences) — content pages stay
# above the 60-word thin floor; /thin stays deliberately below it.
PAGES = {
    "hot": (0, "Smoke hot article",
            ["Alpha orchard harvests run on a strict six-week cycle and the "
             "smoke fixture records every step of the procedure so a failed "
             "run can be replayed from the exact same starting state.",
             "Workers prune the alpha orchard rows before dawn, then grade "
             "each crate of produce against the season's target density and "
             "log the result before the crates are moved to storage.",
             "The alpha orchard ledger balances water, labor and frost risk "
             "against the shipping calendar so the harvest window never "
             "slips past the third week of the cycle."]),
    "fresh": (0, "Smoke fresh article",
              ["Bravo refinery pressure tests happen on a rotating shift and "
               "the smoke fixture captures the full procedure from the first "
               "handover note to the final sign-off on the test record.",
              "Each bravo valve is torque-checked, tagged and re-verified by "
               "a second engineer before the line is pressurized again, and "
               "every reading is stamped with the engineer's badge number.",
              "The bravo test record keeps every gauge reading so a failed "
               "cycle can be replayed exactly in the next maintenance "
               "window without opening the line a second time."]),
    "warm": (30, "Smoke warm article",
             ["Charlie archive rooms stay at constant humidity and the smoke "
              "fixture documents the whole conservation routine from the "
              "morning walkthrough to the evening humidity check.",
             "Technicians walk the charlie stacks every morning, logging "
              "temperature deltas on each shelf before opening any crate, "
              "and flag any shelf that drifts past the tolerance band.",
             "The charlie conservation log cross-references humidity, light "
              "exposure and handling counts for every stored item so a "
              "declining binding can be caught months before it fails."]),
    "old": (700, "Smoke old article",
            ["Delta rail surveys were first recorded a couple of years ago "
             "and the smoke fixture re-serves them verbatim, exactly as they "
             "were written down in the original field book.",
            "Surveyors walked the delta line with hand levels, sketching "
            "every joint, sleeper and bridge in a paper field book, and the "
            "measurements were copied twice before they were trusted.",
            "The delta record stayed untouched for two years, which is "
            "exactly the age that sends a page straight to the vault during "
            "the onboarding backfill instead of into the vector index."]),
    "thin": (0, "Smoke thin article",
             ["Echo note. Very short on purpose."]),
}

LANDING = """<html><head><title>Smoke fixture partner</title></head>
<body><h1>Smoke fixture partner</h1>
<ul>
<li><a href="/hot">Smoke hot article</a></li>
<li><a href="/fresh">Smoke fresh article</a></li>
<li><a href="/warm">Smoke warm article</a></li>
<li><a href="/old">Smoke old article</a></li>
<li><a href="/thin">Smoke thin article</a></li>
<li><a href="/tag/legacy">legacy tag page</a></li>
</ul></body></html>"""

BOILERPLATE = """<html><head><title>Legacy tags</title></head>
<body><h1>Legacy tag index</h1><p>Tag listing page: alpha, bravo, charlie,
delta, echo. This path matches the gate's boilerplate rule, so it must
never be indexed.</p></body></html>"""


def page_html(slug: str) -> str:
    offset, title, paras = PAGES[slug]
    pub = NOW - offset * DAY
    body = "".join(f"<p>{p}</p>" for p in paras)
    return (f"<html><head><title>{title}</title>"
            f'<meta property="article:published_time" content="{pub:%Y-%m-%dT%H:%M:%S+00:00}">'
            f"</head><body><h1>{title}</h1>{body}</body></html>")


def rss() -> str:
    items = []
    for slug in ("hot", "warm", "old"):
        offset, title, _ = PAGES[slug]
        pub = NOW - offset * DAY
        items.append(
            f"  <item><title>{escape(title)}</title>"
            f"<link>http://{HOST}:{PORT}/{slug}</link>"
            f"<pubDate>{pub.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
            f"<description>{escape(' '.join(PAGES[slug][2])[:100])}</description></item>")
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0"><channel>'
            f"<title>Smoke fixture partner</title>"
            f"<link>http://{HOST}:{PORT}/</link>"
            f"<description>fixture feed</description>{''.join(items)}</channel></rss>")


HOST = "127.0.0.1"
PORT = 8801


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/":
            body, ctype = LANDING, "text/html"
        elif path == "/feed":
            body, ctype = rss(), "application/rss+xml"
        elif path.startswith("/tag/"):
            body, ctype = BOILERPLATE, "text/html"
        elif path.lstrip("/") in PAGES:
            body, ctype = page_html(path.lstrip("/")), "text/html"
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("content-type", f"{ctype}; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        print(f"[smoke-server] {format % args}")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8801)
    args = ap.parse_args()
    PORT = args.port
    print(f"serving the fixture partner on http://{HOST}:{PORT} (Ctrl-C to stop)")
    print(f"  /hot /fresh (today)  /warm (30d)  /old (700d)  /thin  /tag/legacy  /feed")
    try:
        Server((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
