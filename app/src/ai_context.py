"""
ai_context.py
=============
Shared data-enrichment helpers that assemble the deeper analytical signals
fed into the AI prompts (Groq game/prop summaries, the player-profile
breakdown, and the Anthropic chat).  Everything is pulled from data already
computed in the app -- rolling snapshots, the pybaseball Statcast cache
(statcast_client), the pitch-mix / batter-vs-pitch tables, the similarity
clusters, and the MLB Stats clients.

Every function is best-effort: a missing key, a cold cache, or a blocked
network call returns ``None`` / ``""`` rather than raising, so a prompt is
never blocked by an unavailable signal.  Statcast lookups are cached daily
inside statcast_client, so the batch summary queue only scrapes once per
player per day.
"""
from __future__ import annotations

import sys
from typing import Optional

# Map common pitch types to the three families the prompts reason about.
_FAMILY = {
    "FF": "fastball", "SI": "fastball", "FT": "fastball", "FC": "fastball",
    "SL": "breaking", "CU": "breaking", "KC": "breaking", "ST": "breaking",
    "SV": "breaking", "CS": "breaking", "SC": "breaking",
    "CH": "offspeed", "FS": "offspeed", "EP": "offspeed", "KN": "offspeed",
}


def _log(msg: str) -> None:
    print(f"AI-CTX: {msg}", file=sys.stderr, flush=True)


def resolve_player_id(name: str) -> Optional[int]:
    """MLB id for a player name via the (cached, negative-cached) lookup."""
    if not name:
        return None
    try:
        from .player_profile_client import search_player_by_name
        return search_player_by_name(name)
    except Exception:                                                     # noqa: BLE001
        return None


# ── Pitch mix / arsenal ──────────────────────────────────────────────────────

def pitch_mix(pitcher_id: Optional[int]) -> Optional[dict]:
    if not pitcher_id:
        return None
    try:
        from .statcast_client import get_pitch_mix
        m = get_pitch_mix(int(pitcher_id))
        return m if m.get("available") else None
    except Exception as exc:                                              # noqa: BLE001
        _log(f"pitch_mix({pitcher_id}) failed: {exc}")
        return None


def pitch_mix_text(mix: Optional[dict]) -> str:
    """'Arsenal: Slider 38% 87mph, 4-Seam FB 33% 95mph, ...'"""
    if not mix or not mix.get("pitches"):
        return ""
    parts = []
    for p in mix["pitches"][:6]:
        velo = f" {p['velocity']:.0f}mph" if isinstance(p.get("velocity"), (int, float)) else ""
        parts.append(f"{p.get('name')} {p.get('usage'):.0f}%{velo}")
    return "Arsenal: " + ", ".join(parts) + "."


def pitch_mix_payload(mix: Optional[dict]) -> Optional[list]:
    """Compact list for JSON payloads: [{name, family, usage, velocity}]."""
    if not mix or not mix.get("pitches"):
        return None
    out = []
    for p in mix["pitches"][:6]:
        out.append({
            "pitch":    p.get("name"),
            "family":   _FAMILY.get(p.get("type", ""), "other"),
            "usage_pct": p.get("usage"),
            "velocity": p.get("velocity"),
        })
    return out


# ── Batter vs pitch type ─────────────────────────────────────────────────────

def batter_vs_pitch(batter_id: Optional[int], pitcher_id: Optional[int]) -> Optional[dict]:
    if not batter_id or not pitcher_id:
        return None
    try:
        from .statcast_client import get_batter_vs_pitch_types
        bvp = get_batter_vs_pitch_types(int(batter_id), int(pitcher_id))
        return bvp if bvp.get("available") else None
    except Exception as exc:                                              # noqa: BLE001
        _log(f"batter_vs_pitch({batter_id},{pitcher_id}) failed: {exc}")
        return None


def batter_vs_pitch_text(bvp: Optional[dict]) -> str:
    """'vs this arsenal: Slider .180/.290 31%K (n=44), 4-Seam .305/.520 ...'"""
    if not bvp or not bvp.get("rows"):
        return ""
    parts = []
    for r in bvp["rows"]:
        if not r.get("faced"):
            continue
        parts.append(
            f"{r.get('pitch')} {r.get('avg')}/{r.get('slg')} "
            f"{r.get('k_pct')}K (n={r.get('faced')})"
        )
    return ("Batter vs this pitcher's pitch types: " + "; ".join(parts) + ".") if parts else ""


# ── Statcast percentile ranks ────────────────────────────────────────────────

def percentile_facts(player_id: Optional[int], is_pitcher: bool,
                     limit: int = 6) -> Optional[list]:
    """Notable Statcast percentile ranks (the most extreme high/low), from
    the 'all' split.  Returns [{metric, value, percentile}] or None."""
    if not player_id:
        return None
    try:
        from .statcast_client import get_batter_percentiles, get_pitcher_percentiles
        data = (get_pitcher_percentiles(int(player_id)) if is_pitcher
                else get_batter_percentiles(int(player_id)))
    except Exception as exc:                                              # noqa: BLE001
        _log(f"percentiles({player_id}) failed: {exc}")
        return None
    if not data.get("available"):
        return None
    rows = ((data.get("splits") or {}).get("all") or {}).get("rows") or []
    scored = [r for r in rows if isinstance(r.get("percentile"), (int, float))]
    if not scored:
        return None
    # Most notable = furthest from the 50th percentile.
    scored.sort(key=lambda r: abs(r["percentile"] - 50), reverse=True)
    return [{"metric": r.get("label"), "value": r.get("value"),
             "percentile": int(round(r["percentile"]))} for r in scored[:limit]]


def percentile_text(facts: Optional[list]) -> str:
    if not facts:
        return ""
    parts = [f"{f['metric']} {f['value']} ({f['percentile']}th pct)" for f in facts]
    return "Statcast percentiles: " + ", ".join(parts) + "."


# ── Similar players (clustering) ─────────────────────────────────────────────

def similar_players(market: str, player_name: str, limit: int = 4) -> list:
    try:
        from .player_similarity import get_similar_players
        return get_similar_players(market, player_name, limit=limit) or []
    except Exception as exc:                                              # noqa: BLE001
        _log(f"similar_players({market},{player_name}) failed: {exc}")
        return []


def similar_text(sims: list, market_label: str = "") -> str:
    if not sims:
        return ""
    names = ", ".join(
        f"{s.get('name')} ({int(round(float(s.get('score', 0)) * 100))}% sim)"
        for s in sims if s.get("name")
    )
    if not names:
        return ""
    tag = f" by {market_label}" if market_label else ""
    return f"Most similar players{tag}: {names}."
