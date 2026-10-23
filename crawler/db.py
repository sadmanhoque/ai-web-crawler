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
    content_hash: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def init_db():
    Base.metadata.create_all(engine)
