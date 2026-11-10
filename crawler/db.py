import os
from datetime import datetime, timezone
from sqlalchemy import JSON, DateTime, String, Text, create_engine
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
    the ingest gate for pages never vectorized."""
    __tablename__ = "page_archives"
    url: Mapped[str] = mapped_column(String(2048), primary_key=True)
    site_id: Mapped[int] = mapped_column(index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(String(8), default="t3")
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def init_db():
    Base.metadata.create_all(engine)


def archive_page(url: str, site_id: int, text: str, content_hash: str | None = None):
    """Write the cold store for a vaulted page (idempotent: an existing row
    is left untouched). Called by the tier bot before publishing a demote to
    t3 (archive BEFORE delete, see docs/tiering.md) and by the feed watcher's
    ingest gate for pages never vectorized."""
    with SessionLocal() as s:
        if s.get(PageArchive, url):
            return
        s.add(PageArchive(url=url, site_id=site_id, text=text, content_hash=content_hash,
                          tier="t3", archived_at=utcnow()))
        s.commit()
