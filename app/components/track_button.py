"""
Track button -- records a model pick for one game to the user's ledger.

Three bet types are supported, each posting to its own in-process
Flask route:
  ml    -> /api/ledger/confirm/<game_id>      (or wnba variant)
  rl    -> /api/ledger/track_prop  {bet_type: "run_line"}   (MLB only)
  total -> /api/ledger/track_prop  {bet_type: "totals"}     (MLB only)

A button can render in a pre-tracked state (a muted "✓ Tracked" chip)
when the ledger already has that game+bet_type confirmed.

Usage:
    track_button.render(backend, game_id, sport="mlb", bet_type="rl")

Returns nothing; the control is added to the current NiceGUI parent.
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

from nicegui import ui

from . import theme as t


# bet_type -> (default label, ledger bet_type key)
_BET_TYPES: dict[str, tuple[str, str]] = {
    "ml":    ("Track ML",    "single"),
    "rl":    ("Track RL",    "run_line"),
    "total": ("Track Total", "totals"),
}

# Short code shown in the tracked chip ("ML ✓").
_SHORT = {"ml": "ML", "rl": "RL", "total": "Total"}


def tracked_bet_types(backend, game_id: str, sport: str = "mlb") -> set[str]:
    """Return the set of ledger bet_type keys ("single"/"run_line"/
    "totals") already CONFIRMED for *game_id* in the sport's ledger.
    Best-effort -- returns an empty set on any error."""
    if not game_id:
        return set()
    try:
        path = "data/wnba_ledger.json" if (sport or "").lower() == "wnba" else "data/ledger.json"
        ledger = backend.Ledger(path=path, starting_bankroll=1000.0)
        out: set[str] = set()
        for b in (ledger.data.get("open_bets") or []):
            if b.get("game_id") == game_id and b.get("confirmed"):
                out.add(b.get("bet_type", "single"))
        return out
    except Exception:                                                     # noqa: BLE001
        return set()


def render(
    backend,
    game_id: Optional[str],
    sport: str = "mlb",
    size: str = "sm",
    label: Optional[str] = None,
    disabled_reason: Optional[str] = None,
    *,
    bet_type: str = "ml",
    already_tracked: bool = False,
    on_tracked: Optional[Callable] = None,
) -> None:
    """Render one Track button for a specific *bet_type*.

    `already_tracked` renders a muted "✓ Tracked" chip instead of a
    live button.  `on_tracked` (if given) is invoked after a
    successful track so a parent section can refresh.
    """
    cfg_label, ledger_key = _BET_TYPES.get(bet_type, _BET_TYPES["ml"])
    label = label or cfg_label
    short = _SHORT.get(bet_type, "ML")

    is_sm   = size == "sm"
    padding = "4px 10px" if is_sm else "6px 14px"
    font_sz = "10.5px"   if is_sm else "12px"

    # Pre-tracked chip -- not clickable.
    if already_tracked:
        ui.label(f"{short} ✓").style(
            f"background: {t.CARD_HI}; color: {t.POS}; "
            f"border: 1px solid {t.POS}; "
            f"font-weight: 800; letter-spacing: .4px; "
            f"font-size: {font_sz}; padding: {padding}; "
            f"border-radius: {t.RADIUS_SM};"
        )
        return

    base_style = (
        f"background: {t.PRIMARY}; color: {t.BG}; "
        f"font-weight: 800; letter-spacing: .4px; "
        f"font-size: {font_sz}; padding: {padding}; "
        f"border-radius: {t.RADIUS_SM}; min-height: 0;"
    )
    btn = ui.button(label).props("no-caps unelevated dense").style(base_style)

    async def _click():
        if disabled_reason:
            ui.notify(disabled_reason, type="warning")
            return
        if not game_id:
            ui.notify("Cannot track: missing game_id on this card.", type="warning")
            return
        # RL / Total tracking only exists for MLB.
        if bet_type in ("rl", "total") and (sport or "").lower() != "mlb":
            ui.notify(f"{short} tracking is only available for MLB.", type="warning")
            return

        btn.props("loading")
        btn.disable()
        try:
            path, body = _endpoint_and_body(bet_type, game_id, sport)
            ok, data, status = await asyncio.to_thread(_post, backend, path, body)
            if ok:
                team = data.get("team")
                amt  = data.get("confirmed_amount")
                if team and amt is not None:
                    msg = f"Tracked {short}: {team} (${amt:.2f})"
                elif amt is not None:
                    msg = f"Tracked {short} (${amt:.2f})"
                else:
                    msg = f"Tracked {short}."
                ui.notify(msg, type="positive")
                btn.text = f"{short} ✓"
                btn.props("disable")
                if on_tracked is not None:
                    try:
                        on_tracked()
                    except Exception:                                     # noqa: BLE001
                        pass
            else:
                err = data.get("error") or "unknown error"
                ui.notify(f"Track {short} failed ({status}): {err}",
                          type="negative", multi_line=True)
        except Exception as exc:                                          # noqa: BLE001
            ui.notify(f"Track failed: {type(exc).__name__}: {exc}",
                      type="negative", multi_line=True)
        finally:
            btn.props(remove="loading")
            if btn.text == label:
                btn.enable()

    btn.on("click", _click)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _endpoint_and_body(bet_type: str, game_id: str, sport: str) -> tuple[str, dict]:
    """Map a bet_type to its Flask route + POST body."""
    bankroll = _default_bankroll(sport)
    if bet_type == "rl":
        return "/api/ledger/track_prop", {
            "game_id": game_id, "bet_type": "run_line", "bankroll": bankroll,
        }
    if bet_type == "total":
        return "/api/ledger/track_prop", {
            "game_id": game_id, "bet_type": "totals", "bankroll": bankroll,
        }
    # moneyline
    path = (
        f"/api/ledger/confirm/{game_id}" if (sport or "").lower() == "mlb"
        else f"/api/wnba/ledger/confirm/{game_id}"
    )
    return path, {"bankroll": bankroll}


def _post(backend, path: str, body: dict) -> tuple[bool, dict, int]:
    """Invoke a Flask /api/ route via the in-process test client."""
    client = backend.app.test_client()
    try:
        resp = client.post(path, json=body or {})
        try:
            data = resp.get_json(force=True, silent=True) or {}
        except Exception:                                                 # noqa: BLE001
            data = {}
        ok = resp.status_code < 400 and data.get("success", True) is not False
        return ok, data, resp.status_code
    except Exception as exc:                                              # noqa: BLE001
        return False, {"error": str(exc)}, 500


def _default_bankroll(sport: str) -> float:
    return 1000.0 if (sport or "").lower() == "wnba" else 250.0
