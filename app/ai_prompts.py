"""ai_prompts.py -- AI prompt construction + analyst-call helpers (PR #287).

The "Phase A" cluster from the post-#286 audit: every function that builds
an LLM prompt (chat context, explain, breakdown, pick-analysis) plus the
two thin Anthropic-call wrappers and the shared system prompt.

These are pure string/context builders -- no Flask coupling.  The full BFS
transitive closure was confirmed before extraction (10 items: 9 functions
+ the _ANALYST_SYSTEM_PROMPT constant); the only app.py module-level
dependency is `Ledger` (src.ledger, a verified leaf module).

Direction (no cycles):
    state.py / utils.py  ->  serializer.py  ->  ai_prompts.py  ->  app.py
    ai_prompts.py imports state, utils, serializer (one-way down) plus
    src.ledger; it NEVER imports app.py or scheduler.py.

Inline imports (datetime, zoneinfo, anthropic, src.pitcher_client,
src.ai_context) live inside the function bodies exactly as they did in
app.py -- they moved verbatim.
"""
from __future__ import annotations

# `Ledger` is the cluster's only app.py module-level dependency (used by
# _build_chat_context for the bankroll context).  src.ledger is a leaf
# module -- it imports no state/app/scheduler/serializer/ai_prompts.
from src.ledger import Ledger

from state import *       # noqa: F401,F403   (_ANTHROPIC_API_KEY, _FEATURE_LABELS, ...)
from utils import *       # noqa: F401,F403   (_fmt_odds, _fmt_pct, _format_odds)
from serializer import *  # noqa: F401,F403   (_serialize)

__all__ = [
    "_ANALYST_SYSTEM_PROMPT",
    "_call_analyst",
    "_call_analyst_chat",
    "_sp_pitch_mix_text",
    "_build_chat_context",
    "_build_explain_prompt",
    "_build_breakdown_prompt",
    "_pitcher_block_for_ai",
    "_resolve_pitcher_data_for_ai",
    "_build_pick_analysis_context",
]

# moved from app.py:744
_ANALYST_SYSTEM_PROMPT = (
    "You are a professional sports analyst with 20 years of experience in MLB and WNBA "
    "betting markets. You have deep expertise in sabermetrics, advanced baseball statistics, "
    "basketball analytics, lineup construction, pitcher matchup analysis, and betting market "
    "inefficiencies. You form your own independent opinions based on the data presented to "
    "you and are not afraid to disagree with model predictions when your analysis suggests a "
    "different outcome. When you disagree with the model you clearly state your own pick and "
    "explain why you see the game differently. Your analysis is direct, confident, and "
    "specific and you never give vague or non-committal answers. You always consider factors "
    "like recent form, situational context, matchup history, and market line movement in "
    "addition to the statistical data provided. After giving your analysis always end with a "
    "clear recommendation of either: 'Agree with model', 'Disagree — my pick is X', or "
    "'Lean with caution' if you partially agree but see significant risk."
)

# moved from app.py:762
def _call_analyst(prompt: str, max_tokens: int = 600) -> str:
    """Call the Anthropic analyst model with a single user prompt."""
    import anthropic as _anthropic
    api_key = _ANTHROPIC_API_KEY
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set in .env")
    client = _anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=_ANALYST_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()

# moved from app.py:778
def _call_analyst_chat(extra_context: str, messages: list, max_tokens: int = 800) -> str:
    """Call the Anthropic analyst model with a multi-turn conversation.

    extra_context is appended to the system prompt so the analyst has today's
    game data in every reply.  messages is the full history including the latest
    user message, in [{role, content}] form.
    """
    import anthropic as _anthropic
    api_key = _ANTHROPIC_API_KEY
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set in .env")
    system = _ANALYST_SYSTEM_PROMPT
    if extra_context:
        system += f"\n\n{extra_context}"
    client = _anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    return msg.content[0].text.strip()

# moved from app.py:802
def _sp_pitch_mix_text(name: str) -> str:
    """Compact pitch-mix string for a starter (cached); '' if unavailable."""
    try:
        from src import ai_context as _aic
        mix = _aic.pitch_mix(_aic.resolve_player_id(name or ""))
        txt = _aic.pitch_mix_text(mix)
        return txt.replace("Arsenal: ", "").rstrip(".") if txt else ""
    except Exception:                                                      # noqa: BLE001
        return ""

