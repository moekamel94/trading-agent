"""
Single source of truth for tier-based screening, add/remove, and committee routing.
All layers (manager, curation, main) import from here — never duplicate thresholds.
"""

# ── Quantitative entry bar per tier ──────────────────────────────────────────
TIER_CRITERIA = {
    "mega": {
        "min_mcap":       500e9,
        "min_rev_growth": 0.08,    # 8% — mature; FCF/margins matter more
        "min_adv":        2e9,
    },
    "large_growth": {
        "min_mcap":         30e9,
        "min_rev_growth":   0.20,
        "min_adv":          200e6,
        "min_gross_margin": 0.50,
    },
    "mid_growth": {
        "min_mcap":         5e9,
        "min_rev_growth":   0.25,
        "min_adv":          50e6,
        "min_gross_margin": 0.40,
    },
    "speculative": {
        "min_mcap":               2e9,
        "max_mcap":               25e9,
        "min_rev_growth":         0.40,   # 40% YoY OR binary catalyst within 18mo
        "min_adv":                50e6,
        "min_inst_own":           0.15,
        "requires_tier1_partner": True,   # NVIDIA / hyperscaler / DoD / Fortune 100 / top OEM
    },
}

# ── Monthly screener caps (max adds/removes per tier per month) ───────────────
MONTHLY_SCREENER_CAPS = {
    "mega":         {"max_add": 1, "max_remove": 1},
    "large_growth": {"max_add": 2, "max_remove": 2},
    "mid_growth":   {"max_add": 2, "max_remove": 2},
    "speculative":  {"max_add": 1, "max_remove": 0},  # 0 auto-removes — committee only
}

# ── Weekly ADD signal requirements per tier ───────────────────────────────────
ADD_CRITERIA = {
    "mega":         {"signals_required": 2},
    "large_growth": {"signals_required": 2},
    "mid_growth":   {"signals_required": 2},
    "speculative":  {"signals_required": 3, "requires_committee": True},
}

# Weekly max additions
WEEKLY_MAX_ADDS       = 3   # total per week
WEEKLY_MAX_SPEC_ADDS  = 1   # speculative additions are committee-gated

# ── Weekly REMOVE criteria per tier ──────────────────────────────────────────
REMOVE_CRITERIA = {
    "mega": {
        "hard":       ["mcap_below_200b", "accounting_fraud"],
        "soft_count": 2,
        "soft":       ["rev_decline_2q", "death_cross_drawdown_30pct", "margin_collapse"],
    },
    "large_growth": {
        "hard":       ["mcap_below_10b", "bankruptcy_signal"],
        "soft_count": 2,
        "soft":       ["death_cross_rev_decline", "price_30pct_below_52w_death_cross",
                       "analyst_majority_sell", "congress_net_sell_3txn", "zombie_no_catalyst"],
    },
    "mid_growth": {
        "hard":       ["mcap_below_2b", "bankruptcy_signal"],
        "soft_count": 2,
        "soft":       ["death_cross_rev_decline", "price_30pct_below_52w_death_cross",
                       "analyst_majority_sell", "congress_net_sell_3txn", "zombie_no_catalyst"],
    },
    "speculative": {
        "hard":               ["mcap_below_1b", "bankruptcy"],
        "soft":               [],        # NO soft-removes — committee only
        "requires_committee": True,
    },
}

# ── Committee routing: True = 6-agent committee required, False = Haiku ──────
COMMITTEE_ROUTING = {
    ("add",          "mega"):         False,
    ("add",          "large_growth"): False,
    ("add",          "mid_growth"):   False,
    ("add",          "speculative"):  True,
    ("remove_soft",  "mega"):         False,
    ("remove_soft",  "large_growth"): False,
    ("remove_soft",  "mid_growth"):   False,
    ("remove_soft",  "speculative"):  True,   # flag only — committee votes
    ("remove_hard",  "mega"):         False,
    ("remove_hard",  "large_growth"): False,
    ("remove_hard",  "mid_growth"):   False,
    ("remove_hard",  "speculative"):  True,
    ("tier_promote", "any"):          True,
    ("tier_demote",  "any"):          True,
}

# ── Quarterly tier promotion thresholds (triggers committee flag, not auto) ───
PROMOTION_FLAGS = {
    "mid_to_large": {
        "min_mcap":             35e9,
        "consecutive_quarters": 2,
    },
    "large_to_mega": {
        "min_mcap":             600e9,
        "consecutive_quarters": 2,
    },
}

