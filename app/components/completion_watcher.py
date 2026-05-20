"""
Analysis-completion watcher -- forces a full page reload when a
background analyze writes a fresh `completed_at` to the shared progress
dict on the backend.

Why the rewrite (was: 5 s polling + app.storage.tab dedup + page-specific
refreshable callbacks):

  The previous design tried to be clever -- refresh just the
  `@ui.refreshable` game grid in place, dedupe notifications via
  app.storage.tab.  In practice users reported that the UI never
  updated after analyze completed; the most plausible failure mode is
  that page-local refreshable wrappers re-render from stale Python
  state (closures captured during the original page render don't see
  re-bound module-level attributes), so the "fresh" render still drew
  yesterday's picks.

  The simplest design that can't fail is: when the background worker
  finishes, force a full browser reload.  That kicks the page through
  every server-side data-load path from scratch -- no closures, no
  refreshable bookkeeping, no Python-state-vs-served-render skew.

How it works:

  1.  Page mount captures `mount_time = time.time()` into a closure dict.
  2.  ui.timer(2.0) ticks every 2 s.  Each tick GETs
      /api/analyze/completions and looks at each sport's `completed_at`.
  3.  If any sport's completed_at > mount_time, the timer fires
      ui.navigate.reload() exactly once, then sets `stopped = True`
      so subsequent ticks no-op (and deactivate()s the timer).
  4.  After the reload the new page render captures a fresh mount_time,
      so it won't re-fire on the same completion.

No app.storage.tab anywhere -- the dedup is implicit in the reload
itself (the new render bumps mount_time past the completed_at value
so the same event can't fire twice).

Mounted on pages that show game data (home, sport, game_detail).  The
admin page is intentionally excluded -- its per-button polling timer
already handles in-session progress + completion feedback, and a
forced reload mid-click would yank the user off their position.
"""
from __future__ import annotations

import time as _time

from nicegui import ui


_POLL_INTERVAL_SEC = 2.0
_PRIMER_DELAY_SEC  = 0.4   # first check shortly after mount


def mount(backend) -> None:
    """Start the watcher on the current page.  No callback parameter --
    completion always triggers a full ui.navigate.reload()."""

    mount_time = _time.time()
    state = {"stopped": False, "timer": None}

    def _trigger_reload() -> None:
        """Fire a full browser reload.  ui.navigate.reload() is the
        canonical NiceGUI 2.x API; falls back to direct JS for older
        builds that may not have plumbed it through to the page handler
        scope (we've seen one NiceGUI kwarg silently missing on this
        deployment before)."""
        try:
            ui.navigate.reload()
        except Exception:                                                  # noqa: BLE001
            try:
                ui.run_javascript("window.location.reload();")
            except Exception:                                              # noqa: BLE001
                pass  # nothing else we can do -- next tick will retry

    async def _tick() -> None:
        if state["stopped"]:
            return
        try:
            client = backend.app.test_client()
            resp   = client.get("/api/analyze/completions")
            data   = resp.get_json(force=True, silent=True) or {}
        except Exception:                                                  # noqa: BLE001
            return  # transient -- next tick retries

        newer = False
        for sport in ("mlb", "wnba"):
            row = data.get(sport) or {}
            completed_at = row.get("completed_at")
            if not completed_at:
                continue
            try:
                completed_at = float(completed_at)
            except (TypeError, ValueError):
                continue
            if completed_at > mount_time:
                newer = True
                break

        if not newer:
            return

        # Mark stopped FIRST so the deactivate path + reload don't race
        # an additional tick into firing a second reload.
        state["stopped"] = True
        timer = state.get("timer")
        if timer is not None:
            try:
                timer.deactivate()
            except Exception:                                              # noqa: BLE001
                pass
        _trigger_reload()

    state["timer"] = ui.timer(_POLL_INTERVAL_SEC, _tick, active=True)
    # Primer: fires shortly after mount so a page opened *just after*
    # an analyze completes (e.g. user clicked Run on /admin then tabbed
    # back to /sports right away) doesn't wait the full 2 s before
    # picking up the fresh data.
    ui.timer(_PRIMER_DELAY_SEC, _tick, once=True)
