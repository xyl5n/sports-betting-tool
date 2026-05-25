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


def _entry(kind, name, pick_type, side, confidence, reasoning, track=None,
           verdict_tier=None) -> dict:
    # When the AI supplied its own verdict tier (prop breakdown / game
    # verdict), the badge follows THAT single determination so the badge and
    # the written reasoning always agree -- reusing the prop fix's tier
    # colours.  Otherwise fall back to the confidence-derived label.
    label = color = None
    ai_tier = None          # the AI's own verdict tier (None when not generated)
    if verdict_tier:
        try:
            from .player_ai_breakdown import _VERDICT_TIERS, tier_color
            if verdict_tier in _VERDICT_TIERS:
                ai_tier = verdict_tier
                label, color = verdict_tier, tier_color(verdict_tier)
        except Exception:                                                 # noqa: BLE001
            ai_tier = label = color = None
    if label is None:
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
        # The AI's own verdict tier (None when no breakdown yet).  Drives the
        # agreement outline + the Top Plays eligibility gate -- same source as
        # the badge, so they can't diverge.
        "ai_tier":        ai_tier,
        "reasoning":      reasoning or "",
        "pending":        not bool(reasoning),  # reasoning still generating
        "combined_score": round(combined, 4),
        "agree":          hi_conf and label in ("Lean", "Strong Lean"),
        "fade":           hi_conf and label in ("Fade", "Strong Fade"),
        # Frozen-at-appearance payload the Top Plays tracker records (odds +
        # grading keys).  Display fields above are unchanged.
        "_track":         track,
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
            game_tier = None
            if _ais is not None:
                try:
                    reasoning = _ais.get_game_summary(sport, g)
                    game_tier = _ais.get_game_verdict_tier(sport, g)
                except Exception:                                         # noqa: BLE001
                    reasoning = None

            gid = str(g.get("id") or g.get("game_id") or r.get("game_id") or "")
            commence = g.get("commence_time") or r.get("commence_time")

            def _track(kind, bet_type, pick_side, line, odds, prob, side_disp):
                return {
                    "kind": kind, "sport": sport, "bet_type": bet_type,
                    "pick_side": pick_side, "line": line, "odds": odds,
                    "prob": prob, "game_id": gid, "name": name,
                    "pick_type": None, "side_display": side_disp,
                    "home_team": home, "away_team": away,
                    "commence_time": commence,
                }

            # Moneyline (always present when the model produced a pick).
            pred = r.get("prediction") or {}
            hw = pred.get("home_win_prob")
            if isinstance(hw, (int, float)):
                side = home if hw >= 0.5 else away
                ml_prob = hw if hw >= 0.5 else 1.0 - hw
                ml_odds = (g.get("pick_odds")
                           or (g.get("home_odds") if side == home else g.get("away_odds")))
                out.append(_entry("game", name, f"{sport.upper()} ML",
                                  side, ml_prob, reasoning,
                                  track=_track("game", "ml", side, None,
                                               ml_odds, ml_prob, side),
                                  verdict_tier=game_tier))

            # RL / Total inherit the game-level AI verdict (the model's pick
            # the AI judged is the moneyline; the game read carries the whole
            # game), so agreement gating is consistent across the game's bets.
            rl = r.get("rl_pred") or r.get("spread_pred") or {}
            if rl.get("value_bet") and isinstance(rl.get("pick_prob"), (int, float)):
                pt = rl.get("run_line_point")
                side = f"{rl.get('pick_team', '')} {pt:+g}" if isinstance(pt, (int, float)) else rl.get("pick_team", "")
                out.append(_entry("game", name,
                                  f"{sport.upper()} {'RL' if sport == 'mlb' else 'Spread'}",
                                  side.strip(), rl.get("pick_prob"), reasoning,
                                  track=_track("game", "rl", rl.get("pick_team"),
                                               pt if isinstance(pt, (int, float)) else None,
                                               rl.get("pick_odds"), rl.get("pick_prob"),
                                               side.strip()),
                                  verdict_tier=game_tier))

            tot = r.get("totals_pred") or {}
            if tot.get("value_bet") and isinstance(tot.get("pick_prob"), (int, float)):
                ln = tot.get("total_line")
                side = f"{(tot.get('direction') or '').title()} {ln}".strip()
                out.append(_entry("game", name, f"{sport.upper()} Total",
                                  side, tot.get("pick_prob"), reasoning,
                                  track=_track("game", "total",
                                               (tot.get("direction") or "").upper(),
                                               ln if isinstance(ln, (int, float)) else None,
                                               tot.get("pick_odds"), tot.get("pick_prob"),
                                               side),
                                  verdict_tier=game_tier))
    return out


