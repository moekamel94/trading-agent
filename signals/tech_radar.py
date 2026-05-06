"""
Technology maturity radar — tracks where each technology wave is on the adoption curve.
Updates weekly. Helps identify stocks before they cross the 'early commercial' threshold.
"""
from datetime import datetime, timezone

# Technology maturity scores (0=research, 50=early commercial, 100=mainstream)
# Updated manually or via news analysis
TECH_MATURITY = {
    "AGI":                   {"score": 25, "trend": "↑↑", "next_milestone": "GPT-5 level reasoning", "companies": ["MSFT", "GOOGL", "META", "NVDA"]},
    "Quantum Computing":     {"score": 20, "trend": "↑",  "next_milestone": "1000+ qubit error correction", "companies": ["IONQ", "GOOGL", "MSFT", "IBM"]},
    "Nuclear SMR":           {"score": 30, "trend": "↑↑", "next_milestone": "NRC first SMR license 2026", "companies": ["OKLO", "BWXT", "CCJ", "VST"]},
    "Autonomous Vehicles":   {"score": 45, "trend": "↑",  "next_milestone": "Waymo US city expansion", "companies": ["GOOGL", "TSLA"]},
    "Gene Editing CRISPR":   {"score": 40, "trend": "↑",  "next_milestone": "Phase 3 sickle cell approval", "companies": ["RXRX"]},
    "Humanoid Robotics":     {"score": 25, "trend": "↑↑", "next_milestone": "1000 units deployed in factories", "companies": ["TSLA", "NVDA"]},
    "Brain-Computer Interface": {"score": 15, "trend": "↑", "next_milestone": "Neuralink FDA expanded clearance", "companies": []},
    "Hypersonic Weapons":    {"score": 50, "trend": "→",  "next_milestone": "DoD program of record award", "companies": ["LMT", "KTOS"]},
    "Space Launch Costs":    {"score": 60, "trend": "↑",  "next_milestone": "$500/kg to LEO target", "companies": ["RKLB", "ASTS"]},
    "Solid-State Batteries": {"score": 20, "trend": "↑",  "next_milestone": "First auto OEM contract", "companies": []},
}


def get_radar_summary() -> str:
    """Return Discord-formatted tech radar update."""
    lines = ["🔭 TECH RADAR UPDATE"]
    for tech, data in TECH_MATURITY.items():
        score = data["score"]
        trend = data["trend"]
        milestone = data["next_milestone"]
        companies = ", ".join(data["companies"][:3]) if data["companies"] else "no basket coverage"

        # Highlight approaching early-commercial threshold
        flag = " 🎯" if 40 <= score <= 60 else ""
        lines.append(f"  {tech} | Maturity: {score}/100 | {trend} | Next: {milestone} | {companies}{flag}")
    return "\n".join(lines)


def get_companies_near_threshold() -> list[dict]:
    """
    Return companies in technologies approaching early-commercial threshold (40-60).
    These have maximum upside as technology crosses into commercial viability.
    """
    results = []
    for tech, data in TECH_MATURITY.items():
        if 40 <= data["score"] <= 65 and data["trend"] in ("↑", "↑↑"):
            for company in data["companies"]:
                results.append({
                    "symbol": company,
                    "technology": tech,
                    "maturity": data["score"],
                    "trend": data["trend"],
                    "milestone": data["next_milestone"],
                    "stage_bonus": 15,  # points added to signal score for threshold approach
                })
    return results
