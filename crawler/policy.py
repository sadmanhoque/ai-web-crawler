"""Decides how deeply to index. Precedence (most specific wins):
   1. site.path_rules   2. site.depth_override   3. automatic (visit_score + page hits)."""
import os
from urllib.parse import urlparse
from shared.messages import Depth

T_FULL = float(os.getenv("SCORE_FULL", "0.6"))
T_SUMMARY = float(os.getenv("SCORE_SUMMARY", "0.25"))
PAGE_PROMOTE_HITS = int(os.getenv("PAGE_PROMOTE_HITS", "50"))
ORDER = [Depth.headline, Depth.summary, Depth.full]


def rank_score(rank: int | None) -> float:
    return max(0.0, 1 - (rank - 1) / 100) if rank else 0.0     # rank 1 -> 1.0, rank 100 -> ~0


def hit_score(total_hits: int) -> float:
    return min(1.0, total_hits / 1000)


def auto_depth(score: float) -> Depth:
    return Depth.full if score >= T_FULL else Depth.summary if score >= T_SUMMARY else Depth.headline


def page_depth(site, url: str, hits: int) -> Depth:
    path = urlparse(url).path
    for rule in site.path_rules or []:
        if path.startswith(rule["prefix"]):
            return Depth(rule["depth"])
    if site.depth_override:
        return Depth(site.depth_override)
    d = auto_depth(site.visit_score)
    if hits >= PAGE_PROMOTE_HITS and d != Depth.full:      # hot pages get one tier deeper
        d = ORDER[ORDER.index(d) + 1]
    return d
