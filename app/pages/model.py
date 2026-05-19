"""
Model page -- the full model tracker.

Five sections, top-to-bottom:
  1. MODEL BANKROLL (Start / Current / P&L / Record / At Risk)
  2. RECORDS BY BET TYPE (Moneyline / RL-Spread / Totals)
  3. TODAY'S MODEL PICKS (top-5 per category from daily_picks.json)
  4. CLASSIFIER ACCURACY (XGB / LR / NN -- correct-call rates)

Data sources, all via the imported backend module:
  - backend.Ledger("data/ledger.json").get_summary()  + WNBA
  - backend.load_daily_picks()
  - hand-rolled aggregation over ledger.data["history"] for type records +
    classifier accuracy (mirrors what the legacy /api/model_performance did)
"""
from __future__ import annotations

from nicegui import ui

from components import theme as t
from components import navbar, sidebar


_CATS = (
    ("moneyline",       "Moneyline",        ("single",)),
    ("run_line_spread", "Run Line / Spread", ("run_line", "spread")),
    ("totals",          "Totals",            ("totals",)),
)


def register(backend) -> None:
    @ui.page("/model")
    def model_page():
        ui.add_head_html(t.page_head_css())
        navbar.render(active=t.TAB_MODEL)
        with ui.row().classes("no-wrap w-full").style("gap: 0;"):
            sidebar.render(backend)
            with ui.column().style(
                f"flex: 1; max-width: {t.MAX_CONTENT_W}; "
                f"gap: {t.SPACE_LG}; padding: {t.SPACE_LG}; min-width: 0;"
            ):
                history = _all_model_history(backend)
                _bankroll_card(backend, history)
                _type_records_card(history)
                _picks_card(backend)
                _classifier_card(history)


# ── Data helpers ────────────────────────────────────────────────────────────

def _all_model_history(backend) -> list[dict]:
    """Return non-confirmed (i.e. model-only) settled bets from both ledgers."""
    out: list[dict] = []
    for path in ("data/ledger.json", "data/wnba_ledger.json"):
        try:
            led = backend.Ledger(path=path, starting_bankroll=1000.0)
        except Exception:                                                 # noqa: BLE001
            continue
        for h in (led.data.get("history") or []):
            out.append(h)
    return out


# ── Section: Model bankroll ─────────────────────────────────────────────────

def _bankroll_card(backend, history: list[dict]) -> None:
    try:
        mlb  = backend.Ledger(path="data/ledger.json",      starting_bankroll=1000.0)
        wnba = backend.Ledger(path="data/wnba_ledger.json", starting_bankroll=1000.0)
        start = float(mlb.data.get("model_starting_bankroll", 1000.0))
        current = float(mlb.data.get("model_bankroll", start))
        # Open model bets across both ledgers, sum their stakes
        at_risk = sum(
            float(b.get("model_amount") or 0)
            for ld in (mlb, wnba)
            for b in (ld.data.get("open_bets") or [])
            if not b.get("confirmed") and not b.get("limit_reached")
        )
    except Exception:                                                     # noqa: BLE001
        start, current, at_risk = 1000.0, 1000.0, 0.0

    pnl = current - start
    w = sum(1 for h in history if h.get("result") == "win")
    l = sum(1 for h in history if h.get("result") == "loss")
    total = w + l
    pct = f"{(w / total * 100):.1f}%" if total else "—"

    pnl_color = t.POS if pnl >= 0 else t.NEG
    pnl_sign  = "+" if pnl >= 0 else "−"

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_LG}; padding: {t.SPACE_LG}; gap: {t.SPACE_MD};"
    ):
        ui.label("MODEL BANKROLL").style(
            f"font-size: 11px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2};"
        )
        with ui.row().classes("w-full").style(f"gap: {t.SPACE_MD};"):
            _stat("START",   f"${start:,.2f}",     t.TEXT_DIM)
            _stat("CURRENT", f"${current:,.2f}",   t.TEXT)
            _stat("P / L",   f"{pnl_sign}${abs(pnl):,.2f}", pnl_color)
        with ui.row().classes("w-full justify-between").style("gap: 12px; padding-top: 6px;"):
            ui.label(f"Record  {w}-{l}" + ("" if pct == "—" else f"  ({pct})")).style(
                f"color: {t.TEXT_DIM}; font-size: 12px; font-family: monospace;"
            )
            ui.label(f"At Risk  ${at_risk:,.2f}").style(
                f"color: {t.WARN}; font-size: 12px; font-family: monospace;"
            )


