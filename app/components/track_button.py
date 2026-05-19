"""
Track button -- records the model's pick for one game to the user's ledger.

Posts to /api/ledger/confirm/<game_id> (MLB) or
/api/wnba/ledger/confirm/<game_id> (WNBA) via Flask's in-process test
client, so no HTTP hop / no app.py modifications beyond the new WNBA
endpoint.  Result toast shows the confirmed stake amount or the error
message the backend returned.

Usage:
    track_button.render(backend, game_id, sport="mlb", size="sm")

Returns nothing; the button is added to the current NiceGUI parent.
"""
from __future__ import annotations

import asyncio

from nicegui import ui

from . import theme as t


def render(
    backend,
    game_id: str | None,
    sport: str = "mlb",
    size: str = "sm",
    label: str = "Track",
    disabled_reason: str | None = None,
) -> None:
    """Render one Track button.

    `disabled_reason` short-circuits the click handler and shows a warning
    toast.  Used by NO MODEL PICK cards (no game_id) and by Top-5 rows
    where the row doesn't carry enough info to confirm.
    """
    is_sm   = size == "sm"
    padding = "4px 10px" if is_sm else "6px 14px"
    font_sz = "10.5px"   if is_sm else "12px"

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
            ui.notify("Cannot track: missing game_id on this card.",
                      type="warning")
            return
        btn.props("loading")
        btn.disable()
        try:
            path = (
                f"/api/ledger/confirm/{game_id}" if (sport or "").lower() == "mlb"
                else f"/api/wnba/ledger/confirm/{game_id}"
            )
            ok, data, status = await asyncio.to_thread(
                _post, backend, path, {"bankroll": _default_bankroll(sport)},
            )
            if ok:
                amt = data.get("confirmed_amount")
                team = data.get("team")
                msg = (
                    f"Tracked: {team} (${amt:.2f})" if (team and amt is not None)
                    else (f"Tracked (${amt:.2f})" if amt is not None
                          else "Tracked.")
                )
                ui.notify(msg, type="positive")
                btn.text = "Tracked ✓"
                btn.props("disable")
            else:
                err = (data.get("error") or "unknown error")
                ui.notify(f"Track failed ({status}): {err}",
                          type="negative", multi_line=True)
        except Exception as exc:                                          # noqa: BLE001
            ui.notify(f"Track failed: {type(exc).__name__}: {exc}",
                      type="negative", multi_line=True)
        finally:
            btn.props(remove="loading")
            # Only re-enable if NOT successfully tracked (success leaves it
            # disabled with "Tracked ✓" so the user can't double-confirm).
            if btn.text == label:
                btn.enable()

    btn.on("click", _click)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _post(backend, path: str, body: dict) -> tuple[bool, dict, int]:
    """Invoke a Flask /api/ route via the in-process test client.
    Mirrors pages.admin._call so behavior is consistent across the UI."""
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
    """Sensible default bankroll per sport when the backend cache is empty."""
    return 1000.0 if (sport or "").lower() == "wnba" else 250.0
