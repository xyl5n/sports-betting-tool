"""
players.py
==========
MLB roster browser -- /players.

Lists every distinct player_name found in the Supabase ``model_picks`` table
as a searchable card grid.  Each card links to that player's profile page at
``/player/mlb/{slug}`` so the no-props player view (season game-log history)
is reachable for anyone the models have ever picked.

Read-only: the only query is a distinct player_name pull via the existing
PostgREST helper ``db.model_picks_list()``; nothing is written.
"""
from __future__ import annotations

import re
import sys

from nicegui import ui

from components import theme as t
from components import navbar, bottom_nav


def _dbg(msg: str) -> None:
    print(f"[PLAYERS] {msg}", flush=True, file=sys.stderr)


def player_name_to_slug(name: str) -> str:
    """"Aaron Judge" -> "aaron-judge", "A.J. Ewing" -> "aj-ewing",
    "Bobby Witt Jr." -> "bobby-witt-jr"."""
    return re.sub(
        r'-+', '-',
        re.sub(r'[^a-z0-9-]', '',
               name.strip().lower().replace(' ', '-').replace('.', '')),
    )


# ── Data ────────────────────────────────────────────────────────────────────

def _load_player_names() -> list[str]:
    """Distinct, alphabetically-sorted player_name values from model_picks
    (PostgREST).  Empty list on any error / Supabase off."""
    try:
        from src import db
        rows = db.model_picks_list() or []
    except Exception as exc:                                               # noqa: BLE001
        _dbg(f"model_picks_list failed: {exc}")
        return []
    names = {
        (r.get("player_name") or "").strip()
        for r in rows
        if (r.get("player_name") or "").strip()
    }
    return sorted(names, key=str.lower)


# ── Page ──────────────────────────────────────────────────────────────────--

def register(backend) -> None:
    @ui.page("/players")
    def players_page():
        _dbg("players_page ENTER")
        try:
            ui.add_head_html(t.page_head_css())
            navbar.render(active=t.TAB_PROPS)
            _layout()
            bottom_nav.render(active=t.TAB_PROPS)
        except Exception as exc:                                          # noqa: BLE001
            import traceback as _tb
            print(f"[PLAYERS PAGE FATAL] {type(exc).__name__}: {exc}\n"
                  f"{_tb.format_exc()}", flush=True, file=sys.stderr)
            ui.label("Players page failed to render").style(
                f"color: {t.NEG}; font-size: 16px; font-weight: 700; "
                f"padding: {t.SPACE_LG};"
            )


def _layout() -> None:
    names = _load_player_names()
    state = {"q": ""}

    with ui.column().classes("page-content w-full").style(
        f"max-width: {t.MAX_CONTENT_W}; margin: 0 auto; "
        f"gap: {t.SPACE_MD}; padding: {t.SPACE_LG}; min-width: 0;"
    ):
        ui.label("PLAYERS").classes("page-title").style(
            f"font-size: 22px; font-weight: 800; color: {t.TEXT};"
        )
        ui.label(
            f"Browse every player the models track ({len(names)}). "
            f"Search by name, then open a profile for season game-log history."
        ).style(f"font-size: 12.5px; color: {t.TEXT_DIM};")

        # ── Search box (filters the grid as the user types) ──────────────
        search = ui.input(placeholder="Search players…").props(
            "outlined dense clearable"
        ).classes("w-full").style(
            f"max-width: 360px; background: {t.CARD}; "
            f"border-radius: {t.RADIUS_MD};"
        )

        @ui.refreshable
        def _grid() -> None:                                              # noqa: WPS430
            q = (state["q"] or "").strip().lower()
            shown = [n for n in names if q in n.lower()] if q else names
            if not names:
                ui.label(
                    "No players found yet — the model_picks table is empty "
                    "or Supabase is unavailable."
                ).style(
                    f"font-size: 12.5px; color: {t.TEXT_DIM}; font-style: italic; "
                    f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                    f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; "
                    f"text-align: center; width: 100%;"
                )
                return
            if not shown:
                ui.label(f"No players match “{state['q']}”.").style(
                    f"font-size: 12.5px; color: {t.TEXT_DIM}; font-style: italic; "
                    f"padding: {t.SPACE_SM} 2px;"
                )
                return
            with ui.element("div").classes("game-grid w-full"):
                for name in shown:
                    _player_card(name)

        def _on_search(e) -> None:                                        # noqa: WPS430
            state["q"] = e.value or ""
            _grid.refresh()
        search.on_value_change(_on_search)

        _grid()


def _player_card(name: str) -> None:
    slug = player_name_to_slug(name)
    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; "
        f"gap: 10px; min-width: 0;"
    ):
        ui.label(name).style(
            f"font-size: 14.5px; font-weight: 800; color: {t.TEXT}; "
            f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        )
        ui.link("View Props", f"/player/mlb/{slug}").style(
            f"align-self: flex-start; text-decoration: none; "
            f"background: {t.PRIMARY}; color: {t.TEXT}; "
            f"font-size: 12px; font-weight: 800; letter-spacing: .3px; "
            f"padding: 6px 14px; border-radius: {t.RADIUS_SM};"
        )
