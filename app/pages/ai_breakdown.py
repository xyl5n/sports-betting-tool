"""
AI Breakdown chat -- wired to the in-process Flask `/api/ai/chat` endpoint.

Conversation flow
-----------------
- Per-page session state: history list + first-message flag + message count.
- On page open we GET /api/ai/usage (cheap, NO Anthropic call) to seed the
  daily counter chip.  Per spec we NEVER auto-fire an Anthropic call when
  the user lands on this page.
- On Send (button click or Enter in the input):
    * POST /api/ai/chat with {message, history, include_context}.
    * include_context=True only on the FIRST message of a session, then
      False forever -- saves the input tokens for today's analysis
      payload on every follow-up turn.
    * History is whatever's stored client-side after the previous turn.
- 10-message-per-session cap: when the 10th user message is sent, the
  input row hides and a "Start New Chat" button replaces it.  Clicking it
  resets history + the include_context flag + the session message count.
- Daily limit: backend returns 429 with limit_reached=True when the
  per-day count hits the configured cap (default 20).  The UI then
  disables Send and shows "Daily AI limit reached, resets at midnight."

Why we cache context at the SESSION level (not per-call)
--------------------------------------------------------
The backend builds today's analysis snapshot from _analysis_state /
_wnba_analysis_state.  Sending that on every message would duplicate
the same ~2-5kB payload on every turn for the same conversation.  By
flipping include_context to False after the first call we keep the
conversation coherent (the model already saw context in turn 1) and
slash the token cost of follow-ups.
"""
from __future__ import annotations

import asyncio

from nicegui import ui

from components import theme as t
from components import navbar, sidebar, bottom_nav


_MESSAGE_LIMIT_PER_SESSION = 10
_STARTER_QUESTIONS = (
    "What are the best value picks today?",
    "Tell me about the starting pitcher matchups.",
    "Which team has the best form right now?",
    "What is the model most confident about today?",
)


def register(backend) -> None:
    @ui.page("/ai")
    def ai_page():
        ui.add_head_html(t.page_head_css())
        navbar.render(active=t.TAB_AI)
        with ui.row().classes("no-wrap w-full").style("gap: 0;"):
            sidebar.render(backend)
            with ui.column().classes("page-content").style(
                f"flex: 1; max-width: {t.MAX_CONTENT_W}; "
                f"gap: {t.SPACE_MD}; padding: {t.SPACE_LG}; min-width: 0;"
            ):
                _render_chat(backend)
        bottom_nav.render(active=t.TAB_AI)


