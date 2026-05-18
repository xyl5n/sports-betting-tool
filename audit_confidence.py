"""
Confidence-constraint audit for MLB analysis results.

Reads either:
  - app/data/daily_snapshot.json  (today's locked picks; the default), or
  - a path passed as argv[1]      (any analysis_cache or snapshot JSON)

and prints a per-game breakdown showing the XGB / LR / NN raw probabilities,
the ensemble averages, any upset-factor adjustments, and the final displayed
confidence percentage for moneyline / run line / totals.  At the end it checks
the two mathematical invariants the run-line ensemble must satisfy and lists
any violations.

Run from the repo root:

    python audit_confidence.py
    python audit_confidence.py app/data/daily_snapshot.json
"""
from __future__ import annotations
import json
import sys
from pathlib import Path


def _fav_label(g: dict) -> tuple[str, str, bool]:
    """Return (favorite_team, underdog_team, home_is_favorite)."""
    home_odds = g.get("home_odds", -110)
    away_odds = g.get("away_odds", -110)
    home_is_fav = home_odds < away_odds   # more-negative odds = favorite
    if home_is_fav:
        return g["home_team"], g["away_team"], True
    return g["away_team"], g["home_team"], False


def _audit_game(idx: int, g: dict) -> dict:
    """Print a detailed breakdown for one game; return a violation summary."""
    fav, dog, home_is_fav = _fav_label(g)
    rl = g.get("run_line") or {}
    t  = g.get("totals") or {}

    print(f"#{idx:2d}  {g['away_team']} @ {g['home_team']}")
    print(f"     favorite: {fav}    underdog: {dog}    line: {rl.get('run_line_point', '?'):+.1f}")
    print(f"     odds:     home {g.get('home_odds')}    away {g.get('away_odds')}    market home_implied={g.get('home_implied_prob'):.3f}")
    print()

    # ── Moneyline ──────────────────────────────────────────────────────────
    ml_xgb  = g.get("xgb_prob")
    ml_lr   = g.get("lr_prob")
    ml_nn   = g.get("nn_prob")
    ml_home = g.get("home_win_prob")            # raw ensemble P(home wins)
    ml_pick_prob = g.get("pick_prob")           # post-upset-adj displayed
    ml_pick_side = g.get("pick_side")
    print("     ── MONEYLINE ─────────────────────────────────────────────────")
    print(f"       raw   XGB(home)={ml_xgb:.3f}   LR(home)={ml_lr:.3f}   NN(home)={ml_nn:.3f}")
    print(f"       ensemble P(home wins) = {ml_home:.3f}")
    print(f"       pick: {g.get('pick_team')} ({ml_pick_side})")
    print(f"       displayed confidence  = {ml_pick_prob:.3f}   (after upset adjust)")

    # ── Run line ───────────────────────────────────────────────────────────
    print("     ── RUN LINE ──────────────────────────────────────────────────")
    print(f"       raw   XGB(home cover)={rl.get('xgb_prob', float('nan')):.3f}   "
          f"LR(home cover)={rl.get('lr_prob', float('nan')):.3f}")
    print(f"       ensemble home_cover_prob = {rl.get('home_cover_prob', float('nan')):.3f}")
    print(f"       pick: {rl.get('pick_team')} ({rl.get('side')})  pt={rl.get('run_line_point'):+.1f}")
    print(f"       displayed confidence  = {rl.get('pick_prob', float('nan')):.3f}   (after upset adjust)")

    # ── Totals ─────────────────────────────────────────────────────────────
    print("     ── TOTALS ────────────────────────────────────────────────────")
    print(f"       XGB pred={t.get('xgb_pred', float('nan')):.2f} runs   "
          f"LR pred={t.get('lr_pred', float('nan')):.2f} runs   "
          f"ensemble={t.get('predicted_total', float('nan')):.2f}   line={t.get('total_line')}")
    print(f"       direction: {t.get('direction','?').upper()}  "
          f"displayed confidence = {t.get('pick_prob', float('nan')):.3f}")

    # ── Constraint checks (same-team, model-output level) ──────────────────
    # Convert ensemble home P(win) and P(home cover line) into same-team
    # probabilities for the favorite and the underdog.
    rl_home_cover = rl.get("home_cover_prob")
    if rl_home_cover is None or ml_home is None:
        print()
        return {"violation": False, "missing": True}

    ml_fav = ml_home if home_is_fav else 1 - ml_home
    ml_dog = 1 - ml_fav
    if home_is_fav:
        rl_fav_minus_1_5 = rl_home_cover                # home covers -1.5
        rl_dog_plus_1_5  = 1 - rl_home_cover            # away covers +1.5
    else:
        rl_fav_minus_1_5 = 1 - rl_home_cover            # away covers -1.5
        rl_dog_plus_1_5  = rl_home_cover                # home covers +1.5

    c1 = ml_fav + 1e-9 >= rl_fav_minus_1_5
    c2 = ml_dog - 1e-9 <= rl_dog_plus_1_5
    print()
    print(f"     Constraint 1  ML({fav}) {ml_fav:.3f}  >=  RL({fav} -1.5) {rl_fav_minus_1_5:.3f}   "
          f"{'OK' if c1 else '✗ VIOLATED'}")
    print(f"     Constraint 2  ML({dog}) {ml_dog:.3f}  <=  RL({dog} +1.5) {rl_dog_plus_1_5:.3f}   "
          f"{'OK' if c2 else '✗ VIOLATED'}")

    # Also surface what the UI tile would display, post-fix.  These use the
    # upset-adjusted pick_prob (the field totals already used) so ML / RL
    # tiles are on the same scale.  Note: run_line_point is the HOME team's
    # line; flip the sign so the displayed line matches the picker's side.
    ml_tile = ml_pick_prob
    rl_tile = rl.get("pick_prob")
    if rl_tile is not None:
        home_pt = rl.get("run_line_point")
        pick_pt = home_pt if rl.get("side") == "home" else -home_pt if home_pt is not None else None
        pick_pt_str = f"{pick_pt:+.1f}" if pick_pt is not None else "?"
        print(f"     UI tiles      ML pick {g.get('pick_team')} = {ml_tile:.3f}   "
              f"RL pick {rl.get('pick_team')} {pick_pt_str} = {rl_tile:.3f}")
    print()
    return {
        "violation":  (not c1) or (not c2),
        "c1_ok":      c1,
        "c2_ok":      c2,
        "matchup":    f"{g['away_team']} @ {g['home_team']}",
        "fav":        fav,
        "dog":        dog,
        "rl_point":   rl.get("run_line_point"),
        "ml_fav":     ml_fav,
        "rl_fav":     rl_fav_minus_1_5,
        "ml_dog":     ml_dog,
        "rl_dog":     rl_dog_plus_1_5,
    }


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("app/data/daily_snapshot.json")
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 1

    blob = json.loads(path.read_text())

    # Snapshot has {"mlb": {"results": [...]}, "wnba": {...}}.
    # analysis_cache has {"results": [...]} directly.
    if isinstance(blob.get("mlb"), dict):
        games = blob["mlb"].get("results", [])
        print(f"reading {path}  (snapshot for {blob.get('date')}, {len(games)} MLB games)")
        print()
    else:
        games = blob.get("results", [])
        print(f"reading {path}  ({len(games)} games)")
        print()

    if not games:
        print("no games to audit.")
        return 0

    summary = []
    for i, g in enumerate(games, 1):
        summary.append(_audit_game(i, g))

    n_violations = sum(1 for s in summary if s.get("violation"))
    print("=" * 76)
    print(f"SUMMARY:  {n_violations} violation(s) across {len(games)} games")
    for s in summary:
        if not s.get("violation"):
            continue
        msgs = []
        if not s.get("c1_ok"):
            msgs.append(f"C1  ML({s['fav']})={s['ml_fav']:.3f} < RL({s['fav']} -1.5)={s['rl_fav']:.3f}")
        if not s.get("c2_ok"):
            msgs.append(f"C2  ML({s['dog']})={s['ml_dog']:.3f} > RL({s['dog']} +1.5)={s['rl_dog']:.3f}")
        print(f"  {s['matchup']}  (line {s['rl_point']:+.1f}):  " + "  |  ".join(msgs))
    return 0 if n_violations == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
