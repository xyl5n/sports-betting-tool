"""
top_picks.py
============
Builds the Top Picks ranking shown on the /top-picks tab.  Every game pick
and prop pick for today is scored by:

    combined_score = ai_verdict_score * 0.60 + model_confidence * 0.40

The AI verdict maps Strong Lean=1.0 / Lean=0.80 / Neutral=0.50 / Fade=0.20 /
Strong Fade=0.0; a pick with no verdict yet is treated as Neutral (0.50).
Verdict label + colour reuse player_ai_breakdown.verdict_label (the app's
existing verdict badge); the 2-3 sentence reasoning comes from the existing
read-only summary caches (never re-generated here).

Pure read: pulls from the in-memory analysis state, the scored-prop cache,
and the AI-summary caches.  Nothing is written to Supabase -- the caller
caches the ranked list per session and re-pulls when verdicts arrive.
"""
from __future__ import annotations

import sys
from typing import Optional

_VERDICT_SCORE = {
    "Strong Lean": 1.0, "Lean": 0.80, "Neutral": 0.50,
    "Fade": 0.20, "Strong Fade": 0.0,
}


def _log(msg: str) -> None:
    print(f"TOP-PICKS: {msg}", file=sys.stderr, flush=True)


def _verdict(confidence) -> tuple[str, str]:
    """(label, colour-token) for a confidence -- the app's existing verdict
    badge.  Falls back to Neutral when confidence is unusable."""
    try:
        from .player_ai_breakdown import verdict_label
        return verdict_label(confidence)
    except Exception:                                                     # noqa: BLE001
        return ("Neutral", "warn")


def _entry(kind, name, pick_type, side, confidence, reasoning) -> dict:
    label, color = _verdict(confidence)
    vscore = _VERDICT_SCORE.get(label, 0.50)
    conf = float(confidence) if isinstance(confidence, (int, float)) else 0.5
    combined = vscore * 0.60 + conf * 0.40
    hi_conf = conf > 0.60
    return {
        "kind":           kind,                 # "game" | "prop"
        "name":           name,
        "pick_type":      pick_type,
        "side":           side,
        "confidence":     round(conf, 4),
        "verdict_label":  label,
        "verdict_color":  color,                # pos | warn | neg
        "reasoning":      reasoning or "",
        "pending":        not bool(reasoning),  # reasoning still generating
        "combined_score": round(combined, 4),
        "agree":          hi_conf and label in ("Lean", "Strong Lean"),
        "fade":           hi_conf and label in ("Fade", "Strong Fade"),
    }


def _game_entries(backend) -> list[dict]:
    out: list[dict] = []
    try:
        from . import ai_summaries as _ais
    except Exception:                                                     # noqa: BLE001
        _ais = None
    for sport, attr in (("mlb", "_analysis_state"), ("wnba", "_wnba_analysis_state")):
        state = getattr(backend, attr, {}) or {}
        for r in (state.get("results") or []):
            g = r.get("game") or {}
            away = g.get("away_team") or "Away"
            home = g.get("home_team") or "Home"
            name = f"{away} @ {home}"
            reasoning = None
            if _ais is not None:
                try:
                    reasoning = _ais.get_game_summary(sport, g)
                except Exception:                                         # noqa: BLE001
                    reasoning = None

            # Moneyline (always present when the model produced a pick).
            pred = r.get("prediction") or {}
            hw = pred.get("home_win_prob")
            if isinstance(hw, (int, float)):
                side = home if hw >= 0.5 else away
                out.append(_entry("game", name, f"{sport.upper()} ML",
                                  side, hw if hw >= 0.5 else 1.0 - hw, reasoning))

            rl = r.get("rl_pred") or r.get("spread_pred") or {}
            if rl.get("value_bet") and isinstance(rl.get("pick_prob"), (int, float)):
                pt = rl.get("run_line_point")
                side = f"{rl.get('pick_team', '')} {pt:+g}" if isinstance(pt, (int, float)) else rl.get("pick_team", "")
                out.append(_entry("game", name,
                                  f"{sport.upper()} {'RL' if sport == 'mlb' else 'Spread'}",
                                  side.strip(), rl.get("pick_prob"), reasoning))

            tot = r.get("totals_pred") or {}
            if tot.get("value_bet") and isinstance(tot.get("pick_prob"), (int, float)):
                ln = tot.get("total_line")
                side = f"{(tot.get('direction') or '').title()} {ln}".strip()
                out.append(_entry("game", name, f"{sport.upper()} Total",
                                  side, tot.get("pick_prob"), reasoning))
    return out


def _prop_entries(backend) -> list[dict]:
    out: list[dict] = []
    try:
        from .props_scored_cache import load_scored_props
        from . import ai_summaries as _ais
    except Exception:                                                     # noqa: BLE001
        return out
    market_label = None
    try:
        from .ai_summaries import _market_label as market_label
    except Exception:                                                     # noqa: BLE001
        market_label = lambda m: (m or "").replace("_", " ")
    for p in ((load_scored_props() or {}).get("picks") or []):
        conf = p.get("confidence")
        if conf is None:
            conf = p.get("model_prob")
        try:
            reasoning = _ais.get_prop_summary(p)
        except Exception:                                                 # noqa: BLE001
            reasoning = None
        side = f"{(p.get('side') or 'Over').title()} {p.get('line')}"
        out.append(_entry("prop", p.get("player") or "—",
                          market_label(p.get("market")), side, conf, reasoning))
    return out


def build_rankings(backend) -> dict:
    """Full ranked list (game + prop) sorted by combined_score desc.  Nothing
    is filtered -- the UI does the All/Game/Props filtering."""
    rows: list[dict] = []
    try:
        rows.extend(_game_entries(backend))
    except Exception as exc:                                              # noqa: BLE001
        _log(f"game entries failed: {exc}")
    try:
        rows.extend(_prop_entries(backend))
    except Exception as exc:                                              # noqa: BLE001
        _log(f"prop entries failed: {exc}")
    rows.sort(key=lambda r: r["combined_score"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return {"rows": rows, "count": len(rows)}