# moved from app.py:813
def _build_chat_context(results: list, bankroll: float, sport: str) -> str:
    """Build a compact text summary of today's games for the chat system prompt."""
    if not results:
        return "No games have been analyzed yet for today. Tell the user to run analysis first."

    try:
        ledger     = Ledger(path="data/ledger.json", starting_bankroll=bankroll)
        s_bankroll = ledger.data.get("personal_starting_bankroll", bankroll)
        serialized = [_serialize(r, bankroll, sport, s_bankroll) for r in results]
    except Exception:
        serialized = [
            {"away_team": r.get("game", {}).get("away_team", ""),
             "home_team": r.get("game", {}).get("home_team", "")}
            for r in results
        ]

    lines = [f"TODAY'S {sport.upper()} SLATE — {len(serialized)} GAMES\n"]

    for g in serialized[:16]:
        away = g.get("away_team", "Away")
        home = g.get("home_team", "Home")

        pick_team = g.get("pick_team", "")
        pick_odds = g.get("pick_odds")
        ml_conf   = g.get("ml_confidence") or g.get("xgb_prob") or 0
        edge      = g.get("pick_edge") or 0
        conflict  = g.get("conflict", False)

        rl_pick  = g.get("run_line_pick_team", "")
        rl_point = g.get("run_line_point", -1.5)
        rl_side  = g.get("run_line_side", "")
        rl_odds  = g.get("run_line_pick_odds")

        total_dir  = (g.get("direction") or "").upper()
        total_line = g.get("total_line", "")

        h_sp_name = g.get("home_sp_name", "")
        a_sp_name = g.get("away_sp_name", "")
        h_sp      = g.get("home_sp") or {}
        a_sp      = g.get("away_sp") or {}

        shap_vals = ((g.get("shap") or {}).get("values") or [])[:3]
        uf_score  = (g.get("upset_factor") or {}).get("score", "n/a")

        parts = [f"{away} @ {home}:"]
        if conflict:
            parts.append("  ML: SKIP (models conflict)")
        elif pick_team:
            parts.append(
                f"  ML: {pick_team} {_format_odds(pick_odds)} | "
                f"{ml_conf * 100:.1f}% conf | {edge * 100:+.1f}% edge"
            )
        if rl_pick:
            pt_str = f"{rl_point:+.1f}" if rl_side == "home" else f"{-rl_point:+.1f}"
            parts.append(f"  RL: {rl_pick} {pt_str} {_format_odds(rl_odds)}")
        if total_dir and total_line:
            parts.append(f"  Total: {total_dir} {total_line}")
        sp_parts = []
        for _nm, _sp in ((a_sp_name, a_sp), (h_sp_name, h_sp)):
            if not _nm:
                continue
            _line = (f"{_nm} ERA:{_sp.get('era', '?')} WHIP:{_sp.get('whip', '?')} "
                     f"K9:{_sp.get('k_per_9', '?')}")
            _mix = _sp_pitch_mix_text(_nm)
            if _mix:
                _line += f" [{_mix}]"
            sp_parts.append(_line)
        if sp_parts:
            parts.append(f"  SPs: {' vs '.join(sp_parts)}")
        if shap_vals:
            top = ", ".join(v.get("label") or v.get("feature", "") for v in shap_vals)
            parts.append(f"  Key factors: {top}")
        parts.append(f"  Upset risk: {uf_score}/10")

        lines.append("\n".join(parts))

    return "\n\n".join(lines)

