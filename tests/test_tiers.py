#!/usr/bin/env python3
"""Standalone tests for shared.tiers (no pytest needed):

    python3 tests/test_tiers.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.tiers import DEPTH_TO_TIER, TIER_TO_DEPTH, Tier, TierConfig, heat, target_tier

NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)
CFG = TierConfig()


def check(name, actual, expected):
    assert actual == expected, f"{name}: expected {expected!r}, got {actual!r}"
    print(f"ok  {name}")


def main():
    # --- heat ---
    check("never accessed -> 0", heat(10, 100, None, 14, now=NOW), 0.0)

    fresh = heat(10, 100, NOW, 14, now=NOW)
    check("just accessed -> full", round(fresh, 9), round(10 * 1.0 + 100 * 0.1, 9))

    half = heat(10, 100, NOW - timedelta(days=14), 14, now=NOW)
    check("half life halves", round(half, 9), round(fresh / 2, 9))

    quarter = heat(10, 100, NOW - timedelta(days=28), 14, now=NOW)
    check("two half lives quarter", round(quarter, 9), round(fresh / 4, 9))

    # --- depth <-> tier mapping ---
    check("depth map", DEPTH_TO_TIER["full"], Tier.t0)
    check("tier map", TIER_TO_DEPTH[Tier.t2], "headline")

    # --- transitions ---
    old = NOW - timedelta(days=30)   # well past min dwell
    recent = NOW - timedelta(days=1)  # inside min dwell

    # dead band: above floor, nothing to promote (t0 is top)
    check("t0 dead band stays", target_tier(30, Tier.t0, CFG, old, NOW)[0], Tier.t0)
    # demote t0->t1 when below floor and dwell elapsed
    check("t0->t1 below floor", target_tier(5, Tier.t0, CFG, old, NOW)[0], Tier.t1)
    # demote blocked inside min dwell
    check("demote blocked by dwell", target_tier(5, Tier.t0, CFG, recent, NOW)[0], Tier.t0)
    # promotion is exempt from dwell
    check("t3->t2 immediate", target_tier(15, Tier.t3, CFG, NOW, NOW)[0], Tier.t2)
    # one step at a time: huge heat still only moves t3->t2 this pass
    check("one step per pass", target_tier(500, Tier.t3, CFG, NOW, NOW)[0], Tier.t2)
    # per-step thresholds
    check("t2->t1 needs t1 threshold", target_tier(24, Tier.t2, CFG, NOW, NOW)[0], Tier.t2)
    check("t2->t1 at threshold", target_tier(25, Tier.t2, CFG, NOW, NOW)[0], Tier.t1)
    check("t1->t0 at threshold", target_tier(60, Tier.t1, CFG, NOW, NOW)[0], Tier.t0)
    # t1 demote floor
    check("t1 dead band stays", target_tier(10, Tier.t1, CFG, old, NOW)[0], Tier.t1)
    check("t1->t2 below floor", target_tier(3, Tier.t1, CFG, old, NOW)[0], Tier.t2)
    # t2 demote floor
    check("t2->t3 below floor", target_tier(0.5, Tier.t2, CFG, old, NOW)[0], Tier.t3)
    check("t3 stays at zero", target_tier(0.0, Tier.t3, CFG, old, NOW)[0], Tier.t3)
    # reason is returned for dry-run display
    tier, reason = target_tier(5, Tier.t0, CFG, old, NOW)
    assert "floor" in reason, reason
    print("ok  reason text")

    # --- declarative config (config/tiers.yaml) ---
    import tempfile, types
    from shared.tiers import load_tiers_file, tier_config_for

    tf = load_tiers_file()
    check("repo file loads", os.path.exists(tf.path), True)
    check("file defaults == code defaults (no behavior change)",
          TierConfig(**tf.defaults_raw), TierConfig())
    check("no partner blocks in the shipped file", tf.partners, {})

    yaml = """
config:
  - type: defaults
    half_life_days: 10
    t2_floor: 2
  - type: partner
    domain: slow.example
    half_life_days: 90
    t2_floor: 4
    views_per_month_t1: 100
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(yaml)
        tmp = f.name
    tft = load_tiers_file(tmp)
    os.remove(tmp)
    check("defaults block parsed", tft.defaults_raw.get("half_life_days"), 10)
    check("partner block parsed", tft.partners.get("slow.example", {}).get("t2_floor"), 4)

    plain = types.SimpleNamespace(domain="plain.example", tier_config=None)
    db_only = types.SimpleNamespace(domain="plain.example",
                                    tier_config={"min_dwell_days": 30})
    drift = types.SimpleNamespace(domain="slow.example",
                                  tier_config={"half_life_days": 30, "t0_floor": 7})

    check("no partner -> file defaults", tier_config_for(plain, tft).half_life_days, 10.0)
    check("partner None -> file defaults", tier_config_for(None, tft).half_life_days, 10.0)
    check("db column applies when no file block",
          tier_config_for(db_only, tft).min_dwell_days, 30.0)
    check("db column fills keys the file block lacks",
          tier_config_for(drift, tft).t0_floor, 7.0)
    check("file block wins over db column (terraform)",
          tier_config_for(drift, tft).half_life_days, 90.0)
    check("file block value used by the logic",
          target_tier(3.5, Tier.t2, tier_config_for(drift, tft), old, NOW)[0], Tier.t3)
    eff = tier_config_for(drift, tft)
    check("future key kept, not rejected", getattr(eff, "views_per_month_t1", None), 100)
    check("future key does not change heat",
          round(heat(10, 100, NOW - timedelta(days=10), eff.half_life_days, now=NOW), 9),
          round((10 * 1.0 + 100 * 0.1) * 0.5 ** (10 / 90), 9))

    check("missing file falls back to code defaults",
          load_tiers_file("/nonexistent/tiers.yaml").defaults_raw.get("half_life_days"), 14.0)

    print("all tier tests passed")


if __name__ == "__main__":
    main()
