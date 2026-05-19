"""
Theme tokens — single source of truth for colors, spacing, and typography.

Edit ONLY this file to retune the look of the app.  Every component and
page reads from here so a one-line change here flows everywhere.

Color palette is the OLED-black spec from the migration brief:
  bg          pure black  for surfaces that should disappear on OLED
  card        very dark   for elevated panels
  primary     blue        accent for active state, ML pick, buttons
  text        white       primary text
  text_dim    gray        secondary / metadata text
  pos / neg   green / red P/L and W/L semantic colors
  warn        amber       push / void / caution
"""
from __future__ import annotations

# ── Colors ──────────────────────────────────────────────────────────────────
BG          = "#000000"          # pure black -- OLED
CARD        = "#0d0d0d"          # elevated panel
CARD_HI     = "#161616"          # nested elevation (hover, sub-panel)
BORDER      = "#1f1f1f"          # subtle line
BORDER_SOFT = "#141414"          # almost-invisible separator
PRIMARY     = "#3b82f6"          # blue accent (active tab, ML pick, CTA)
PRIMARY_HI  = "#60a5fa"          # primary hover / lighter
TEXT        = "#ffffff"          # primary text
TEXT_DIM    = "#a0a0a0"          # secondary text
TEXT_DIM2   = "#6b7280"          # caption / footnote
POS         = "#22c55e"          # win, profit, value
NEG         = "#ef4444"          # loss, deficit
WARN        = "#fbbf24"          # push, void, warning
CYAN        = "#22d3ee"          # legacy accent (charts, links)

# ── Spacing ─────────────────────────────────────────────────────────────────
SPACE_XS    = "4px"
SPACE_SM    = "8px"
SPACE_MD    = "14px"
SPACE_LG    = "20px"
SPACE_XL    = "32px"

# ── Radii ───────────────────────────────────────────────────────────────────
RADIUS_SM   = "6px"
RADIUS_MD   = "10px"
RADIUS_LG   = "16px"
RADIUS_PILL = "999px"

# ── Layout ──────────────────────────────────────────────────────────────────
NAVBAR_HEIGHT  = "56px"
SIDEBAR_WIDTH  = "260px"
MAX_CONTENT_W  = "1180px"

# ── Tab keys (single source of truth so navbar + router stay in sync) ───────
TAB_HOME    = "home"
TAB_SPORTS  = "sports"
TAB_AI      = "ai"
TAB_MYBETS  = "mybets"
TAB_MODEL   = "model"

TABS = (TAB_HOME, TAB_SPORTS, TAB_AI, TAB_MYBETS, TAB_MODEL)


def page_head_css() -> str:
    """Inline CSS injected once per page via ui.add_head_html().  Sets the
    OLED background on body and styles the few primitives we re-use a lot
    (chip, accent text).  Everything else is set per-element via the
    constants above so the theme stays compositional."""
    return f"""
    <style>
      body, .nicegui-content, .q-page-container {{
        background: {BG} !important;
        color: {TEXT};
      }}
      .q-page {{ background: {BG} !important; }}
      ::-webkit-scrollbar         {{ width: 8px; height: 8px; }}
      ::-webkit-scrollbar-track   {{ background: {BG}; }}
      ::-webkit-scrollbar-thumb   {{ background: {BORDER}; border-radius: 4px; }}
      ::-webkit-scrollbar-thumb:hover {{ background: {TEXT_DIM2}; }}

      .theme-card {{
        background: {CARD};
        border: 1px solid {BORDER};
        border-radius: {RADIUS_MD};
        padding: {SPACE_MD};
      }}
      .theme-card-hi {{ background: {CARD_HI}; }}
      .theme-chip {{
        display: inline-block;
        padding: 2px 8px;
        font-size: 10px; font-weight: 700; letter-spacing: .5px;
        background: {CARD_HI}; color: {TEXT_DIM};
        border-radius: {RADIUS_PILL};
      }}
      .theme-mono {{ font-family: "SF Mono", Consolas, ui-monospace, monospace; }}
      .theme-pos  {{ color: {POS}; }}
      .theme-neg  {{ color: {NEG}; }}
      .theme-warn {{ color: {WARN}; }}
      .theme-dim  {{ color: {TEXT_DIM}; }}
    </style>
    """
