import time
import urllib.robotparser
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse
import requests
from bs4 import BeautifulSoup

UA = "LocalMind-Crawler/2.0 (private search tool)"
TIMEOUT = 15
SKIP_EXT = {".pdf", ".zip", ".tar", ".gz", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".mp4",
            ".mp3", ".woff", ".woff2", ".ttf", ".eot", ".css", ".js", ".xml", ".json"}
SKIP_PATTERNS = ["/cdn-cgi/", "/wp-admin/", "/wp-login", "?replytocom=", "?share=", "/feed/"]


def normalise(url: str) -> str:
    return urldefrag(url)[0].rstrip("/")


def in_scope(url: str, start: str) -> bool:
    u, b = urlparse(url), urlparse(start)
    return u.scheme in ("http", "https") and u.netloc == b.netloc and u.path.startswith(b.path.rstrip("/"))


def skippable(url: str) -> bool:
    return Path(urlparse(url).path).suffix.lower() in SKIP_EXT or any(p in url for p in SKIP_PATTERNS)


def load_robots(start: str) -> urllib.robotparser.RobotFileParser:
    p = urlparse(start)
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = requests.get(f"{p.scheme}://{p.netloc}/robots.txt", headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code in (401, 403):
            rp.disallow_all = True
        elif r.status_code == 200:
            rp.parse(r.text.splitlines())
        else:
            rp.allow_all = True
    except requests.RequestException:
        rp.allow_all = True
    return rp


@contextmanager
def requests_fetcher():
    s = requests.Session()
    s.headers["User-Agent"] = UA

    def get(url):
        try:
            r = s.get(url, timeout=TIMEOUT)
            ok = r.status_code == 200 and "text/html" in r.headers.get("content-type", "")
            return r.text if ok else None
        except requests.RequestException:
            return None
    try:
        yield get
    finally:
        s.close()


@contextmanager
def playwright_fetcher():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA)

        def get(url):
            try:
                resp = page.goto(url, timeout=30000, wait_until="networkidle")
                return page.content() if resp and resp.status == 200 else None
            except Exception:
                return None
        try:
            yield get
        finally:
            browser.close()


class SiteCrawler:
    """BFS within the start URL's host + path prefix. `complete` is True only if the queue
    was exhausted without truncation or excessive errors (safe to delete stale points)."""

    def __init__(self, site):
        self.site, self.complete = site, False

    def pages(self):
        start = normalise(self.site.start_url)
        rp = load_robots(start)
        queue, seen, fetched, errors = deque([start]), set(), 0, 0
        fetcher = playwright_fetcher if self.site.render_js else requests_fetcher
        with fetcher() as get:
            while queue and fetched < self.site.max_pages:
                url = queue.popleft()
                if url in seen or skippable(url) or not rp.can_fetch(UA, url):
                    continue
                seen.add(url)
                html = get(url)
                if html is None:
                    errors += 1
                    continue
                soup = BeautifulSoup(html, "html.parser")
                for a in soup.find_all("a", href=True):          # collect links BEFORE extraction strips nav
                    link = normalise(urljoin(url, a["href"].strip()))
                    if link not in seen and in_scope(link, start):
                        queue.append(link)
                fetched += 1
                yield url, soup
                time.sleep(self.site.delay_seconds)
        truncated = any(u not in seen for u in queue)
        self.complete = not truncated and errors <= max(3, 0.05 * (fetched + errors))
