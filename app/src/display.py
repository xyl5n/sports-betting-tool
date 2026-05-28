"""
Rich terminal display: prediction tables + SHAP waterfall charts.
Windows-safe: uses only ASCII box-drawing and passes color via style= parameter.
"""
import logging
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None  # fallback: use fixed UTC-4 offset


def _to_et(iso_utc: str) -> str:
    """Convert ISO UTC string to 'M/D H:MM AM/PM ET' format."""
    try:
        dt_utc = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        if _ET:
            dt_et = dt_utc.astimezone(_ET)
        else:
            dt_et = dt_utc.astimezone(timezone(timedelta(hours=-4)))  # EDT fallback
        return dt_et.strftime("%-m/%-d %-I:%M %p ET")
    except Exception:
        # Windows strftime doesn't support %-m; use lstrip("0") instead
        try:
            dt_utc = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
            if _ET:
                dt_et = dt_utc.astimezone(_ET)
            else:
                dt_et = dt_utc.astimezone(timezone(timedelta(hours=-4)))
            date_part = dt_et.strftime("%m/%d").lstrip("0").replace("/0", "/")
            time_part = dt_et.strftime("%I:%M %p").lstrip("0")
            return f"{date_part} {time_part} ET"
        except Exception:
            return iso_utc[:16]

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from .kelly import size_bet

# ── helpers ─────────────────────────────────────────────────────────────────────

def _edge_color(edge: float) -> str:
    if edge >= 0.05:
        return "bold green"
    if edge >= 0.02:
        return "green"
    if edge >= -0.02:
        return "yellow"
    return "red"


def _recommendation(model_prob: float, market_prob: float, conflict: bool = False) -> tuple[str, str]:
    if conflict:
        return "MODELS CONFLICT — SKIP", "bold yellow"
    edge = model_prob - market_prob
    if edge >= 0.05:
        return "STRONG BET", "bold green"
    if edge >= 0.03:
        return "VALUE BET", "green"
    if edge >= -0.03:
        return "PASS", "yellow"
    return "FADE", "red"


def _bar(value: float, max_width: int = 18) -> str:
    """ASCII signed bar. Positive = home advantage direction."""
    filled = int(min(abs(value), 1.0) * max_width)
    empty = max_width - filled
    return ("=" * filled + "." * empty) if value >= 0 else ("." * empty + "=" * filled)


def _kelly_str(model_prob: float, american_odds: int, bankroll: float) -> str:
    """Return '$X (Yu)' sized by Half Kelly. Caller must validate edge >= threshold."""
    _, dollars, units, display = size_bet(
        model_prob, american_odds, bankroll, bankroll, is_user_bet=True
    )
    return display if dollars > 0 else "--"


def _time_et(iso_utc: str) -> str:
    """Return just 'H:MM PM ET' from an ISO UTC string (strips the date prefix)."""
    parts = _to_et(iso_utc).split()  # ["5/13", "7:41", "PM", "ET"]
    if len(parts) >= 4:
        return f"{parts[1]} {parts[2]} {parts[3]}"
    return _to_et(iso_utc)


def _short_name(team: str) -> str:
    """Return compact team nickname for the Matchup column."""
    parts = team.split()
    last = parts[-1]
    if last == "Sox":
        return " ".join(parts[-2:])   # "Red Sox" / "White Sox"
    if last == "Jays":
        return "Blue Jays"
    return last


def _pick_row(game: dict, pred: dict, bankroll: float, min_edge: float = 0.0):
    """
    Determine the model's preferred side and build common row fields.
    Returns (time_str, matchup, pick_team, odds_str, model_p, pick_edge,
             bet_str, conflict, xgb_p, lr_p).
    bet_str is None when bankroll == 0.
    """
    home = game["home_team"]
    away = game["away_team"]
    home_prob   = pred["home_win_prob"]
    market_prob = game["home_implied_prob"]
    home_edge   = home_prob - market_prob
    conflict    = not pred.get("models_agree", True)
    xgb_p       = pred.get("xgb_prob", home_prob)
    lr_p        = pred.get("lr_prob",  home_prob)
    _nn         = pred.get("nn_prob")
    nn_p        = float(_nn) if _nn is not None else None

    if home_prob >= 0.5:
        pick_team = home
        odds      = int(game.get("h2h_home_odds") or -110)
        model_p   = home_prob
        pick_edge = home_edge
    else:
        pick_team = away
        odds      = int(game.get("h2h_away_odds") or -110)
        model_p   = 1 - home_prob
        pick_edge = -home_edge   # positive when away has value

    time_str = _time_et(game.get("commence_time", ""))
    matchup  = f"{_short_name(home)} vs {_short_name(away)}"
    odds_str = f"+{odds}" if odds > 0 else str(odds)

    bet_str = None
    if bankroll > 0:
        if not conflict and pick_edge >= 0.05 and odds > -300:
            bet_str = _kelly_str(model_p, odds, bankroll)
        else:
            bet_str = "--"

    return time_str, matchup, pick_team, odds_str, model_p, pick_edge, bet_str, conflict, xgb_p, lr_p, nn_p