# moved from app.py:6708
def _build_explain_prompt(d: dict) -> str:
    bet_type = d.get("bet_type", "ml")
    home     = d.get("home_team", "Home")
    away     = d.get("away_team", "Away")
    home_sp  = d.get("home_sp") or {}
    away_sp  = d.get("away_sp") or {}
    uf       = d.get("upset_factor") or {}
    shap     = d.get("shap_features") or []

    odds_val = d.get("pick_odds")
    odds_str = (f"{odds_val:+d}" if isinstance(odds_val, int)
                else f"{int(odds_val):+d}" if odds_val is not None else "n/a")

    edge     = d.get("pick_edge") or 0
    edge_str = f"{edge * 100:+.1f}%"

    if bet_type == "ml":
        pick_desc = f"{d.get('pick_team')} moneyline at {odds_str}"
        conf_desc = (f"XGBoost {d.get('xgb_prob', 0)*100:.1f}% / "
                     f"LR {d.get('lr_prob', 0)*100:.1f}%")
    elif bet_type == "run_line":
        home_pt  = float(d.get("run_line_point") or -1.5)
        side     = d.get("pick_side", "home")
        team     = d.get("pick_team") or (home if side == "home" else away)
        pick_pt  = home_pt if side == "home" else -home_pt
        pt_str   = f"+{abs(pick_pt)}" if pick_pt > 0 else f"{pick_pt}"
        pick_desc = f"{team} {pt_str} run line at {odds_str}"
        conf_desc = (f"XGBoost {d.get('xgb_prob', 0)*100:.1f}% / "
                     f"LR {d.get('lr_prob', 0)*100:.1f}%")
    else:  # totals
        pf = d.get("park_factor", 1.0) or 1.0
        pick_desc = (f"{(d.get('direction') or 'over').upper()} "
                     f"{d.get('total_line')} at {odds_str}")
        conf_desc = (f"Predicted total: {d.get('predicted_total')} runs "
                     f"(XGB {d.get('xgb_pred')}, LR {d.get('lr_pred')}) · "
                     f"Park factor {pf:.2f}×")

    shap_lines = "\n".join(
        f"  - {f.get('label', f.get('feature', '?'))}: {f.get('shap_value', 0):+.3f}"
        for f in shap[:3]
    )
    shap_block = f"Top model features:\n{shap_lines}" if shap_lines else ""

    sp_lines = []
    h_name = d.get("home_sp_name") or home
    a_name = d.get("away_sp_name") or away
    if home_sp:
        sp_lines.append(
            f"  {h_name} ({home_sp.get('hand','RHP')}): "
            f"ERA {home_sp.get('era','?')}  WHIP {home_sp.get('whip','?')}  "
            f"K% {home_sp.get('k_rate','?')}  {home_sp.get('rest','?')}d rest"
        )
    if away_sp:
        sp_lines.append(
            f"  {a_name} ({away_sp.get('hand','RHP')}): "
            f"ERA {away_sp.get('era','?')}  WHIP {away_sp.get('whip','?')}  "
            f"K% {away_sp.get('k_rate','?')}  {away_sp.get('rest','?')}d rest"
        )
    sp_block = ("Starting pitchers:\n" + "\n".join(sp_lines)) if sp_lines else ""

    uf_parts = []
    if uf.get("score") is not None:
        uf_parts.append(f"Chaos/upset score: {uf['score']}/10")
    if uf.get("confidence_reduction"):
        uf_parts.append(
            f"confidence reduced {round(uf['confidence_reduction']*100)}pp, "
            f"stake −{round(uf.get('kelly_reduction', 0)*100)}%"
        )
    uf_block = " · ".join(uf_parts)

    bd, bu = d.get("bet_dollars") or 0, d.get("bet_units") or 0
    kelly_block = f"Recommended stake: ${bd:.0f} ({bu:.1f}U)" if bd and bd > 0 else ""

    sections = [s for s in [shap_block, sp_block, uf_block, kelly_block] if s]

    prompt = (
        f"Analyze this betting pick and give your expert opinion in 3–4 sentences. "
        f"Cover: why the model favors this side, the key factors driving the edge, "
        f"the main risk, and your own independent assessment of this pick. "
        f"Be specific and direct. Do not use bullet points or headers. "
        f"Do not repeat the raw numbers verbatim — synthesize them into insight. "
        f"End with exactly one line formatted as: "
        f"ANALYST VERDICT: followed by one of these three options: "
        f"'Agree with model', 'Disagree — my pick is [team/side]', or 'Lean with caution'.\n\n"
        f"Game: {away} @ {home}\n"
        f"Pick: {pick_desc}\n"
        f"Model confidence: {conf_desc}\n"
        f"Edge vs market: {edge_str}\n"
    )
    if sections:
        prompt += "\n" + "\n".join(sections)

    return prompt.strip()

