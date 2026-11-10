"""Index tier model + pure transition logic (no I/O).

Tiers are an S3-storage-class-style ladder for partner content: the lower
the tier, the cheaper to store and the less findable by vector search.
All transition decisions live here so the tier bot, the indexer and the
tierctl CLI share one implementation (see docs/tiering.md).
"""
from datetime import datetime, timezone
from enum import Enum
from pydantic import BaseModel


class Tier(str, Enum):
    t0 = "t0"   # full:    all chunk vectors + text
    t1 = "t1"   # summary: summary/lede chunk vectors only
    t2 = "t2"   # stub:    one vector (title + h1 + meta description)
    t3 = "t3"   # vault:   no vectors; Postgres metadata + archived text,
                #          findable by keyword/title only; restore = re-embed


TIER_ORDER = [Tier.t0, Tier.t1, Tier.t2, Tier.t3]

# PageMsg.depth describes what was fetched; it maps 1:1 onto the tier a
# freshly indexed page is stored at. t3 has no depth (nothing is indexed).
DEPTH_TO_TIER = {"full": Tier.t0, "summary": Tier.t1, "headline": Tier.t2}
TIER_TO_DEPTH = {t: d for d, t in DEPTH_TO_TIER.items()}


def tier_for_depth(depth: str) -> Tier:
    """Tier a freshly indexed page is stored at, given what was fetched."""
    return DEPTH_TO_TIER[depth]


class TierConfig(BaseModel):
    """Per-partner overrides (partners.tier_config); defaults below."""
    half_life_days: float = 14.0      # heat decay: 0.5 ** (idle_days / half_life)
    min_dwell_days: float = 14.0      # min time in a tier before auto-DEMOTE
                                      # (promotion is exempt)
    view_weight: float = 1.0          # weight of an actual page open
    impression_weight: float = 0.1    # weight of "appeared in a result set"
    # Hysteresis: demote floors sit below promote thresholds so a page
    # hovering near a boundary does not bounce between tiers.
    t0_floor: float = 20.0            # demote t0->t1 when heat < t0_floor
    t1_floor: float = 5.0             # demote t1->t2 when heat < t1_floor
    t2_floor: float = 1.0             # demote t2->t3 when heat < t2_floor
    t2_promote: float = 10.0          # promote t3->t2 when heat >= t2_promote
    t1_promote: float = 25.0          # promote t2->t1 when heat >= t1_promote
    t0_promote: float = 60.0          # promote t1->t0 when heat >= t0_promote


def as_utc(dt: datetime | None) -> datetime | None:
    """Naive datetimes (e.g. read back from SQLite) are treated as UTC."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


def heat(hits: int, impressions: int, last_accessed_at: datetime | None,
         half_life_days: float, view_weight: float = 1.0,
         impression_weight: float = 0.1, now: datetime | None = None) -> float:
    """Decayed activity score. Never accessed -> 0.0 (a fresh page is hot by
    default via its t0 start + min dwell, not by age alone)."""
    last_accessed_at = as_utc(last_accessed_at)
    if last_accessed_at is None:
        return 0.0
    now = as_utc(now) or datetime.now(timezone.utc)
    idle_days = max(0.0, (now - last_accessed_at).total_seconds() / 86400)
    decay = 0.5 ** (idle_days / half_life_days) if half_life_days > 0 else 0.0
    return (hits * view_weight + impressions * impression_weight) * decay


def target_tier(score: float, current: Tier, cfg: TierConfig,
                last_tier_change: datetime | None,
                now: datetime | None = None) -> tuple[Tier, str]:
    """One transition step per evaluation. Demotion is gated by min dwell;
    promotion is immediate. Returns (tier, reason); tier == current means
    no transition. Pure function: safe for the tier bot, tierctl dry-run
    and unit tests alike."""
    now = as_utc(now) or datetime.now(timezone.utc)
    last_tier_change = as_utc(last_tier_change)
    idx = TIER_ORDER.index(current)
    dwell_ok = last_tier_change is None or \
        (now - last_tier_change).total_seconds() >= cfg.min_dwell_days * 86400

    if idx > 0:                                   # promote, one step up
        up = TIER_ORDER[idx - 1]
        thresholds = {Tier.t2: cfg.t2_promote, Tier.t1: cfg.t1_promote,
                      Tier.t0: cfg.t0_promote}
        if score >= thresholds[up]:
            return up, f"heat {score:.1f} >= {up.value} threshold ({thresholds[up]})"

    if idx < len(TIER_ORDER) - 1 and dwell_ok:    # demote, one step down
        down = TIER_ORDER[idx + 1]
        floors = {Tier.t1: cfg.t0_floor, Tier.t2: cfg.t1_floor, Tier.t3: cfg.t2_floor}
        if score < floors[down]:
            return down, f"heat {score:.1f} < {down.value} floor ({floors[down]})"

    return current, "no transition"