def _slate_table(title: str, show_bet: bool, has_nn: bool = False) -> "Table":
    """Return a pre-configured Table with XGB%, LR%, (NN%,) and Avg% columns."""
    t = Table(
        title=title,
        box=box.ASCII2,
        show_header=True,
        header_style="bold",
        title_style="bold",
        expand=True,
    )
    t.add_column("Time (ET)",  style="dim", no_wrap=True, min_width=12)
    t.add_column("Matchup",    min_width=18)
    t.add_column("Pick",       min_width=20)
    t.add_column("Odds",       justify="right", min_width=7)
    t.add_column("XGB%",       justify="right", min_width=7)
    t.add_column("LR%",        justify="right", min_width=7)
    if has_nn:
        t.add_column("NN%",    justify="right", min_width=7)
    t.add_column("Avg%",       justify="right", min_width=7)
    t.add_column("Edge",       justify="right", min_width=10)
    if show_bet:
        t.add_column("Bet (Half Kelly)", justify="right", min_width=14)
    return t


def _has_nn(results: list[dict]) -> bool:
    """True when any result contains a trained NN probability."""
    return any(r["prediction"].get("nn_prob") is not None for r in results)


# ── feature labels ───────────────────────────────────────────────────────────────

_FEATURE_LABELS: dict[str, str] = {
    "net_scoring_diff":    "Net scoring margin",
    "ppg_diff":            "Points per game",
    "papg_diff":           "Points allowed/gm",
    "win_pct_diff":        "Win percentage",
    "home_away_split_diff":"Home/Away split",
    "last5_diff":          "Last-5 form",
    "home_implied_prob":   "Market win prob",
    "spread":              "Point spread",
    "net_run_diff":        "Net run margin",
    "rpg_diff":            "Runs per game",
    "rapg_diff":           "Runs allowed/gm",
    "last10_diff":         "Last-10 form",
    "hits_diff":           "Hits per game",
    "errors_diff":         "Errors (fielding)",
    "run_line":            "Run line",
    "sp_era_diff":         "SP ERA advantage",
    "sp_whip_diff":        "SP WHIP advantage",
    "sp_k_rate_diff":      "SP strikeout rate",
    "home_sp_rest":        "Home SP rest days",
    "away_sp_rest":        "Away SP rest days",
    "sp_hand_adv":         "Pitcher handedness",
    "park_run_factor":     "Ballpark run factor",
    "wind_speed":          "Wind speed (mph)",
    "wind_direction":      "Wind direction",
    "bullpen_era_diff":    "Bullpen ERA advantage",
    "bullpen_fatigue_diff":"Bullpen fatigue edge",
    "lineup_confirmed":    "Lineup confirmed",
    "line_movement":       "Line movement",
}


def _feature_context(name: str, value: float, home: str, away: str) -> str:
    if name == "home_implied_prob":
        return f"mkt {value:.0%} home"
    if name in ("spread", "run_line"):
        if value < 0:
            return f"{home[:14]} favored {abs(value):.1f}"
        elif value > 0:
            return f"{away[:14]} favored {value:.1f}"
        return "pick'em"
    if name in ("win_pct_diff", "home_away_split_diff", "last5_diff", "last10_diff"):
        return f"home {value:+.1%}"
    return f"{value:+.2f}"


