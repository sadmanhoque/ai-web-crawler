#!/usr/bin/env python3
"""tierctl — monitor the partner content tiering system (docs/tiering.md).

Read-only in v1: it shows the current state, computes what the tier bot will
do, and lists recent transitions. It calls the exact same shared/tiers.py
functions the bot uses, so what you see is what will happen.

Usage (from anywhere; needs DATABASE_URL, crawler side):
    python3 cli/tierctl.py status [--partner DOMAIN]
    python3 cli/tierctl.py heat URL
    python3 cli/tierctl.py dry-run [--partner DOMAIN]
    python3 cli/tierctl.py transitions [--since 24h]
    python3 cli/tierctl.py promote URL [--tier t0|t1|t2]   # one step up, or jump to --tier
    python3 cli/tierctl.py demote URL                       # one step down
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ~KB per page per tier, see docs/tiering.md (estimates for status output)
TIER_KB = {"t0": 9.0, "t1": 3.0, "t2": 3.5, "t3": 0.5}


def _db():
    try:
        from crawler.db import PageArchive, PageStat, Partner, Site, SessionLocal
    except KeyError:
        sys.exit("DATABASE_URL is not set (tierctl runs on the crawler side)")
    except ModuleNotFoundError as e:
        sys.exit(f"missing dependency {e.name!r} — run tierctl inside the crawler "
                 f"environment (deps from crawler/requirements.txt)")
    return PageStat, Site, Partner, PageArchive, SessionLocal


def _parse_since(s: str) -> float:
    """'24h' / '7d' / '90m' -> hours."""
    unit = s[-1].lower()
    val = float(s[:-1])
    return val * {"h": 1.0, "d": 24.0, "m": 1 / 60.0}.get(unit, 1.0)


def _partner_cfg(PageStat, Site, Partner, SessionLocal, url=None):
    """(Partner | None, TierConfig) for one URL, or defaults when not a partner."""
    from shared.tiers import TierConfig
    db = SessionLocal()
    if url:
        row = db.query(Site, Partner).outerjoin(
            Partner, Partner.domain == Site.domain).join(
            PageStat, PageStat.site_id == Site.id).filter(PageStat.url == url).first()
        if not row:
            db.close()
            return None, None
        site, partner = row[0], row[1]
    else:
        site, partner = None, None
    cfg = TierConfig(**(partner.tier_config or {})) if partner and partner.tier_config \
        else TierConfig()
    db.close()
    return partner, cfg


def _rows(PageStat, Site, SessionLocal, partner_domain=None):
    db = SessionLocal()
    q = db.query(PageStat, Site).join(Site, Site.id == PageStat.site_id)
    if partner_domain:
        q = q.filter(Site.domain == partner_domain)
    rows = q.all()
    db.close()
    return rows


def _score_and_target(PageStat, Site, Partner, SessionLocal, ps, site, now=None):
    """Mirror the tier bot's per-page evaluation: (heat, target_tier, reason, dwell_ok)."""
    from datetime import datetime, timezone
    from shared.tiers import Tier, TierConfig, heat, target_tier
    now = now or datetime.now(timezone.utc)

    partner = None
    if site.domain:
        db = SessionLocal()
        partner = db.query(Partner).filter(
            Partner.domain == site.domain, Partner.enabled.is_(True)).first()
        db.close()
    cfg = TierConfig(**(partner.tier_config or {})) if partner and partner.tier_config \
        else TierConfig()
    score = heat(ps.hits, ps.impressions, ps.last_accessed_at, cfg.half_life_days,
                 cfg.view_weight, cfg.impression_weight, now=now)
    current = Tier(ps.tier)
    target, reason = target_tier(score, current, cfg, ps.last_tier_change, now=now)
    return score, target, reason


