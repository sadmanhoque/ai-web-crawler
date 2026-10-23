"""Site registry helpers: upsert, schedule math, manual yaml sync."""
import os
import zlib
from datetime import datetime, timedelta
import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .db import Site, utcnow

SITES_FILE = os.getenv("SITES_FILE", "config/sites.yaml")
SCHEDULE_FIELDS = {"interval_hours", "run_hour_utc", "run_minute_utc"}


def load_cfg() -> dict:
    return yaml.safe_load(open(SITES_FILE)) or {}


def compute_next(site: Site, now: datetime | None = None) -> datetime:
    """Next run strictly after `now`. 24h-multiples anchor to HH:MM UTC; shorter intervals
    (e.g. 3h for breaking news) are simply now + interval."""
    now = now or utcnow()
    if site.interval_hours % 24 == 0:
        t = now.replace(hour=site.run_hour_utc, minute=site.run_minute_utc, second=0, microsecond=0)
        while t <= now:
            t += timedelta(days=1)
        return t + timedelta(days=site.interval_hours // 24 - 1)
    return now + timedelta(hours=site.interval_hours)


def assign_slot(s: Session, domain: str) -> tuple[int, int]:
    """Spread sites across the day: pick the least-loaded UTC hour."""
    counts = dict(s.execute(select(Site.run_hour_utc, func.count()).group_by(Site.run_hour_utc)).all())
    hour = min(range(24), key=lambda h: (counts.get(h, 0), h))
    return hour, zlib.crc32(domain.encode()) % 60


def upsert_site(s: Session, domain: str, *, create: dict | None = None, update: dict | None = None) -> Site:
    update = update or {}
    site = s.execute(select(Site).where(Site.domain == domain)).scalar_one_or_none()
    if site is None:
        fields = {"start_url": f"https://{domain}/", **(create or {}), **update}
        site = Site(domain=domain, **fields)
        if "run_hour_utc" not in fields:
            site.run_hour_utc, site.run_minute_utc = assign_slot(s, domain)
        site.next_run_at = compute_next(site)
        s.add(site)
    else:
        for k, v in update.items():
            setattr(site, k, v)
        if SCHEDULE_FIELDS & update.keys():
            site.next_run_at = compute_next(site)
    s.flush()
    return site


def sync_manual(s: Session) -> int:
    n = 0
    for item in load_cfg().get("sites", []):
        item = dict(item)
        domain = item.pop("domain")
        upsert_site(s, domain, update={**item, "source": "manual"})
        n += 1
    s.commit()
    return n