# moved from app.py:6871
def _build_breakdown_prompt(serialized: list) -> str:
    """Build the AI breakdown prompt from serialized game results."""
    if not serialized:
        return ""

    games_text = []
    for g in serialized[:14]:  # cap at 14 games
        away = g.get("away_team", "Away")
        home = g.get("home_team", "Home")

        # ML pick
        pick_team  = g.get("pick_team", "")
        pick_odds  = g.get("pick_odds")
        odds_str   = _format_odds(pick_odds)
        ml_conf    = g.get("ml_confidence") or g.get("xgb_prob") or 0
        edge       = g.get("pick_edge") or 0
        conflict   = g.get("conflict", False)

        # Run line
        rl_pick   = g.get("run_line_pick_team", "")
        rl_point  = g.get("run_line_point", -1.5)

        # Totals
        total_dir  = (g.get("direction") or "").upper()
        total_line = g.get("total_line", "")
        pred_total = g.get("predicted_total", "")

        # Starting pitchers
        h_sp_name = g.get("home_sp_name", "")
        a_sp_name = g.get("away_sp_name", "")
        h_sp      = g.get("home_sp") or {}
        a_sp      = g.get("away_sp") or {}

        # Upset factor
        uf_score = (g.get("upset_factor") or {}).get("score", "n/a")

        lines = [f"Game: {away} @ {home}"]
        if conflict:
            lines.append("ML: SKIP — models conflict")
        else:
            lines.append(
                f"ML pick: {pick_team} {odds_str} | "
                f"Confidence: {ml_conf * 100:.1f}% | Edge: {edge * 100:+.1f}%"
            )
        if rl_pick:
            rl_side = "home" if g.get("run_line_side") == "home" else "away"
            pt_str  = f"{rl_point:+.1f}" if rl_side == "home" else f"{-rl_point:+.1f}"
            lines.append(f"Run line: {rl_pick} {pt_str}")
        if total_dir and total_line:
            lines.append(
                f"Totals: {total_dir} {total_line}"
                + (f" (model pred: {pred_total})" if pred_total else "")
            )
        sp_parts = []
        if a_sp_name:
            sp_parts.append(f"{a_sp_name} ERA {a_sp.get('era', '?')} WHIP {a_sp.get('whip', '?')}")
        if h_sp_name:
            sp_parts.append(f"{h_sp_name} ERA {h_sp.get('era', '?')} WHIP {h_sp.get('whip', '?')}")
        if sp_parts:
            lines.append("SPs: " + " vs ".join(sp_parts))
        lines.append(f"Chaos/upset factor: {uf_score}/10")

        games_text.append("\n".join(lines))

    all_games = "\n\n".join(games_text)

    return (
        f"Here is today's MLB slate with model predictions. Provide:\n"
        f"1. A brief 2-sentence analysis for each game\n"
        f"2. Your top 3-5 best bet recommendations across all games\n"
        f"3. One strong 2-team parlay and one 3-team parlay\n\n"
        f"Today's games:\n{all_games}\n\n"
        f"Respond ONLY with valid JSON (no markdown fences, no extra text):\n"
        f'{{"games":[{{"matchup":"Away @ Home","analysis":"2 sentence analysis"}}],'
        f'"best_bets":[{{"pick":"Team ML / Over X / Team RL","reason":"Why this is top value"}}],'
        f'"parlays":{{"2-team":[{{"legs":["Pick 1","Pick 2"],"note":"Why they pair well"}}],'
        f'"3-team":[{{"legs":["Pick 1","Pick 2","Pick 3"],"note":"Why this parlay works"}}]}}}}'
    )

