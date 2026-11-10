import logging
import os
import threading
from contextlib import asynccontextmanager
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from shared.config import load_profiles
from shared.messages import Depth, MoveMsg, get_redis, publish
from .db import PageStat, SessionLocal, Site, init_db, utcnow
from .feedwatcher import feed_worker_loop
from .radar import sync_radar
from .registry import sync_manual, upsert_site
from .scheduler import worker_loop

logging.basicConfig(level=logging.INFO)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
WORKERS = int(os.getenv("CRAWL_WORKERS", "1"))
FEED_WORKERS = int(os.getenv("FEED_WORKERS", "1"))
stop = threading.Event()


@asynccontextmanager
async def lifespan(app):
    init_db()
    with SessionLocal() as s:
        sync_manual(s)
    for _ in range(WORKERS):
        threading.Thread(target=worker_loop, args=(stop,), daemon=True).start()
    for _ in range(FEED_WORKERS):
        threading.Thread(target=feed_worker_loop, args=(stop,), daemon=True).start()
    yield
    stop.set()


app = FastAPI(title="LocalMind crawler/planner", lifespan=lifespan)


def auth(authorization: str = Header(None)):
    if ADMIN_TOKEN and authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(401)


api = APIRouter(dependencies=[Depends(auth)])


class PathRule(BaseModel):
    prefix: str
    depth: Depth


class SiteFields(BaseModel):
    """Every field optional. Send null to clear (e.g. depth_override: null => back to auto)."""
    start_url: str | None = None
    profile: str | None = None
    enabled: bool | None = None
    depth_override: Depth | None = None
    path_rules: list[PathRule] | None = None
    max_pages: int | None = None
    render_js: bool | None = None
    delay_seconds: float | None = None
    interval_hours: int | None = None       # 24 = daily; 3 = every 3h (time-sensitive news)
    run_hour_utc: int | None = None
    run_minute_utc: int | None = None


def _check_profile(p):
    if p is not None and p not in load_profiles():
        raise HTTPException(400, f"unknown profile '{p}'. Known: {list(load_profiles())}")


def _fields(body: SiteFields) -> dict:
    d = body.model_dump(mode="json", exclude_unset=True)
    _check_profile(d.get("profile"))
    d["source"] = "manual"
    return d


@api.put("/sites/{domain}")
def put_site(domain: str, body: SiteFields):
    with SessionLocal() as s:
        site = upsert_site(s, domain, update=_fields(body))
        s.commit()
        return site.to_dict()


@api.patch("/sites/{domain}")
def patch_site(domain: str, body: SiteFields):
    with SessionLocal() as s:
        if not s.execute(select(Site).where(Site.domain == domain)).scalar_one_or_none():
            raise HTTPException(404)
        site = upsert_site(s, domain, update=_fields(body))
        s.commit()
        return site.to_dict()


@api.post("/sites/{domain}/run-now")
def run_now(domain: str):
    """Pull the next run forward (time-sensitive news). Normal schedule resumes afterwards."""
    with SessionLocal() as s:
        site = s.execute(select(Site).where(Site.domain == domain)).scalar_one_or_none()
        if not site:
            raise HTTPException(404)
        site.next_run_at, site.enabled = utcnow(), True
        s.commit()
    return {"queued": domain}


class MoveBody(BaseModel):
    collection: str | None = None            # null => back to the profile's shared collection


@api.post("/sites/{domain}/move")
def move_site(domain: str, body: MoveBody):
    with SessionLocal() as s:
        site = s.execute(select(Site).where(Site.domain == domain)).scalar_one_or_none()
        if not site:
            raise HTTPException(404)
        prof = load_profiles()[site.profile]
        src = site.collection_override or prof.collection
        dst = body.collection or prof.collection
        if src == dst:
            raise HTTPException(400, "already in that collection")
        site.collection_override = None if dst == prof.collection else dst
        s.commit()
        publish(get_redis(), MoveMsg(site_id=site.id, profile=site.profile, src=src, dst=dst))
    return {"moving": domain, "from": src, "to": dst}


@api.get("/plan")
def plan():
    """What the indexer will be asked to do: per site -> collection, depth, schedule."""
    from . import policy
    profiles = load_profiles()
    with SessionLocal() as s:
        return [{"domain": x.domain, "enabled": x.enabled, "profile": x.profile,
                 "collection": x.collection_override or profiles[x.profile].collection,
                 "depth": (x.depth_override or policy.auto_depth(x.visit_score).value),
                 "depth_source": "override" if x.depth_override else "auto",
                 "path_rules": x.path_rules, "visit_score": round(x.visit_score, 3), "rank": x.rank,
                 "interval_hours": x.interval_hours, "slot_utc": f"{x.run_hour_utc:02d}:{x.run_minute_utc:02d}",
                 "next_run_at": x.next_run_at, "last_status": x.last_status}
                for x in s.execute(select(Site).order_by(Site.next_run_at)).scalars()]


class Hit(BaseModel):
    url: str
    site_domain: str
    count: int = 1


@api.post("/hits")
def hits(batch: list[Hit]):
    """Your search server reports which pages users actually open => drives page-level depth."""
    with SessionLocal() as s:
        for h in batch:
            site = s.execute(select(Site).where(Site.domain == h.site_domain)).scalar_one_or_none()
            if not site:
                continue
            ps = s.get(PageStat, h.url) or PageStat(url=h.url, site_id=site.id, hits=0)
            ps.hits += h.count
            ps.updated_at = utcnow()
            s.merge(ps)
        s.commit()
    return {"ok": len(batch)}


@api.post("/admin/sync-radar")
def admin_radar():
    with SessionLocal() as s:
        return sync_radar(s)


@api.post("/admin/sync-manual")
def admin_manual():
    with SessionLocal() as s:
        return {"synced": sync_manual(s)}


app.include_router(api)


@app.get("/healthz")
def healthz():
    return {"ok": True}
