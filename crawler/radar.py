"""Discover popular sites via Cloudflare Radar's domain ranking.
NOTE: verify endpoint/params against the current Radar API docs; parsing is defensive."""
import os
import requests
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import Site
from .registry import load_cfg, upsert_site

BASE = "https://api.cloudflare.com/client/v4/radar/ranking/top"


def fetch_top_domains(limit: int, category: str | None = None) -> list[dict]:
    params = {"limit": limit, "format": "json"}
    if category:
        params["domainCategory"] = category
    r = requests.get(BASE, params=params, timeout=30,
                     headers={"Authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}"})
    r.raise_for_status()
    result = r.json()["result"]
    for v in result.values():                      # e.g. {"top_0": [{"rank":1,"domain":"..."}], "meta": {...}}
        if isinstance(v, list):
            return v
    return []


def sync_radar(s: Session) -> dict:
    cfg = load_cfg().get("radar", {})
    limit, deny = cfg.get("limit", 100), set(cfg.get("denylist", []))
    jobs = [{"cat": c["name"], "api_cat": c.get("domainCategory", c["name"]), "profile": c["profile"]}
            for c in cfg.get("categories", [])]
    jobs.append({"cat": None, "api_cat": None, "profile": cfg.get("default_profile", "general")})
    found: dict[str, dict] = {}                    # categories first, so the specific profile wins
    for job in jobs:
        for item in fetch_top_domains(limit, job["api_cat"]):
            d = item.get("domain")
            if not d or d in deny:
                continue
            e = found.setdefault(d, {"rank": item.get("rank"), "category": job["cat"], "profile": job["profile"]})
            if item.get("rank") and (e["rank"] is None or item["rank"] < e["rank"]):
                e["rank"] = item["rank"]
    for d, e in found.items():
        existing = s.execute(select(Site).where(Site.domain == d)).scalar_one_or_none()
        if existing and existing.source == "manual":
            upsert_site(s, d, update={"rank": e["rank"]})          # never touch manual config
        else:
            upsert_site(s, d, create={"source": "radar", "profile": e["profile"],
                                      "max_pages": cfg.get("max_pages", 200)},
                        update={"rank": e["rank"], "category": e["category"], "enabled": True})
    dropped = 0
    for site in s.execute(select(Site).where(Site.source == "radar")).scalars():
        if site.domain not in found and site.enabled:
            site.enabled, site.rank = False, None
            dropped += 1
    s.commit()
    return {"seen": len(found), "disabled": dropped}
