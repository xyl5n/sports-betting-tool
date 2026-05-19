"""
AI Breakdown page.

Skeleton for the chat surface that hooks the model up to Claude.  The
legacy Flask UI streamed via /api/ai/chat and /api/ai/breakdown -- this
page just shows the placeholder shell + a button to navigate back to
Home.  A follow-up PR will wire the chat input to backend._call_analyst_chat.
"""
from __future__ import annotations

from nicegui import ui

from components import theme as t
from components import navbar, sidebar, bottom_nav


def register(backend) -> None:
    @ui.page("/ai")
    def ai_page():
        ui.add_head_html(t.page_head_css())
        navbar.render(active=t.TAB_AI)
        with ui.row().classes("no-wrap w-full").style("gap: 0;"):
            sidebar.render(backend)
            with ui.column().classes("page-content").style(
                f"flex: 1; max-width: {t.MAX_CONTENT_W}; "
                f"gap: {t.SPACE_LG}; padding: {t.SPACE_LG}; min-width: 0;"
            ):
                ui.label("AI BREAKDOWN").classes("page-title").style(
                    f"font-size: 22px; font-weight: 800; color: {t.TEXT};"
                )
                with ui.column().style(
                    f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                    f"border-radius: {t.RADIUS_LG}; padding: {t.SPACE_XL}; "
                    f"gap: {t.SPACE_MD}; align-items: center; text-align: center;"
                ):
                    ui.label("Chat shell coming next").style(
                        f"font-size: 16px; font-weight: 700; color: {t.TEXT};"
                    )
                    ui.label(
                        "The Anthropic-backed analyst chat (backend._call_analyst_chat) "
                        "will be wired into this surface in the follow-up PR.  For now "
                        "the legacy /api/ai/chat endpoint is still reachable from the "
                        "imported Flask app for any in-flight callers."
                    ).style(
                        f"color: {t.TEXT_DIM}; font-size: 12.5px; max-width: 520px; "
                        f"line-height: 1.5;"
                    )
        bottom_nav.render(active=t.TAB_AI)