def cmd_status(args):
    PageStat, Site, Partner, PageArchive, SessionLocal = _db()
    rows = _rows(PageStat, Site, SessionLocal, args.partner)
    per_site: dict[str, dict] = {}
    archive_sites: dict[str, int] = {}
    db = SessionLocal()
    pairs = db.query(PageArchive.site_id).all()
    db.close()
    for (site_id,) in pairs:
        archive_sites[site_id] = archive_sites.get(site_id, 0) + 1

    site_ids = {s.id for _, s in rows}
    names = {s.id: s.domain for _, s in rows}
    partner_domains = set()
    if site_ids:
        db = SessionLocal()
        for p in db.query(Partner).filter(Partner.domain.isnot(None)).all():
            partner_domains.add(p.domain)
        db.close()

    for ps, site in rows:
        d = per_site.setdefault(site.domain, {t: 0 for t in TIER_KB})
        d[ps.tier] = d.get(ps.tier, 0) + 1
    for site_id, n in archive_sites.items():
        dom = names.get(site_id, f"site {site_id}")
        d = per_site.setdefault(dom, {t: 0 for t in TIER_KB})
        d["t3_archived"] = d.get("t3_archived", 0) + n

    print(f"{'domain':<40} {'partner':<8} {'t0':>6} {'t1':>6} {'t2':>6} {'t3':>6} "
          f"{'arch':>6} {'est. KB':>10}")
    total_kb = 0.0
    for dom in sorted(per_site):
        d = per_site[dom]
        kb = sum(d.get(t, 0) * k for t, k in TIER_KB.items())
        total_kb += kb
        mark = "yes" if dom in partner_domains else "-"
        print(f"{dom:<40} {mark:<8} {d.get('t0', 0):>6} {d.get('t1', 0):>6} "
              f"{d.get('t2', 0):>6} {d.get('t3', 0):>6} {d.get('t3_archived', 0):>6} "
              f"{kb:>10.0f}")
    print(f"{'TOTAL':<40} {'':<8} {'':>6} {'':>6} {'':>6} {'':>6} {'':>6} {total_kb:>10.0f}")
    if not rows:
        print("(no pages yet)")


def cmd_heat(args):
    PageStat, Site, Partner, PageArchive, SessionLocal = _db()
    from datetime import datetime, timezone
    db = SessionLocal()
    ps = db.query(PageStat).filter(PageStat.url == args.url).first()
    if not ps:
        sys.exit(f"no page_stats row for {args.url}")
    site = db.get(Site, ps.site_id)
    partner = db.query(Partner).filter(Partner.domain == site.domain).first() if site else None
    db.close()
    score, target, reason = _score_and_target(PageStat, Site, Partner, SessionLocal, ps, site)
    from shared.tiers import TierConfig, as_utc
    cfg = TierConfig(**(partner.tier_config or {})) if partner and partner.tier_config else TierConfig()
    la = as_utc(ps.last_accessed_at)
    idle = None
    if la:
        idle = (datetime.now(timezone.utc) - la).total_seconds() / 86400
    print(f"url:            {args.url}")
    print(f"partner:        {partner.domain if partner else '-'}")
    print(f"tier (current): {ps.tier}")
    print(f"hits:           {ps.hits}")
    print(f"impressions:    {ps.impressions}")
    print(f"last accessed:  {la or 'never'}"
          + (f"  ({idle:.1f}d idle, decay {0.5 ** (idle / cfg.half_life_days):.3f})" if idle is not None else ""))
    print(f"half-life:      {cfg.half_life_days}d")
    print(f"heat:           {score:.2f}")
    print(f"expected tier:  {target.value}  ({reason})")


def cmd_dry_run(args):
    PageStat, Site, Partner, PageArchive, SessionLocal = _db()
    from datetime import datetime, timezone
    from shared.tiers import Tier, TierConfig, heat, target_tier
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    # exactly the rows evaluate_pass sees (enabled partners only)
    q = (db.query(PageStat, Site, Partner)
         .join(Site, Site.id == PageStat.site_id)
         .join(Partner, Partner.site_id == Site.id)
         .filter(Partner.enabled))
    if args.partner:
        q = q.filter(Partner.domain == args.partner)
    rows = q.all()
    db.close()
    planned = []
    for ps, site, partner in rows:
        cfg = TierConfig(**(partner.tier_config or {}))
        score = heat(ps.hits, ps.impressions, ps.last_accessed_at,
                     cfg.half_life_days, cfg.view_weight, cfg.impression_weight, now=now)
        target, reason = target_tier(score, Tier(ps.tier), cfg, ps.last_tier_change, now=now)
        if target.value != ps.tier:
            planned.append((score, ps.tier, target.value, reason, site.domain, ps.url))
    planned.sort(key=lambda p: p[0])
    for score, cur, tgt, reason, dom, url in planned:
        print(f"{cur} -> {tgt}  heat {score:8.2f}  {dom}  {url}  ({reason})")
    print(f"\n{len(planned)} planned transition(s) out of {len(rows)} partner pages; "
          f"nothing was applied.")