def _render_chat(backend) -> None:
    """Build the chat surface for one page session.

    All session state lives in the `session` dict so the inner closures
    (refresh handlers, send button on_click) read/write the same object
    across the lifetime of this page connection.
    """
    session: dict = {
        # OpenAI-style {role, content} list -- both user + assistant turns.
        "history": [],
        # True until the first user message has been sent.  Drives
        # include_context: True on call #1 (heavy), False forever after.
        "first_send_pending": True,
        # Number of USER messages sent this session.  Resets on
        # "Start New Chat".
        "user_msg_count": 0,
        # Daily counter from the backend.  Seeded by /api/ai/usage on open
        # and updated by every successful /api/ai/chat response.
        "calls_today":   0,
        "daily_limit":   20,
        "limit_reached": False,
        # In-flight flag so a rapid double-click on Send doesn't double-fire.
        "busy": False,
    }

    # ── Header (title + counter + new-chat button) ──────────────────────
    with ui.row().classes("items-center w-full").style("gap: 8px;"):
        ui.label("AI BREAKDOWN").classes("page-title").style(
            f"font-size: 22px; font-weight: 800; color: {t.TEXT};"
        )
        # Spacer pushes counter to the right edge.
        ui.element("div").style("flex: 1;")
        counter_chip = ui.label("0 calls today").style(
            f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
            f"font-size: 11px; font-weight: 700; letter-spacing: .4px; "
            f"padding: 4px 10px; border-radius: {t.RADIUS_PILL}; "
            f"font-family: monospace;"
        )

    # ── Scrollable message area ─────────────────────────────────────────
    msg_area = ui.scroll_area().style(
        f"width: 100%; height: 460px; "
        f"background: {t.CARD}; "
        f"border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD};"
    )
    with msg_area:
        message_column = ui.column().classes("w-full").style(
            f"gap: 10px;"
        )

    # ── Input row + helper buttons (suggestions / new-chat) ─────────────
    suggestions_box = ui.column().classes("w-full").style(f"gap: 6px;")
    input_box = ui.column().classes("w-full").style(f"gap: 6px;")
    newchat_box = ui.column().classes("w-full").style(f"gap: 6px;")

    # ── Refresh helpers (closures over `session`) ───────────────────────

    def _set_counter_text() -> None:
        chip = f"{session['calls_today']} calls today"
        if session["daily_limit"]:
            chip += f" / {session['daily_limit']}"
        if session["limit_reached"]:
            chip += "  •  LIMIT REACHED"
        counter_chip.text = chip
        counter_chip.style(
            f"background: {t.CARD_HI}; "
            f"color: {t.NEG if session['limit_reached'] else t.TEXT_DIM}; "
            f"font-size: 11px; font-weight: 700; letter-spacing: .4px; "
            f"padding: 4px 10px; border-radius: {t.RADIUS_PILL}; "
            f"font-family: monospace;"
        )

    def _render_messages() -> None:
        """Re-render the message bubbles from session['history']."""
        message_column.clear()
        with message_column:
            if not session["history"]:
                ui.label(
                    "Ask the analyst anything about today's picks, "
                    "pitcher matchups, team form, or value bets."
                ).style(
                    f"color: {t.TEXT_DIM}; font-size: 12.5px; "
                    f"text-align: center; padding: {t.SPACE_LG} 0;"
                )
                return
            for m in session["history"]:
                _bubble(m["role"], m["content"])
            if session.get("busy"):
                _bubble("assistant", "…", placeholder=True)

    def _render_suggestions() -> None:
        """Suggested starter questions -- only visible when the chat is
        empty AND the daily limit hasn't been hit."""
        suggestions_box.clear()
        if session["history"] or session["limit_reached"]:
            return
        with suggestions_box:
            ui.label("SUGGESTIONS").style(
                f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT_DIM2};"
            )
            with ui.row().classes("w-full").style("gap: 6px; flex-wrap: wrap;"):
                for q in _STARTER_QUESTIONS:
                    def _click(_e=None, q=q):
                        if not session["busy"] and not session["limit_reached"]:
                            asyncio.create_task(_send(q))
                    ui.button(q, on_click=_click).props("no-caps unelevated dense") \
                        .style(
                            f"background: {t.CARD_HI}; color: {t.TEXT}; "
                            f"border: 1px solid {t.BORDER}; "
                            f"font-size: 11.5px; font-weight: 500; "
                            f"padding: 6px 12px; border-radius: {t.RADIUS_PILL}; "
                            f"min-height: 0;"
                        )

    def _render_input() -> None:
        """Render either the input+Send row OR the 'Start New Chat' button
        OR the disabled 'limit reached' notice, depending on session state."""
        input_box.clear()
        newchat_box.clear()
        if session["limit_reached"]:
            with newchat_box:
                ui.label(
                    "Daily AI limit reached, resets at midnight."
                ).style(
                    f"background: {t.CARD}; border: 1px dashed {t.NEG}; "
                    f"color: {t.NEG}; font-size: 12.5px; font-weight: 700; "
                    f"padding: 10px 14px; border-radius: {t.RADIUS_MD}; "
                    f"text-align: center;"
                )
            return
        if session["user_msg_count"] >= _MESSAGE_LIMIT_PER_SESSION:
            with newchat_box:
                ui.label(
                    f"Session message limit reached ({_MESSAGE_LIMIT_PER_SESSION}). "
                    f"Start a new chat to continue."
                ).style(
                    f"color: {t.TEXT_DIM}; font-size: 12.5px; "
                    f"text-align: center; padding: 4px 0;"
                )
                ui.button("Start New Chat", on_click=_reset_session) \
                    .props("no-caps unelevated") \
                    .style(
                        f"background: {t.PRIMARY}; color: {t.BG}; "
                        f"font-weight: 700; padding: 8px 18px; "
                        f"border-radius: {t.RADIUS_SM}; align-self: center;"
                    )
            return

        # Normal input row.
        with input_box:
            with ui.row().classes("w-full items-end no-wrap").style("gap: 8px;"):
                txt = ui.input(placeholder="Ask the analyst…").style(
                    f"flex: 1; min-width: 0; "
                    f"background: {t.CARD_HI}; border-radius: {t.RADIUS_SM};"
                ).props("outlined dense dark")
                # Submit on Enter.
                async def _on_keydown(e):
                    if e.args.get("key") == "Enter" and not session["busy"]:
                        msg = (txt.value or "").strip()
                        if msg:
                            txt.value = ""
                            await _send(msg)
                txt.on("keydown", _on_keydown)
                send_btn = ui.button("Send").props("no-caps unelevated").style(
                    f"background: {t.PRIMARY}; color: {t.BG}; "
                    f"font-weight: 700; padding: 8px 16px; "
                    f"border-radius: {t.RADIUS_SM}; min-height: 0;"
                )
                async def _click(_e=None):
                    if session["busy"]:
                        return
                    msg = (txt.value or "").strip()
                    if not msg:
                        ui.notify("Type a message first.", type="warning")
                        return
                    txt.value = ""
                    await _send(msg)
                send_btn.on("click", _click)
                if session["busy"]:
                    send_btn.props("loading")
                    send_btn.disable()

    # ── Actions ─────────────────────────────────────────────────────────

    async def _send(message: str) -> None:
        if session["busy"] or session["limit_reached"]:
            return
        if session["user_msg_count"] >= _MESSAGE_LIMIT_PER_SESSION:
            ui.notify(
                f"Session limit ({_MESSAGE_LIMIT_PER_SESSION}) reached. "
                f"Click Start New Chat to begin again.",
                type="warning",
            )
            return

        session["busy"] = True
        session["history"].append({"role": "user", "content": message})
        session["user_msg_count"] += 1
        _render_messages()
        _render_suggestions()
        _render_input()

        include_context = session["first_send_pending"]
        body = {
            "message":         message,
            "history":         session["history"][:-1],  # backend re-adds the new one
            "include_context": include_context,
        }
        ok, data, status = await asyncio.to_thread(
            _post, backend, "/api/ai/chat", body
        )

        session["busy"] = False
        if ok:
            session["first_send_pending"] = False
            session["history"].append({
                "role":    "assistant",
                "content": data.get("response", "(no response)"),
            })
            # Update counter from server-authoritative response.
            if isinstance(data.get("calls_today"), int):
                session["calls_today"]   = data["calls_today"]
            if isinstance(data.get("daily_limit"), int):
                session["daily_limit"]   = data["daily_limit"]
            session["limit_reached"]     = bool(data.get("limit_reached"))
        else:
            # On 429 the backend includes the counter / limit fields too --
            # honor them so the UI flips into "limit reached" state without
            # the user needing to refresh.
            if status == 429:
                session["limit_reached"] = True
                if isinstance(data.get("calls_today"), int):
                    session["calls_today"] = data["calls_today"]
                if isinstance(data.get("daily_limit"), int):
                    session["daily_limit"] = data["daily_limit"]
                # Reverse the user message bookkeeping so the user can
                # retry after a daily reset without an off-by-one in the
                # session counter.
                if session["history"] and session["history"][-1]["role"] == "user":
                    session["history"].pop()
                    session["user_msg_count"] = max(0, session["user_msg_count"] - 1)
                ui.notify(
                    "Daily AI limit reached, resets at midnight.",
                    type="warning",
                )
            else:
                # Generic failure -- leave the user message in place so the
                # context for retry is preserved, but show an error toast.
                err = data.get("error") or f"HTTP {status}"
                ui.notify(f"AI chat failed: {err}", type="negative",
                          multi_line=True)
                # Drop the user message we optimistically appended so the
                # next retry doesn't double it.
                if session["history"] and session["history"][-1]["role"] == "user":
                    session["history"].pop()
                    session["user_msg_count"] = max(0, session["user_msg_count"] - 1)

        _set_counter_text()
        _render_messages()
        _render_suggestions()
        _render_input()

    def _reset_session(_e=None) -> None:
        """Clear the chat and start a fresh session.  Daily counter is
        NOT reset (that's a per-day server-side counter)."""
        session["history"]             = []
        session["first_send_pending"]  = True
        session["user_msg_count"]      = 0
        session["busy"]                = False
        _render_messages()
        _render_suggestions()
        _render_input()

    # ── Initial seed (no Anthropic call) ────────────────────────────────

    async def _seed() -> None:
        ok, data, _ = await asyncio.to_thread(_post, backend, "/api/ai/usage",
                                              None, method="GET")
        if ok:
            session["calls_today"]   = int(data.get("calls_today") or 0)
            session["daily_limit"]   = int(data.get("daily_limit") or 20)
            session["limit_reached"] = bool(data.get("limit_reached"))
        _set_counter_text()
        _render_input()

    _set_counter_text()
    _render_messages()
    _render_suggestions()
    _render_input()
    ui.timer(0.1, _seed, once=True)