# moved from app.py:7230
def _pitcher_block_for_ai(sp: dict, side: str) -> str:
    """One pitcher's stat lines for the AI context.  Tolerant of the
    pitcher_client output shape (full_name / team_abbrev / era / whip /
    k_per_9 / bb9 / era_home / era_away / last3_era / wins / losses /
    rest / hand).  Missing fields render '?'."""
    if not isinstance(sp, dict) or not sp:
        return f"{side} SP: (no probable starter)"
    def _f(key, fmt: str) -> str:
        v = sp.get(key)
        if v is None or v == "":
            return "?"
        try:
            return fmt.format(float(v))
        except (TypeError, ValueError):
            return str(v)
    name = (sp.get("full_name") or "TBD").strip()
    team = (sp.get("team_abbrev") or "?").strip().upper()
    hand = sp.get("hand")
    hand_s = (
        "LHP" if hand == 1 or str(hand).upper() == "LHP"
        else "RHP" if hand == 0 or str(hand).upper() == "RHP"
        else "?"
    )
    wins   = int(sp.get("wins")   or 0)
    losses = int(sp.get("losses") or 0)
    record = f"{wins}-{losses}" if (wins or losses) else "?"
    return (
        f"{side} SP: {name} ({team}, {hand_s}, {record})  "
        f"ERA {_f('era', '{:.2f}')}  "
        f"WHIP {_f('whip', '{:.2f}')}  "
        f"K/9 {_f('k_per_9', '{:.1f}')}  "
        f"BB/9 {_f('bb9', '{:.1f}')}  "
        f"Home ERA {_f('era_home', '{:.2f}')}  "
        f"Away ERA {_f('era_away', '{:.2f}')}  "
        f"Last 3 ERA {_f('last3_era', '{:.2f}')}  "
        f"Rest {_f('rest', '{:.0f}')}d"
    )

# moved from app.py:7269
def _resolve_pitcher_data_for_ai(raw: dict, sport: str) -> tuple[dict, dict]:
    """Best-effort pitcher dict resolution for the AI payload.
    Preference order: raw meta -> serialized passthrough top-level ->
    direct pitcher_client fetch (snapshot-hydrated path, MLB only).
    Returns (home_sp, away_sp) -- empty dicts when nothing resolves."""
    meta = raw.get("meta") or {}
    home_sp = meta.get("home_sp") or raw.get("home_sp") or {}
    away_sp = meta.get("away_sp") or raw.get("away_sp") or {}
    if (home_sp and away_sp) or sport != "mlb":
        return home_sp, away_sp
    # Fall back to pitcher_client direct fetch -- same path the matchup
    # page uses (PR #88) for snapshot-hydrated rows that lack meta.
    game = raw.get("game") or {}
    home = game.get("home_team") or raw.get("home_team") or ""
    away = game.get("away_team") or raw.get("away_team") or ""
    commence = game.get("commence_time") or raw.get("commence_time") or ""
    if not (home and away):
        return home_sp, away_sp
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _Z
        game_date = ""
        if commence:
            try:
                dt = _dt.fromisoformat(str(commence).replace("Z", "+00:00"))
                game_date = dt.astimezone(_Z("America/New_York")).date().isoformat()
            except Exception:                                              # noqa: BLE001
                pass
        from src.pitcher_client import get_pitcher_client
        data = get_pitcher_client().get_starters_for_game(
            home, away, game_date, commence_time=commence)
        return (
            (data or {}).get("home") or home_sp,
            (data or {}).get("away") or away_sp,
        )
    except Exception:                                                     # noqa: BLE001
        return home_sp, away_sp

