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
from pages import home, sport, mybets, model, ai_breakdown, admin, game_detail, props, player, top_picks, model_history, picks, bets  # noqa: E402


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
props.register(backend)
player.register(backend)
top_picks.register(backend)
model_history.register(backend)
picks.register(backend)
bets.register(backend)


# ── Boot-time analysis-state hydration ──────────────────────────────────────
# Backend exposes hydrate_state() which re-reads daily_snapshot.json +
# analysis_cache.json into the in-memory _analysis_state /
# _wnba_analysis_state dicts.  We call it once at boot here, AND every
# page calls it again on render so the UI always sees the freshest
# cached results (covers schedulers, external Run, container restarts
# with a populated cache, etc.).
try:
    mlb_n, wnba_n = backend.hydrate_state()
    print(f"UI_APP: hydrated -- MLB={mlb_n} games, WNBA={wnba_n} games",
          flush=True, file=sys.stderr)
except Exception as exc:                                                  # noqa: BLE001
    print(f"UI_APP: boot hydrate failed: {exc}",
          flush=True, file=sys.stderr)


# ── Boot ────────────────────────────────────────────────────────────────────
if __name__ in {"__main__", "__mp_main__"}:
    port = int(os.environ.get("PORT", 8080))
    print(f"UI_APP: NiceGUI starting on 0.0.0.0:{port}", flush=True, file=sys.stderr)
    ui.run(
        host="0.0.0.0",
        port=port,
        title="Sports Analysis",
        favicon="🏀",
        dark=True,
        reload=False,
        show=False,
        storage_secret=os.environ.get("UI_STORAGE_SECRET", "sports-analysis-ui"),
        # Bump the client reconnect grace from NiceGUI's 3 s default to
        # 300 s so brief WebSocket drops during a long analysis (or
        # mobile-screen-lock events) don't tear down the page state
        # before the background-worker polling can resume.  The actual
        # analyze run is decoupled into a daemon thread (see
        # app._run_analysis_worker) so even longer drops are fine; this
        # is the safety-net mentioned in PR #49.
        reconnect_timeout=300,
        # NOTE: ping_interval=30 was removed -- the installed NiceGUI
        # version does not expose it as a ui.run() kwarg and the import
        # crashed boot with TypeError: Config.__init__() got an
        # unexpected keyword argument 'ping_interval'.
        # Disable uvicorn's default color formatter -- Railway's logger
        # wrapper trips its dictConfig() with "Unable to configure
        # formatter 'default'" because the formatter probes isatty().
        log_config=None,
    )
