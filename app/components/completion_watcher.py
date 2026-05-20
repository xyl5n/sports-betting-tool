"""
Cross-page analysis-completion watcher.

Mounts a 5 s polling timer on every page that compares each sport's
`completed_at` (from /api/analyze/completions) against a per-tab
"last seen" value persisted in `app.storage.tab`.  When a new completion
is observed:

  - fires a single ui.notify ("Analysis complete -- N MLB games and
    M WNBA games loaded")
  - records the new completed_at in tab storage so a page reload won't
    re-fire
  - invokes the page's `on_complete` callback (default: a soft refresh
    of the current page) so game data updates without a manual click.

Why a per-page watcher (instead of relying on a single long-running
button-bound timer):

  1.  Browser WebSocket drops kill the timer; reconnects don't restore
      it.  A page-level timer is re-created on every page mount, so a
      disconnect + reconnect always picks back up.
  2.  Users on the Home / Sports tab while analysis runs in the
      background should still see the completion banner -- not just
      users sitting on the Admin tab.
  3.  Per-tab "seen" markers in app.storage.tab dedupe notifications
      across page reloads in the same tab without preventing other tabs
      from firing their own.

Pairs with the existing admin per-button polling: that timer drives the
inline "step 4: Predicting Spread (37s)" label.  Completion *notification*
is owned by this watcher.  Admin's _start_status_poll now writes its
own completed_at to app.storage.tab before the watcher polls, so the
admin page doesn't double-notify when the user clicked Run there.
"""
from __future__ import annotations

import time as _time
from typing import Callable, Optional

from nicegui import ui, app


_POLL_INTERVAL_SEC   = 5.0
_PRIMER_DELAY_SEC    = 0.3
_RECENT_WINDOW_SEC   = 600        # only fire for completions <10 min old
_TAB_KEY_TEMPLATE    = "_analysis_completion_seen_{sport}"


def mount(
    backend,
    on_complete: Optional[Callable[[list[tuple[str, int, str | None]]], None]] = None,
) -> None:
    """Start the watcher on the current page.

    `on_complete` receives the list of new completions, where each entry
    is (sport, n_games, error_or_None).  If omitted, no page refresh is
    triggered -- only the ui.notify fires.  Pages that show game data
    should pass a refresher (e.g. `lambda _: _refreshable_grid.refresh()`).
    """

    async def _tick() -> None:
        try:
            client = backend.app.test_client()
            resp   = client.get("/api/analyze/completions")
            data   = resp.get_json(force=True, silent=True) or {}
        except Exception:                                                  # noqa: BLE001
            return  # transient -- next tick retries

        # tab storage is only available inside a page handler; defensive
        # check for the rare case we're called from elsewhere.
        try:
            tab_store = app.storage.tab
        except Exception:                                                  # noqa: BLE001
            tab_store = None

        now = _time.time()
        new_completions: list[tuple[str, int, str | None]] = []
        for sport in ("mlb", "wnba"):
            row = data.get(sport) or {}
            completed_at = row.get("completed_at")
            if not completed_at:
                continue
            try:
                completed_at = float(completed_at)
            except (TypeError, ValueError):
                continue
            if completed_at < (now - _RECENT_WINDOW_SEC):
                continue   # stale completion -- don't surface

            seen_key  = _TAB_KEY_TEMPLATE.format(sport=sport)
            if tab_store is not None:
                last_seen = float(tab_store.get(seen_key) or 0)
            else:
                last_seen = 0.0
            if completed_at <= last_seen + 0.5:
                continue   # already surfaced this completion in this tab

            if tab_store is not None:
                tab_store[seen_key] = completed_at
            new_completions.append((
                sport,
                int(row.get("n_games") or 0),
                row.get("error"),
            ))

        if not new_completions:
            return

        had_err = any(err for _, _, err in new_completions)
        parts: list[str] = []
        for sport, n, err in new_completions:
            if err:
                parts.append(f"{sport.upper()} failed: {err}")
            else:
                parts.append(f"{n} {sport.upper()} games")

        ui.notify(
            f"Analysis complete -- {' and '.join(parts)} loaded.",
            type="warning" if had_err else "positive",
            multi_line=True, timeout=8000, close_button=True,
        )

        if on_complete is not None:
            try:
                on_complete(new_completions)
            except Exception:                                              # noqa: BLE001
                pass   # never let a page's refresh crash the watcher

    ui.timer(_POLL_INTERVAL_SEC, _tick, active=True)
    # Primer: fires once on mount so a disconnected-then-reconnected
    # client sees a fresh completion immediately, not after 5 s.
    ui.timer(_PRIMER_DELAY_SEC, _tick, once=True)


def mark_seen(sport: str, completed_at: float) -> None:
    """Record a completed_at as already-surfaced in this tab.  Called
    from the admin per-button polling timer when IT surfaces a completion,
    so the watcher's next tick won't double-notify."""
    try:
        tab_store = app.storage.tab
    except Exception:                                                      # noqa: BLE001
        return
    if not completed_at:
        return
    seen_key = _TAB_KEY_TEMPLATE.format(sport=sport)
    current  = float(tab_store.get(seen_key) or 0)
    if completed_at > current:
        tab_store[seen_key] = float(completed_at)