def make_console() -> Console:
    """UTF-8 safe console — switches Windows codepage before creating."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception as _exc:
            logging.warning("Suppressed exception in %s: %s", __name__, _exc)
    return Console(highlight=False, safe_box=True)


# ── display class ────────────────────────────────────────────────────────────────

class Display:
    def __init__(self, console: Console):
        self.console = console

    def show_model_status(
        self, status: str,
        cv_acc: Optional[float] = None,
        lr_cv_acc: Optional[float] = None,
        nn_val_acc: Optional[float] = None,
    ) -> None:
        self.console.print(f"  Model: [cyan]{status}[/cyan]")
        parts = []
        if cv_acc:
            parts.append(f"XGBoost CV: [bold]{cv_acc:.1%}[/bold]")
        if lr_cv_acc:
            parts.append(f"LR CV: [bold]{lr_cv_acc:.1%}[/bold]")
        if nn_val_acc:
            parts.append(f"NN val: [bold]{nn_val_acc:.1%}[/bold]")
        if parts:
            self.console.print("  " + "  |  ".join(parts))

    # ── Full Slate ────────────────────────────────────────────────────────────────

    def show_predictions(self, results: list[dict], sport_cfg=None, bankroll: float = 0.0) -> None:
        if not results:
            self.console.print("[yellow]No analyzable games found.[/yellow]")
            return

        sport_label = sport_cfg.name if sport_cfg else "Game"
        show_bet    = bankroll > 0
        nn          = _has_nn(results)
        table       = _slate_table(f"{sport_label} — Full Slate", show_bet, has_nn=nn)

        for r in results:
            time_str, matchup, pick_team, odds_str, model_p, pick_edge, bet_str, conflict, xgb_p, lr_p, nn_p = \
                _pick_row(r["game"], r["prediction"], bankroll)

            if conflict:
                pick_display = Text("SKIP", style="bold yellow")
                edge_cell    = Text("models conflict", style="yellow")
            else:
                pick_display = Text(pick_team[:22])
                edge_cell    = Text(f"{pick_edge:+.1%}", style=_edge_color(pick_edge))

            row = [time_str, matchup, pick_display, odds_str, f"{xgb_p:.1%}", f"{lr_p:.1%}"]
            if nn:
                row.append(f"{nn_p:.1%}" if nn_p is not None else "--")
            row += [f"{model_p:.1%}", edge_cell]
            if show_bet:
                row.append(bet_str or "--")
            table.add_row(*row)

        self.console.print()
        self.console.print(table)
        self.console.print()

    # ── Per-game SHAP panel ───────────────────────────────────────────────────────

    def _show_game_explanation(self, result: dict, bankroll: float = 0.0) -> None:
        game      = result["game"]
        pred      = result["prediction"]
        shap_data = result.get("shap")
        meta      = result.get("meta", {})

        home        = game["home_team"]
        away        = game["away_team"]
        home_prob   = pred["home_win_prob"]
        market_prob = game["home_implied_prob"]
        edge        = home_prob - market_prob
        conflict    = not pred.get("models_agree", True)
        xgb_p       = pred.get("xgb_prob", home_prob)
        lr_p        = pred.get("lr_prob",  home_prob)
        _nn         = pred.get("nn_prob")
        nn_p        = float(_nn) if _nn is not None else None
        rec, rec_color = _recommendation(home_prob, market_prob, conflict)

        title = Text()
        title.append(home, style="bold")
        title.append(" vs ", style="dim")
        title.append(away, style="bold")
        title.append("  |  XGB: ")
        title.append(f"{xgb_p:.1%}", style="bold cyan")
        title.append("  LR: ")
        title.append(f"{lr_p:.1%}", style="bold blue")
        if nn_p is not None:
            title.append("  NN: ")
            title.append(f"{nn_p:.1%}", style="bold magenta")
        title.append("  Avg: ")
        title.append(f"{home_prob:.1%}", style="bold")
        title.append("  Market: ")
        title.append(f"{market_prob:.1%}", style="dim")
        title.append("  -> ")
        title.append(rec, style=rec_color)

        body = Text()

        if conflict:
            body.append(
                "  !! XGBoost and Logistic Regression disagree on the winner.\n"
                "     This game is flagged SKIP — no bet recommended.\n\n",
                style="bold yellow",
            )

        body.append(f"  {'Feature':<28}", style="bold")
        body.append(f"{'Bar (HOME=  AWAY=)':<20}", style="bold")
        body.append(f"  {'d-Prob':>7}", style="bold")
        body.append(f"  Context\n", style="bold")
        body.append("  " + "-" * 70 + "\n", style="dim")

        if shap_data:
            base = shap_data["base_value"]
            src  = shap_data["source"]

            for entry in shap_data["shap_values"][:8]:
                fname = entry["feature"]
                sv    = entry["shap_value"]
                fval  = entry["feature_value"]
                label = _FEATURE_LABELS.get(fname, fname)
                ctx   = _feature_context(fname, fval, home, away)
                bar   = _bar(sv / 0.15)
                color = "green" if sv > 0 else "red"

                body.append(f"  {label:<28}")
                body.append(bar + " ", style=color)
                body.append(f"{sv:+.3f}", style=color)
                body.append(f"  {ctx}\n", style="dim")

            body.append("  " + "-" * 70 + "\n", style="dim")
            nn_str = f"  NN {nn_p:.3f}" if nn_p is not None else ""
            body.append(
                f"  Base {base:.3f} -> XGB {xgb_p:.3f}  LR {lr_p:.3f}"
                f"{nn_str}  Avg {home_prob:.3f}  ({src})\n",
                style="dim",
            )
        else:
            body.append("  No explanation available.\n", style="dim")

        if bankroll > 0 and not conflict:
            if edge >= 0.05:
                odds = int(game.get("h2h_home_odds") or -110)
                if odds > -300:
                    bet = _kelly_str(home_prob, odds, bankroll)
                    body.append(f"  Half Kelly bet: ", style="dim")
                    body.append(f"{bet}", style="bold green")
                    body.append(f" on {home}\n", style="dim")
                else:
                    body.append(f"  No bet (odds {odds} too prohibitive)\n", style="dim")
            elif edge <= -0.05:
                odds = int(game.get("h2h_away_odds") or -110)
                if odds > -300:
                    bet = _kelly_str(1 - home_prob, odds, bankroll)
                    body.append(f"  Half Kelly bet: ", style="dim")
                    body.append(f"{bet}", style="bold red")
                    body.append(f" on {away}\n", style="dim")
                else:
                    body.append(f"  No bet (odds {odds} too prohibitive)\n", style="dim")
            else:
                body.append(f"  No bet (edge {abs(edge):.1%} < 5% threshold)\n", style="dim")

        h = meta.get("home_stats", {})
        a = meta.get("away_stats", {})
        if h and a:
            body.append(
                f"  {home[:20]}: {h.get('ppg',0):.1f} scored, "
                f"{h.get('papg',0):.1f} allowed, {h.get('win_pct',0):.0%} W  |  "
                f"{away[:20]}: {a.get('ppg',0):.1f} scored, "
                f"{a.get('papg',0):.1f} allowed, {a.get('win_pct',0):.0%} W\n",
                style="dim",
            )

        border = "yellow" if conflict else "blue"
        self.console.print(Panel(body, title=title, border_style=border, padding=(0, 1)))
        self.console.print()

    # ── Value Picks ───────────────────────────────────────────────────────────────

    def show_value_picks(
        self,
        results: list[dict],
        sport_cfg=None,
        bankroll: float = 0.0,
        min_edge: float = 0.05,
        max_fav_odds: int = -300,
    ) -> None:
        """Filtered table + SHAP panels. Requires both models to agree on winner."""
        nn = _has_nn(results)
        qualified = []
        for r in results:
            time_str, matchup, pick_team, odds_str, model_p, pick_edge, bet_str, conflict, xgb_p, lr_p, nn_p = \
                _pick_row(r["game"], r["prediction"], bankroll)
            odds_val = int(odds_str.replace("+", ""))
            if not conflict and pick_edge >= min_edge and odds_val > max_fav_odds:
                qualified.append((r, time_str, matchup, pick_team, odds_str, model_p, pick_edge, bet_str, xgb_p, lr_p, nn_p))

        if not qualified:
            self.console.print(
                "[yellow]No value picks found (edge >= 5%, odds better than -300, "
                "all models agree).[/yellow]\n"
            )
            return

        show_bet = bankroll > 0
        table = _slate_table(
            "Value Picks  (edge >= 5%  |  odds better than -300  |  all models agree)",
            show_bet, has_nn=nn,
        )

        for r, time_str, matchup, pick_team, odds_str, model_p, pick_edge, bet_str, xgb_p, lr_p, nn_p in qualified:
            row = [time_str, matchup, pick_team[:22], odds_str, f"{xgb_p:.1%}", f"{lr_p:.1%}"]
            if nn:
                row.append(f"{nn_p:.1%}" if nn_p is not None else "--")
            row += [f"{model_p:.1%}", Text(f"{pick_edge:+.1%}", style=_edge_color(pick_edge))]
            if show_bet:
                row.append(bet_str or "--")
            table.add_row(*row)

        self.console.print()
        self.console.print(table)
        self.console.print()

        for r, *_ in qualified:
            self._show_game_explanation(r, bankroll=bankroll)

    # ── Shared filter ────────────────────────────────────────────────────────────

    @staticmethod
    def _todays_upcoming(results: list[dict]) -> list[dict]:
        from datetime import datetime, timezone as tz
        try:
            from zoneinfo import ZoneInfo
            et = ZoneInfo("America/New_York")
        except Exception:
            from datetime import timedelta
            et = tz(timedelta(hours=-4))

        now_utc  = datetime.now(tz.utc)
        today_et = now_utc.astimezone(et).date()

        filtered = []
        for r in results:
            ct_str = r["game"].get("commence_time", "")
            try:
                ct = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
            except Exception:
                continue
            if ct <= now_utc:
                continue
            if ct.astimezone(et).date() != today_et:
                continue
            filtered.append(r)
        return filtered

    # ── Top 5 ─────────────────────────────────────────────────────────────────────

    def show_top_picks(self, results: list[dict], n_picks: int = 5, bankroll: float = 0.0) -> None:
        """Rank today's remaining agreed-upon games by edge and display the top N."""
        eligible = self._todays_upcoming(results)
        if not eligible:
            self.console.print("[yellow]No eligible games remaining today for picks.[/yellow]\n")
            return

        nn   = _has_nn(eligible)
        rows = []
        for r in eligible:
            time_str, matchup, pick_team, odds_str, model_p, pick_edge, bet_str, conflict, xgb_p, lr_p, nn_p = \
                _pick_row(r["game"], r["prediction"], bankroll)
            rows.append((pick_edge, time_str, matchup, pick_team, odds_str, model_p, bet_str, conflict, xgb_p, lr_p, nn_p))

        # Sort by edge; put conflicts at the end
        rows.sort(key=lambda x: (-x[7], -x[0]))   # conflict=True sinks to bottom
        top = rows[:n_picks]

        if not top:
            return

        show_bet = bankroll > 0
        table    = _slate_table(f"Top {n_picks} Plays (by edge)", show_bet, has_nn=nn)

        for pick_edge, time_str, matchup, pick_team, odds_str, model_p, bet_str, conflict, xgb_p, lr_p, nn_p in top:
            if conflict:
                pick_cell = Text("SKIP", style="bold yellow")
                edge_cell = Text("models conflict", style="yellow")
            else:
                pick_cell = Text(pick_team[:22])
                edge_cell = Text(f"{pick_edge:+.1%}", style=_edge_color(pick_edge))

            row = [time_str, matchup, pick_cell, odds_str, f"{xgb_p:.1%}", f"{lr_p:.1%}"]
            if nn:
                row.append(f"{nn_p:.1%}" if nn_p is not None else "--")
            row += [f"{model_p:.1%}", edge_cell]
            if show_bet:
                row.append(bet_str or "--")
            table.add_row(*row)

        self.console.print()
        self.console.print(table)
        self.console.print()

    # ── Settled bets ─────────────────────────────────────────────────────────────

    def show_settled_bets(self, settled: list[dict]) -> None:
        body = Text()
        body.append(
            f"  {'Result':<6}  {'Bet':<26}{'Odds':>6}  {'Model P&L':>10}  {'Your P&L':>10}\n",
            style="bold",
        )
        body.append("  " + "-" * 65 + "\n", style="dim")

        for b in settled:
            won  = b["result"] == "win"
            odds = b["american_odds"]
            odds_str = f"+{odds}" if odds > 0 else str(odds)
            mpnl = b["model_pnl"]
            cpnl = b["confirmed_pnl"]

            body.append("  ")
            body.append(f"{'WIN' if won else 'LOSS':<6}", style="bold green" if won else "bold red")
            body.append(f"  {b['bet_team']:<26}{odds_str:>6}  ")
            body.append(f"{mpnl:>+10.2f}", style="green" if mpnl >= 0 else "red")
            body.append("  ")
            if b.get("confirmed"):
                body.append(f"{cpnl:>+10.2f}\n", style="green" if cpnl >= 0 else "red")
            else:
                body.append(f"{'--':>10}\n", style="dim")

        body.append("  " + "-" * 65 + "\n", style="dim")
        body.append("  Your P&L: '--' means this was a model-only bet (not confirmed)\n",
                    style="dim")

        self.console.print(Panel(body, title="Settled Bets", border_style="cyan", padding=(0, 1)))
        self.console.print()

    # ── Bankroll summary ──────────────────────────────────────────────────────────

    def show_bankroll_summary(self, ledger) -> None:
        s       = ledger.get_summary()
        mw, ml  = s["model_record"]
        cw, cl  = s["confirmed_record"]
        tm, tc  = mw + ml, cw + cl

        body = Text()

        def _row(label: str, bankroll: float, wins: int, losses: int,
                 total: int, pnl: float) -> None:
            pnl_color = "green" if pnl >= 0 else "red"
            body.append(f"  {label:<22}", style="dim")
            body.append(f"${bankroll:>8,.2f}  ", style="bold")
            if total > 0:
                body.append(f"{wins}-{losses} ({wins/total:.0%})  ", style="dim")
            else:
                body.append("no history   ", style="dim")
            body.append(f"{pnl:>+8.2f}\n", style=pnl_color)

        _row("Model (all picks)", s["model_bankroll"],    mw, ml, tm, s["model_pnl"])
        _row("Personal (yours)",  s["personal_bankroll"], cw, cl, tc, s["confirmed_pnl"])

        if s["open_bets"] > 0:
            body.append(
                f"\n  Pending: {s['open_bets']} model, {s['open_confirmed']} confirmed"
                f"  (settle when results post)\n",
                style="dim",
            )

        self.console.print(Panel(body, title="Bankroll Tracker", border_style="cyan",
                                 padding=(0, 1)))
        self.console.print()

    # ── 2-leg parlay ──────────────────────────────────────────────────────────────

    def show_parlay(self, results: list[dict], legs: int = 2) -> None:
        """Pick the N highest-confidence agreed-upon legs from today's remaining games."""
        eligible = self._todays_upcoming(results)

        rows = []
        for r in eligible:
            time_str, matchup, pick_team, odds_str, model_p, pick_edge, _, conflict, xgb_p, lr_p, nn_p = \
                _pick_row(r["game"], r["prediction"], 0.0)
            if conflict:
                continue  # don't include conflicted picks in parlays
            rows.append((model_p, time_str, matchup, pick_team, odds_str, pick_edge))

        rows.sort(key=lambda x: x[0], reverse=True)

        if len(rows) < legs:
            return

        parlay_legs = rows[:legs]

        combined_prob  = 1.0
        parlay_decimal = 1.0
        for model_p, _, _, _, odds_str, _ in parlay_legs:
            combined_prob  *= model_p
            odds_val        = int(odds_str.replace("+", ""))
            if odds_val > 0:
                parlay_decimal *= odds_val / 100 + 1
            else:
                parlay_decimal *= 100 / abs(odds_val) + 1

        fair_mult   = 1.0 / combined_prob if combined_prob > 0 else 0
        if parlay_decimal >= 2.0:
            parlay_american = int((parlay_decimal - 1) * 100)
        else:
            parlay_american = int(-100 / (parlay_decimal - 1))
        parlay_odds_str = f"+{parlay_american}" if parlay_american > 0 else str(parlay_american)

        table = _slate_table(f"{legs}-Leg Parlay — Highest Confidence (models agree)", False)
        for model_p, time_str, matchup, pick_team, odds_str, pick_edge in parlay_legs:
            table.add_row(
                time_str, matchup, pick_team[:22], odds_str,
                "--", "--", f"{model_p:.1%}",
                Text(f"{pick_edge:+.1%}", style=_edge_color(pick_edge)),
            )

        self.console.print()
        self.console.print(table)
        self.console.print(
            f"  Combined probability: [bold cyan]{combined_prob:.1%}[/bold cyan]"
            f"  (fair: {fair_mult:.2f}x)   "
            f"Payout: [bold]{parlay_odds_str}[/bold] ({parlay_decimal:.2f}x)"
        )
        self.console.print()
