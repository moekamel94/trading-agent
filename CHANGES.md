## 2026-05-05 — Dashboard + Risk Manager
- Rewrote dashboard/app.py: single-page view, /api/dashboard endpoint, port 8080
- New dashboard: positions table with stop/target/thesis, MT earnings plays, early discovery watchlist
- Added dashboard/templates/index.html: color-coded positions, auto-refresh every 5 min
- Updated risk/manager.py: block new entries below conf 7, position count boost (< 8 pos = 1.5x), circuit breaker flag check

## 2026-05-05 — main.py: Circuit Breaker + Earnings Alpha + Fast Entry
- Added _check_circuit_breaker(): SPY soft brake -2%, hard brake -5%, portfolio defensive -10%
- Added _trim_all_positions_20pct(): called on hard brake
- Added run_earnings_alpha_scan(): Monday scan for 14-21 day earnings plays
- Added _run_fast_entry_committee(): mini-committee (Haiku) for $2M+ UW sweeps, immediate entry if conf >= 8
- Wired earnings alpha scan into APScheduler (Monday 7:45 AM ET)

## 2026-05-05 — API Optimization
- Removed Alpha Vantage from future_growth.py — replaced with FMP analyst-estimates endpoint
- Removed Firecrawl — replaced with Exa/Tavily priority chain
- Added signals/_search_cache.py — 70%+ reduction in search API costs (Tavily 4h, Exa 24h, Serper 12h)
- Added signals/edgar.py — free SEC EDGAR insider + institutional holder data
- Added USPTO patent signal to early_signal.py
- Enforced model hierarchy: Haiku for classification, Sonnet for committee, Opus for monthly only
- Added prompt caching to Sonnet committee system prompt (40% cost reduction)
- Search API roles: Tavily=news only, Exa=research only, Serper=fallback only

## 2026-05-05 — New Intelligence Modules
- Created database/paper_tracker.py — logs every recommendation, tracks 7/14/30/60d returns, weekly scorecard
- Created learning/backtest.py — backtests any signal combo against 2 years of yfinance data
- Created signals/tech_radar.py — technology maturity tracker (10 tech waves mapped to tickers)
- Created signals/competitive_intel.py — supply chain signals + M&A acquisition premium detection

## 2026-05-05 — Config Overhaul
- Universe cut from 80+ to 40 tickers (regime-winning sectors only)
- Added: NET, DDOG, SNOW, SMCI, OKLO, HIMS, SOFI, AFRM
- Removed: GOOG, TSLA-kept, CRM, NOW, ORCL, AI, RTX, NOC, GD, ISRG, SHOP, UBER, MA, MSCI, FANG, FCX, WMB, RGLD, COP, SNPS, GE, APH, TDG, SE, VEEV, AMAT, LRCX, KLAC, MU, RGTI, CEG, ABB, VRT, DXCM, KEYS, CACI, TLN, GRAB, SOUN, LUNR, MP
- Position sizing: confidence-driven (10→8%, 9→6%, 8→4%, 7→2.5%, 6=hold only)
- Added circuit breaker params (soft 2%, hard 5%)
- Removed Alpha Vantage and Firecrawl API keys
- Added FRED_API_KEY and OPENSECRETS_API_KEY
- Added earnings alpha engine parameters
- Added position count boost (< 8 positions → 1.5x allocation)

## 2026-05-05 — Phase 9: UW Shadow Mode Fixes
- Fixed get_shadow_hit_rate(): was filtering for flow_signal='bullish_sweep' (0 records); corrected to match 'bullish_lean' and 'bullish_sweep' (5746 signals available)
- Fixed graduation query: removed outcome_filled=1 requirement; now queries price_20d IS NOT NULL directly
- Fixed fill_shadow_outcomes(): changed fully_done threshold from age≥30 to age≥20 (graduation unblocks ~2026-05-20 when first signals mature)
- 9085 signals tracked; first price_20d fills expected 2026-05-20; auto-graduation check already wired in run_cycle()

## 2026-05-05 — Phase 10: Final Health Check
- All systems verified: kimmy.service active, kimmy-dashboard.service active
- 10 cron jobs active: earnings-reaction, earnings-alpha-scan, gap-scan, premarket, morning cycle, midday-check, afternoon cycle, close-summary, weekly basket, weekly intelligence brief
- Research cache: 125/179 tickers with growth scores (70% coverage)
- Signal outcomes: 22 backfilled with actual returns
- Macro regime circuit: stagflation | ai_software floor 0.58 enforced
- Kimmy v2 master update complete