def _prop_entries(backend) -> list[dict]:
    out: list[dict] = []
    try:
        from .props_scored_cache import load_scored_props
        from . import ai_summaries as _ais
        from components import live_score
    except Exception:                                                     # noqa: BLE001
        return out
    market_label = None
    try:
        from .ai_summaries import _market_label as market_label
    except Exception:                                                     # noqa: BLE001
        market_label = lambda m: (m or "").replace("_", " ")
    for p in ((load_scored_props() or {}).get("picks") or []):
        # Hide props whose game has already started -- same live_score
        # detection the Props tab + My Bets use, so 'started' is consistent.
        try:
            if live_score.game_has_started(
                backend,
                commence_time=p.get("commence_time"),
                home_team=p.get("home_team"),
                away_team=p.get("away_team"),
                sport="mlb",
            ):
                continue
        except Exception:                                                 # noqa: BLE001
            pass
        conf = p.get("confidence")
        if conf is None:
            conf = p.get("model_prob")
        try:
            reasoning = _ais.get_prop_summary(p)
        except Exception:                                                 # noqa: BLE001
            reasoning = None
        # The prop's AI verdict tier comes from its cached breakdown -- the
        # same source the player page uses -- so the agreement outline + the
        # Top Plays gate key off the identical determination.
        prop_tier = None
        try:
            from . import player_ai_breakdown as _pab
            bd = _pab.peek_breakdown(p) or {}
            prop_tier = (bd.get("verdict_tier") or "").strip() or None
        except Exception:                                                 # noqa: BLE001
            prop_tier = None
        side = f"{(p.get('side') or 'Over').title()} {p.get('line')}"
        try:
            line_f = float(p.get("line"))
        except (TypeError, ValueError):
            line_f = None
        track = {
            "kind": "prop", "sport": "mlb",
            "bet_type": p.get("market"),                       # market KEY (grading)
            "pick_side": (p.get("side") or "Over").upper(),    # OVER / UNDER
            "line": line_f, "odds": p.get("best_odds"),
            "prob": conf if isinstance(conf, (int, float)) else None,
            "game_id": None, "event_id": p.get("event_id"),
            "player": p.get("player"), "name": p.get("player") or "—",
            "pick_type": None, "side_display": side,
            "home_team": p.get("home_team"), "away_team": p.get("away_team"),
            "commence_time": p.get("commence_time"),
        }
        out.append(_entry("prop", p.get("player") or "—",
                          market_label(p.get("market")), side, conf, reasoning,
                          track=track, verdict_tier=prop_tier))
    return out


def build_rankings(backend) -> dict:
    """Ranked Top Plays list (game + prop), sorted by combined_score desc.

    ELIGIBILITY GATE: a pick only appears if the AI clearly AGREES with the
    model -- verdict tier Lean / Strong Lean.  Fade / Strong Fade / Neutral
    (and picks with no AI verdict yet) are excluded.  Keys off the SAME
    ai_tier the agreement outline uses.  The existing combined-score ranking
    is unchanged for the picks that survive the gate."""
    rows: list[dict] = []
    try:
        rows.extend(_game_entries(backend))
    except Exception as exc:                                              # noqa: BLE001
        _log(f"game entries failed: {exc}")
    try:
        rows.extend(_prop_entries(backend))
    except Exception as exc:                                              # noqa: BLE001
        _log(f"prop entries failed: {exc}")

    # Top Plays = AI-agreed picks only.
    try:
        from .player_ai_breakdown import agrees_with_model
        rows = [r for r in rows if agrees_with_model(r.get("ai_tier"))]
    except Exception as exc:                                              # noqa: BLE001
        _log(f"agreement gate failed: {exc}")

    rows.sort(key=lambda r: r["combined_score"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    # Record each appearing (now AI-agreed) play into the standalone Top
    # Plays scorecard (frozen odds + Kelly unit stake).  Idempotent -- dedup
    # by id, so re-renders never re-stake.  Best-effort; never blocks the page.
    try:
        from . import top_plays_tracker
        top_plays_tracker.record_plays(rows)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"top-plays record failed: {exc}")
    # The _track payload is internal to recording -- drop it before handing
    # rows to the UI so the display layer is unchanged.
    for r in rows:
        r.pop("_track", None)
    return {"rows": rows, "count": len(rows)}
