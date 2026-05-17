"""
Sports Betting Analysis Tool — MLB & WNBA
Usage:
    python main.py                          # analyse upcoming MLB games (default)
    python main.py --sport wnba             # analyse upcoming WNBA games
    python main.py --retrain                # force re-train the XGBoost model
    python main.py --season 2025            # override season year for training
    python main.py --games 5               # limit output to first 5 games
"""
import argparse
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from src.cache import Cache
from src.display import Display, make_console
from src.explainer import PredictionExplainer
from src.game_store import GameStore
from src.ledger import Ledger
from src.model import BettingModel
from src.odds_client import OddsClient
from src.sports_config import SPORTS


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Sports betting analysis — XGBoost + SHAP")
    parser.add_argument("--sport", choices=list(SPORTS.keys()), default="mlb")
    parser.add_argument("--retrain", action="store_true", help="Force model retraining")
    parser.add_argument(
        "--season", type=int, default=int(os.getenv("SEASON", 2025)),
        help="Season year for training data (default: SEASON env or 2025)",
    )
    parser.add_argument("--games", type=int, default=0, help="Limit to first N games (0 = all)")
    args = parser.parse_args()

    odds_key = os.getenv("ODDS_API_KEY", "")
    sports_key = os.getenv("API_SPORTS_KEY", "")

    if not odds_key or odds_key == "your_odds_api_key_here":
        sys.exit("ERROR: ODDS_API_KEY not set. Add it to your .env file.")
    if not sports_key or sports_key == "your_api_sports_key_here":
        sys.exit("ERROR: API_SPORTS_KEY not set. Add it to your .env file.")

    sport_cfg = SPORTS[args.sport]
    console = make_console()
    display = Display(console)

    console.print()
    console.print(
        f"[bold white on dark_blue]  {sport_cfg.name} Betting Analysis "
        f"— XGBoost + SHAP  [/bold white on dark_blue]"
    )
    console.print()

    # ── Bankroll prompt ─────────────────────────────────────────────────────────
    _env_bankroll = float(os.getenv("BANKROLL", "0") or "0")
    _prompt = (
        f"  Bankroll [${_env_bankroll:,.0f}] (Enter to keep, or type new amount): "
        if _env_bankroll > 0
        else "  Bankroll ($ amount, Enter to skip): "
    )
    try:
        _raw = input(_prompt).strip().lstrip("$").replace(",", "")
        bankroll = float(_raw) if _raw else _env_bankroll
    except (ValueError, EOFError):
        bankroll = _env_bankroll
    if bankroll > 0:
        console.print(f"  Bankroll: [bold]${bankroll:,.2f}[/bold]  "
                      f"[dim](Half Kelly sizing, capped at 5% per bet)[/dim]")
    console.print()

    cache = Cache()
    odds_client = OddsClient(odds_key, cache)
    model = BettingModel(sport_cfg)
    explainer = PredictionExplainer(sport_cfg)

    # ── Ledger: settle open bets before new analysis ─────────────────────────────
    ledger = Ledger(path="data/ledger.json", starting_bankroll=bankroll or 250.0)
    settled = ledger.settle(odds_client, sport_cfg.odds_key)
    if settled:
        display.show_settled_bets(settled)
    if ledger.is_active():
        display.show_bankroll_summary(ledger)

    # ── Load game data (1 API call, cached 24 h) ────────────────────────────────
    console.print("[bold]Step 1 — Season data[/bold]")
    store = GameStore(
        api_key=sports_key,
        base_url=sport_cfg.api_sports_base,
        league_id=sport_cfg.league_id,
        sport_tag=args.sport,
        cache=cache,
    )
    with console.status(f"Loading {sport_cfg.name} {args.season} season data…"):
        n_completed = store.load(args.season)
    console.print(f"  Loaded [bold]{n_completed}[/bold] completed games for training.\n")

    # ── Build sport-specific feature builder ────────────────────────────────────
    from src.mlb_features import MLBFeatureBuilder
    feature_builder = MLBFeatureBuilder(store)

    # ── Train / load model ──────────────────────────────────────────────────────
    console.print("[bold]Step 2 — Model[/bold]")
    with console.status("Training / loading model…"):
        status = model.train_or_load(
            stats_client=store,
            feature_builder=feature_builder,
            season=args.season,
            force_retrain=args.retrain,
        )
    display.show_model_status(status, model.cv_accuracy, model.lr_cv_accuracy, model.nn_val_accuracy)
    console.print()

    # ── Fetch upcoming games + odds ─────────────────────────────────────────────
    console.print("[bold]Step 3 — Upcoming games[/bold]")
    with console.status(f"Fetching current {sport_cfg.name} odds…"):
        games = odds_client.get_odds(sport_key=sport_cfg.odds_key)

    if not games:
        hints = {
            "mlb":  "MLB regular season runs March–October.",
            "wnba": "WNBA regular season runs May–September.",
        }
        console.print(f"[yellow]No upcoming {sport_cfg.name} games found.[/yellow]")
        console.print(f"[dim]{hints.get(args.sport, '')}[/dim]")
        return

    if args.games > 0:
        games = games[: args.games]

    console.print(f"  Found [bold]{len(games)}[/bold] upcoming games.\n")

    # ── Enrich + predict + explain ──────────────────────────────────────────────
    console.print("[bold]Step 4 — Analysis[/bold]")
    results = []
    for game in games:
        home, away = game["home_team"], game["away_team"]
        with console.status(f"Analysing {home} vs {away}…"):
            built = feature_builder.build_for_game(game)

        if built is None:
            console.print(f"  [dim]Skipping {home} vs {away} — stats unavailable[/dim]")
            continue

        feature_vec, meta = built
        prediction = model.predict(feature_vec)
        shap_result = explainer.explain(
            feature_vec,
            model=model.get_raw_model(),
            scaler=model.get_scaler(),
            is_trained=model.is_trained,
        )

        results.append({
            "game": game,
            "prediction": prediction,
            "shap": shap_result,
            "meta": meta,
        })

    # ── Display — 4 sections ─────────────────────────────────────────────────────
    console.print()
    console.print("[bold]Step 5 — Results[/bold]")

    console.rule("[bold]1 — Full Slate[/bold]", style="dim")
    display.show_predictions(results, sport_cfg=sport_cfg, bankroll=bankroll)

    console.rule("[bold]2 — Value Picks  (edge >= 5%  |  odds better than -300)[/bold]",
                 style="dim")
    display.show_value_picks(results, sport_cfg=sport_cfg, bankroll=bankroll)

    console.rule("[bold]3 — Top 5 Plays[/bold]", style="dim")
    display.show_top_picks(results, n_picks=5, bankroll=bankroll)

    console.rule("[bold]4 — 2-Leg Parlay[/bold]", style="dim")
    display.show_parlay(results, legs=2)

    console.print("[dim]Disclaimer: predictions are for informational purposes only.[/dim]\n")

    # ── Step 6 — Bet tracking ────────────────────────────────────────────────────
    console.print("[bold]Step 6 — Bet Tracking[/bold]")
    now_utc = datetime.now(timezone.utc)

    # Collect all upcoming games (not yet started)
    upcoming = []
    for r in results:
        try:
            ct = datetime.fromisoformat(
                r["game"].get("commence_time", "").replace("Z", "+00:00")
            )
            if ct > now_utc:
                upcoming.append(r)
        except Exception:
            pass

    # Pass 1 — silently auto-log model picks (edge ≥ 5%, odds better than -300,
    #           both models agree on the winner)
    auto_count = 0
    for r in upcoming:
        game = r["game"]
        if ledger.has_bet(game["id"]):
            continue
        pred       = r["prediction"]
        if not pred.get("models_agree", True):
            continue   # skip conflicted games
        home_prob  = float(pred["home_win_prob"])
        edge       = home_prob - float(game["home_implied_prob"])
        h_odds     = int(game.get("h2h_home_odds") or -110)
        a_odds     = int(game.get("h2h_away_odds") or -110)

        if edge >= 0.05 and h_odds > -300:
            side, team, odds, model_p = "home", game["home_team"], h_odds, home_prob
        elif edge <= -0.05 and a_odds > -300:
            side, team, odds, model_p = "away", game["away_team"], a_odds, 1 - home_prob
        else:
            continue

        model_amt, _ = ledger.kelly_amounts(model_p, odds)
        if model_amt <= 0:
            continue

        odds_str = f"+{odds}" if odds > 0 else str(odds)
        console.print(
            f"  [dim]Model logs:[/dim] [bold]{team}[/bold] ({odds_str})"
            f"  ${model_amt:.0f} | edge {abs(edge):.1%}"
        )
        ledger.add_bet(
            game=game, sport=args.sport, sport_key=sport_cfg.odds_key,
            side=side, team=team, odds=odds,
            model_prob=model_p, edge=abs(edge),
            model_amount=model_amt, confirmed=False, confirmed_amount=0.0,
        )
        auto_count += 1

    if auto_count > 0:
        ledger.save()
        console.print()

    # Pass 2 — manual pick: show ALL upcoming games, let user choose any to track
    already_confirmed = {
        b["game_id"] for b in ledger.data["open_bets"] if b.get("confirmed")
    }
    pickable = [r for r in upcoming if r["game"]["id"] not in already_confirmed]

    if pickable:
        console.print("  [dim]All upcoming games — enter numbers to add to YOUR ledger:[/dim]")
        for i, r in enumerate(pickable, 1):
            game      = r["game"]
            pred      = r["prediction"]
            conflict  = not pred.get("models_agree", True)
            home_prob = float(pred["home_win_prob"])
            xgb_p     = float(pred.get("xgb_prob", home_prob))
            lr_p      = float(pred.get("lr_prob",  home_prob))
            _nn       = pred.get("nn_prob")
            nn_p      = float(_nn) if _nn is not None else None
            edge      = home_prob - float(game["home_implied_prob"])
            if home_prob >= 0.5:
                team      = game["home_team"]
                odds      = int(game.get("h2h_home_odds") or -110)
                pick_edge = edge
            else:
                team      = game["away_team"]
                odds      = int(game.get("h2h_away_odds") or -110)
                pick_edge = -edge
            odds_str = f"+{odds}" if odds > 0 else str(odds)
            if conflict:
                tag = " [bold yellow]CONFLICT[/bold yellow]"
            elif pick_edge >= 0.05 and odds > -300:
                tag = " [bold green]VALUE[/bold green]"
            else:
                tag = ""
            nn_str = f" / NN {nn_p:.0%}" if nn_p is not None else ""
            console.print(
                f"    [dim]{i:2}.[/dim]  {game['home_team']} vs {game['away_team']}"
                f"  |  Pick: [bold]{team}[/bold] {odds_str}"
                f"  |  XGB {xgb_p:.0%} / LR {lr_p:.0%}{nn_str} / Avg {home_prob:.0%}"
                f"  |  Edge: {pick_edge:+.1%}{tag}"
            )

        try:
            raw = input("\n  Track (comma-separated numbers, or Enter to skip): ").strip()
        except EOFError:
            raw = ""

        manual_count = 0
        if raw:
            for token in raw.split(","):
                try:
                    idx = int(token.strip()) - 1
                    if not (0 <= idx < len(pickable)):
                        continue
                    r         = pickable[idx]
                    game      = r["game"]
                    home_prob = float(r["prediction"]["home_win_prob"])
                    edge      = home_prob - float(game["home_implied_prob"])
                    if home_prob >= 0.5:
                        side, team, odds, model_p = "home", game["home_team"], int(game.get("h2h_home_odds") or -110), home_prob
                        pick_edge = edge
                    else:
                        side, team, odds, model_p = "away", game["away_team"], int(game.get("h2h_away_odds") or -110), 1 - home_prob
                        pick_edge = -edge
                    odds_str  = f"+{odds}" if odds > 0 else str(odds)
                    model_amt, conf_amt = ledger.kelly_amounts(model_p, odds)
                    if model_amt == 0:
                        model_amt = round(bankroll * 0.02, 2) if bankroll > 0 else 5.0
                    if conf_amt == 0:
                        conf_amt  = round((ledger.data.get("personal_bankroll", bankroll or 250)) * 0.02, 2)

                    # Promote existing model bet to confirmed, or add fresh
                    existing = next(
                        (b for b in ledger.data["open_bets"] if b["game_id"] == game["id"]),
                        None,
                    )
                    if existing:
                        existing["confirmed"]        = True
                        existing["confirmed_amount"] = round(conf_amt, 2)
                    else:
                        ledger.add_bet(
                            game=game, sport=args.sport, sport_key=sport_cfg.odds_key,
                            side=side, team=team, odds=odds,
                            model_prob=model_p, edge=abs(pick_edge),
                            model_amount=model_amt, confirmed=True, confirmed_amount=conf_amt,
                        )
                    console.print(f"  ✓ Tracked: [bold]{team}[/bold] ({odds_str})  ${conf_amt:.0f}")
                    manual_count += 1
                except (ValueError, IndexError):
                    continue

        if manual_count > 0:
            ledger.save()
            console.print(f"\n  Added [bold]{manual_count}[/bold] manual bet(s) to YOUR ledger.\n")
        else:
            console.print()
    else:
        console.print("  All upcoming games are already in your ledger.\n")

    display.show_bankroll_summary(ledger)


if __name__ == "__main__":
    main()