# ── Bubble renderer ────────────────────────────────────────────────────────

def _bubble(role: str, content: str, *, placeholder: bool = False) -> None:
    """One message bubble.  user -> right-aligned + primary tint;
    assistant -> left-aligned + card tint.  Plain text only (per the
    system prompt that bans markdown)."""
    is_user = role == "user"
    align   = "flex-end" if is_user else "flex-start"
    bg      = t.PRIMARY if is_user else t.CARD_HI
    fg      = t.BG      if is_user else t.TEXT
    border  = "none" if is_user else f"1px solid {t.BORDER}"

    with ui.row().classes("w-full").style(f"justify-content: {align};"):
        # Use ui.html with newline -> <br> so the assistant's line-break
        # separation survives.  Escape angle brackets so injected text
        # can't render as HTML.
        text = _escape_html(content).replace("\n", "<br>")
        if placeholder:
            text = '<span style="opacity:.6;">…</span>'
        ui.html(
            f'<div style="'
            f'background:{bg}; color:{fg}; border:{border}; '
            f'border-radius:{t.RADIUS_MD}; padding:8px 12px; '
            f'max-width:78%; font-size:13px; line-height:1.5; '
            f'white-space:pre-wrap; word-break:break-word;"'
            f'>{text}</div>'
        )


def _escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


# ── Flask test-client helper (matches the admin.py pattern) ────────────────

def _post(backend, path: str, body: dict | None,
          *, method: str = "POST") -> tuple[bool, dict, int]:
    """In-process call to a Flask /api/ route.  Returns (ok, data, status)."""
    client = backend.app.test_client()
    fn     = client.post if method.upper() == "POST" else client.get
    try:
        resp = fn(path, json=body or {}) if method.upper() == "POST" else fn(path)
        try:
            data = resp.get_json(force=True, silent=True) or {}
        except Exception:                                                 # noqa: BLE001
            data = {}
        ok = resp.status_code < 400 and data.get("success", True) is not False
        return ok, data, resp.status_code
    except Exception as exc:                                              # noqa: BLE001
        return False, {"error": str(exc)}, 500
