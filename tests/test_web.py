#!/usr/bin/env python3
"""Standalone tests for the CSE/Bing web search leg (search/web.py,
build order: the union source alongside Qdrant + keyword fallback).
No network: requests.get is stubbed; both providers' request construction
and response parsing are verified, plus dispatch and failure paths:

    python3 tests/test_web.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import search.web as web

CALLS = []


class FakeResp:
    def __init__(self, payload, status=200, text=""):
        self._payload, self.status_code, self.text = payload, status, text

    def json(self):
        return self._payload


def fake_get(url, params=None, headers=None, timeout=None):
    CALLS.append({"url": url, "params": params or {}, "headers": headers or {}})
    if isinstance(_NEXT, Exception):
        raise _NEXT
    return _NEXT


_NEXT = None
web.requests.get = fake_get          # no network: every call is captured + replayed


def check(name, cond, extra=None):
    assert cond, f"FAIL: {name} {'' if cond else extra}"
    print(f"ok  {name}")


CSE_PAYLOAD = {"items": [
    {"title": "Kubernetes Docs", "link": "https://k8s.io/docs", "snippet": "Run containers"},
    {"title": "Helm", "link": "https://helm.sh", "snippet": "Kubernetes package manager"},
]}
BING_PAYLOAD = {"webPages": {"value": [
    {"name": "K8s in Practice", "webUrl": "https://example.com/k8s", "snippet": "practical k8s"},
]}}

# --- google CSE: request construction + parsing -----------------------------
CALLS.clear()
_NEXT = FakeResp(CSE_PAYLOAD)
hits = web.google_cse_search("kubernetes", api_key="key123", cx="abc:def", num=10)
check("cse: hits parsed into the shared WebHit shape",
      len(hits) == 2 and hits[0].title == "Kubernetes Docs"
      and hits[0].url == "https://k8s.io/docs" and hits[0].source == "google-cse", hits)
c = CALLS[0]
check("cse: hit the CSE endpoint with key/cx/q/num/page params",
      c["url"] == web.CSE_ENDPOINT
      and c["params"] == {"key": "key123", "cx": "abc:def", "q": "kubernetes",
                          "num": 10, "page": 1}, c)
check("cse: no auth header (key travels as a param)", "Ocp-Apim-Subscription-Key" not in c["headers"])

# --- bing: request construction + parsing -----------------------------------
_NEXT = FakeResp(BING_PAYLOAD)
hits = web.bing_search("kubernetes", api_key="bkey", count=5, offset=10)
check("bing: hits parsed (name/webUrl/snippet -> title/url/snippet)",
      len(hits) == 1 and hits[0].title == "K8s in Practice"
      and hits[0].url == "https://example.com/k8s" and hits[0].source == "bing", hits)
c = CALLS[-1]
check("bing: hit the v7 endpoint with the subscription header",
      c["url"] == web.BING_ENDPOINT
      and c["headers"]["Ocp-Apim-Subscription-Key"] == "bkey"
      and c["params"]["q"] == "kubernetes" and c["params"]["count"] == 5
      and c["params"]["offset"] == 10, c)

# --- pagination bounds -------------------------------------------------------
_NEXT = FakeResp({"items": []})
web.google_cse_search("x", api_key="k", cx="c", num=99)
check("cse: num clamped to 10", CALLS[-1]["params"]["num"] == 10, CALLS[-1])
_NEXT = FakeResp({"webPages": {"value": []}})
web.bing_search("x", api_key="k", count=999)
check("bing: count clamped to 50", CALLS[-1]["params"]["count"] == 50, CALLS[-1])
web.bing_search("x", api_key="k", offset=-5)
check("bing: negative offset clamped to 0", CALLS[-1]["params"]["offset"] == 0, CALLS[-1])

# --- error handling ----------------------------------------------------------
_NEXT = FakeResp({"error": {"code": 429}}, status=429, text="resource exhausted")
try:
    web.google_cse_search("x", api_key="k", cx="c")
    check("cse: 429 raises WebSearchError", False)
except web.WebSearchError as e:
    check("cse: 429 raises WebSearchError(provider, status)",
          e.provider == "google" and e.status == 429 and "resource exhausted" in str(e), str(e))
_NEXT = FakeResp({"error": "Forbidden"}, status=403, text="access not valid for this project")
try:
    web.bing_search("x", api_key="k")
    check("bing: 403 raises WebSearchError", False)
except web.WebSearchError as e:
    check("bing: 403 raises WebSearchError(provider, status)",
          e.provider == "bing" and e.status == 403, str(e))

# --- dispatch: WEB_SEARCH_PROVIDER + keys ------------------------------------
os.environ["WEB_SEARCH_PROVIDER"] = "bing"
os.environ["BING_API_KEY"] = "bkey"
os.environ.pop("CSE_API_KEY", None)
os.environ.pop("CSE_CX", None)
_NEXT = FakeResp(BING_PAYLOAD)
hits = web.web_search("kubernetes")
check("dispatch: env provider bing used, bing key read from env",
      len(hits) == 1 and CALLS[-1]["url"] == web.BING_ENDPOINT, CALLS[-1])

os.environ["WEB_SEARCH_PROVIDER"] = "google"
os.environ["CSE_API_KEY"] = "gkey"
os.environ["CSE_CX"] = "cx1"
os.environ["BING_API_KEY"] = ""        # bing misconfigured
_NEXT = FakeResp(CSE_PAYLOAD)
try:
    web.web_search("kubernetes", provider="bing")
    check("dispatch: bing without key -> ValueError", False)
except ValueError as e:
    check("dispatch: bing without key -> ValueError", "BING_API_KEY" in str(e), str(e))
try:
    web.web_search("kubernetes", provider="duckduckgo")
    check("dispatch: unknown provider -> ValueError", False)
except ValueError as e:
    check("dispatch: unknown provider -> ValueError", "duckduckgo" in str(e), str(e))
os.environ["CSE_API_KEY"] = ""
try:
    web.web_search("kubernetes")
    check("dispatch: google without keys -> ValueError", False)
except ValueError as e:
    check("dispatch: google without keys -> ValueError", "CSE_API_KEY" in str(e), str(e))
os.environ["CSE_API_KEY"] = "gkey"
_NEXT = FakeResp(CSE_PAYLOAD)
hits = web.web_search("kubernetes")
check("dispatch: google with keys parses items", len(hits) == 2, hits)

# --- empty results ------------------------------------------------------------
_NEXT = FakeResp({"items": []})
check("cse: empty items -> []", web.google_cse_search("x", api_key="k", cx="c") == [])
_NEXT = FakeResp({"webPages": {}})
check("bing: missing webPages -> []", web.bing_search("x", api_key="k") == [])

print("\nweb search tests passed")
