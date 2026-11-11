import os
from datetime import datetime, timezone
from sqlalchemy import JSON, DateTime, Float, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Site(Base):
    __tablename__ = "sites"
    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True)
    start_url: Mapped[str] = mapped_column(String(2048))
    profile: Mapped[str] = mapped_column(String(64), default="general")
    collection_override: Mapped[str | None] = mapped_column(String(128), default=None)
    source: Mapped[str] = mapped_column(String(16), default="manual")      # manual | radar
    category: Mapped[str | None] = mapped_column(String(128), default=None)
    enabled: Mapped[bool] = mapped_column(default=True)
    # --- popularity / depth ---
    rank: Mapped[int | None] = mapped_column(default=None)                 # Cloudflare Radar rank
    visit_score: Mapped[float] = mapped_column(default=0.0)                # 0..1, recomputed per run
    depth_override: Mapped[str | None] = mapped_column(String(16), default=None)
    path_rules: Mapped[list] = mapped_column(JSON, default=list)           # [{"prefix","depth"}]
    # --- crawl scope ---
    max_pages: Mapped[int] = mapped_column(default=200)
    render_js: Mapped[bool] = mapped_column(default=False)
    delay_seconds: Mapped[float] = mapped_column(default=0.5)
    # --- schedule ---
    interval_hours: Mapped[int] = mapped_column(default=24)
    run_hour_utc: Mapped[int] = mapped_column(default=0)
    run_minute_utc: Mapped[int] = mapped_column(default=0)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(255))

    def to_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class PageStat(Base):
    """Per-page visit counts (fed by POST /hits from your search server) + summary cache."""
    __tablename__ = "page_stats"
    url: Mapped[str] = mapped_column(String(2048), primary_key=True)
    site_id: Mapped[int] = mapped_column(index=True)
    hits: Mapped[int] = mapped_column(default=0)
    impressions: Mapped[int] = mapped_column(default=0)              # appeared in a result set
    tier: Mapped[str] = mapped_column(String(8), default="t0")       # t0|t1|t2|t3, see shared/tiers.py
    heat: Mapped[float] = mapped_column(default=0.0)                 # last computed heat score
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_tier_change: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(2048))            # filled at index/transition time
    summary: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Partner(Base):
    """A partner website: the only sites we index into Qdrant and pay out for.
    Manual onboarding; see docs/tiering.md."""
    __tablename__ = "partners"
    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True)
    profile: Mapped[str] = mapped_column(String(64), default="general")
    collection: Mapped[str | None] = mapped_column(String(128), default=None)  # None = profile's shared collection
    site_id: Mapped[int | None] = mapped_column(default=None)                  # link to the crawler's sites row
    feed_type: Mapped[str] = mapped_column(String(16), default="crawl")        # crawl | rss | api
    feed_url: Mapped[str | None] = mapped_column(String(2048))
    poll_interval_minutes: Mapped[int] = mapped_column(default=30)
    backfill_cutoff_days: Mapped[int] = mapped_column(default=365)             # older pages skip vector indexing
    tier_config: Mapped[dict] = mapped_column(JSON, default=dict)              # TierConfig overrides
    enabled: Mapped[bool] = mapped_column(default=True)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PageArchive(Base):
    """Cold text store for t3 (vault) pages: what vector search no longer
    holds, kept for restore. Written by the tier bot before demote, and by
    the ingest gate for pages never vectorized. title/sections let restore
    re-embed the exact archived copy without a live fetch."""
    __tablename__ = "page_archives"
    url: Mapped[str] = mapped_column(String(2048), primary_key=True)
    site_id: Mapped[int] = mapped_column(index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(String(2048))
    sections: Mapped[list | None] = mapped_column(JSON, default=None)     # [{heading, heading_tag, text}]
    tier: Mapped[str] = mapped_column(String(8), default="t3")
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TierEvent(Base):
    """Append-only log of tiering actions: transitions (auto bot or manual
    tierctl), restores, and ingest-time archiving straight to the vault.
    Feeds `tierctl metrics` (transition rate, restore latency) and
    `tierctl transitions` (full history, not just each page's last change).
    Never updated or deleted: it is the system's audit trail."""
    __tablename__ = "tier_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    url: Mapped[str] = mapped_column(String(2048), index=True)
    site_id: Mapped[int] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(String(16))            # promote | demote | restore | archive
    from_tier: Mapped[str | None] = mapped_column(String(8))
    to_tier: Mapped[str | None] = mapped_column(String(8))
    via: Mapped[str] = mapped_column(String(8), default="auto")     # auto (bot) | manual (tierctl)
    dwell_hours: Mapped[float | None] = mapped_column(Float)        # hours in from_tier before this action
                                                                     # (restore: hours spent in the vault)


def init_db():
    Base.metadata.create_all(engine)


def record_event(url: str, site_id: int, kind: str, from_tier: str | None = None,
                 to_tier: str | None = None, via: str = "auto",
                 dwell_hours: float | None = None, ts: datetime | None = None):
    """Append one row to the tiering event log. Its own session + commit:
    small and infrequent, and it must not hold the caller's session open."""
    with SessionLocal() as s:
        s.add(TierEvent(url=url, site_id=site_id, kind=kind, from_tier=from_tier,
                        to_tier=to_tier, via=via, dwell_hours=dwell_hours,
                        ts=ts or utcnow()))
        s.commit()


def archive_page(url: str, site_id: int, text: str, content_hash: str | None = None,
                 title: str | None = None, sections: list | None = None):
    """Write the cold store for a vaulted page (upsert: an existing row is
    refreshed, so re-demoting a changed page does not leave a stale cold
    copy). Called by the tier bot before publishing a demote to t3
    (archive BEFORE delete, see docs/tiering.md) and by the feed watcher's
    ingest gate for pages never vectorized. sections (shared.messages
    Section models or plain dicts) are stored so restore can re-embed the
    exact archived copy."""
    sec = None
    if sections:
        sec = [s.model_dump() if hasattr(s, "model_dump") else dict(s) for s in sections]
    with SessionLocal() as s:
        row = s.get(PageArchive, url)
        if row is not None:
            row.text, row.content_hash, row.archived_at = text, content_hash, utcnow()
            if title is not None:
                row.title = title
            if sec is not None:
                row.sections = sec
        else:
            s.add(PageArchive(url=url, site_id=site_id, text=text, content_hash=content_hash,
                              title=title, sections=sec, tier="t3", archived_at=utcnow()))
        s.commit()