# moved from app.py:7308
def _build_pick_analysis_context(raw: dict, bet_type: str, sport: str) -> str:
    """Build the rich per-pick context fed to the Analyze button.

    The AI now sees a full data card: matchup + lines, both pitchers'
    stats and splits, all three model picks (ML / RL or Spread / Totals),
    per-model probabilities (XGB / LR / NN), the requested bet's edge
    + Kelly size + top SHAP factors with their actual numeric values,
    and the upset risk score.  Designed to support specific, opinionated
    output rather than generic "the model favors the home team" copy.
    """
    game = raw.get("game") or {}
    pred = raw.get("prediction") or {}
    meta = raw.get("meta") or {}
    away = game.get("away_team", "Away")
    home = game.get("home_team", "Home")
    commence_time = game.get("commence_time", "") or raw.get("commence_time", "")

    # ── Market lines (h2h moneyline + run line / spread + totals) ─────
    ml_home_odds = game.get("h2h_home_odds")
    ml_away_odds = game.get("h2h_away_odds")
    rl_pred      = raw.get("rl_pred") or raw.get("spread_pred") or {}
    totals_pred  = raw.get("totals_pred") or {}
    rl_line      = rl_pred.get("run_line_point") or rl_pred.get("spread_line")
    totals_line  = totals_pred.get("total_line")

    # ── All three picks (ML / RL or Spread / Totals) with confidences ─
    hp = float(pred.get("home_win_prob") or 0.5)
    market_p = float(game.get("home_implied_prob") or 0.5)
    if hp >= 0.5:
        ml_pick_team, ml_pick_prob, ml_edge = home, hp, hp - market_p
        ml_pick_odds = ml_home_odds
    else:
        ml_pick_team, ml_pick_prob, ml_edge = away, 1 - hp, (1 - hp) - (1 - market_p)
        ml_pick_odds = ml_away_odds

    rl_pick_team = rl_pred.get("pick_team") or "?"
    rl_pick_prob = float(rl_pred.get("pick_prob") or 0)
    rl_edge      = float(rl_pred.get("edge") or 0)
    rl_pick_odds = rl_pred.get("pick_odds")

    tot_dir   = (totals_pred.get("direction") or "over").title()
    tot_pick  = f"{tot_dir} {totals_line}" if totals_line is not None else "?"
    tot_prob  = float(totals_pred.get("pick_prob") or 0)
    tot_edge  = float(totals_pred.get("edge") or 0)
    tot_odds  = (
        totals_pred.get("over_odds") if tot_dir.lower() == "over"
        else totals_pred.get("under_odds")
    )

    # ── Per-model probabilities (XGB / LR / NN) ───────────────────────
    xgb_p = pred.get("xgb_prob")
    lr_p  = pred.get("lr_prob")
    nn_p  = pred.get("nn_prob")
    models_agree = bool(pred.get("models_agree", True))

    # ── Which bet did the user click Analyze on?  Mark it + pull SHAP ─
    if bet_type in ("moneyline", "single", "ml"):
        focus_label = "Moneyline"
        focus_pick  = ml_pick_team
        focus_prob  = ml_pick_prob
        focus_edge  = ml_edge
        focus_odds  = ml_pick_odds
        focus_shap  = pred.get("shap") or []
    elif bet_type in ("run_line", "spread"):
        focus_label = "Run Line" if sport == "mlb" else "Spread"
        line_s = f" {float(rl_line):+g}" if isinstance(rl_line, (int, float)) else ""
        focus_pick = f"{rl_pick_team}{line_s}"
        focus_prob = rl_pick_prob
        focus_edge = rl_edge
        focus_odds = rl_pick_odds
        focus_shap = rl_pred.get("shap") or []
    elif bet_type == "totals":
        focus_label = "Totals"
        focus_pick  = tot_pick
        focus_prob  = tot_prob
        focus_edge  = tot_edge
        focus_odds  = tot_odds
        focus_shap  = totals_pred.get("shap") or []
    else:
        focus_label = bet_type.title()
        focus_pick  = "?"
        focus_prob  = 0.0
        focus_edge  = 0.0
        focus_odds  = None
        focus_shap  = []

    # ── SHAP top 5 with actual numeric values ─────────────────────────
    shap_lines: list[str] = []
    for s in (focus_shap or [])[:5]:
        try:
            label = (
                s.get("label")
                or _FEATURE_LABELS.get(s.get("feature", ""), s.get("feature", "factor"))
            )
            shap_val = float(s.get("shap_value") or 0)
            direction = "+" if shap_val >= 0 else ""
            shap_lines.append(
                f"  - {label}: {direction}{shap_val:.3f} "
                f"({'supports' if shap_val >= 0 else 'argues against'} the pick)"
            )
        except Exception:                                                 # noqa: BLE001
            continue
    shap_block = "\n".join(shap_lines) if shap_lines else "  (none recorded)"

    # ── Kelly / bet sizing -- pull from whichever shape carried it ────
    kelly = (
        meta.get("model_amount")
        or raw.get("bet_dollars")
        or (rl_pred.get("bet_dollars") if bet_type in ("run_line", "spread") else None)
        or (totals_pred.get("bet_dollars") if bet_type == "totals" else None)
    )
    kelly_s = f"${float(kelly):.2f}" if isinstance(kelly, (int, float)) else "?"

    # ── Upset risk ────────────────────────────────────────────────────
    upset = raw.get("upset") or {}
    upset_score = upset.get("score")
    upset_s = f"{float(upset_score):.0f}/10" if isinstance(upset_score, (int, float)) else "?"

    # ── Pitchers (MLB only; WNBA falls through to "no SP data") ───────
    if sport == "mlb":
        home_sp, away_sp = _resolve_pitcher_data_for_ai(raw, sport)
        pitching_block = (
            f"{_pitcher_block_for_ai(away_sp, 'AWAY')}\n"
            f"{_pitcher_block_for_ai(home_sp, 'HOME')}"
        )
    else:
        pitching_block = "(WNBA -- no starting pitcher data)"

    rl_line_s = f"{float(rl_line):+g}" if isinstance(rl_line, (int, float)) else "?"

    return (
        f"=== MATCHUP ===\n"
        f"Sport: {sport.upper()}\n"
        f"Game: {away} @ {home}\n"
        f"Start: {commence_time or '?'}\n"
        f"\n=== STARTING PITCHERS ===\n"
        f"{pitching_block}\n"
        f"\n=== MARKET LINES ===\n"
        f"Moneyline: {away} {_fmt_odds(ml_away_odds)} / {home} {_fmt_odds(ml_home_odds)}\n"
        f"Run Line: home {rl_line_s} at {_fmt_odds(rl_pred.get('run_line_home_odds') or rl_pred.get('pick_odds'))}, "
        f"away {(-float(rl_line)) if isinstance(rl_line, (int, float)) else '?'} "
        f"at {_fmt_odds(rl_pred.get('run_line_away_odds') or rl_pred.get('pick_odds'))}\n"
        f"Totals: O {totals_line if totals_line is not None else '?'} "
        f"at {_fmt_odds(totals_pred.get('over_odds'))} / "
        f"U {totals_line if totals_line is not None else '?'} "
        f"at {_fmt_odds(totals_pred.get('under_odds'))}\n"
        f"\n=== ALL MODEL PICKS ===\n"
        f"Moneyline: {ml_pick_team} @ {_fmt_odds(ml_pick_odds)}  "
        f"conf={_fmt_pct(ml_pick_prob)}  edge={ml_edge * 100:+.1f}%\n"
        f"Run Line/Spread: {rl_pick_team} {rl_line_s} @ {_fmt_odds(rl_pick_odds)}  "
        f"conf={_fmt_pct(rl_pick_prob)}  edge={rl_edge * 100:+.1f}%\n"
        f"Totals: {tot_pick} @ {_fmt_odds(tot_odds)}  "
        f"conf={_fmt_pct(tot_prob)}  edge={tot_edge * 100:+.1f}%\n"
        f"\n=== PER-MODEL HOME WIN PROBABILITY ===\n"
        f"XGB: {_fmt_pct(xgb_p)}   LR: {_fmt_pct(lr_p)}   NN: {_fmt_pct(nn_p)}\n"
        f"Models agree: {'YES' if models_agree else 'NO -- ensemble split'}\n"
        f"\n=== FOCUS BET (user clicked Analyze on this) ===\n"
        f"Type: {focus_label}\n"
        f"Pick: {focus_pick} @ {_fmt_odds(focus_odds)}\n"
        f"Confidence: {_fmt_pct(focus_prob)}\n"
        f"Edge over market: {focus_edge * 100:+.1f}%\n"
        f"Half-Kelly bet size: {kelly_s}\n"
        f"\n=== TOP SHAP FACTORS (with values) ===\n"
        f"{shap_block}\n"
        f"\n=== UPSET / CHAOS SCORE ===\n"
        f"{upset_s} (higher = more unpredictable matchup)"
    )
