"""
ui_app.py -- NiceGUI front-end for the sports betting tool.

Replaces templates/index.html as the served UI.  Imports the existing
Flask app.py as a plain module (no HTTP server starts because
app.run() is __main__-guarded) so every backend helper -- Ledger,
load_daily_picks, _serialize, _analysis_state, etc. -- is available
via plain Python calls.

Architecture
------------
                +-----------------------------------+
                |             ui_app.py             |  (this file)
                |   - ui.run(host=0.0.0.0, port=$P) |
                |   - imports & passes `backend`    |
                +----------------+------------------+
                                 |
                 +---------------+---------------+
                 |                               |
        components/                         pages/
          theme.py        navbar.py          home.py    sport.py
          sidebar.py      game_card.py       mybets.py  model.py
          bet_box.py                         ai_breakdown.py

All theming flows through components/theme.py.  All pages take
`backend` (the imported `app` module) so they can call backend.Ledger,
backend.load_daily_picks, etc., without any HTTP hop.

Deployment
----------
Procfile / railway.toml run `python ui_app.py`.  Port is read from
$PORT (Railway sets it) with 8080 fallback.  The legacy templates/
folder stays on disk but is no longer served -- /api/* routes from
app.py are registered (since we import the module) but not served by
NiceGUI; switch back by reverting the Procfile if needed.
"""
from __future__ import annotations

# ── Railway stdout shim ─────────────────────────────────────────────────────
# Railway wraps stdout/stderr in a _StdoutToLogger object that does not
# implement isatty().  uvicorn's default color-formatter calls isatty()
# during logging config and crashes with AttributeError, taking the boot
# down with it.  Patch isatty() onto whatever stream is present *before*
# anything imports uvicorn (transitively, NiceGUI -> FastAPI -> uvicorn).
# Must run before every other import in this file.
import sys                                                                # noqa: E402
if not hasattr(sys.stdout, "isatty"):
    sys.stdout.isatty = lambda: False
if not hasattr(sys.stderr, "isatty"):
    sys.stderr.isatty = lambda: False

import os
from pathlib import Path

# Ensure the `app/` directory is on sys.path so `import app` resolves to
# our backend even when Railway / Procfile launches from the repo root.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

print("UI_APP: importing backend (app.py)...", flush=True, file=sys.stderr)
import app as backend                                                     # noqa: E402
print("UI_APP: backend imported -- Flask app registered, schedulers booting.",
      flush=True, file=sys.stderr)

from nicegui import ui                                                    # noqa: E402

from components import theme as t                                         # noqa: E402
from pages import home, sport, mybets, model, ai_breakdown, admin, game_detail  # noqa: E402


# ── Quasar palette override -- ties Quasar primitives (buttons, menus,
#    inputs) to our theme colors so the OLED look is consistent without
#    per-element overrides. ────────────────────────────────────────────
ui.colors(
    primary=t.PRIMARY,
    secondary=t.CYAN,
    accent=t.PRIMARY_HI,
    positive=t.POS,
    negative=t.NEG,
    warning=t.WARN,
    dark=t.BG,
)

# ── Register all routes ─────────────────────────────────────────────────────
home.register(backend)
sport.register(backend)
mybets.register(backend)
model.register(backend)
ai_breakdown.register(backend)
admin.register(backend)
game_detail.register(backend)


# ── Boot-time analysis-state hydration ──────────────────────────────────────
# The legacy Flask UI called /api/init + /api/wnba/init from JS on every
# page load, which returned today's snapshot in the response body and the
# frontend stored it in its own state.  /api/init never populated the
# in-process `_analysis_state["results"]` dict -- that only happens when
# /api/analyze runs.
#
# The new NiceGUI pages read `backend._analysis_state["results"]` directly,
# so after every container restart (== every Railway deploy) the home /
# sports pages show "0 games" until the 8 AM / 12 PM scheduler fires or the
# user clicks Run Analysis in /admin.
#
# Fix: at boot, read today's daily_snapshot.json (and the per-sport
# analysis_cache.json fallbacks) directly via backend's internal helpers
# and seed the in-memory state.  Same data path /api/init walks, but we
# assign to the dict instead of returning JSON.

def _hydrate_state() -> None:
    try:
        snap = backend._read_daily_snapshot()
        is_today = backend._snapshot_is_today(snap)
    except Exception as exc:                                              # noqa: BLE001
        print(f"UI_APP: hydrate -- snapshot read failed: {exc}",
              flush=True, file=sys.stderr)
        snap, is_today = {}, False

    def _seed(state_dict, sport_key: str, cache_path: str) -> int:
        sp = (snap.get(sport_key) or {}) if is_today else {}
        results = sp.get("results")
        analyzed_at = sp.get("analyzed_at")

        if not results:
            try:
                from datetime import datetime as _dt
                p = Path(cache_path)
                if p.exists():
                    import json as _json
                    payload = _json.loads(p.read_text(encoding="utf-8"))
                    today = backend._today_et()
                    if payload.get("date") == today:
                        results = backend._filter_stale_games(
                            payload.get("results") or []
                        )
                        analyzed_at = payload.get("analyzed_at") or analyzed_at
            except Exception as exc:                                      # noqa: BLE001
                print(f"UI_APP: hydrate -- {sport_key} cache read failed: {exc}",
                      flush=True, file=sys.stderr)

        if results:
            state_dict["results"] = list(results)
            if analyzed_at and state_dict.get("last_analyzed_at") is None:
                try:
                    from datetime import datetime as _dt
                    state_dict["last_analyzed_at"] = _dt.fromisoformat(analyzed_at)
                except Exception:                                         # noqa: BLE001
                    pass
            return len(results)
        return 0

    mlb_n  = _seed(backend._analysis_state,      "mlb",  "data/analysis_cache.json")
    wnba_n = _seed(backend._wnba_analysis_state, "wnba", "data/wnba_analysis_cache.json")
    print(f"UI_APP: hydrated -- MLB={mlb_n} games, WNBA={wnba_n} games",
          flush=True, file=sys.stderr)


_hydrate_state()


# ── Boot ────────────────────────────────────────────────────────────────────
if __name__ in {"__main__", "__mp_main__"}:
    port = int(os.environ.get("PORT", 8080))
    print(f"UI_APP: NiceGUI starting on 0.0.0.0:{port}", flush=True, file=sys.stderr)
    ui.run(host='0.0.0.0', port=8080, reload=False, log_config=None)