# ── Medium-term basket sourcing rules ────────────────────────────────────────
MT_BASKET_SOURCES = {
    "congress_buy": {
        "description": "Net congress buying ≥2 transactions — event-driven, 3-12w play",
        "max_slots":   8,
        "ttl_days":    45,
        "auto_add":    True,   # no Haiku needed — congress buy = auto-qualify
    },
    "earnings_catalyst": {
        "description": "Earnings in 3-8 weeks + analyst upside >15% + above SMA20",
        "max_slots":   6,
        "ttl_days":    60,     # expires after earnings date + buffer
        "min_rsi":     35,
        "max_rsi":     75,
        "min_upside":  0.15,
    },
    "sector_rotation": {
        "description": "Top RS ticker in thesis sectors outperforming SPY 4-week",
        "max_slots":   4,
        "ttl_days":    21,     # refreshed weekly — short TTL keeps it current
        "min_rs_vs_spy": 0.03, # must beat SPY by ≥3% over 4 weeks
    },
    "uw_discovery": {
        "description": "Out-of-basket UW call sweep ≥$500K — institutional interest signal",
        "max_slots":   5,
        "ttl_days":    30,
        "auto_add":    True,
    },
}

# MT basket auto-retirement criteria (checked every weekly review)
MT_BASKET_RETIRE = {
    "death_cross":          True,        # death cross confirmed → remove
    "rsi_overbought":       78,          # RSI ≥ 78 at weekly check → remove
    "catalyst_passed_days": 7,           # catalyst date passed >7 days ago → remove
    "congress_ttl_days":    45,          # congress buy older than 45 days → remove
    "uw_ttl_days":          30,          # UW discovery older than 30 days → remove
}

# ── Earnings position caps (enforced pre-earnings-day) ───────────────────────
EARNINGS_CAP_PCT = {
    "mega":         6.0,   # never hold >6% into a binary event
    "large_growth": 6.0,
    "mid_growth":   5.0,
    "speculative":  4.0,
}

# ── Regime-conditional screening thresholds ───────────────────────────────────
# In inflationary/geo-stressed regimes, energy/defense/commodities have lower margins
# but are still the right sectors — relax margin requirements, tighten growth a bit.
REGIME_THRESHOLD_PRESETS: dict[str, dict] = {
    "growth_driven": {
        # Default thresholds — same as TIER_CRITERIA
        "large_growth": {"min_rev_growth": 0.20, "min_gross_margin": 0.50},
        "mid_growth":   {"min_rev_growth": 0.25, "min_gross_margin": 0.40},
        "mega":         {"min_rev_growth": 0.08, "min_gross_margin": None},
    },
    "inflationary": {
        # Energy/commodities/defense — lower margins, still strong revenue
        "large_growth": {"min_rev_growth": 0.10, "min_gross_margin": 0.25},
        "mid_growth":   {"min_rev_growth": 0.12, "min_gross_margin": 0.20},
        "mega":         {"min_rev_growth": 0.06, "min_gross_margin": None},
    },
    "recessionary": {
        # Defense/healthcare — quality over growth, margins matter more
        "large_growth": {"min_rev_growth": 0.05, "min_gross_margin": 0.45},
        "mid_growth":   {"min_rev_growth": 0.08, "min_gross_margin": 0.38},
        "mega":         {"min_rev_growth": 0.03, "min_gross_margin": None},
    },
    "geopolitically_stressed": {
        # Defense/energy/cyber — wide margin tolerance
        "large_growth": {"min_rev_growth": 0.08, "min_gross_margin": 0.25},
        "mid_growth":   {"min_rev_growth": 0.10, "min_gross_margin": 0.20},
        "mega":         {"min_rev_growth": 0.05, "min_gross_margin": None},
    },
    "stagflation": {
        # Survival-mode: energy + defense + healthcare
        "large_growth": {"min_rev_growth": 0.08, "min_gross_margin": 0.28},
        "mid_growth":   {"min_rev_growth": 0.10, "min_gross_margin": 0.22},
        "mega":         {"min_rev_growth": 0.05, "min_gross_margin": None},
    },
}


def get_tier_criteria(tier: str, regime_label: str = "growth_driven") -> dict:
    """
    Return entry thresholds for a tier, adjusted for the current macro regime.
    Falls back to TIER_CRITERIA defaults for speculative (regime-independent).
    """
    base = dict(TIER_CRITERIA.get(tier, TIER_CRITERIA["mid_growth"]))
    if tier == "speculative":
        return base  # speculative criteria are always absolute — no regime adjustment
    preset = REGIME_THRESHOLD_PRESETS.get(regime_label, REGIME_THRESHOLD_PRESETS["growth_driven"])
    overrides = preset.get(tier, {})
    base.update({k: v for k, v in overrides.items() if v is not None})
    return base