def cmd_transitions(args):
    PageStat, Site, Partner, PageArchive, SessionLocal = _db()
    from datetime import datetime, timezone, timedelta
    db = SessionLocal()
    since = datetime.now(timezone.utc) - timedelta(hours=_parse_since(args.since))
    rows = db.query(PageStat, Site).join(Site, Site.id == PageStat.site_id) \
        .filter(PageStat.last_tier_change >= since).all()
    db.close()
    for ps, site in rows:
        print(f"{ps.last_tier_change:%Y-%m-%d %H:%M} UTC  {ps.tier:<4} heat {ps.heat:8.2f}  "
              f"{site.domain:<30} {ps.url}")
    if not rows:
        print(f"(no tier changes in the last {args.since})")


def _manual(name: str, args):
    """Manual transition: operator's call (skips dwell gate + heat thresholds)
    but the exact same pipeline as the bot — fetch, archive (t3), indexer
    message, page_stats update."""
    PageStat, Site, Partner, PageArchive, SessionLocal = _db()
    from shared.messages import get_redis
    from shared.tiers import TIER_ORDER, Tier, TierConfig, heat
    from crawler.tierbot import transition_page
    db = SessionLocal()
    ps = db.query(PageStat).filter(PageStat.url == args.url).first()
    if not ps:
        sys.exit(f"no page_stats row for {args.url}")
    site = db.get(Site, ps.site_id)
    if site is None:
        sys.exit("no site row for this page")
    partner = (db.query(Partner).filter(Partner.site_id == site.id,
                                        Partner.enabled.is_(True)).first()
               if site else None)
    if not partner:
        sys.exit("no enabled partner for this page (tiering is partner-only)")
    cfg = TierConfig(**(partner.tier_config or {}))
    score = heat(ps.hits, ps.impressions, ps.last_accessed_at,
                 cfg.half_life_days, cfg.view_weight, cfg.impression_weight)
    cur = Tier(ps.tier)
    if name == "demote":
        idx = TIER_ORDER.index(cur)
        if idx >= len(TIER_ORDER) - 1:
            sys.exit(f"already at {cur.value} (vault)")
        target = TIER_ORDER[idx + 1]
    else:
        target = Tier(args.tier) if args.tier else None
        if target is None:
            if cur is Tier.t0:
                sys.exit(f"already at {cur.value} (full)")
            target = TIER_ORDER[TIER_ORDER.index(cur) - 1]
        if target is Tier.t3:
            sys.exit("promote target must be t0, t1 or t2")
        if target is cur:
            sys.exit(f"already at {cur.value}")
    result = transition_page(db=db, r=get_redis(), ps=ps, site=site, partner=partner,
                             to_tier=target, score=score)
    final = ps.tier if result == "fetch_failed" else target.value
    db.close()
    print(f"{name} {args.url}: {result}, now at {final} "
          f"(manual overrides dwell/thresholds; the bot re-evaluates heat on the next pass)")


def main():
    ap = argparse.ArgumentParser(
        description="monitor the partner content tiering system (docs/tiering.md)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="tier distribution + est. storage per site")
    p.add_argument("--partner", help="filter by partner/site domain")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("heat", help="full heat breakdown for one URL")
    p.add_argument("url")
    p.set_defaults(fn=cmd_heat)

    p = sub.add_parser("dry-run", help="planned transitions, apply nothing")
    p.add_argument("--partner", help="filter by partner/site domain")
    p.set_defaults(fn=cmd_dry_run)

    p = sub.add_parser("transitions", help="recent tier changes")
    p.add_argument("--since", default="24h", help="window: 90m / 24h / 7d (default 24h)")
    p.set_defaults(fn=cmd_transitions)

    p = sub.add_parser("promote", help="manual promote: one step up, or jump to --tier")
    p.add_argument("url")
    p.add_argument("--tier", choices=["t0", "t1", "t2"], default=None)
    p.set_defaults(fn=lambda a: _manual("promote", a))

    p = sub.add_parser("demote", help="manual demote: one step down (t3 = vault)")
    p.add_argument("url")
    p.set_defaults(fn=lambda a: _manual("demote", a))

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
