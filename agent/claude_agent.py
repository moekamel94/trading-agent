import json
import anthropic
import config

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

_SYSTEM = """You are an expert quantitative trading analyst making paper trading decisions.
You receive aggregated signals for a ticker and must return a structured JSON trading decision.
Be conservative: only trade when signals are clearly aligned. Never exceed the risk parameters given.
Always return valid JSON only — no prose, no markdown fences."""

_DECISION_SCHEMA = """
Return exactly this JSON structure:
{
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": <integer 1-10>,
  "allocation_pct": <float 0.0-5.0>,
  "asset_type": "stock" | "crypto" | "option",
  "option_direction": "call" | "put" | null,
  "rationale": "<one sentence>"
}
"""


def decide(symbol: str, signals: dict, portfolio: dict) -> dict:
    prompt = f"""
Ticker: {symbol}
Portfolio: equity=${portfolio.get('equity', 0):,.2f}, cash=${portfolio.get('cash', 0):,.2f}
Open positions: {portfolio.get('position_count', 0)} / {config.MAX_POSITIONS}
Options exposure: {portfolio.get('options_pct', 0):.1f}% / {config.MAX_OPTIONS_PCT}%
Crypto exposure: {portfolio.get('crypto_pct', 0):.1f}% / {config.MAX_CRYPTO_PCT}%

Signals:
{json.dumps(signals, indent=2, default=str)}

Risk rules:
- Max allocation per trade: {config.MAX_POSITION_PCT}%
- Only trade if confidence >= {config.MIN_CONFIDENCE}
- Prefer HOLD when signals conflict

{_DECISION_SCHEMA}
"""

    response = _client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    try:
        decision = json.loads(raw)
    except json.JSONDecodeError:
        decision = {"action": "HOLD", "confidence": 0, "allocation_pct": 0,
                    "asset_type": "stock", "option_direction": None,
                    "rationale": "parse error — defaulting to HOLD"}

    return decision
