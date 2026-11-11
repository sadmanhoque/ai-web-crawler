"""Keyword fallback over the tiering DB (build order 7, docs/tiering.md).

t3 (vault) pages have no vectors — what remains is the page_stats metadata
row and the archived text in page_archives. This is how they stay findable:
a plain keyword pass over title, URL and archived text, complementary to
vector search (which only ever sees t0-t2). Pure query, one DB session, no
network — the search server calls this for its keyword fallback leg."""
import re
from crawler.db import PageArchive, PageStat, SessionLocal, Site

URL_WEIGHT = 3.0      # a term in the URL is a strong signal
TITLE_WEIGHT = 2.0    # a term in the (archived) title
TEXT_WEIGHT = 1.0     # a term in the archived body text
SNIPPET = 90


def _terms(query: str) -> list[str]:
    return [t for t in re.split(r"\W+", query.lower()) if len(t) >= 2]


def _score(*fields: str | None, terms: list[str], weights: tuple[float, ...]) -> float:
    """Per distinct matched term: URL > title > body weight."""
    score = 0.0
    for field, w in zip(fields, weights):
        if not field:
            continue
        low = field.lower()
        score += w * sum(1 for t in terms if t in low)
    return score


def _snippet(text: str, terms: list[str]) -> str:
    """~90 chars around the first term hit (flat, for result display)."""
    low = text.lower()
    pos = -1
    for t in terms:
        pos = low.find(t)
        if pos >= 0:
            break
    if pos < 0:
        return text[:SNIPPET].replace("\n", " ")
    start = max(0, pos - 30)
    s = text[start:start + SNIPPET].replace("\n", " ")
    return ("..." if start else "") + s + ("..." if start + SNIPPET < len(text) else "")


def _make_snippet(title: str | None, text: str | None, terms: list[str]) -> str | None:
    """Body snippet centered on the match; if the term hits the title only,
    the title IS the snippet."""
    if text and any(t in text.lower() for t in terms):
        return _snippet(text, terms)
    if title and any(t in title.lower() for t in terms):
        return title
    return _snippet(text, terms) if text else None


def keyword_search(query: str, *, partner: str | None = None,
                   limit: int = 20) -> list[dict]:
    """Keyword matches over indexed pages (page_stats, t0-t2) AND vaulted
    pages (page_stats at t3 and/or page_archives). A URL present in both
    tables is reported once: labelled by the page_stats tier (t3 -> vault,
    snippet from the archived copy; t0-t2 -> indexed). `partner` filters by
    site/partner domain. Sorted by score, then heat, then url."""
    terms = _terms(query)
    if not terms:
        return []
    with SessionLocal() as s:
        q = s.query(PageStat, Site).join(Site, Site.id == PageStat.site_id)
        if partner:
            q = q.filter(Site.domain == partner)
        stats = q.all()
        aq = s.query(PageArchive, Site).join(Site, Site.id == PageArchive.site_id)
        if partner:
            aq = aq.filter(Site.domain == partner)
        archives = aq.all()

    results: dict[str, dict] = {}
    arch_by_url = {a.url: (a, s) for a, s in archives}
    for ps, site in stats:
        arch_row = arch_by_url.get(ps.url, (None, site))[0]
        if ps.tier == "t3":
            # demoted: the vectors are gone; the archived copy is the content
            text = arch_row.text if arch_row else None
            title = (arch_row.title or ps.title) if arch_row else ps.title
            score = _score(ps.url, title, text, terms=terms,
                           weights=(URL_WEIGHT, TITLE_WEIGHT, TEXT_WEIGHT))
            if score <= 0:
                continue
            results[ps.url] = {
                "url": ps.url, "title": title, "tier": "t3",
                "heat": round(ps.heat, 3), "source": "vault", "domain": site.domain,
                "score": round(score, 3),
                **({"snippet": snip} if (snip := _make_snippet(title, text, terms)) else {}),
            }
        else:
            score = _score(ps.url, ps.title, None, terms=terms,
                           weights=(URL_WEIGHT, TITLE_WEIGHT, 0.0))
            if score <= 0:
                continue
            results[ps.url] = {
                "url": ps.url, "title": ps.title, "tier": ps.tier,
                "heat": round(ps.heat, 3), "source": "indexed", "domain": site.domain,
                "score": round(score, 3),
            }
    for arch, site in archives:
        if arch.url in results:            # the page_stats row already covers it
            continue
        score = _score(arch.url, arch.title, arch.text, terms=terms,
                       weights=(URL_WEIGHT, TITLE_WEIGHT, TEXT_WEIGHT))
        if score <= 0:
            continue
        snip = _make_snippet(arch.title, arch.text, terms)
        results[arch.url] = {
            "url": arch.url, "title": arch.title, "tier": "t3",
            "heat": 0.0, "source": "vault", "domain": site.domain,
            "score": round(score, 3),
            **({"snippet": snip} if snip else {}),
        }
    out = sorted(results.values(),
                 key=lambda r: (-r["score"], -r["heat"], r["url"]))
    return out[:limit]
