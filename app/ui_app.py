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
from pages import home, sport, mybets, model, ai_breakdown, admin, game_detail, props, player, top_picks, model_history, picks, bets, players, research  # noqa: E402


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
research.register(backend)
top_picks.register(backend)
model_history.register(backend)
picks.register(backend)
bets.register(backend)


# ── Flask + Tailwind props page wiring (PR #304) ────────────────────────────
# NiceGUI (FastAPI/uvicorn) owns the HTTP server here; the Flask app in app.py
# is imported as a module and is normally only reached in-process via
# test_client.  The migrated props page is a plain Flask route returning a
# Tailwind template, so we bridge it onto NiceGUI's FastAPI app with two thin
# passthroughs (the page itself + its /static assets) using an in-process
# Flask test client.  The NiceGUI props page remains served at /props-legacy.
from nicegui import app as _ng_app                                       # noqa: E402
from starlette.responses import Response as _StarletteResponse           # noqa: E402
from starlette.requests import Request as _StarletteRequest              # noqa: E402

_flask_client = backend.app.test_client()


@_ng_app.get("/props")
def _props_flask_page():
    rv = _flask_client.get("/props")
    return _StarletteResponse(
        content=rv.get_data(),
        status_code=rv.status_code,
        media_type=rv.headers.get("Content-Type", "text/html"),
    )


@_ng_app.get("/")
def _home_flask_page():
    # Bridge / to the Flask home page (Phase-1 Tailwind port, PR #324).
    # Without this, NiceGUI's FastAPI server has no handler for "/" and the
    # request never reaches Flask's WSGI layer -- producing the blank-page
    # symptom while /props (which has its own bridge above) renders fine.
    # Same in-process test-client passthrough as /props -- no extra logic.
    print("[HOME-BRIDGE] /", flush=True, file=sys.stderr)
    rv = _flask_client.get("/")
    return _StarletteResponse(
        content=rv.get_data(),
        status_code=rv.status_code,
        media_type=rv.headers.get("Content-Type", "text/html"),
    )


@_ng_app.get("/static/{path:path}")
def _props_static(path: str):
    rv = _flask_client.get("/static/" + path)
    return _StarletteResponse(
        content=rv.get_data(),
        status_code=rv.status_code,
        media_type=rv.headers.get("Content-Type", "application/octet-stream"),
    )


@_ng_app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def _api_flask_bridge(path: str, request: _StarletteRequest):
    # Catch-all that forwards any /api/* request to the in-process Flask app,
    # same passthrough pattern as /props and /home-v2 but for the whole JSON
    # API surface.  Unlike those GET-only page bridges, /api/* includes POST /
    # PUT / PATCH / DELETE endpoints with JSON bodies (analyze, track, ledger
    # confirm, set bankroll, …), so the HTTP method, query string, request
    # body and content type are all forwarded.  Reading the body requires an
    # async handler.
    qs        = request.url.query
    full_path = "/api/" + path + (f"?{qs}" if qs else "")
    body      = await request.body()
    rv = _flask_client.open(
        full_path,
        method=request.method,
        data=body,
        content_type=request.headers.get("content-type"),
    )
    return _StarletteResponse(
        content=rv.get_data(),
        status_code=rv.status_code,
        media_type=rv.headers.get("Content-Type", "application/json"),
    )


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
