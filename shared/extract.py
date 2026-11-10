"""HTML -> sections. Ported from ingest.py (hidden-text filter kept).
Fix vs. the original: <title>/meta are read BEFORE <head> is decomposed
(the original removed <head> first, so page_title always fell back to the URL)."""
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup
from .messages import Section

NAMED = {"white": (255, 255, 255), "black": (0, 0, 0), "red": (255, 0, 0), "green": (0, 128, 0),
         "blue": (0, 0, 255), "yellow": (255, 255, 0), "orange": (255, 165, 0),
         "purple": (128, 0, 128), "pink": (255, 192, 203), "grey": (128, 128, 128),
         "gray": (128, 128, 128), "silver": (192, 192, 192)}


def parse_colour(v):
    if not v:
        return None
    v = v.strip().lower()
    if v in NAMED:
        return NAMED[v]
    if re.match(r'^#[0-9a-f]{3}$', v):
        return tuple(int(c * 2, 16) for c in v[1:4])
    if re.match(r'^#[0-9a-f]{6,8}$', v):
        return int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16)
    m = re.match(r'^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)$', v)
    if m:
        if m.group(4) is not None and float(m.group(4)) < 0.05:
            return None
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


def parse_inline_style(style):
    out = {}
    for decl in (style or "").split(";"):
        if ":" in decl:
            k, _, val = decl.partition(":")
            out[k.strip().lower()] = val.strip().lower()
    return out


def build_css_rules(soup):
    rules = []
    for tag in soup.find_all("style"):
        css = re.sub(r'/\*.*?\*/', '', tag.get_text(), flags=re.DOTALL)
        for m in re.finditer(r'([^{]+)\{([^}]*)\}', css):
            props = parse_inline_style(m.group(2))
            if props:
                rules.append((m.group(1).strip(), props))
    return rules


def _colours(el, rules):
    combined = {}
    name = el.name or ""
    for sel, props in rules:
        if sel == name:
            combined.update(props)
    for cls in el.get("class", []):
        for sel, props in rules:
            if sel in (f".{cls}", f"{name}.{cls}"):
                combined.update(props)
    combined.update(parse_inline_style(el.get("style", "")))
    return (parse_colour(combined.get("color", "")),
            parse_colour(combined.get("background-color") or combined.get("background", "")))


def is_hidden_text(el, rules) -> bool:
    for e in [el, *el.parents]:
        if not hasattr(e, "get"):
            continue
        st = parse_inline_style(e.get("style", ""))
        if st.get("display") == "none" or st.get("visibility") == "hidden":
            return True
        if e.has_attr("hidden") or e.get("aria-hidden") == "true":
            return True
    fg, bg = _colours(el, rules)
    return (fg is not None and fg == bg) or (fg == (255, 255, 255) and bg is None)


def extract_headline(soup: BeautifulSoup) -> tuple[str, str]:
    """(title, headline text). Call BEFORE extract_sections (which mutates the soup)."""
    t = soup.find("title")
    title = t.get_text(strip=True) if t else ""
    h1 = soup.find("h1")
    h1 = h1.get_text(strip=True) if h1 else ""
    meta = soup.find("meta", attrs={"name": "description"})
    desc = (meta.get("content") or "").strip() if meta else ""
    text = ". ".join(dict.fromkeys(p for p in (title, h1, desc) if p))
    return title or h1, text


def extract_published(soup: BeautifulSoup) -> datetime | None:
    """Best-effort publication date: meta tags, then JSON-LD, then <time datetime>.
    Call BEFORE extract_sections (which decomposes <head>/scripts). None => unknown."""
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or "").lower()
        if key in ("article:published_time", "og:article:published_time", "pubdate",
                   "publishdate", "parsely-pub-date", "dc.date", "dc.date.issued"):
            dt = parse_dt(str(tag.get("content") or ""))
            if dt:
                return dt
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            data = data.get("@graph", [data])
        for node in data if isinstance(data, list) else [data]:
            if isinstance(node, dict) and node.get("datePublished"):
                dt = parse_dt(str(node["datePublished"]))
                if dt:
                    return dt
    t = soup.find("time", attrs={"datetime": True})
    return parse_dt(str(t["datetime"])) if t else None


def parse_dt(v: str) -> datetime | None:
    """ISO 8601 or RFC 822 -> aware datetime (naive assumed UTC), else None."""
    v = (v or "").strip()
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(v)
        except (TypeError, ValueError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def extract_sections(soup: BeautifulSoup) -> list[Section]:
    head, body = soup.find("head"), soup.find("body")
    if head is not None and body is not None and body.find_parent("head") is head:
        # Missing </head>: html.parser nested <body> inside <head> (browsers
        # close it implicitly, this parser does not). Decomposing head would
        # drop the whole article, so strip only the actual head elements.
        for el in head.find_all(["title", "meta", "script", "style", "link"]):
            el.decompose()
    else:
        for tag in soup(["script", "style", "nav", "footer", "head", "aside", "noscript"]):
            tag.decompose()
    rules = build_css_rules(soup)
    sections, buf = [], []
    heading, htag = "Introduction", "h1"
    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th",
                             "dt", "dd", "blockquote"]):
        if is_hidden_text(el, rules):
            continue
        if el.name.startswith("h"):
            if buf:
                sections.append(Section(heading=heading, heading_tag=htag, text=" ".join(buf)))
                buf = []
            heading, htag = el.get_text(strip=True) or heading, el.name
        else:
            t = el.get_text(strip=True)
            if t:
                buf.append(t)
    if buf:
        sections.append(Section(heading=heading, heading_tag=htag, text=" ".join(buf)))
    return sections
