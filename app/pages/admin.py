"""
Admin page -- operational controls for analysis, models, and bets.

Mirrors the legacy More -> Admin sub-page in templates/index.html.
Every action is driven by the Flask test client (backend.app.test_client()),
which invokes the existing /api/admin/* routes in-process with no HTTP
hop and no modifications to app.py.

Sections (top to bottom)
------------------------
  ANALYSIS      Run analysis for MLB / WNBA / Both; last-run timestamps.
  MODELS        Refresh models with cached odds; clear today's snapshot.
  MODEL BETS    Per-sport auto-pick toggles; re-pick / reset today's picks;
                reset model bankroll.
  MY BETS       Wipe tracked bets per sport; set personal bankroll.
  SYSTEM        Read-only: Supabase + DB mode.

Long-running calls (Run Analysis, Refresh Models) are dispatched via
asyncio.to_thread so the NiceGUI event loop stays responsive and the
button shows a spinner while the work runs.  Destructive actions
(wipe / reset) go through a confirmation dialog.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from nicegui import ui

from components import theme as t
from components import navbar, sidebar, bottom_nav


_ET = ZoneInfo("America/New_York")


def register(backend) -> None:
    @ui.page("/admin")
    def admin_page():
        ui.add_head_html(t.page_head_css())
        navbar.render(active=t.TAB_ADMIN)
        with ui.row().classes("no-wrap w-full").style("gap: 0;"):
            sidebar.render(backend)
            with ui.column().classes("page-content").style(
                f"flex: 1; max-width: {t.MAX_CONTENT_W}; "
                f"gap: {t.SPACE_LG}; padding: {t.SPACE_LG}; min-width: 0;"
            ):
                ui.label("ADMIN").classes("page-title").style(
                    f"font-size: 22px; font-weight: 800; color: {t.TEXT};"
                )

                # status_holder doubles as the SYSTEM card -- declared
                # first so it sits at the top of the page (last-analyzed
                # timestamps are the first thing the user wants to see),
                # populated by _refresh() below after every mutation.
                status_holder = ui.column().classes("w-full")
                _refresh = lambda: _render_status(backend, status_holder)

                _section_analysis(backend, _refresh)
                _section_props(backend, _refresh)
                _section_ai_analysis(backend)
                _section_models(backend, _refresh)
                _section_model_performance(backend)
                _section_model_bets(backend, _refresh)
                _section_my_bets(backend, _refresh)
                _section_data_reset(backend, _refresh)
                _section_data_explorer(backend)
                _section_diagnostics(backend)
                _refresh()

                # Note: the cross-page completion watcher is NOT mounted
                # on /admin.  Admin's per-button polling timer
                # (_start_status_poll) already handles in-session
                # progress + completion feedback, and the watcher's
                # forced ui.navigate.reload() would yank the user off
                # their position mid-click.  On Home and Sports the
                # watcher IS mounted so a scheduler-driven analyze
                # reloads the page automatically.
        bottom_nav.render(active=t.TAB_ADMIN)


# ───────────────────────────────────────────────────────────────────────────
#  Inline status helper -- mobile-friendly replacement for ui.notify.
#
#  Every button on /admin renders a sibling status holder right below it.
#  Calling _show_inline_status(holder, text, kind) fills the holder with
#  a colored label and schedules a 4 s ui.timer to clear it.  Nothing
#  overlays the page so other buttons stay tappable on mobile.
# ───────────────────────────────────────────────────────────────────────────

# Color map shared by every wrapper -- mirrors the ui.notify type names
# the old code used so existing call sites only need a search-and-replace.
_STATUS_COLORS: dict[str, str] = {
    "success":  t.POS,
    "positive": t.POS,
    "error":    t.NEG,
    "negative": t.NEG,
    "warning":  t.WARN,
    "ongoing":  t.TEXT_DIM,
    "info":     t.TEXT_DIM,
}


def _make_status_holder() -> "ui.row":
    """Return a fresh row used as the per-button status slot.  min-height
    keeps the surrounding layout stable so the page doesn't reflow when
    a message appears or fades out."""
    return ui.row().classes("w-full").style(
        "min-height: 18px; padding: 2px 0 0 0; gap: 6px; align-items: center;"
    )


def _show_inline_status(holder, text: str, kind: str = "info") -> None:
    """Render *text* inside *holder* with a color picked from
    _STATUS_COLORS and schedule a 4-second ui.timer to clear it.

    Replaces ui.notify across the admin page so status messages appear
    inline next to the button that fired them (mobile-friendly: never
    overlays other buttons / the navbar).  Each call cancels any
    previous in-flight timer for the same holder by clearing first.
    """
    color = _STATUS_COLORS.get(kind, t.TEXT_DIM)
    try:
        holder.clear()
    except Exception:                                                     # noqa: BLE001
        pass
    with holder:
        ui.label(text).style(
            f"font-size: 11.5px; line-height: 1.4; color: {color}; "
            f"font-weight: 600; white-space: normal; word-break: break-word;"
        )

    def _clear() -> None:
        try:
            holder.clear()
        except Exception:                                                 # noqa: BLE001
            pass

    try:
        ui.timer(4.0, _clear, once=True)
    except Exception:                                                     # noqa: BLE001
        # ui.timer requires an active client context.  When the user
        # has already navigated away, the message stays in the (now
        # orphaned) holder -- harmless.
        pass


# ───────────────────────────────────────────────────────────────────────────
#  Backend invocation helper -- Flask test client over the imported app
# ───────────────────────────────────────────────────────────────────────────

def _call(backend, method: str, path: str, body: dict | None = None) -> tuple[bool, dict, int]:
    """Invoke an /api/* route in-process via Flask's test client.

    Returns (ok, payload, status_code).  ok=False either when the HTTP
    status is >= 400 or when the JSON payload has {"success": false}.
    payload is always a dict (empty on parse error).
    """
    client = backend.app.test_client()
    fn = client.post if method.upper() == "POST" else client.get
    try:
        resp = fn(path, json=body or {})
        try:
            data = resp.get_json(force=True, silent=True) or {}
        except Exception:                                                 # noqa: BLE001
            data = {}
        ok = resp.status_code < 400 and data.get("success", True) is not False
        return ok, data, resp.status_code
    except Exception as exc:                                              # noqa: BLE001
        return False, {"error": str(exc)}, 500


# ───────────────────────────────────────────────────────────────────────────
#  Section: ANALYSIS
# ───────────────────────────────────────────────────────────────────────────

def _section_analysis(backend, refresh) -> None:
    with _card(
        "ANALYSIS",
        "Cache-aware Run.  Within 15 minutes of the last live fetch, "
        "clicking Run reuses the cached odds -- zero quota burn.  After "
        "15 minutes the button asks for confirmation before pulling "
        "fresh odds.  Daily cap is 500 (~666/day average for a 20k-month "
        "plan); use Approve Additional Odds Pull below to grant +50 "
        "more when the cap is reached.",
    ):
        # Quota row -- counter + remaining + approve button.  Re-renders on
        # every refresh() call (i.e. after every analyze + on page open).
        quota_holder = ui.column().classes("w-full").style("gap: 6px;")

        def _render_quota() -> None:
            quota_holder.clear()
            ok, data, _ = _call(backend, "GET", "/api/odds/usage")
            if not ok:
                with quota_holder:
                    ui.label("Odds quota: could not load.").style(
                        f"font-size: 11.5px; color: {t.TEXT_DIM};"
                    )
                return
            count   = int(data.get("count") or 0)
            limit   = int(data.get("effective_limit") or 500)
            remain  = int(data.get("remaining") or 0)
            extra   = int(data.get("extra_allowance") or 0)
            reached = bool(data.get("limit_reached"))
            color   = t.NEG if reached else (t.WARN if remain < 50 else t.POS)
            extra_s = (f"  (+{extra} bonus granted today)") if extra else ""
            with quota_holder:
                with ui.row().classes("items-center w-full").style("gap: 10px;"):
                    ui.label(
                        f"{count} of {limit} requests used today{extra_s}"
                    ).style(
                        f"font-size: 12.5px; font-weight: 700; color: {color}; "
                        f"font-family: monospace;"
                    )
                    ui.label(
                        f"({remain} remaining)" if not reached
                        else "LIMIT REACHED"
                    ).style(
                        f"font-size: 11px; color: {color}; "
                        f"font-family: monospace; margin-left: auto;"
                    )
                if reached:
                    ui.label(
                        "Daily Odds API limit reached, additional pulls "
                        "require manual approval."
                    ).style(
                        f"background: {t.CARD}; border: 1px dashed {t.NEG}; "
                        f"color: {t.NEG}; font-size: 11.5px; font-weight: 600; "
                        f"padding: 6px 10px; border-radius: {t.RADIUS_SM};"
                    )
                # Approve button -- bumps allowance by +50.  Always visible
                # so the user can pre-emptively grant more even before the
                # limit hits.  Re-renders the quota row after success so
                # the new effective_limit is visible immediately.
                with ui.column().classes("w-full").style(
                    "gap: 2px; min-width: 0;"
                ):
                    approve_status = _make_status_holder()
                    async def _approve():
                        print("[ADMIN-BTN] _approve click",
                              flush=True, file=sys.stderr)
                        ok2, data2, _ = await asyncio.to_thread(
                            _call, backend, "POST",
                            "/api/admin/odds/approve_additional",
                        )
                        if ok2:
                            _show_inline_status(
                                approve_status,
                                f"Approved +50.  New limit: "
                                f"{data2.get('effective_limit')} "
                                f"({data2.get('remaining')} remaining today).",
                                "success",
                            )
                            _render_quota()
                        else:
                            _show_inline_status(
                                approve_status,
                                f"Approve failed: {data2.get('error') or 'unknown'}",
                                "error",
                            )
                    ui.button("Approve Additional Odds Pull (+50)",
                              on_click=_approve) \
                        .props("no-caps unelevated dense") \
                        .style(_btn_style("warn"))

        _render_quota()

        # 15-min cache info row -- shows whether each sport is currently
        # cached-fresh so the user knows up-front whether clicking Run
        # would be free (cache hit) or would burn a quota request.
        cache_info_holder = ui.row().classes("items-center w-full").style(
            "gap: 12px; flex-wrap: wrap;"
        )

        def _render_cache_info() -> None:
            cache_info_holder.clear()
            ok, data, _ = _call(backend, "GET", "/api/odds/cache_status?sport=both")
            mlb  = (data.get("mlb")  or {}) if ok else {}
            wnba = (data.get("wnba") or {}) if ok else {}
            with cache_info_holder:
                ui.label("15-min cache:").style(
                    f"font-size: 11.5px; color: {t.TEXT_DIM2};"
                )
                for label, blob in (("MLB", mlb), ("WNBA", wnba)):
                    fresh = bool(blob.get("fresh"))
                    color = t.POS if fresh else t.WARN
                    txt   = f"{label}: {'fresh' if fresh else 'stale'}"
                    ui.label(txt).style(
                        f"font-size: 11.5px; font-weight: 700; color: {color}; "
                        f"background: {t.CARD_HI}; padding: 2px 8px; "
                        f"border-radius: {t.RADIUS_PILL};"
                    )

            # Odds Status indicator -- shows at-a-glance whether the
            # last analyze actually wrote odds + when it ran.  Per
            # user spec: "visible indicator in the settings menu
            # showing the odds cache last updated timestamp and how
            # many games currently have odds".
            with cache_info_holder:
                ui.label("Odds status:").style(
                    f"font-size: 11.5px; color: {t.TEXT_DIM2}; "
                    f"margin-top: 4px;"
                )
                for label, blob in (("MLB", mlb), ("WNBA", wnba)):
                    games_total = int(blob.get("games_total") or 0)
                    games_odds  = int(blob.get("games_with_odds") or 0)
                    last_ts     = blob.get("last_analyzed_at") or ""
                    # Format the timestamp as HH:MM ET when present;
                    # show "never" otherwise so the user can spot a
                    # boot-fresh state immediately.
                    if last_ts:
                        try:
                            from datetime import datetime as _dt
                            from zoneinfo import ZoneInfo as _ZI
                            _d = _dt.fromisoformat(last_ts.replace("Z", "+00:00"))
                            last_pretty = _d.astimezone(_ZI("America/New_York")) \
                                            .strftime("%-I:%M %p ET")
                        except Exception:                                  # noqa: BLE001
                            last_pretty = last_ts[:16]
                    else:
                        last_pretty = "never"

                    has_any = games_odds > 0
                    color   = t.POS if has_any else (t.WARN if games_total > 0 else t.NEG)
                    ui.label(
                        f"{label}: {games_odds}/{games_total} games w/odds  "
                        f"(last {last_pretty})"
                    ).style(
                        f"font-size: 11.5px; font-weight: 700; color: {color}; "
                        f"background: {t.CARD_HI}; padding: 2px 8px; "
                        f"border-radius: {t.RADIUS_PILL};"
                    )

        _render_cache_info()

        # Run buttons.  Cache-aware behavior:
        #   - if /api/odds/cache_status says fresh -> POST analyze directly
        #     (cache hit inside OddsClient.get_odds means 0 quota burn)
        #   - if stale -> _confirm_dialog asks the user before firing the
        #     live API call.  Cancelling aborts the run entirely.
        with ui.row().classes("w-full").style("gap: 8px; flex-wrap: wrap;"):
            _cache_aware_run_button(
                backend, "Run MLB Analysis",
                "/api/analyze", {"bankroll": 250},
                sport_key="mlb",
                refresh_status=lambda: (refresh(), _render_quota(), _render_cache_info()),
                style="primary",
            )
            _cache_aware_run_button(
                backend, "Run WNBA Analysis",
                "/api/wnba/analyze", {"bankroll": 1000},
                sport_key="wnba",
                refresh_status=lambda: (refresh(), _render_quota(), _render_cache_info()),
                style="primary",
            )
            _run_both_button(
                backend, refresh,
                post_refresh=lambda: (_render_quota(), _render_cache_info()),
            )


def _cache_aware_run_button(
    backend, label: str, path: str, body: dict, *,
    sport_key: str, refresh_status=None, style: str = "primary",
) -> None:
    """Run button: cache check + confirm dialog (if stale), then POST
    the analyze route synchronously and force a full page reload on
    success.

    The earlier polling-based architecture (background thread + 5 s
    status timer) was over-engineered: analyze typically completes in
    well under a second once the model is warmed, and the polling
    indirection introduced a UI-never-updates bug (the timer fired,
    the soft-refresh callback redrew from stale closures, and users
    had to F5).  Synchronous + ui.navigate.reload() can't desync from
    server state -- the reload re-runs every data-load path from
    scratch.

    The /api/analyze/start + /api/analyze/status endpoints are kept on
    the backend for completeness (other callers may want async kick-off),
    but this UI no longer uses them.

    `path` is the synchronous analyze route ("/api/analyze" or
    "/api/wnba/analyze").
    """
    with ui.column().classes("w-full").style(
        "gap: 2px; min-width: 0; flex: 1 1 auto;"
    ):
        btn = ui.button(label).props("no-caps unelevated").style(_btn_style(style))
        status = _make_status_holder()

    async def _click():
        print(f"[ADMIN-BTN] _cache_aware_run_button click: {label!r}  "
              f"sport={sport_key}  path={path}",
              flush=True, file=sys.stderr)
        btn.props("loading"); btn.disable()
        try:
            # 1) Cache freshness check -- free, no upstream traffic.
            ok, status_data, _ = await asyncio.to_thread(
                _call, backend, "GET",
                f"/api/odds/cache_status?sport={sport_key}",
            )
            fresh = bool(ok and status_data.get("fresh"))

            # 2) Stale -> ask the user.
            if not fresh:
                ok_u, usage, _ = await asyncio.to_thread(
                    _call, backend, "GET", "/api/odds/usage")
                remaining_s = ""
                if ok_u:
                    rem  = int(usage.get("remaining") or 0)
                    lim  = int(usage.get("effective_limit") or 500)
                    remaining_s = f"  ({rem} of {lim} remaining today)"
                proceed = await _confirm_dialog(
                    f"{sport_key.upper()} odds cache is older than 15 minutes "
                    f"(or missing).  Pull fresh odds from The Odds API now?"
                    f"\n\nThis uses 1 daily quota request.{remaining_s}"
                )
                if not proceed:
                    _show_inline_status(
                        status,
                        f"{sport_key.upper()} analysis cancelled.  "
                        f"No API request made.",
                        "info",
                    )
                    return  # finally re-enables the button

            _show_inline_status(status, f"Running {sport_key.upper()} analysis...", "info")

            # 3) Run analysis synchronously.  asyncio.to_thread keeps the
            # NiceGUI event loop responsive (button stays in loading
            # state, other clients keep working) while the analyze
            # pipeline runs on a worker thread.
            ok_a, data_a, status_a = await asyncio.to_thread(
                _call, backend, "POST", path, body,
            )
            if not ok_a:
                _show_inline_status(
                    status,
                    f"{sport_key.upper()} analysis failed: "
                    f"{data_a.get('error') or f'HTTP {status_a}'}",
                    "error",
                )
                return

            n_games = len(data_a.get("results") or [])

            # 4) Re-hydrate the in-memory state from disk + refresh the
            # admin status widgets in place.
            try:
                backend.hydrate_state()
            except Exception as exc:                                       # noqa: BLE001
                print(f"admin: post-run hydrate failed: {exc}", flush=True)
            if refresh_status:
                refresh_status()

            _show_inline_status(
                status,
                f"{sport_key.upper()}: analyzed {n_games} games.  "
                f"Open Home or Sports to see fresh picks.",
                "success",
            )
            print(f"[ADMIN-BTN] OK: {label!r} -> {n_games} games",
                  flush=True, file=sys.stderr)
        except Exception as exc:                                          # noqa: BLE001
            import traceback as _tb
            print(f"[ADMIN-BTN] CRASH: {label!r}  {type(exc).__name__}: {exc}\n"
                  f"{_tb.format_exc()}", flush=True, file=sys.stderr)
            _show_inline_status(
                status, f"Click handler error: {type(exc).__name__}: {exc}", "error",
            )
        finally:
            btn.props(remove="loading"); btn.enable()

    btn.on("click", _click)


def _run_both_button(backend, refresh, post_refresh=None) -> None:
    """Run All Models: combined cache check + single confirmation, then run
    the MLB game model, the WNBA game model, and the MLB props fresh re-pull
    synchronously back-to-back.  Same architecture as _cache_aware_run_button
    -- no background workers, no polling timer.

    Calls, in sequence:
      MLB game model    -> POST /api/analyze
      WNBA game model   -> POST /api/wnba/analyze
      MLB props re-pull -> POST /api/admin/props/repull (run_full_props_repull)
    """
    with ui.column().classes("w-full").style(
        "gap: 2px; min-width: 0; flex: 1 1 auto;"
    ):
        btn = ui.button("Run All Models").props("no-caps unelevated").style(
            f"background: {t.PRIMARY}; color: {t.BG}; "
            f"font-weight: 700; padding: 8px 16px; border-radius: {t.RADIUS_SM};"
        )
        status = _make_status_holder()

    async def _click():
        print("[ADMIN-BTN] _run_both_button (Run All Models) click",
              flush=True, file=sys.stderr)
        btn.props("loading"); btn.disable()
        try:
            # Combined cache check.
            _, status_data, _ = await asyncio.to_thread(
                _call, backend, "GET", "/api/odds/cache_status?sport=both")
            mlb_fresh  = bool((status_data.get("mlb")  or {}).get("fresh"))
            wnba_fresh = bool((status_data.get("wnba") or {}).get("fresh"))
            stale = [
                lbl for lbl, fresh in (("MLB", mlb_fresh), ("WNBA", wnba_fresh))
                if not fresh
            ]
            if stale:
                _, usage, _ = await asyncio.to_thread(
                    _call, backend, "GET", "/api/odds/usage")
                rem  = int(usage.get("remaining") or 0)
                lim  = int(usage.get("effective_limit") or 500)
                stale_s = " + ".join(stale)
                proceed = await _confirm_dialog(
                    f"{stale_s} odds cache is older than 15 minutes (or "
                    f"missing).  Pull fresh odds from The Odds API now?"
                    f"\n\nThis uses {len(stale)} daily quota request"
                    f"{'s' if len(stale) > 1 else ''}  "
                    f"({rem} of {lim} remaining today)."
                )
                if not proceed:
                    _show_inline_status(
                        status, "Run All Models cancelled.  No API requests made.",
                        "info",
                    )
                    return

            _show_inline_status(
                status, "Running MLB + WNBA models + MLB props re-pull...", "info")

            # Run all three synchronously back-to-back.
            msgs: list[str] = []
            had_err = False
            for sport_key, sport_path, sport_body in (
                ("mlb",  "/api/analyze",      {"bankroll": 250}),
                ("wnba", "/api/wnba/analyze", {"bankroll": 1000}),
            ):
                ok_a, data_a, status_a = await asyncio.to_thread(
                    _call, backend, "POST", sport_path, sport_body,
                )
                if ok_a:
                    n = len(data_a.get("results") or [])
                    msgs.append(f"{sport_key.upper()}: {n} games")
                else:
                    had_err = True
                    msgs.append(
                        f"{sport_key.upper()} failed: "
                        f"{data_a.get('error') or f'HTTP {status_a}'}"
                    )

            # Third: MLB props fresh re-pull (full ALL_MODEL_MARKETS pull + rescore).
            _show_inline_status(
                status, " | ".join(msgs) + " | Re-pulling MLB props...", "info")
            ok_p, data_p, status_p = await asyncio.to_thread(
                _call, backend, "POST", "/api/admin/props/repull", {},
            )
            if ok_p:
                msgs.append(f"Props: {data_p.get('kept', 0)} picks")
            else:
                had_err = True
                msgs.append(
                    f"Props failed: {data_p.get('error') or f'HTTP {status_p}'}")

            # Re-hydrate + refresh admin widgets in place.
            try:
                backend.hydrate_state()
            except Exception as exc:                                       # noqa: BLE001
                print(f"admin: post-run-both hydrate failed: {exc}", flush=True)
            refresh()
            if post_refresh:
                post_refresh()

            _show_inline_status(
                status,
                " | ".join(msgs) + ".  Open Home or Sports to see fresh picks.",
                "error" if had_err else "success",
            )
        except Exception as exc:                                          # noqa: BLE001
            import traceback as _tb
            print(f"[ADMIN-BTN] CRASH: Run All Models  {type(exc).__name__}: {exc}\n"
                  f"{_tb.format_exc()}", flush=True, file=sys.stderr)
            _show_inline_status(
                status, f"Click handler error: {type(exc).__name__}: {exc}", "error",
            )
        finally:
            btn.props(remove="loading"); btn.enable()

    btn.on("click", _click)


# ───────────────────────────────────────────────────────────────────────────
#  Section: MODELS
# ───────────────────────────────────────────────────────────────────────────

# ───────────────────────────────────────────────────────────────────────────
#  Section: PROPS  (manual refresh of the props_scored cache)
# ───────────────────────────────────────────────────────────────────────────

def _section_props(backend, refresh) -> None:
    """One-button forced refresh of the props_scored cache.

    Fires the same code path as the auto_props_refresh APScheduler job
    that runs every 15 min during 11 AM–11 PM ET -- raw-line fetch
    (Tier 1) followed by the scoring + enrichment pass that populates
    .cache/props_scored_mlb_{date}.json and the matching Supabase row.

    Useful right after a deploy: until the next scheduled tick the
    /props page would otherwise show the "Props loading" empty state
    because the local cache is wiped on Railway redeploys.  Clicking
    Refresh Props Now repopulates it in one shot.
    """
    with _card(
        "PROPS",
        "Refresh Props Now force-runs the same merge refresh + scoring pass "
        "auto_props_refresh fires every 15 minutes.  MLB Props does a full "
        "FRESH re-pull of all 11 model-backed markets, overwriting the cache "
        "from scratch — use it if the slate is missing markets (e.g. an old "
        "cache from before the all-markets fix).",
    ):
        with ui.row().classes("w-full").style("gap: 8px; flex-wrap: wrap;"):
            _async_button(
                backend, "Refresh Props Now",
                "POST", "/api/admin/props/refresh_now",
                spinner_msg="Refreshing… fetching lines and scoring all props.",
                done_msg=lambda d: (
                    f"Done — {d.get('kept', 0)} picks above threshold "
                    f"({d.get('scored', 0)} scored, "
                    f"{d.get('deduped', 0)} after dedup, "
                    f"{d.get('elapsed_ms', 0)} ms)."
                ),
                refresh_status=refresh,
            )
            _async_button(
                backend, "MLB Props",
                "POST", "/api/admin/props/repull",
                spinner_msg="Fresh re-pull… re-hitting the Odds API for all 11 "
                            "markets and re-scoring from scratch.",
                done_msg=lambda d: (
                    f"Fresh re-pull done — {d.get('kept', 0)} picks above "
                    f"threshold ({d.get('scored', 0)} scored, "
                    f"{d.get('deduped', 0)} after dedup, "
                    f"{d.get('elapsed_ms', 0)} ms)."
                ),
                refresh_status=refresh,
                style="primary",
            )


def _section_ai_analysis(backend) -> None:
    """On-demand Groq generation: game summaries, prop summaries, and player
    breakdowns that aren't cached yet.  Live progress counter polls the
    status endpoint every few seconds; the button is disabled + spins while a
    run is in progress (can't double-click)."""
    with _card(
        "AI ANALYSIS",
        "Run AI Analysis generates Groq summaries for every game pick, prop "
        "pick, and player breakdown that isn't cached yet (highest-confidence "
        "props first).  Force AI Refresh re-runs every one regardless of "
        "cache.  Sequential with a 150 ms gap between calls.",
    ):
        with ui.row().classes("w-full").style("gap: 8px; flex-wrap: wrap;"):
            btn = ui.button("Run AI Analysis").props(
                "no-caps unelevated").style(_btn_style("primary"))
            btn_force = ui.button("Force AI Refresh").props(
                "no-caps unelevated").style(_btn_style("warn"))
        prog = ui.column().classes("w-full").style("gap: 4px;")

        def _set_busy(busy: bool) -> None:
            for b in (btn, btn_force):
                if busy:
                    b.disable()
                else:
                    b.props(remove="loading")
                    b.enable()

        def _render(data: dict) -> None:
            prog.clear()
            with prog:
                if data.get("running"):
                    with ui.row().classes("items-center").style("gap: 8px;"):
                        ui.spinner(size="sm").style(f"color: {t.PRIMARY};")
                        ui.label(
                            f"Generating {data.get('phase') or 'summaries'}… "
                            f"{int(data.get('done') or 0)}/{int(data.get('total') or 0)} complete"
                        ).style(f"font-size: 12.5px; font-weight: 700; color: {t.TEXT};")
                elif data.get("summary"):
                    s = data["summary"]
                    ui.label(
                        f"Done in {s.get('elapsed')}s — "
                        f"{s.get('games_generated', 0)} game summaries, "
                        f"{s.get('props_generated', 0)} prop summaries, "
                        f"{s.get('breakdowns_generated', 0)} player breakdowns generated; "
                        f"{s.get('skipped', 0)} already cached/skipped"
                        + (f"; {s.get('failed', 0)} failed" if s.get('failed') else "")
                        + "."
                    ).style(
                        f"font-size: 12px; font-weight: 600; color: {t.POS}; "
                        f"white-space: normal; line-height: 1.4;"
                    )

        async def _poll() -> None:
            ok, data, _ = await asyncio.to_thread(
                _call, backend, "GET", "/api/admin/ai_analysis/status")
            if not ok:
                return
            _render(data)
            if not data.get("running"):
                _timer.active = False
                _set_busy(False)

        _timer = ui.timer(2.0, _poll, active=False)

        async def _start(force: bool, spinner_btn) -> None:
            spinner_btn.props("loading")
            _set_busy(True)
            ok, data, _ = await asyncio.to_thread(
                _call, backend, "POST", "/api/admin/ai_analysis/run",
                {"force": force})
            if not ok:
                _show_inline_status(prog, f"Failed to start: {data.get('error')}", "error")
                _set_busy(False)
                return
            _timer.active = True
            await _poll()

        async def _click() -> None:
            await _start(False, btn)

        async def _click_force() -> None:
            proceed = await _confirm_dialog(
                "This re-runs AI analysis on all games and props and uses API "
                "quota. Continue?"
            )
            if not proceed:
                return
            await _start(True, btn_force)

        btn.on("click", _click)
        btn_force.on("click", _click_force)

        # On (re)load, reflect an already-running run or the last result.
        async def _init() -> None:
            ok, data, _ = await asyncio.to_thread(
                _call, backend, "GET", "/api/admin/ai_analysis/status")
            if not ok:
                return
            if data.get("running"):
                (btn_force if data.get("forced") else btn).props("loading")
                _set_busy(True)
                _render(data)
                _timer.active = True
            elif data.get("summary"):
                _render(data)
        ui.timer(0.3, _init, once=True)


def _section_models(backend, refresh) -> None:
    with _card("MODELS", "Re-run predictions against cached odds; clear today's snapshot."):
        with ui.row().classes("w-full").style("gap: 8px; flex-wrap: wrap;"):
            _async_button(
                backend, "Refresh Models (cached odds)",
                "POST", "/api/refresh_models",
                spinner_msg="Re-running predictions on cached odds...",
                done_msg=lambda d: "Models refreshed against cached odds.",
                refresh_status=refresh,
            )
            _async_button(
                backend, "Clear MLB Snapshot",
                "POST", "/api/reset-sport",
                body={"sport": "mlb"},
                spinner_msg="Clearing MLB snapshot...",
                done_msg=lambda d: d.get("message") or "MLB snapshot cleared.",
                refresh_status=refresh,
                style="warn",
            )
            _async_button(
                backend, "Clear WNBA Snapshot",
                "POST", "/api/reset-sport",
                body={"sport": "wnba"},
                spinner_msg="Clearing WNBA snapshot...",
                done_msg=lambda d: d.get("message") or "WNBA snapshot cleared.",
                refresh_status=refresh,
                style="warn",
            )


# ───────────────────────────────────────────────────────────────────────────
#  Section: MODEL BETS
# ───────────────────────────────────────────────────────────────────────────

def _section_model_bets(backend, refresh) -> None:
    # Pull current toggle state up-front so the switches render correctly
    ok, data, _ = _call(backend, "GET", "/api/admin/model/settings")
    settings = (data or {}).get("settings") or {"mlb_enabled": True, "wnba_enabled": True}

    with _card(
        "MODEL BETS",
        "Top 5 by confidence per bet type. Auto-runs after each analysis for enabled sports.",
    ):
        _toggle_row(
            backend, "MLB auto-picks",
            "Include MLB in the model's auto-picks",
            field="mlb_enabled", initial=bool(settings.get("mlb_enabled")),
        )
        _toggle_row(
            backend, "WNBA auto-picks",
            "Include WNBA in the model's auto-picks",
            field="wnba_enabled", initial=bool(settings.get("wnba_enabled")),
        )
        # Home-page top-bar control -- toggles the overall-record chip.
        # Lives here because it shares model_settings.json (no need for a
        # second settings file or new endpoint).
        _toggle_row(
            backend, "Home: 'Overall' chip",
            "Show the overall W-L chip at the top of the home page",
            field="show_overall_chip",
            initial=bool(settings.get("show_overall_chip", True)),
        )
        # Anthropic chat daily cap.  Counted in Supabase app_cache under
        # 'ai_calls:<today_et>'; the AI Breakdown page disables Send once
        # the count hits this number.
        _number_row(
            backend, "AI chat: daily limit",
            "Max Anthropic API calls per day from the AI Breakdown chat",
            field="ai_daily_limit",
            initial=int(settings.get("ai_daily_limit", 20) or 20),
            min_value=1, max_value=500,
        )

        ui.label("Re-pick").style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2}; margin-top: 10px;"
        )
        with ui.row().classes("w-full").style("gap: 8px; flex-wrap: wrap;"):
            _async_button(
                backend, "Re-pick Both",
                "POST", "/api/admin/model/repick", body={"sport": "both"},
                spinner_msg="Re-picking model picks...",
                done_msg=lambda d: "Model picks regenerated.",
                refresh_status=refresh,
                style="primary",
            )
            _async_button(
                backend, "Re-pick MLB",
                "POST", "/api/admin/model/repick", body={"sport": "mlb"},
                spinner_msg="Re-picking MLB...",
                done_msg=lambda d: "MLB model picks regenerated.",
                refresh_status=refresh,
            )
            _async_button(
                backend, "Re-pick WNBA",
                "POST", "/api/admin/model/repick", body={"sport": "wnba"},
                spinner_msg="Re-picking WNBA...",
                done_msg=lambda d: "WNBA model picks regenerated.",
                refresh_status=refresh,
            )

        ui.label("Reset today's picks").style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2}; margin-top: 10px;"
        )
        with ui.row().classes("w-full").style("gap: 8px; flex-wrap: wrap;"):
            # done_msg surfaces the backend's "audit trail" string -- one
            # comma-separated line per storage layer that got cleared.
            # Falls back to the per-sport removed count when the backend
            # is older and doesn't return a message field.
            def _reset_done_msg(d):
                if d.get("message"):
                    return d["message"]
                rem = d.get("removed") or {}
                return (
                    f"Reset. MLB removed: {rem.get('mlb', 0)}, "
                    f"WNBA removed: {rem.get('wnba', 0)}."
                )

            _confirm_button(
                backend, "Reset MLB",
                "Are you sure? This permanently deletes today's pending MLB "
                "model picks from the Supabase model_picks table and refunds "
                "their stakes. This cannot be undone.",
                "POST", "/api/admin/model/reset", body={"sport": "mlb"},
                done_msg=_reset_done_msg,
                refresh_status=refresh,
                style="warn",
            )
            _confirm_button(
                backend, "Reset WNBA",
                "Are you sure? This permanently deletes today's pending WNBA "
                "model picks from the Supabase model_picks table and refunds "
                "their stakes. This cannot be undone.",
                "POST", "/api/admin/model/reset", body={"sport": "wnba"},
                done_msg=_reset_done_msg,
                refresh_status=refresh,
                style="warn",
            )
            _confirm_button(
                backend, "Reset Both",
                "Are you sure? This permanently deletes today's pending MLB + "
                "WNBA model picks from the Supabase model_picks table and "
                "refunds all stakes. This cannot be undone.",
                "POST", "/api/admin/model/reset", body={"sport": "both"},
                done_msg=_reset_done_msg,
                refresh_status=refresh,
                style="danger",
            )

        ui.label("Bankroll").style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2}; margin-top: 10px;"
        )
        _bankroll_button(
            backend, "Reset Model Bankroll...",
            which="model",
            done_msg="Model bankroll reset.",
            refresh_status=refresh,
        )

        # Force Settlement -- runs the auto-settle job NOW, bypassing
        # the 11 AM-2 AM ET time gate that the scheduled version uses.
        # Useful for manually closing open bets after a slate finishes.
        ui.label("Settlement").style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2}; margin-top: 10px;"
        )
        _async_button(
            backend, "Force Settlement",
            "POST", "/api/admin/settle_now",
            spinner_msg="Running settlement now...",
            done_msg=lambda d: (
                f"Settled {d.get('settled', 0)} bet(s) "
                f"({d.get('wins', 0)}W / {d.get('losses', 0)}L) "
                f"+ voided {d.get('voided', 0)} postponed."
            ),
            refresh_status=refresh,
        )


# ───────────────────────────────────────────────────────────────────────────
#  Section: MY BETS
# ───────────────────────────────────────────────────────────────────────────

def _section_my_bets(backend, refresh) -> None:
    with _card(
        "MY BETS",
        "Wipe your tracked bets and set your personal bankroll. Bets are unified across MLB + WNBA.",
    ):
        with ui.row().classes("w-full").style("gap: 8px; flex-wrap: wrap;"):
            _confirm_button(
                backend, "Wipe MLB Bets",
                "Wipe all MLB bets (open + history) and reset MLB bankrolls?",
                "POST", "/api/admin/wipe_ledger", body={"sport": "mlb"},
                done_msg=lambda d: f"Wiped: {', '.join(d.get('wiped') or [])}.",
                refresh_status=refresh,
                style="warn",
            )
            _confirm_button(
                backend, "Wipe WNBA Bets",
                "Wipe all WNBA bets (open + history) and reset WNBA bankrolls?",
                "POST", "/api/admin/wipe_ledger", body={"sport": "wnba"},
                done_msg=lambda d: f"Wiped: {', '.join(d.get('wiped') or [])}.",
                refresh_status=refresh,
                style="warn",
            )
            _confirm_button(
                backend, "Wipe Both Sports",
                "Wipe ALL bets across MLB + WNBA and reset all bankrolls?",
                "POST", "/api/admin/wipe_ledger", body={"sport": "both"},
                done_msg=lambda d: f"Wiped: {', '.join(d.get('wiped') or [])}.",
                refresh_status=refresh,
                style="danger",
            )
            _bankroll_button(
                backend, "Set My Bankroll...",
                which="personal",
                done_msg="Personal bankroll updated.",
                refresh_status=refresh,
            )


# ───────────────────────────────────────────────────────────────────────────
#  Section: DATA RESET ACTIONS -- Cannot Be Undone
#
#  Four granular resets per spec.  Each one is gated behind a confirm
#  dialog that quotes exactly what will be deleted.  The buttons are all
#  warn/danger-styled so the destructive nature is unmistakable.
#
#  Surface area:
#    1. Reset Model Record    -- drop unconfirmed history rows
#    2. Reset Model Bankroll  -- $1000 reset + drop unconfirmed open bets
#    3. Reset Confidence Record -- null out confidence_tier on history
#    4. Reset My Bets Record  -- drop confirmed history rows
#
#  Backend endpoints all live under /api/admin/reset/*  (see app.py).
# ───────────────────────────────────────────────────────────────────────────

def _section_data_reset(backend, refresh) -> None:
    # Heavy divider + warning header so this section reads as separate from
    # the normal admin controls above.  No card wrapper -- the buttons sit
    # directly on the page surface to feel deliberate / not routine.
    ui.element("div").style(
        f"width: 100%; height: 1px; background: {t.BORDER}; "
        f"margin-top: {t.SPACE_LG};"
    )
    with ui.row().classes("items-center w-full").style(
        f"gap: 10px; margin-top: {t.SPACE_LG};"
    ):
        ui.icon("warning").style(
            f"font-size: 18px; color: {t.NEG};"
        )
        ui.label("DATA RESET ACTIONS — Cannot Be Undone").style(
            f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.NEG};"
        )
    ui.label(
        "Each button below permanently deletes one slice of saved data. "
        "Every action requires explicit confirmation. Bankrolls survive the "
        "history resets; history survives the bankroll reset."
    ).style(
        f"font-size: 12px; color: {t.TEXT_DIM}; line-height: 1.5; "
        f"max-width: 720px;"
    )

    with _card(
        "Resets",
        "Choose carefully — there is no undo.",
    ):
        # Group 1 -- model side
        ui.label("Model side").style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2}; margin-top: 4px;"
        )
        with ui.row().classes("w-full").style("gap: 8px; flex-wrap: wrap;"):
            _confirm_button(
                backend, "Reset Model Record",
                (
                    "Are you sure? This permanently deletes ALL model pick "
                    "history across MLB + WNBA from the Supabase model_picks "
                    "table.  The model W/L record and units will reset to "
                    "0-0 and 0U.  The model bankroll dollar amount, open "
                    "bets, and your personal records are NOT affected.\n\n"
                    "This acts on live persistent Supabase data and cannot "
                    "be undone."
                ),
                "POST", "/api/admin/reset/model_record",
                done_msg=lambda d: (
                    f"Model record cleared. Removed: "
                    f"MLB={(d.get('removed') or {}).get('mlb', 0)}, "
                    f"WNBA={(d.get('removed') or {}).get('wnba', 0)}"
                ),
                refresh_status=refresh,
                style="danger",
            )
            _confirm_button(
                backend, "Reset Model Bankroll",
                (
                    "Permanently reset the model bankroll back to its "
                    "starting amount ($1000) on both MLB + WNBA ledgers, "
                    "AND clear every open model bet.  The settled W/L "
                    "history, your personal bankroll, and your personal "
                    "tracked bets are NOT affected.\n\n"
                    "This cannot be undone."
                ),
                "POST", "/api/admin/reset/model_bankroll",
                done_msg=lambda d: (
                    f"Model bankroll reset to starting amount. Removed open "
                    f"bets: "
                    f"MLB={(d.get('removed_open_bets') or {}).get('mlb', 0)}, "
                    f"WNBA={(d.get('removed_open_bets') or {}).get('wnba', 0)}"
                ),
                refresh_status=refresh,
                style="danger",
            )

        # Group 2 -- everything else
        ui.label("Other").style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2}; margin-top: 10px;"
        )
        with ui.row().classes("w-full").style("gap: 8px; flex-wrap: wrap;"):
            _confirm_button(
                backend, "Reset Confidence Record",
                (
                    "Permanently clear the Confidence Performance tracker. "
                    "Strong, Moderate, and Low tier W/L records will all "
                    "reset to 0-0 for BOTH model picks and your personal "
                    "picks.\n\n"
                    "The underlying win/loss results stay on the bets — "
                    "only the confidence-tier tagging is cleared, so the "
                    "Confidence Performance card recomputes from scratch.\n\n"
                    "This cannot be undone."
                ),
                "POST", "/api/admin/reset/confidence_record",
                done_msg=lambda d: (
                    f"Confidence tiers cleared. Updated rows: "
                    f"MLB={(d.get('cleared') or {}).get('mlb', 0)}, "
                    f"WNBA={(d.get('cleared') or {}).get('wnba', 0)}"
                ),
                refresh_status=refresh,
                style="danger",
            )
            _confirm_button(
                backend, "Reset My Bets Record",
                (
                    "Permanently delete ALL personal tracked bet history "
                    "across MLB + WNBA.  Personal W/L records and personal "
                    "units will reset to 0-0 and 0U.  The personal bankroll "
                    "dollar amount, the model's record, and open bets are "
                    "NOT affected.\n\n"
                    "This cannot be undone."
                ),
                "POST", "/api/admin/reset/my_bets_record",
                done_msg=lambda d: (
                    f"My Bets record cleared. Removed: "
                    f"MLB={(d.get('removed') or {}).get('mlb', 0)}, "
                    f"WNBA={(d.get('removed') or {}).get('wnba', 0)}"
                ),
                refresh_status=refresh,
                style="danger",
            )


# ───────────────────────────────────────────────────────────────────────────
#  Section: DIAGNOSTICS
#  End-to-end probe of every data source the UI depends on.  No mutations,
#  no API quota burn -- everything here is read-only.
# ───────────────────────────────────────────────────────────────────────────

def _section_diagnostics(backend) -> None:
    """Live probe panel.  Renders empty rows and a 'Run' button; the
    button populates the rows when pressed (and re-runs on click)."""
    with _card(
        "DIAGNOSTICS",
        "Probe every data source: in-memory state, snapshot files, daily "
        "picks, Supabase, Odds API key, ledgers.",
    ):
        results_holder = ui.column().classes("w-full").style("gap: 0;")

        async def _run():
            results_holder.clear()
            with results_holder:
                ui.label("Running probes...").style(
                    f"color: {t.TEXT_DIM}; font-size: 12px; padding: 8px 0;"
                )
            probes = await asyncio.to_thread(_run_diagnostics, backend)
            results_holder.clear()
            with results_holder:
                for label, status, detail in probes:
                    _diag_row(label, status, detail)

        with ui.row().style("gap: 8px; flex-wrap: wrap;"):
            ui.button("Run Diagnostics", on_click=_run) \
                .props("no-caps unelevated") \
                .style(_btn_style("primary"))
            _sharpapi_probe_button(results_holder)

        # Auto-run on page open so the user doesn't need to click first
        ui.timer(0.5, _run, once=True)


def _sharpapi_probe_button(results_holder) -> None:
    """One-shot SharpAPI endpoint + auth-style probe.  Renders the result
    rows into the same `results_holder` the main diagnostics use, so the
    output sits in the user's existing field of view."""
    with ui.column().classes("w-full").style("gap: 2px; min-width: 0;"):
        btn = ui.button("Probe SharpAPI").props("no-caps unelevated") \
            .style(_btn_style("default"))
        status_holder = _make_status_holder()

    async def _click():
        print("[ADMIN-BTN] _sharpapi_probe_button click",
              flush=True, file=sys.stderr)
        btn.props("loading"); btn.disable()
        try:
            import os as _os
            key = (_os.environ.get("SHARPAPI_KEY") or "").strip()
            if not key:
                _show_inline_status(
                    status_holder,
                    "SHARPAPI_KEY is not set in Railway -- nothing to probe.",
                    "warning",
                )
                return

            _show_inline_status(
                status_holder, "Probing SharpAPI endpoints + auth styles...", "info",
            )

            def _do():
                from src.odds_client import SharpApiClient
                from src.cache import Cache
                client = SharpApiClient(key, Cache())
                return client.probe_endpoints()

            rows = await asyncio.to_thread(_do)

            results_holder.clear()
            with results_holder:
                ui.label(f"SharpAPI probe: {len(rows)} endpoint/auth combos tried").style(
                    f"font-size: 12px; font-weight: 700; color: {t.TEXT}; "
                    f"padding: 8px 0; letter-spacing: .5px;"
                )
                for r in rows:
                    label  = f"{r['endpoint']}  [{r['auth']}]"
                    diag_status = (
                        "ok"   if (r.get("ok") and r.get("status") == 200)
                        else "warn" if (r.get("status") in (200, 401, 403))
                        else "err"
                    )
                    sample = (r.get("sample") or "")[:400].replace("\n", " ")
                    detail = (
                        f"status={r.get('status')}  bytes={r.get('bytes')}  "
                        f"body[:400]={sample}"
                    )
                    _diag_row(label, diag_status, detail)

            ok_count = sum(1 for r in rows if r.get("ok") and r.get("status") == 200)
            _show_inline_status(
                status_holder,
                f"SharpAPI probe done: {ok_count}/{len(rows)} returned 200 OK.  "
                f"Body samples below show what each endpoint returns.",
                "success" if ok_count else "warning",
            )
        except Exception as exc:                                          # noqa: BLE001
            import traceback as _tb
            print(f"[ADMIN-BTN] CRASH: Probe SharpAPI  {type(exc).__name__}: {exc}\n"
                  f"{_tb.format_exc()}", flush=True, file=sys.stderr)
            _show_inline_status(
                status_holder,
                f"SharpAPI probe failed: {type(exc).__name__}: {exc}",
                "error",
            )
        finally:
            btn.props(remove="loading"); btn.enable()

    btn.on("click", _click)


def _diag_row(label: str, status: str, detail: str) -> None:
    """One diagnostic line.  status: 'ok' | 'warn' | 'err' | 'info'."""
    color = {
        "ok":   t.POS,
        "warn": t.WARN,
        "err":  t.NEG,
        "info": t.TEXT_DIM,
    }.get(status, t.TEXT_DIM)
    icon = {"ok": "✓", "warn": "!", "err": "×", "info": "·"}.get(status, "·")
    with ui.row().classes("items-start w-full").style(
        f"padding: 8px 0; gap: 10px; "
        f"border-bottom: 1px solid {t.BORDER_SOFT};"
    ):
        ui.label(icon).style(
            f"color: {color}; font-weight: 800; min-width: 16px; "
            f"font-family: monospace; font-size: 13px;"
        )
        with ui.column().style("flex: 1; gap: 2px; min-width: 0;"):
            ui.label(label).style(
                f"color: {t.TEXT}; font-size: 13px; font-weight: 600;"
            )
            ui.label(detail).style(
                f"color: {color}; font-size: 11.5px; font-family: monospace; "
                f"line-height: 1.4; word-break: break-word;"
            )


# ─── Probe runner ─────────────────────────────────────────────────────────

def _run_diagnostics(backend) -> list[tuple[str, str, str]]:
    """Run every probe synchronously and return (label, status, detail)
    tuples.  Called via asyncio.to_thread so the UI thread stays free."""
    out: list[tuple[str, str, str]] = []

    # 1. In-memory analysis state
    try:
        n_mlb  = len(backend._analysis_state.get("results")      or [])
        n_wnba = len(backend._wnba_analysis_state.get("results") or [])
        ts_mlb  = backend._analysis_state.get("last_analyzed_at")
        ts_wnba = backend._wnba_analysis_state.get("last_analyzed_at")
        ok = (n_mlb + n_wnba) > 0
        out.append((
            "In-memory analysis state",
            "ok" if ok else "warn",
            f"MLB: {n_mlb} games (last {ts_mlb or '—'})  |  "
            f"WNBA: {n_wnba} games (last {ts_wnba or '—'})",
        ))
    except Exception as exc:                                              # noqa: BLE001
        out.append(("In-memory analysis state", "err", f"{type(exc).__name__}: {exc}"))

    # 1b. Games skipped during prediction (per-sport)
    # The analyze loop now records games that survived the API + stale-date
    # filters but got dropped during model prediction (e.g. unknown team
    # has no training data).  Surface them here so "0 games" with N>0 in
    # the cache is immediately distinguishable from "0 games returned by
    # the API".
    for sport_label, state_attr in (
        ("MLB",  "_analysis_state"),
        ("WNBA", "_wnba_analysis_state"),
    ):
        try:
            state = getattr(backend, state_attr, {}) or {}
            sk = state.get("skipped") or []
            if not sk:
                continue
            preview = "  |  ".join(
                f"{s.get('matchup','?')} ({s.get('reason','?')})"
                for s in sk[:3]
            )
            more = f"  +{len(sk)-3} more" if len(sk) > 3 else ""
            out.append((
                f"{sport_label} games skipped during prediction",
                "warn",
                f"{len(sk)} games dropped post-API.  {preview}{more}",
            ))
        except Exception as exc:                                          # noqa: BLE001
            out.append((f"{sport_label} skipped probe", "err",
                        f"{type(exc).__name__}: {exc}"))

    # 2. Daily snapshot file
    try:
        from pathlib import Path
        import json as _json
        p = Path("data/daily_snapshot.json")
        if not p.exists():
            out.append(("Daily snapshot file", "warn", "data/daily_snapshot.json -- missing"))
        else:
            snap = _json.loads(p.read_text(encoding="utf-8"))
            today = backend._today_et()
            snap_date = snap.get("date") or snap.get("mlb", {}).get("date")
            stale = snap_date != today
            mlb_n  = len((snap.get("mlb",  {}) or {}).get("results") or [])
            wnba_n = len((snap.get("wnba", {}) or {}).get("results") or [])
            status = "warn" if stale else ("ok" if (mlb_n + wnba_n) > 0 else "warn")
            out.append((
                "Daily snapshot file",
                status,
                f"date={snap_date} (today={today})  MLB={mlb_n}  WNBA={wnba_n}",
            ))
    except Exception as exc:                                              # noqa: BLE001
        out.append(("Daily snapshot file", "err", f"{type(exc).__name__}: {exc}"))

    # 3. Per-sport analysis caches
    for sport, cache_path in (("MLB",  "data/analysis_cache.json"),
                               ("WNBA", "data/wnba_analysis_cache.json")):
        try:
            from pathlib import Path
            import json as _json
            p = Path(cache_path)
            if not p.exists():
                out.append((f"{sport} analysis cache", "warn", f"{cache_path} -- missing"))
                continue
            payload = _json.loads(p.read_text(encoding="utf-8"))
            today = backend._today_et()
            stale = payload.get("date") != today
            n = len(payload.get("results") or [])
            out.append((
                f"{sport} analysis cache",
                "warn" if stale else ("ok" if n > 0 else "warn"),
                f"{cache_path}  date={payload.get('date')} (today={today})  games={n}",
            ))
        except Exception as exc:                                          # noqa: BLE001
            out.append((f"{sport} analysis cache", "err", f"{type(exc).__name__}: {exc}"))

    # 4. Daily picks file (10 best game picks + 5 prop picks)
    try:
        daily = backend.load_daily_picks() or {}
        picks = daily.get("picks") or {}
        n_game  = len(picks.get("game_picks") or [])
        n_props = len(picks.get("prop_picks") or [])
        total   = n_game + n_props
        out.append((
            "Daily picks file",
            "ok" if total > 0 else "warn",
            f"data/daily_picks.json  Game={n_game}  Props={n_props}",
        ))
    except Exception as exc:                                              # noqa: BLE001
        out.append(("Daily picks file", "err", f"{type(exc).__name__}: {exc}"))

    # 5. Analysis timestamps file
    try:
        ts = backend._read_analysis_timestamps() or {}
        mlb_ts  = (ts.get("mlb")  or {}).get("analyzed_at")  or "—"
        wnba_ts = (ts.get("wnba") or {}).get("analyzed_at")  or "—"
        out.append((
            "Analysis timestamps",
            "ok" if (mlb_ts != "—" or wnba_ts != "—") else "warn",
            f"MLB={mlb_ts}  WNBA={wnba_ts}",
        ))
    except Exception as exc:                                              # noqa: BLE001
        out.append(("Analysis timestamps", "err", f"{type(exc).__name__}: {exc}"))

    # 6. Supabase status
    try:
        from src import db as _db
        st = _db.status() or {}
        mode = st.get("mode", "json")
        sb_on = bool(st.get("supabase"))
        url_set = bool(st.get("url_set"))
        key_set = bool(st.get("key_set"))
        if mode == "supabase" and sb_on:
            level, detail = "ok", f"mode=supabase  url_set={url_set}  key_set={key_set}"
        elif url_set or key_set:
            level = "warn"
            detail = (f"mode={mode}  url_set={url_set}  key_set={key_set}  "
                      f"-- creds present but not connected")
        else:
            level = "info"
            detail = f"mode=json  (SUPABASE_URL / SUPABASE_KEY not set -- JSON-only fallback)"
        out.append(("Supabase", level, detail))
    except Exception as exc:                                              # noqa: BLE001
        out.append(("Supabase", "err", f"{type(exc).__name__}: {exc}"))

    # 6b. Supabase app_cache table (persistent snapshot + analysis caches)
    try:
        from src import db as _db
        if not _db.is_supabase():
            out.append(("Supabase app_cache", "info",
                        "skipped (Supabase not connected)"))
        else:
            keys = ("daily_snapshot", "analysis_cache:mlb", "analysis_cache:wnba")
            bits = []
            anything = False
            for k in keys:
                row = _db.cache_get(k)
                if row:
                    anything = True
                    bits.append(f"{k}: date={row.get('date')}")
                else:
                    bits.append(f"{k}: —")
            out.append((
                "Supabase app_cache",
                "ok" if anything else "warn",
                " | ".join(bits),
            ))

            # 6c. Daily odds cache rows -- the new ~1-call-per-sport-per-day
            # entries.  Show date + game count so the user can see at a glance
            # whether today's MLB/WNBA odds are already cached (no live API
            # call needed) and how many games are in each.
            odds_keys = (
                ("MLB",  "odds_daily:baseball_mlb:h2h,spreads,totals:us"),
                ("WNBA", "odds_daily:basketball_wnba:h2h,spreads,totals:us"),
            )
            today = backend._today_et()
            for label, k in odds_keys:
                row = _db.cache_get(k)
                if row is None:
                    out.append((
                        f"Odds daily cache: {label}",
                        "warn",
                        f"key={k}  -- no row yet, next analyze will burn 1 live API call",
                    ))
                    continue
                row_date = row.get("date")
                data = row.get("data") or []
                n = len(data) if isinstance(data, list) else "?"
                fresh = (row_date == today)
                out.append((
                    f"Odds daily cache: {label}",
                    "ok" if fresh else "warn",
                    f"date={row_date}  games={n}  "
                    + ("(today -- analyze will skip live API)" if fresh
                       else f"(stale -- today is {today}, will be refreshed)"),
                ))
    except Exception as exc:                                              # noqa: BLE001
        # Most likely failure: table doesn't exist yet.  Surface that clearly.
        msg = str(exc)
        hint = (" -- create the table via the SQL in src/db.py header"
                if "PGRST205" in msg or "does not exist" in msg.lower() else "")
        out.append(("Supabase app_cache", "err",
                    f"{type(exc).__name__}: {msg}{hint}"))

    # 7. Odds API key presence + live validity probe.  Calls the
    # /v4/sports endpoint which does NOT count against quota, so this
    # probe is safe to run on every diagnostic refresh.  Surfaces 401
    # ("key invalid / expired / revoked") and 429 ("quota exhausted")
    # explicitly so the user doesn't see them as a 15-minute "hang"
    # again -- those are the exact failures behind today's incident.
    try:
        import os as _os
        key = _os.environ.get("ODDS_API_KEY") or ""
        if not key:
            out.append(("Odds API key", "err",
                        "ODDS_API_KEY env var not set -- analysis cannot fetch odds"))
        else:
            try:
                import requests as _req
                resp = _req.get(
                    "https://api.the-odds-api.com/v4/sports",
                    params={"apiKey": key},
                    timeout=5,
                )
                used = resp.headers.get("x-requests-used", "?")
                rem  = resp.headers.get("x-requests-remaining", "?")
                if resp.status_code == 401:
                    out.append((
                        "Odds API key", "err",
                        f"401 Unauthorized -- key invalid / expired / revoked. "
                        f"Update ODDS_API_KEY in Railway -> Variables.",
                    ))
                elif resp.status_code == 429:
                    out.append((
                        "Odds API key", "err",
                        f"429 quota exhausted (used={used}, remaining={rem}). "
                        f"Wait for monthly reset or upgrade plan.",
                    ))
                elif resp.status_code >= 400:
                    out.append((
                        "Odds API key", "warn",
                        f"HTTP {resp.status_code} from /v4/sports  "
                        f"(used={used}, remaining={rem})",
                    ))
                else:
                    out.append((
                        "Odds API key", "ok",
                        f"valid -- used={used}, remaining={rem}  "
                        f"(prefix={key[:4]}..., len={len(key)})",
                    ))
                    # Drill-down: which sports does this plan cover?  The
                    # /v4/sports response is a JSON array of {key, active,
                    # title, ...} dicts.  Surface a per-sport row for
                    # baseball_mlb and basketball_wnba so the user can
                    # tell "0 games returned" apart from "plan does not
                    # include this sport".
                    try:
                        sports = resp.json() or []
                        wanted = {
                            "baseball_mlb":     "MLB",
                            "basketball_wnba":  "WNBA",
                        }
                        seen = {
                            s.get("key"): s for s in sports
                            if isinstance(s, dict)
                        }
                        for sport_key, label in wanted.items():
                            entry = seen.get(sport_key)
                            if entry is None:
                                out.append((
                                    f"Odds API: {label} availability",
                                    "err",
                                    f"sport_key '{sport_key}' NOT in your plan's "
                                    f"coverage list ({len(seen)} sports returned). "
                                    f"Upgrade plan or check "
                                    f"https://the-odds-api.com/sports-odds-data/",
                                ))
                            elif entry.get("active") is False:
                                out.append((
                                    f"Odds API: {label} availability",
                                    "warn",
                                    f"sport is in plan but currently inactive "
                                    f"(off-season / between updates). "
                                    f"title='{entry.get('title')}', "
                                    f"active=False -- no games will be returned",
                                ))
                            else:
                                out.append((
                                    f"Odds API: {label} availability",
                                    "ok",
                                    f"active=True, group={entry.get('group')}, "
                                    f"title='{entry.get('title')}' -- 0 games at "
                                    f"runtime just means no books have posted "
                                    f"lines yet (typical between ~midnight and "
                                    f"~10 AM ET)",
                                ))
                    except Exception as exc:                              # noqa: BLE001
                        out.append((
                            "Odds API: sport availability",
                            "warn",
                            f"could not parse /v4/sports response: "
                            f"{type(exc).__name__}: {exc}",
                        ))
            except _req.Timeout:
                out.append(("Odds API key", "warn",
                            "probe timed out after 5s -- key untested, "
                            "analysis may still work"))
            except Exception as exc:                                      # noqa: BLE001
                out.append(("Odds API key", "warn",
                            f"probe failed ({type(exc).__name__}: {exc}); "
                            f"env var is set (prefix={key[:4]}...)"))
    except Exception as exc:                                              # noqa: BLE001
        out.append(("Odds API key", "err", f"{type(exc).__name__}: {exc}"))

    # 8. Ledger files
    for sport, path in (("MLB", "data/ledger.json"), ("WNBA", "data/wnba_ledger.json")):
        try:
            from pathlib import Path
            p = Path(path)
            if not p.exists():
                out.append((f"{sport} ledger", "warn", f"{path} -- missing"))
                continue
            led = backend.Ledger(path=path, starting_bankroll=1000.0)
            s = led.get_summary()
            open_n = len(led.data.get("open_bets") or [])
            hist_n = len(led.data.get("history")   or [])
            out.append((
                f"{sport} ledger",
                "ok",
                f"{path}  model=${s.get('model_bankroll',0):.2f}  "
                f"personal=${s.get('personal_bankroll',0):.2f}  "
                f"open={open_n}  history={hist_n}",
            ))
        except Exception as exc:                                          # noqa: BLE001
            out.append((f"{sport} ledger", "err", f"{type(exc).__name__}: {exc}"))

    # 8b. Auto-analysis lock status -- is an analysis actively running?
    try:
        lock = getattr(backend, "_auto_analysis_lock", None)
        if lock is None:
            out.append(("Auto-analysis lock", "info",
                        "lock object not found on backend module"))
        else:
            held = lock.locked()
            out.append((
                "Auto-analysis lock",
                "warn" if held else "ok",
                ("held -- the scheduled auto-analysis is currently running. "
                 "Manual Run buttons may appear stuck until this finishes.")
                if held else "free -- no scheduled analysis in flight",
            ))
    except Exception as exc:                                              # noqa: BLE001
        out.append(("Auto-analysis lock", "err", f"{type(exc).__name__}: {exc}"))

    # 9. Auto-analysis log (when did the scheduler last fire?)
    try:
        from pathlib import Path
        import json as _json
        p = Path("data/auto_analysis_log.json")
        if not p.exists():
            out.append(("Auto-analysis log", "info",
                        "data/auto_analysis_log.json -- not yet written"))
        else:
            log = _json.loads(p.read_text(encoding="utf-8"))
            last = log.get("last_run") or log.get("history", [{}])[-1] if isinstance(log, dict) else None
            out.append((
                "Auto-analysis log",
                "ok",
                f"last_run={last}",
            ))
    except Exception as exc:                                              # noqa: BLE001
        out.append(("Auto-analysis log", "err", f"{type(exc).__name__}: {exc}"))

    return out


# ───────────────────────────────────────────────────────────────────────────
#  Status header (last-analyzed + DB)
# ───────────────────────────────────────────────────────────────────────────

def _render_status(backend, holder) -> None:
    """Re-poll /api/admin/status and refresh the meta rows.  Called on page
    load and after every mutation so timestamps stay accurate."""
    holder.clear()
    ok, data, _ = _call(backend, "GET", "/api/admin/status")
    mlb_ts  = data.get("mlb_analyzed_at")  if ok else None
    wnba_ts = data.get("wnba_analyzed_at") if ok else None
    db      = (data.get("db") or {}) if ok else {}

    def _row(label: str, value: str, value_color: str = t.TEXT) -> None:
        with ui.row().classes("items-center w-full").style(
            f"justify-content: space-between; gap: 8px; "
            f"padding: 4px 0; border-bottom: 1px solid {t.BORDER_SOFT};"
        ):
            ui.label(label).style(f"color: {t.TEXT_DIM}; font-size: 12px;")
            ui.label(value).style(
                f"color: {value_color}; font-size: 12px; font-family: monospace;"
            )

    with holder:
        with ui.column().classes("w-full").style(
            f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 2px;"
        ):
            ui.label("STATUS").style(
                f"font-size: 11px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT_DIM2}; margin-bottom: 4px;"
            )
            _row("Last MLB analyzed",  _fmt_ts(mlb_ts))
            _row("Last WNBA analyzed", _fmt_ts(wnba_ts))
            _row("DB mode", str(db.get("mode") or "json"))
            sb = db.get("supabase")
            if sb is not None:
                _row("Supabase", "connected" if sb else "off",
                     value_color=t.POS if sb else t.TEXT_DIM)


def _fmt_ts(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_ET)
        return dt.strftime("%a %b %-d  %-I:%M %p ET")
    except Exception:                                                     # noqa: BLE001
        return iso[:19]


# ───────────────────────────────────────────────────────────────────────────
#  Reusable widgets
# ───────────────────────────────────────────────────────────────────────────

def _section_model_performance(backend) -> None:
    """PART 4 -- per-model W/L table with a date-range filter.  Reads the
    aggregated model_picks rows directly (in-process) and refreshes on load."""
    state = {"range": "all", "date": None}
    _RANGES = {"all": "All-time", "today": "Today", "yesterday": "Yesterday",
               "7d": "Last 7 Days", "30d": "Last 30 Days"}

    with _card("MODEL PERFORMANCE",
               "Per-model record from every logged pick. Sorted by win% desc. "
               "Click a row to browse that model's picks by date."):
        with ui.row().classes("items-center").style("gap: 8px; flex-wrap: wrap;"):
            def _on_range(e) -> None:
                state["range"] = e.value or "all"
                state["date"] = None
                _table.refresh()
            ui.toggle(_RANGES, value="all", on_change=_on_range).props(
                "dense no-caps").style(f"color: {t.TEXT_DIM};")

            def _on_date(e) -> None:
                if e.value:
                    state["date"] = e.value
                    _table.refresh()
            with ui.input("Custom day").props("dense outlined").style(
                "width: 150px;") as _date_in:
                with _date_in.add_slot("append"):
                    ui.icon("event").classes("cursor-pointer")
                with ui.menu() as _menu:
                    ui.date(on_change=_on_date).bind_value(_date_in)
                _date_in.on("click", _menu.open)

        @ui.refreshable
        def _table() -> None:                                             # noqa: WPS430
            from src import model_picks as _mp
            since = until = None
            if state.get("date"):
                since = until = state["date"]               # single custom day
            elif state["range"] != "all":
                since, until = _mp.date_range(state["range"])
            try:
                data = _mp.performance(since, until)
            except Exception as exc:                                      # noqa: BLE001
                ui.label(f"Model performance unavailable: {exc}").style(
                    f"font-size: 12px; color: {t.TEXT_DIM};")
                return
            rows = data.get("rows") or []
            ts = (data.get("updated_at") or "")[:19].replace("T", " ")
            ui.label(f"Updated {ts} UTC · {len(rows)} model rows").style(
                f"font-size: 11px; color: {t.TEXT_DIM2};")
            if not rows:
                ui.label("No settled model picks in this range yet. Rows appear "
                         "once the model_picks Supabase table exists and games "
                         "settle in the 15-minute cycle.").style(
                    f"font-size: 12px; color: {t.TEXT_DIM}; font-style: italic;")
                return

            th = (f"font-size:10px; font-weight:800; letter-spacing:.4px; "
                  f"color:{t.TEXT_DIM2}; padding:6px 8px; text-align:right; "
                  f"border-bottom:1px solid {t.BORDER}; white-space:nowrap;")
            th_l = th.replace("text-align:right", "text-align:left")
            head = (f"<th style='{th_l}'>Model</th><th style='{th_l}'>Sport</th>"
                    f"<th style='{th_l}'>Type</th><th style='{th}'>W</th>"
                    f"<th style='{th}'>L</th><th style='{th}'>Win%</th>"
                    f"<th style='{th_l}'>Last 10</th><th style='{th}'>Avg Conf</th>")
            body = ""
            for r in rows:
                td = (f"font-size:12px; font-family:monospace; padding:6px 8px; "
                      f"text-align:right; color:{t.TEXT}; "
                      f"border-bottom:1px solid {t.BORDER_SOFT}; white-space:nowrap;")
                td_l = td.replace("text-align:right", "text-align:left")
                wp = r.get("win_pct")
                wp_s = f"{wp:.1f}%" if isinstance(wp, (int, float)) else "—"
                wp_color = (t.POS if (wp or 0) >= 55 else
                            t.NEG if (wp is not None and wp < 50) else t.TEXT)
                last10 = "".join(
                    f"<span style='color:{t.POS if c == 'W' else t.NEG};'>{c}</span>"
                    for c in (r.get("last10") or "")
                ) or "—"
                ac = r.get("avg_confidence")
                ac_s = f"{ac * 100:.0f}%" if isinstance(ac, (int, float)) else "—"
                _mn = r.get("model_name", "")
                _sp = r.get("sport") or ""
                _link = (f"<a href='/model-history/{_sp}/{_mn}' "
                         f"style='color:{t.PRIMARY}; text-decoration:none; "
                         f"font-weight:800;'>{_mn}</a>")
                body += (
                    f"<tr><td style='{td_l}'>{_link}</td>"
                    f"<td style='{td_l} color:{t.TEXT_DIM};'>{_sp.upper()}</td>"
                    f"<td style='{td_l} color:{t.TEXT_DIM};'>{r.get('pick_type', '')}</td>"
                    f"<td style='{td} color:{t.POS};'>{r.get('wins', 0)}</td>"
                    f"<td style='{td} color:{t.NEG};'>{r.get('losses', 0)}</td>"
                    f"<td style='{td} font-weight:800; color:{wp_color};'>{wp_s}</td>"
                    f"<td style='{td_l} font-family:monospace; font-weight:800;'>{last10}</td>"
                    f"<td style='{td} color:{t.TEXT_DIM};'>{ac_s}</td></tr>"
                )
            ui.html(
                f"<div style='overflow-x:auto; width:100%;'>"
                f"<table style='width:100%; border-collapse:collapse;'>"
                f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
            )

        _table()


def _card(title: str, subtitle: str | None = None):
    """Context-manager card.  Body of caller's `with` block goes inside."""
    col = ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 10px;"
    )
    with col:
        ui.label(title).style(
            f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT};"
        )
        if subtitle:
            ui.label(subtitle).style(
                f"font-size: 12px; color: {t.TEXT_DIM};"
            )
    return col


def _btn_style(style: str) -> str:
    """Common styling for admin buttons.  `style` is one of:
       'default' | 'primary' | 'warn' | 'danger'."""
    if style == "primary":
        return (
            f"background: {t.PRIMARY}; color: {t.BG}; "
            f"font-weight: 700; padding: 8px 16px; border-radius: {t.RADIUS_SM};"
        )
    if style == "warn":
        return (
            f"background: transparent; color: {t.WARN}; "
            f"border: 1px solid {t.WARN}; "
            f"font-weight: 700; padding: 7px 15px; border-radius: {t.RADIUS_SM};"
        )
    if style == "danger":
        return (
            f"background: {t.NEG}; color: {t.TEXT}; "
            f"font-weight: 700; padding: 8px 16px; border-radius: {t.RADIUS_SM};"
        )
    return (
        f"background: {t.CARD_HI}; color: {t.TEXT}; "
        f"border: 1px solid {t.BORDER}; "
        f"font-weight: 600; padding: 7px 15px; border-radius: {t.RADIUS_SM};"
    )


def _async_button(
    backend, label: str, method: str, path: str, *,
    body: dict | None = None,
    spinner_msg: str = "Working...",
    done_msg=None,
    refresh_status=None,
    style: str = "default",
) -> None:
    # Wrap the button + status holder in a column so the inline message
    # sits directly under the button and the column flows correctly
    # whether the parent context is a row or a stack.
    with ui.column().classes("w-full").style(
        "gap: 2px; min-width: 0; flex: 1 1 auto;"
    ):
        btn = ui.button(label).props("no-caps unelevated").style(_btn_style(style))
        status = _make_status_holder()

    async def _click():
        print(f"[ADMIN-BTN] _async_button click: {label!r}  path={path}  body={body}",
              flush=True, file=sys.stderr)
        btn.props("loading")
        btn.disable()
        try:
            _show_inline_status(status, spinner_msg, "info")
            ok, data, _ = await asyncio.to_thread(_call, backend, method, path, body)
            if ok:
                msg = done_msg(data) if callable(done_msg) else (done_msg or "Done.")
                print(f"[ADMIN-BTN] OK: {label!r} -> {msg!r}",
                      flush=True, file=sys.stderr)
                _show_inline_status(status, msg, "success")
                if refresh_status:
                    refresh_status()
            else:
                err = data.get('error') or 'unknown error'
                print(f"[ADMIN-BTN] FAIL: {label!r} -> {err!r}",
                      flush=True, file=sys.stderr)
                _show_inline_status(status, f"{label} failed: {err}", "error")
        except Exception as exc:                                          # noqa: BLE001
            import traceback as _tb
            tb_str = _tb.format_exc()
            print(f"[ADMIN-BTN] CRASH: {label!r}  {type(exc).__name__}: {exc}\n"
                  f"{tb_str}", flush=True, file=sys.stderr)
            try:
                _show_inline_status(
                    status,
                    f"{label} crashed: {type(exc).__name__}: {exc}",
                    "error",
                )
            except Exception:                                              # noqa: BLE001
                pass
        finally:
            btn.props(remove="loading")
            btn.enable()

    btn.on("click", _click)


def _confirm_button(
    backend, label: str, prompt: str, method: str, path: str, *,
    body: dict | None = None,
    done_msg=None,
    refresh_status=None,
    style: str = "default",
) -> None:
    """Button that opens a confirm dialog before firing the request."""
    with ui.column().classes("w-full").style(
        "gap: 2px; min-width: 0; flex: 1 1 auto;"
    ):
        btn = ui.button(label).props("no-caps unelevated").style(_btn_style(style))
        status = _make_status_holder()

    async def _click():
        print(f"[ADMIN-BTN] _confirm_button click: {label!r}  path={path}  body={body}",
              flush=True, file=sys.stderr)
        try:
            confirmed = await _confirm_dialog(prompt)
        except Exception as exc:                                          # noqa: BLE001
            import traceback as _tb
            print(f"[ADMIN-BTN] CRASH (confirm dialog): {label!r}  "
                  f"{type(exc).__name__}: {exc}\n{_tb.format_exc()}",
                  flush=True, file=sys.stderr)
            _show_inline_status(
                status, f"{label}: confirm dialog crashed: {exc}", "error",
            )
            return
        if not confirmed:
            print(f"[ADMIN-BTN] {label!r}: confirm cancelled",
                  flush=True, file=sys.stderr)
            return
        btn.props("loading"); btn.disable()
        try:
            ok, data, _ = await asyncio.to_thread(_call, backend, method, path, body)
            if ok:
                msg = done_msg(data) if callable(done_msg) else (done_msg or "Done.")
                print(f"[ADMIN-BTN] OK: {label!r} -> {msg!r}",
                      flush=True, file=sys.stderr)
                _show_inline_status(status, msg, "success")
                if refresh_status:
                    refresh_status()
            else:
                err = data.get('error') or 'unknown'
                print(f"[ADMIN-BTN] FAIL: {label!r} -> {err!r}",
                      flush=True, file=sys.stderr)
                _show_inline_status(status, f"{label} failed: {err}", "error")
        except Exception as exc:                                          # noqa: BLE001
            import traceback as _tb
            print(f"[ADMIN-BTN] CRASH: {label!r}  {type(exc).__name__}: {exc}\n"
                  f"{_tb.format_exc()}", flush=True, file=sys.stderr)
            _show_inline_status(
                status, f"{label} crashed: {type(exc).__name__}: {exc}", "error",
            )
        finally:
            btn.props(remove="loading"); btn.enable()

    btn.on("click", _click)


def _bankroll_button(
    backend, label: str, *, which: str,
    done_msg: str, refresh_status=None,
) -> None:
    """Open a numeric-input dialog, then POST the new value.  The
    underlying endpoints update BOTH MLB + WNBA ledgers in one call --
    `which` only selects personal vs model field, not a sport."""
    path = (
        "/api/ledger/set_model_bankroll" if which == "model"
        else "/api/ledger/set_bankroll"
    )

    with ui.column().classes("w-full").style(
        "gap: 2px; min-width: 0; flex: 1 1 auto;"
    ):
        btn = ui.button(label).props("no-caps unelevated").style(_btn_style("default"))
        status = _make_status_holder()

    async def _click():
        print(f"[ADMIN-BTN] _bankroll_button click: {label!r}  which={which}  path={path}",
              flush=True, file=sys.stderr)
        try:
            value = await _number_dialog(
                title=label.rstrip("."),
                placeholder="e.g. 1000",
            )
        except Exception as exc:                                          # noqa: BLE001
            import traceback as _tb
            print(f"[ADMIN-BTN] CRASH (number dialog): {label!r}  "
                  f"{type(exc).__name__}: {exc}\n{_tb.format_exc()}",
                  flush=True, file=sys.stderr)
            _show_inline_status(
                status, f"{label}: number dialog crashed: {exc}", "error",
            )
            return
        if value is None:
            print(f"[ADMIN-BTN] {label!r}: number dialog cancelled",
                  flush=True, file=sys.stderr)
            return
        if value <= 0:
            _show_inline_status(status, "Bankroll must be greater than 0.", "warning")
            return
        btn.props("loading"); btn.disable()
        try:
            ok, data, _ = await asyncio.to_thread(
                _call, backend, "POST", path, {"bankroll": value})
            if ok:
                print(f"[ADMIN-BTN] OK: {label!r} -> bankroll={value}",
                      flush=True, file=sys.stderr)
                _show_inline_status(status, done_msg, "success")
                if refresh_status:
                    refresh_status()
            else:
                err = data.get('error') or 'unknown'
                print(f"[ADMIN-BTN] FAIL: {label!r} -> {err!r}",
                      flush=True, file=sys.stderr)
                _show_inline_status(status, f"Failed: {err}", "error")
        except Exception as exc:                                          # noqa: BLE001
            import traceback as _tb
            print(f"[ADMIN-BTN] CRASH: {label!r}  {type(exc).__name__}: {exc}\n"
                  f"{_tb.format_exc()}", flush=True, file=sys.stderr)
            _show_inline_status(
                status, f"{label} crashed: {type(exc).__name__}: {exc}", "error",
            )
        finally:
            btn.props(remove="loading"); btn.enable()

    btn.on("click", _click)


def _toggle_row(backend, label: str, sub: str, field: str, initial: bool) -> None:
    """Per-sport auto-pick toggle backed by /api/admin/model/settings."""
    with ui.column().classes("w-full").style(
        f"padding: 6px 0; border-bottom: 1px solid {t.BORDER_SOFT}; gap: 2px;"
    ):
        with ui.row().classes("items-center w-full justify-between"):
            with ui.column().style("gap: 2px;"):
                ui.label(label).style(f"color: {t.TEXT}; font-size: 13px; font-weight: 600;")
                ui.label(sub).style(f"color: {t.TEXT_DIM}; font-size: 11px;")
            sw = ui.switch(value=initial).props("dense color=primary").classes("styled-switch")
        status = _make_status_holder()

        async def _on_change(e):
            print(f"[ADMIN-BTN] _toggle_row change: {label!r} -> {bool(e.value)}",
                  flush=True, file=sys.stderr)
            try:
                body = {field: bool(e.value)}
                ok, data, _ = await asyncio.to_thread(
                    _call, backend, "POST", "/api/admin/model/settings", body)
                if ok:
                    _show_inline_status(
                        status,
                        f"{label} {'enabled' if e.value else 'disabled'}.",
                        "success",
                    )
                else:
                    _show_inline_status(
                        status,
                        f"Toggle failed: {data.get('error') or 'unknown'}",
                        "error",
                    )
                    sw.value = not e.value
            except Exception as exc:                                      # noqa: BLE001
                _show_inline_status(status, f"Toggle failed: {exc}", "error")
                sw.value = not e.value

        sw.on_value_change(_on_change)


def _number_row(backend, label: str, sub: str, field: str,
                initial: int, *, min_value: int = 1, max_value: int = 500) -> None:
    """Persisted integer setting -- mirrors _toggle_row but for ints.
    Saves via the same /api/admin/model/settings endpoint; the backend's
    _save_model_settings preserves int type for any default that's int."""
    with ui.column().classes("w-full").style(
        f"padding: 6px 0; border-bottom: 1px solid {t.BORDER_SOFT}; gap: 2px;"
    ):
        with ui.row().classes("items-center w-full justify-between"):
            with ui.column().style("gap: 2px; min-width: 0; flex: 1;"):
                ui.label(label).style(f"color: {t.TEXT}; font-size: 13px; font-weight: 600;")
                ui.label(sub).style(
                    f"color: {t.TEXT_DIM}; font-size: 11px; "
                    f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
                )
            num = ui.number(
                value=int(initial), min=min_value, max=max_value, step=1, format="%.0f",
            ).style(
                f"width: 110px; flex-shrink: 0;"
            ).props("dense")
        status = _make_status_holder()

        async def _on_change(e):
            print(f"[ADMIN-BTN] _number_row change: {label!r}",
                  flush=True, file=sys.stderr)
            try:
                v = int(e.value) if e.value is not None else initial
            except (TypeError, ValueError):
                v = initial
            v = max(min_value, min(int(v), max_value))
            try:
                body = {field: v}
                ok, data, _ = await asyncio.to_thread(
                    _call, backend, "POST", "/api/admin/model/settings", body)
                if ok:
                    _show_inline_status(status, f"{label} set to {v}.", "success")
                else:
                    _show_inline_status(
                        status,
                        f"Save failed: {data.get('error') or 'unknown'}",
                        "error",
                    )
            except Exception as exc:                                      # noqa: BLE001
                _show_inline_status(status, f"Save failed: {exc}", "error")

        num.on_value_change(_on_change)


# ───────────────────────────────────────────────────────────────────────────
#  Dialog helpers (awaitable)
# ───────────────────────────────────────────────────────────────────────────

async def _confirm_dialog(prompt: str) -> bool:
    """Awaitable Yes / No dialog.  `await dlg` resolves to whatever the
    button passed to dlg.submit(...); closing without submitting returns
    None, which we coerce to False below."""
    with ui.dialog() as dlg, ui.card().style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_LG}; "
        f"min-width: 320px; max-width: 480px; gap: {t.SPACE_MD};"
    ):
        ui.label("Confirm").style(
            f"font-size: 14px; font-weight: 800; color: {t.TEXT}; "
            f"letter-spacing: .5px;"
        )
        ui.label(prompt).style(f"color: {t.TEXT_DIM}; font-size: 13px; line-height: 1.5;")
        with ui.row().classes("w-full justify-end").style("gap: 8px; margin-top: 8px;"):
            ui.button("Cancel", on_click=lambda: dlg.submit(False)) \
                .props("no-caps flat") \
                .style(f"color: {t.TEXT_DIM};")
            ui.button("Confirm", on_click=lambda: dlg.submit(True)) \
                .props("no-caps unelevated") \
                .style(f"background: {t.PRIMARY}; color: {t.BG}; font-weight: 700;")
    result = await dlg
    return bool(result)


async def _number_dialog(title: str, placeholder: str) -> float | None:
    """Awaitable numeric-input dialog.  Returns the float, or None on
    cancel / close.  Validation lives at the caller -- this just collects
    the value."""
    with ui.dialog() as dlg, ui.card().style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_LG}; "
        f"min-width: 320px; max-width: 480px; gap: {t.SPACE_MD};"
    ):
        ui.label(title).style(
            f"font-size: 14px; font-weight: 800; color: {t.TEXT}; "
            f"letter-spacing: .5px;"
        )
        amount = ui.number(label="Amount ($)", placeholder=placeholder,
                           min=0, step=1, format="%.2f").style("width: 100%;")
        # Inline validation message -- ui.notify is banned on /admin so
        # we render the warning under the input instead.
        dialog_status = ui.label("").style(
            f"font-size: 11.5px; font-weight: 600; color: {t.WARN}; "
            f"min-height: 16px; line-height: 1.3;"
        )

        def _save():
            v = amount.value
            if v is None:
                dialog_status.text = "Enter a number."
                return
            dialog_status.text = ""
            dlg.submit(float(v))

        with ui.row().classes("w-full justify-end").style("gap: 8px; margin-top: 8px;"):
            ui.button("Cancel", on_click=lambda: dlg.submit(None)) \
                .props("no-caps flat") \
                .style(f"color: {t.TEXT_DIM};")
            ui.button("Save", on_click=_save) \
                .props("no-caps unelevated") \
                .style(f"background: {t.PRIMARY}; color: {t.BG}; font-weight: 700;")
    result = await dlg
    return None if result is None else float(result)


# ═══════════════════════════════════════════════════════════════════════════
#  Supabase Data Explorer -- live view of app_cache + ledger, with edits
# ═══════════════════════════════════════════════════════════════════════════

def _age_str(iso) -> str:
    """Compact 'how long ago' from an ISO-8601 timestamp."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:                                                       # noqa: BLE001
        return str(iso)[:19]
    if secs < 0:
        return "future"
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs // 60)}m ago"
    if secs < 172800:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _fmt_bytes(n) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} MB"


def _xr_row():
    """One compact data row inside an explorer panel."""
    return ui.row().classes("items-center w-full").style(
        f"gap: 8px; padding: 6px 8px; background: {t.CARD_HI}; "
        f"border-radius: {t.RADIUS_SM}; flex-wrap: wrap;"
    )


def _xr_mini_btn(label: str, color: str, on_click):
    return ui.button(label, on_click=on_click).props("no-caps unelevated dense").classes("touch-44h").style(
        f"background: {color}; color: {t.BG}; font-size: 10px; font-weight: 800; "
        f"padding: 3px 9px; border-radius: {t.RADIUS_SM}; min-height: 0;"
    )


def _xr_mono(text: str, *, flex: bool = False, dim: bool = False) -> None:
    ui.label(text).style(
        f"font-size: 11px; font-family: monospace; "
        f"color: {t.TEXT_DIM if dim else t.TEXT}; "
        + ("flex: 1; min-width: 120px; white-space: nowrap; overflow: hidden; "
           "text-overflow: ellipsis;" if flex else "white-space: nowrap;")
    )


def _explorer_panel(backend, *, title: str, path: str, render_body, post_body=None):
    """Collapsible category that lazily fetches `path` on first expand and
    re-fetches on its own Refresh button.  Returns the async loader so the
    top-level 'Refresh All' can drive it too."""
    st = {"loaded": False, "loading": False, "data": None, "error": None}

    exp = ui.expansion(title).classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; color: {t.TEXT};"
    )
    with exp:
        status = _make_status_holder()

        @ui.refreshable
        def body() -> None:                                               # noqa: WPS430
            async def _rc() -> None:
                await _load(force=True)
            with ui.row().classes("items-center w-full").style("gap: 8px; margin-bottom: 6px;"):
                ui.button("↻ Refresh", on_click=_rc).props("no-caps flat dense").style(
                    f"color: {t.PRIMARY}; font-size: 11px; font-weight: 700; min-height: 0;"
                )
            if st["loading"]:
                with ui.row().classes("items-center").style("gap: 8px;"):
                    ui.spinner(size="sm")
                    ui.label("Loading…").style(f"color: {t.TEXT_DIM}; font-size: 12px;")
                return
            if st["error"]:
                ui.label(f"⚠ {st['error']}").style(
                    f"color: {t.NEG}; font-size: 12px; white-space: normal; word-break: break-word;"
                )
                return
            if not st["loaded"]:
                ui.label("Expand to load…").style(f"color: {t.TEXT_DIM2}; font-size: 12px;")
                return
            try:
                render_body(backend, st["data"], _load, status)
            except Exception as exc:                                       # noqa: BLE001
                ui.label(f"render error: {exc}").style(f"color: {t.NEG}; font-size: 12px;")

        body()

    async def _load(force: bool = False) -> None:
        if st["loading"]:
            return
        if st["loaded"] and not force:
            return
        st["loading"], st["error"] = True, None
        body.refresh()
        ok, data, _ = await asyncio.to_thread(_call, backend, "POST", path, post_body or {})
        st["loading"] = False
        if ok:
            st["data"], st["loaded"] = data, True
        else:
            st["error"] = data.get("error") or "fetch failed"
        body.refresh()

    async def _on_toggle(e) -> None:                                       # noqa: WPS430
        if getattr(e, "value", False) and not st["loaded"] and not st["loading"]:
            await _load()
    exp.on_value_change(_on_toggle)
    return _load


# ── Per-category render bodies ─────────────────────────────────────────────

def _xr_models(backend, data, reload, status) -> None:
    if not data.get("supabase"):
        ui.label("Supabase not configured — nothing stored remotely.").style(
            f"color: {t.TEXT_DIM}; font-size: 12px;")
        return
    models = data.get("models") or []
    if not models:
        ui.label("No model rows found in app_cache.").style(
            f"color: {t.TEXT_DIM}; font-size: 12px;")
        return
    for m in models:
        with _xr_row():
            _xr_mono(m.get("key") or "?", flex=True)
            _xr_mono(_fmt_bytes(m.get("size")), dim=True)
            _xr_mono((m.get("sha256") or "—")[:12], dim=True)
            _xr_mono(_age_str(m.get("updated_at")), dim=True)
            async def _del(key=m.get("key")) -> None:
                if not await _confirm_dialog(f"Delete model cache row '{key}'?"):
                    return
                ok, d, _ = await asyncio.to_thread(
                    _call, backend, "POST", "/api/admin/explorer/cache_delete", {"key": key})
                _show_inline_status(status, f"Deleted {key}" if ok
                                    else f"Failed: {d.get('error')}",
                                    "success" if ok else "error")
                if ok:
                    await reload(force=True)
            _xr_mini_btn("Delete", t.NEG, _del)


def _xr_props_cache(backend, data, reload, status) -> None:
    today = data.get("today") or {}
    ui.label(
        f"Today ({today.get('date') or '—'}): {today.get('markets', 0)} markets · "
        f"{today.get('total', 0)} props · written {_age_str(today.get('generated_at'))}"
    ).style(f"color: {t.TEXT}; font-size: 12px; font-weight: 600;")
    rows = data.get("rows") or []
    if not rows:
        ui.label("No props_* rows in app_cache.").style(
            f"color: {t.TEXT_DIM}; font-size: 12px;")
        return
    for r in rows:
        with _xr_row():
            _xr_mono(r.get("key") or "?", flex=True)
            _xr_mono(str(r.get("date") or "—"), dim=True)
            _xr_mono(_fmt_bytes(r.get("size")), dim=True)
            _xr_mono(_age_str(r.get("updated_at")), dim=True)
            async def _del(key=r.get("key")) -> None:
                if not await _confirm_dialog(f"Delete props cache key '{key}'?"):
                    return
                ok, d, _ = await asyncio.to_thread(
                    _call, backend, "POST", "/api/admin/explorer/cache_delete", {"key": key})
                _show_inline_status(status, f"Deleted {key}" if ok
                                    else f"Failed: {d.get('error')}",
                                    "success" if ok else "error")
                if ok:
                    await reload(force=True)
            _xr_mini_btn("Delete", t.NEG, _del)


def _xr_bet_row(backend, b, *, kind, reload, status) -> None:
    with _xr_row():
        if kind == "prop":
            label = f"{b.get('player')} {b.get('side')} {b.get('line')} ({b.get('market')})"
        else:
            label = f"{b.get('sport', '').upper()} {b.get('team')} {b.get('bet_type')}"
        _xr_mono(label, flex=True)
        res = (b.get("result") or "pending")
        rc = {"win": t.POS, "won": t.POS, "loss": t.NEG, "lost": t.NEG}.get(res, t.TEXT_DIM)
        ui.label(res.upper()).style(f"font-size: 10px; font-weight: 800; color: {rc};")
        sport = b.get("sport") or "mlb"
        bet_id = b.get("id")

        async def _mark(result: str) -> None:
            if not await _confirm_dialog(f"Mark this {kind} bet as {result.upper()}?"):
                return
            ok, d, _ = await asyncio.to_thread(
                _call, backend, "POST", "/api/admin/explorer/mark_bet",
                {"kind": kind, "id": bet_id, "sport": sport, "result": result})
            _show_inline_status(status, "Updated" if ok else f"Failed: {d.get('error')}",
                                "success" if ok else "error")
            if ok:
                await reload(force=True)

        async def _del() -> None:
            if not await _confirm_dialog(f"Delete this {kind} bet record permanently?"):
                return
            ok, d, _ = await asyncio.to_thread(
                _call, backend, "POST", "/api/mybets/remove",
                {"kind": kind, "id": bet_id, "sport": sport})
            _show_inline_status(status, "Deleted" if ok else f"Failed: {d.get('error')}",
                                "success" if ok else "error")
            if ok:
                await reload(force=True)

        _xr_mini_btn("Won", t.POS, lambda: _mark("won"))
        _xr_mini_btn("Lost", t.NEG, lambda: _mark("lost"))
        _xr_mini_btn("Pending", t.TEXT_DIM, lambda: _mark("pending"))
        _xr_mini_btn("Delete", t.WARN, _del)


def _xr_picks(backend, data, reload, status) -> None:
    ledgers = data.get("ledgers") or {}
    mlb = ledgers.get("mlb") or {}

    # ── Bankroll editors (set_* routes write both sports) ────────────────
    with ui.row().classes("items-center w-full").style("gap: 8px; flex-wrap: wrap;"):
        model_in = ui.number("Model bankroll", value=mlb.get("model_bankroll"), format="%.2f") \
            .props("outlined dense dark").style(f"background: {t.CARD_HI}; flex: 1; min-width: 140px;")
        pers_in = ui.number("Personal bankroll", value=mlb.get("personal_bankroll"), format="%.2f") \
            .props("outlined dense dark").style(f"background: {t.CARD_HI}; flex: 1; min-width: 140px;")

        async def _save_model() -> None:
            v = model_in.value
            if v is None or float(v) <= 0:
                return
            if not await _confirm_dialog(f"Set MODEL bankroll to ${float(v):,.2f}?"):
                return
            ok, d, _ = await asyncio.to_thread(
                _call, backend, "POST", "/api/ledger/set_model_bankroll", {"bankroll": float(v)})
            _show_inline_status(status, "Model bankroll set" if ok else f"Failed: {d.get('error')}",
                                "success" if ok else "error")
            if ok:
                await reload(force=True)

        async def _save_pers() -> None:
            v = pers_in.value
            if v is None or float(v) <= 0:
                return
            if not await _confirm_dialog(f"Set PERSONAL bankroll to ${float(v):,.2f}?"):
                return
            ok, d, _ = await asyncio.to_thread(
                _call, backend, "POST", "/api/ledger/set_bankroll", {"bankroll": float(v)})
            _show_inline_status(status, "Personal bankroll set" if ok else f"Failed: {d.get('error')}",
                                "success" if ok else "error")
            if ok:
                await reload(force=True)

        _xr_mini_btn("Save model", t.PRIMARY, _save_model)
        _xr_mini_btn("Save personal", t.PRIMARY, _save_pers)

    for sport in ("mlb", "wnba"):
        s = ledgers.get(sport) or {}
        if not s:
            continue
        ui.label(
            f"{sport.upper()}: model ${s.get('model_bankroll')} · personal ${s.get('personal_bankroll')} · "
            f"open {s.get('open_bets', 0)} · settled {s.get('settled_bets', 0)} · "
            f"model P/L {s.get('model_pnl')} · personal P/L {s.get('confirmed_pnl')}"
        ).style(f"color: {t.TEXT_DIM}; font-size: 11.5px; font-family: monospace;")

    rec = (data.get("props") or {}).get("record") or {}
    ui.label(
        f"PROPS: {rec.get('total', 0)} settled · {rec.get('open', 0)} pending · "
        f"{rec.get('wins', 0)}W-{rec.get('losses', 0)}L-{rec.get('voids', 0)}V"
    ).style(f"color: {t.TEXT_DIM}; font-size: 11.5px; font-family: monospace;")

    open_bets = data.get("open_bets") or []
    settled = data.get("settled_bets") or []
    if open_bets:
        ui.label("OPEN GAME BETS").style(
            f"font-size: 10px; font-weight: 800; color: {t.TEXT_DIM2}; margin-top: 4px;")
        for b in open_bets:
            _xr_bet_row(backend, b, kind="game", reload=reload, status=status)
    if settled:
        ui.label("SETTLED GAME BETS").style(
            f"font-size: 10px; font-weight: 800; color: {t.TEXT_DIM2}; margin-top: 4px;")
        for b in settled:
            _xr_bet_row(backend, b, kind="game", reload=reload, status=status)
    props_picks = (data.get("props") or {}).get("picks") or []
    if props_picks:
        ui.label("PROP PICKS").style(
            f"font-size: 10px; font-weight: 800; color: {t.TEXT_DIM2}; margin-top: 4px;")
        for p in props_picks:
            _xr_bet_row(backend, p, kind="prop", reload=reload, status=status)


def _xr_timestamps(backend, data, reload, status) -> None:
    fields = (
        ("MLB last analyzed",  "mlb",           data.get("mlb")),
        ("WNBA last analyzed", "wnba",          data.get("wnba")),
        ("Last props refresh", "props_refresh", data.get("props_refresh")),
        ("Last settlement",    "settlement",    data.get("settlement")),
    )
    for label, field, value in fields:
        with _xr_row():
            ui.label(label).style(f"font-size: 11.5px; color: {t.TEXT}; min-width: 150px;")
            _xr_mono(str(value or "—"), flex=True, dim=True)
            inp = ui.input(value=str(value or ""), placeholder="ISO-8601") \
                .props("outlined dense dark").style(
                    f"background: {t.CARD_HI}; min-width: 180px;")

            async def _save(field=field, inp=inp) -> None:
                v = (inp.value or "").strip()
                if not v:
                    return
                if not await _confirm_dialog(f"Override {field} to '{v}'?"):
                    return
                ok, d, _ = await asyncio.to_thread(
                    _call, backend, "POST", "/api/admin/explorer/set_timestamp",
                    {"field": field, "value": v})
                _show_inline_status(status, "Overridden" if ok else f"Failed: {d.get('error')}",
                                    "success" if ok else "error")
                if ok:
                    await reload(force=True)
            _xr_mini_btn("Override", t.PRIMARY, _save)


def _xr_key_detail(backend, key: str, status) -> None:
    """Per-key expandable: lazily fetch + show the raw JSON value."""
    kst = {"loaded": False, "loading": False, "text": "", "error": None}
    inner = ui.expansion("▸ view raw").classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER_SOFT}; "
        f"border-radius: {t.RADIUS_SM}; color: {t.TEXT_DIM};")
    with inner:
        @ui.refreshable
        def vb() -> None:                                                  # noqa: WPS430
            if kst["loading"]:
                ui.spinner(size="sm")
                return
            if kst["error"]:
                ui.label(f"⚠ {kst['error']}").style(f"color: {t.NEG}; font-size: 11px;")
                return
            ui.code(kst["text"] or "—").style(
                "width: 100%; max-height: 320px; overflow: auto; font-size: 10.5px;")
        vb()

    async def _on_toggle(e) -> None:                                       # noqa: WPS430
        if getattr(e, "value", False) and not kst["loaded"] and not kst["loading"]:
            kst["loading"] = True
            vb.refresh()
            ok, d, _ = await asyncio.to_thread(
                _call, backend, "POST", "/api/admin/explorer/cache_value", {"key": key})
            kst["loading"] = False
            if ok:
                try:
                    kst["text"] = json.dumps(d.get("value"), indent=2, default=str)
                except Exception:                                          # noqa: BLE001
                    kst["text"] = str(d.get("value"))
                kst["loaded"] = True
            else:
                kst["error"] = d.get("error") or "fetch failed"
            vb.refresh()
    inner.on_value_change(_on_toggle)


def _xr_cache_keys(backend, data, reload, status) -> None:
    if not data.get("supabase"):
        ui.label("Supabase not configured.").style(f"color: {t.TEXT_DIM}; font-size: 12px;")
        return
    keys = data.get("keys") or []
    ui.label(f"{len(keys)} keys in app_cache").style(
        f"color: {t.TEXT}; font-size: 12px; font-weight: 600;")
    for k in keys:
        with ui.column().classes("w-full").style(
            f"gap: 4px; padding: 6px 8px; background: {t.CARD_HI}; "
            f"border-radius: {t.RADIUS_SM};"
        ):
            with ui.row().classes("items-center w-full").style("gap: 8px; flex-wrap: wrap;"):
                _xr_mono(k.get("key") or "?", flex=True)
                _xr_mono(_fmt_bytes(k.get("size")), dim=True)
                _xr_mono(_age_str(k.get("updated_at")), dim=True)
                async def _del(key=k.get("key")) -> None:
                    if not await _confirm_dialog(f"Delete app_cache key '{key}'?"):
                        return
                    ok, d, _ = await asyncio.to_thread(
                        _call, backend, "POST", "/api/admin/explorer/cache_delete", {"key": key})
                    _show_inline_status(status, f"Deleted {key}" if ok
                                        else f"Failed: {d.get('error')}",
                                        "success" if ok else "error")
                    if ok:
                        await reload(force=True)
                _xr_mini_btn("Delete", t.NEG, _del)
            _xr_key_detail(backend, k.get("key") or "", status)


def _xr_raw_editor(backend) -> None:
    """Power-user panel: load any app_cache key's JSON, edit, save back."""
    exp = ui.expansion("Raw Editor").classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; color: {t.TEXT};")
    with exp:
        status = _make_status_holder()
        key_in = ui.input(placeholder="app_cache key (e.g. analysis_timestamps)") \
            .props("outlined dense dark").style(f"background: {t.CARD_HI}; width: 100%;")
        area = ui.textarea(placeholder="JSON value — load a key, edit, then Save") \
            .props("outlined dense dark").style(
                f"background: {t.CARD_HI}; width: 100%; font-family: monospace;")

        async def _load() -> None:
            key = (key_in.value or "").strip()
            if not key:
                _show_inline_status(status, "Enter a key first.", "warning")
                return
            ok, d, _ = await asyncio.to_thread(
                _call, backend, "POST", "/api/admin/explorer/cache_value", {"key": key})
            if ok:
                try:
                    area.value = json.dumps(d.get("value"), indent=2, default=str)
                except Exception:                                          # noqa: BLE001
                    area.value = str(d.get("value"))
                _show_inline_status(status, f"Loaded {key}", "success")
            else:
                _show_inline_status(status, f"Failed: {d.get('error')}", "error")

        async def _save() -> None:
            key = (key_in.value or "").strip()
            if not key:
                _show_inline_status(status, "Enter a key first.", "warning")
                return
            # Validate JSON locally before the confirm prompt.
            try:
                json.loads(area.value or "")
            except Exception as exc:                                       # noqa: BLE001
                _show_inline_status(status, f"Invalid JSON: {exc}", "error")
                return
            if not await _confirm_dialog(
                    f"Overwrite Supabase key '{key}' with the edited JSON? "
                    "This cannot be undone."):
                return
            ok, d, _ = await asyncio.to_thread(
                _call, backend, "POST", "/api/admin/explorer/cache_save",
                {"key": key, "value": area.value})
            _show_inline_status(status, f"Saved {key}" if ok else f"Failed: {d.get('error')}",
                                "success" if ok else "error")

        with ui.row().classes("w-full").style("gap: 8px;"):
            _xr_mini_btn("Load", t.PRIMARY, _load)
            _xr_mini_btn("Save", t.WARN, _save)


def _section_data_explorer(backend) -> None:
    loaders: list = []
    with _card("SUPABASE DATA EXPLORER",
               "Live view of what's stored in Supabase. Expand a category to load it."):
        async def _refresh_all() -> None:
            for ld in loaders:
                try:
                    await ld(force=True)
                except Exception:                                          # noqa: BLE001
                    pass
        ui.button("↻ Refresh All", on_click=_refresh_all).props("no-caps unelevated dense").style(
            f"background: {t.PRIMARY}; color: {t.BG}; font-size: 11px; font-weight: 800; "
            f"padding: 5px 14px; border-radius: {t.RADIUS_SM}; min-height: 0; align-self: flex-start;")

        loaders.append(_explorer_panel(
            backend, title="Models", path="/api/admin/explorer/models", render_body=_xr_models))
        loaders.append(_explorer_panel(
            backend, title="Props Cache", path="/api/admin/explorer/props_cache", render_body=_xr_props_cache))
        loaders.append(_explorer_panel(
            backend, title="Picks & Ledger", path="/api/admin/explorer/picks", render_body=_xr_picks))
        loaders.append(_explorer_panel(
            backend, title="Timestamps", path="/api/admin/explorer/timestamps", render_body=_xr_timestamps))
        loaders.append(_explorer_panel(
            backend, title="App Cache Keys", path="/api/admin/explorer/cache_keys", render_body=_xr_cache_keys))
        _xr_raw_editor(backend)
