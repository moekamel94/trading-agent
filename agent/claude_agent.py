import json
import anthropic
import config

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

_SYSTEM = """You are a disciplined quantitative portfolio manager making paper trading decisions.

STRICT TRADING CRITERIA — you must enforce these. If any criterion is not clearly met, return HOLD.

ENTRY (all must be true to BUY):
- RSI between 40 and 68 (momentum without being overbought)
- Price above SMA50 (confirmed uptrend)
- MACD not in bearish crossover
- EPS growth YoY > 5%
- Revenue growth YoY > 3%
- Profit margin > 8%
- P/E ratio < 55
- Sentiment not negative

EXIT (any one triggers SELL):
- Position is profitable and RSI > 75 (take profits before reversal)
- MACD turns bearish crossover on a profitable position

POSITION SIZING by confidence:
- Confidence 7/10 -> 3% allocation
- Confidence 8/10 -> 4% allocation
- Confidence 9-10/10 -> 5% allocation

Only trade when signals CLEARLY align. HOLD is always the safe default.
Return valid JSON only — no prose, no markdown fences."""

_SCHEMA = """
Return exactly this JSON:
{
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": <integer 1-10>,
  "allocation_pct": <float 0.0-5.0>,
  "asset_type": "stock" | "crypto" | "option",
  "option_direction": "call" | "put" | null,
  "rationale": "<one concise sentence stating which criteria drove the decision>"
}
"""


def decide(symbol: str, signals: dict, portfolio: dict) -> dict:
    research = signals.get("research", {})
    research_block = ""
    if research.get("snippets"):
        lines = "\n".join(f"  - {s}" for s in research["snippets"][:15])
        research_block = f"\nWeb Research ({research.get('source_count', 0)} sources — {', '.join(research.get('sources', []))}):\n{lines}\n"

    signals_without_research = {k: v for k, v in signals.items() if k != "research"}

    prompt = f"""
Ticker: {symbol}
Portfolio: equity=${portfolio.get('equity', 0):,.2f}  cash=${portfolio.get('cash', 0):,.2f}
Open positions: {portfolio.get('position_count', 0)} / {config.MAX_POSITIONS}
Options exposure: {portfolio.get('options_pct', 0):.1f}% / {config.MAX_OPTIONS_PCT}%
Crypto exposure:  {portfolio.get('crypto_pct', 0):.1f}% / {config.MAX_CRYPTO_PCT}%

Signals:
{json.dumps(signals_without_research, indent=2, default=str)}
{research_block}
{_SCHEMA}
"""

    response = _client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {
            "action": "HOLD", "confidence": 0, "allocation_pct": 0,
            "asset_type": "stock", "option_direction": None,
            "rationale": "JSON parse error — defaulting to HOLD",
        }