def _stat(label: str, value: str, color: str) -> None:
    with ui.column().style(
        f"flex: 1; background: {t.CARD_HI}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_SM}; padding: 10px 12px; gap: 4px;"
    ):
        ui.label(label).style(
            f"font-size: 10px; font-weight: 700; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2};"
        )
        ui.label(value).style(
            f"font-size: 18px; font-weight: 800; color: {color}; "
            f"font-family: monospace; letter-spacing: -.2px;"
        )


# ── Section: Records by bet type ────────────────────────────────────────────

def _type_records_card(history: list[dict]) -> None:
    rows: list[tuple[str, int, int]] = []
    for _, label, aliases in _CATS:
        sub = [h for h in history if (h.get("bet_type") or "single") in aliases]
        w = sum(1 for h in sub if h.get("result") == "win")
        l = sum(1 for h in sub if h.get("result") == "loss")
        rows.append((label, w, l))

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: {t.SPACE_SM};"
    ):
        ui.label("RECORD BY BET TYPE").style(
            f"font-size: 11px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2};"
        )
        for label, w, l in rows:
            total = w + l
            pct = f"{(w / total * 100):.1f}%" if total else "—"
            with ui.row().classes("w-full justify-between items-center").style(
                f"padding: 6px 0; border-bottom: 1px solid {t.BORDER_SOFT};"
            ):
                ui.label(label).style(f"color: {t.TEXT_DIM}; font-size: 12px;")
                ui.label(f"{w}-{l}" + ("" if pct == "—" else f"  ({pct})")).style(
                    f"color: {t.TEXT}; font-size: 12px; font-family: monospace;"
                )


# ── Section: Today's model picks ────────────────────────────────────────────

def _picks_card(backend) -> None:
    try:
        daily = backend.load_daily_picks() or {}
        picks = daily.get("picks") or {}
    except Exception:                                                     # noqa: BLE001
        picks = {}

    ui.label("TODAY'S MODEL PICKS").style(
        f"font-size: 11px; font-weight: 800; letter-spacing: .8px; "
        f"color: {t.TEXT_DIM2};"
    )
    any_rendered = False
    for cat_key, label, _aliases in _CATS:
        arr = picks.get(cat_key) or []
        if not arr:
            continue
        any_rendered = True
        with ui.column().classes("w-full").style(
            f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 6px;"
        ):
            with ui.row().classes("items-center w-full justify-between"):
                ui.label(label.upper()).style(
                    f"font-size: 12px; font-weight: 800; letter-spacing: .8px; "
                    f"color: {t.TEXT};"
                )
                ui.label(f"{len(arr)} pick{'s' if len(arr) != 1 else ''}").style(
                    f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                    f"font-size: 10px; font-weight: 700; "
                    f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
                )
            for p in arr:
                _pick_row(p, cat_key)

    if not any_rendered:
        with ui.column().classes("w-full").style(
            f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_LG}; align-items: center;"
        ):
            ui.label("No picks generated yet -- run analysis.").style(
                f"color: {t.TEXT_DIM}; font-size: 12px;"
            )


