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
from pages import home, sport, mybets, model, ai_breakdown, admin, game_detail, props, player, top_picks, model_history, picks, bets, players  # noqa: E402


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
players.register(backend)
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


# ── model_picks store diagnostic ─────────────────────────────────────────────
# The home GAME/PROPS MODELS cards read the Supabase `model_picks` table
# (all-time, via model_picks.store_record / models_record).  A 0-0 there is
# almost always one of two states, which this boot log makes obvious in the
# Railway logs without a manual Supabase query:
#   * Supabase NOT configured  -> the whole store is a no-op (every insert /
#     settle / read short-circuits on `_mode != "supabase"`), so the table is
#     effectively empty and the cards read 0-0.
#   * Supabase ON but rows are all pending -> picks were logged but nothing has
#     settled yet (settlement runs in the 12 PM-1 AM ET cycle; props settle via
#     the gamelog stat lookup).
# Settlement itself IS already scheduled (the 15-min auto_props_refresh cycle
# calls _run_auto_settlement_job, plus the 1 AM JOB1) and logged
# (SETTLE-SUMMARY / SETTLE-CYCLE-SUMMARY); this is purely a read-only summary
# of the current store state.
try:
    from src import db as _db, model_picks as _mp
    if _db.is_supabase():
        _mp_lines = _mp.store_summary_counts()
        print(
            "UI_APP: model_picks store (Supabase=ON): "
            + ("; ".join(_mp_lines) if _mp_lines else "EMPTY (0 rows)"),
            flush=True, file=sys.stderr,
        )
    else:
        print(
            "UI_APP: model_picks store DISABLED -- Supabase not configured "
            "(SUPABASE_URL / SUPABASE_KEY unset).  GAME/PROPS MODELS cards will "
            "read 0-0 and no picks can be logged or settled until Supabase is "
            "wired.",
            flush=True, file=sys.stderr,
        )
except Exception as _mp_exc:                                              # noqa: BLE001
    print(f"UI_APP: model_picks diagnostic failed: {_mp_exc}",
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
        # WebSocket keep-alive through Railway's edge proxy.  `ping_interval`
        # is NOT a ui.run() kwarg in this NiceGUI version (it raised TypeError
        # at boot -- see history below), but ui.run forwards unknown kwargs
        # straight to uvicorn, so we set uvicorn's own websocket ping knobs:
        #   ws_ping_interval=20 -> a PING frame every 20s keeps the socket from
        #                          looking idle, so Railway's proxy won't drop
        #                          it during quiet periods.
        #   ws_ping_timeout=60  -> tolerate up to 60s of proxy latency before
        #                          declaring the socket dead.  uvicorn's 20s
        #                          default closes too eagerly behind Railway's
        #                          proxy, which surfaced as the persistent
        #                          "Connection lost. Trying to reconnect..."
        #                          banner.  Browsers auto-reply to WS PING with
        #                          PONG, so this needs no client-side code.
        # (Historic note: a bare `ping_interval=30` ui.run kwarg crashed boot
        #  with "Config.__init__() got an unexpected keyword argument
        #  'ping_interval'"; the ws_ping_* names below are the supported ones.)
        ws_ping_interval=20,
        ws_ping_timeout=60,
        # Disable uvicorn's default color formatter -- Railway's logger
        # wrapper trips its dictConfig() with "Unable to configure
        # formatter 'default'" because the formatter probes isatty().
        log_config=None,
    )
