"""Web search union sources: Google Custom Search JSON API and Bing Web
Search (search server side, docs/tiering.md build order: the CSE/Bing leg
that runs in parallel with Qdrant + the keyword fallback).

The providers are deliberately interchangeable — one WebHit shape, one
`web_search()` dispatcher — because which provider to standardize on is a
product decision, not a code one. Selection (env, or per-call):

    WEB_SEARCH_PROVIDER=google|bing        (default: google)
    CSE_API_KEY, CSE_CX                    Google: key + Programmable Search
                                           Engine ID (customsearch.googleapis.com)
    BING_API_KEY                           Bing Web Search API (v7)

A hard upstream failure (403/429/5xx) raises WebSearchError so the caller
can degrade gracefully (the other legs still answer the query); a missing
API key raises ValueError (misconfiguration, not a transient fault).
"""
import os
from dataclasses import dataclass

import requests

CSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
BING_ENDPOINT = "https://api.bing.microsoft.com/v7.0/search"
TIMEOUT = 10


@dataclass
class WebHit:
    title: str
    url: str
    snippet: str
    source: str                       # "google-cse" | "bing"


class WebSearchError(Exception):
    """Upstream provider failure (non-2xx)."""

    def __init__(self, provider: str, status: int, body: str):
        self.provider, self.status = provider, status
        super().__init__(f"{provider} returned {status}: {body[:200]}")


def _check(resp: requests.Response, provider: str) -> dict:
    if resp.status_code != 200:
        raise WebSearchError(provider, resp.status_code, resp.text)
    return resp.json()


def google_cse_search(query: str, *, api_key: str, cx: str,
                      num: int = 10, page: int = 1) -> list[WebHit]:
    """Custom Search JSON API v3. `cx` is the Programmable Search Engine ID;
    `num` per page (1-10)."""
    resp = requests.get(CSE_ENDPOINT, params={
        "key": api_key, "cx": cx, "q": query,
        "num": min(max(num, 1), 10), "page": page,
    }, timeout=TIMEOUT)
    data = _check(resp, "google")
    return [WebHit(title=i.get("title", ""), url=i.get("link", ""),
                   snippet=i.get("snippet", ""), source="google-cse")
            for i in data.get("items", [])]


def bing_search(query: str, *, api_key: str, count: int = 10,
                offset: int = 0) -> list[WebHit]:
    """Bing Web Search API v7. `count` per page (1-50)."""
    resp = requests.get(BING_ENDPOINT, params={
        "q": query, "count": min(max(count, 1), 50),
        "offset": max(offset, 0), "mkt": "en-US",
    }, headers={"Ocp-Apim-Subscription-Key": api_key}, timeout=TIMEOUT)
    data = _check(resp, "bing")
    return [WebHit(title=i.get("name", ""), url=i.get("webUrl", ""),
                   snippet=i.get("snippet", ""), source="bing")
            for i in data.get("webPages", {}).get("value", [])]


def web_search(query: str, *, provider: str | None = None,
               num: int = 10) -> list[WebHit]:
    """Dispatch on the requested (or WEB_SEARCH_PROVIDER) provider.
    Raises ValueError on an unknown provider or missing API key."""
    provider = (provider or os.getenv("WEB_SEARCH_PROVIDER", "google")).lower()
    if provider == "google":
        key, cx = os.getenv("CSE_API_KEY"), os.getenv("CSE_CX")
        if not (key and cx):
            raise ValueError("google web search needs CSE_API_KEY and CSE_CX")
        return google_cse_search(query, api_key=key, cx=cx, num=num)
    if provider == "bing":
        key = os.getenv("BING_API_KEY")
        if not key:
            raise ValueError("bing web search needs BING_API_KEY")
        return bing_search(query, api_key=key, count=num)
    raise ValueError(f"unknown web search provider {provider!r} (google|bing)")