def _pick_row(p: dict, cat_key: str) -> None:
    rank   = p.get("rank", "·")
    team   = p.get("team", "—")
    sport  = (p.get("sport_label") or p.get("sport") or "").upper()
    prob   = float(p.get("pick_prob") or 0) * 100
    odds   = p.get("odds")
    odds_s = f"+{odds}" if isinstance(odds, (int, float)) and odds > 0 else f"{odds}"
    amt    = p.get("model_amount")
    amt_s  = f"${float(amt):.0f}" if amt is not None else "—"
    line   = p.get("prop_line")
    line_s = ""
    if cat_key == "run_line_spread" and line is not None:
        try:
            pt = float(line)
            line_s = f" {pt:+g}"
        except Exception:                                                 # noqa: BLE001
            line_s = ""
    below = p.get("below_threshold")

    with ui.row().classes("items-center w-full").style(
        f"padding: 6px 0; border-bottom: 1px solid {t.BORDER_SOFT}; gap: 10px;"
    ):
        ui.label(f"{rank}").style(
            f"color: {t.TEXT_DIM}; font-weight: 800; min-width: 18px; "
            f"font-family: monospace; text-align: center;"
        )
        with ui.column().style("flex: 1; gap: 2px; min-width: 0;"):
            ui.label(f"{team}{line_s}").style(
                f"font-size: 13px; font-weight: 700; color: {t.TEXT}; "
                f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            if below:
                ui.label("BELOW THRESHOLD").style(
                    f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {t.WARN};"
                )
        ui.label(sport).style(
            f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
            f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
            f"padding: 1px 7px; border-radius: {t.RADIUS_PILL};"
        )
        with ui.row().style("gap: 10px; font-family: monospace;"):
            ui.label(f"{prob:.0f}%").style(
                f"font-size: 12px; font-weight: 700; color: {t.PRIMARY};"
            )
            ui.label(odds_s).style(f"font-size: 11px; color: {t.TEXT_DIM};")
            ui.label(amt_s).style(f"font-size: 12px; font-weight: 700; color: {t.TEXT};")


# ── Section: Per-classifier accuracy ────────────────────────────────────────

def _classifier_card(history: list[dict]) -> None:
    models = ("xgb", "lr", "nn")
    labels = {"xgb": "XGBoost", "lr": "Logistic Regression", "nn": "Neural Net"}
    # tallies[model][bet_type] = [correct, total]
    tallies: dict[str, dict[str, list[int]]] = {
        m: {k: [0, 0] for k, _, _ in _CATS} for m in models
    }
    overall: dict[str, list[int]] = {m: [0, 0] for m in models}

    for bet in history:
        result = bet.get("result")
        if result not in ("win", "loss"):
            continue
        side = bet.get("bet_side") or bet.get("side") or "home"
        bt   = bet.get("bet_type") or "single"
        cat  = next((k for k, _, aliases in _CATS if bt in aliases), None)
        if cat is None:
            continue
        for m in models:
            p = bet.get(f"{m}_prob")
            if p is None:
                continue
            picks_home = float(p) >= 0.5
            bet_on_home = side == "home"
            home_won = bet_on_home if result == "win" else not bet_on_home
            correct = (picks_home == home_won)
            tallies[m][cat][1] += 1
            tallies[m][cat][0] += int(correct)
            overall[m][1] += 1
            overall[m][0] += int(correct)

    # Best model by overall win % (minimum 10 settled bets to qualify)
    best = None
    best_pct = 0.0
    for m in models:
        c, n = overall[m]
        if n >= 10 and (c / n) > best_pct:
            best_pct = c / n
            best = m

    ui.label("CLASSIFIER ACCURACY").style(
        f"font-size: 11px; font-weight: 800; letter-spacing: .8px; "
        f"color: {t.TEXT_DIM2};"
    )
    with ui.row().classes("w-full").style(f"gap: {t.SPACE_MD};"):
        for m in models:
            _classifier_block(m, labels[m], overall[m], tallies[m], m == best)


def _classifier_block(model: str, label: str, ov: list[int], by_cat: dict[str, list[int]], is_best: bool) -> None:
    correct, total = ov
    pct = (correct / total * 100) if total else None
    pct_s = "—" if pct is None else f"{pct:.1f}%"
    border = f"1px solid {t.POS}" if is_best else f"1px solid {t.BORDER}"
    shadow = f"box-shadow: 0 0 12px rgba(34, 197, 94, .12);" if is_best else ""

    with ui.column().style(
        f"flex: 1; background: {t.CARD}; border: {border}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: {t.SPACE_SM}; "
        f"{shadow}"
    ):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label(label).style(
                f"font-size: 13px; font-weight: 800; color: {t.TEXT};"
            )
            if is_best:
                ui.label("BEST").style(
                    f"background: {t.POS}; color: {t.BG}; "
                    f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
                    f"padding: 2px 6px; border-radius: 3px;"
                )
        with ui.row().classes("items-baseline").style("gap: 8px;"):
            ui.label(pct_s).style(
                f"font-size: 24px; font-weight: 800; color: {t.PRIMARY}; "
                f"font-family: monospace; letter-spacing: -.4px;"
            )
            ui.label(f"{correct}-{total - correct}").style(
                f"font-size: 11px; color: {t.TEXT_DIM}; font-family: monospace;"
            )
        # Per-category rows
        for cat_key, cat_label, _aliases in _CATS:
            c, n = by_cat[cat_key]
            row_pct = "—" if not n else f"{(c / n * 100):.1f}%"
            with ui.row().classes("w-full justify-between items-center").style(
                f"padding: 4px 0; border-bottom: 1px solid {t.BORDER_SOFT};"
            ):
                ui.label(cat_label).style(
                    f"color: {t.TEXT_DIM}; font-size: 11px;"
                )
                ui.label(f"{c}-{n - c}" + ("" if not n else f"  ({row_pct})")).style(
                    f"color: {t.TEXT}; font-size: 11px; font-family: monospace;"
                )
