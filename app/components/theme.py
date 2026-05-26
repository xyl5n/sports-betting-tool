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
# Palette redesign: OLED black base + vibrant purple-to-emerald accent
# gradient.  Purple (PRIMARY) carries every active / selected / CTA
# affordance; emerald (POS) carries every win / confirmation / "good"
# semantic; amber (WARN) for edge picks + cautions; rose (NEG) for
# losses.  The two accent hues are chosen to read distinctly against
# pure-black backgrounds while still working as a gradient pair on
# hover borders (see card-glow CSS below).
BG          = "#000000"          # pure black -- OLED unlit
CARD        = "#111111"          # elevated panel
CARD_HI     = "#1a1a1a"          # nested elevation / hover
BORDER      = "#262232"          # subtle purple-tinted line
BORDER_SOFT = "#161420"          # almost-invisible separator
PRIMARY     = "#7c3aed"          # vibrant purple -- active tab, ML pick, CTA
PRIMARY_HI  = "#a855f7"          # lighter purple -- hover / gradient tail
SECONDARY   = "#10b981"          # emerald -- positive accent pair to purple
TEXT        = "#ffffff"          # primary text
TEXT_DIM    = "#a0a0a0"          # secondary text
TEXT_DIM2   = "#6b7280"          # caption / footnote
POS         = "#10b981"          # win, profit, value (aliased to SECONDARY)
NEG         = "#f43f5e"          # rose -- loss, deficit
WARN        = "#f59e0b"          # amber -- push, void, edge picks
CYAN        = "#22d3ee"          # legacy accent (charts, links) -- unchanged

# RGB components of PRIMARY + SECONDARY so the CSS layer can build
# rgba() tints + gradients without hard-coding the same hue more than
# once.  Updating one of these here flows through every card glow,
# button gradient, and nav-tab halo on the next render.
PRIMARY_R,   PRIMARY_G,   PRIMARY_B   = 124,  58, 237   # #7c3aed
PRIMARY_HI_R, PRIMARY_HI_G, PRIMARY_HI_B = 168, 85, 247  # #a855f7
SECONDARY_R, SECONDARY_G, SECONDARY_B =  16, 185, 129   # #10b981
NEG_R,       NEG_G,       NEG_B       = 244,  63,  94   # #f43f5e
WARN_R,      WARN_G,      WARN_B      = 245, 158,  11   # #f59e0b

# Confidence tier color map -- consumed by sidebar._tier_row + any
# other display that wants the same semantic.  Strong uses emerald
# (most-confident == best == green), Moderate uses amber (the user's
# "yellow zone"), Low uses a muted grey rather than red so it doesn't
# read as a loss.
TIER_COLOR  = {
    "strong":   POS,        # emerald
    "moderate": WARN,       # amber
    "low":      TEXT_DIM2,  # muted grey (not NEG -- low confidence != loss)
}

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
NAVBAR_HEIGHT     = "56px"
SIDEBAR_WIDTH     = "260px"
MAX_CONTENT_W     = "1180px"
MOBILE_BREAKPOINT = "768px"        # below this width -> mobile layout
BOTTOM_NAV_HEIGHT = "60px"         # mobile bottom tab bar

# ── Tab keys (single source of truth so navbar + router stay in sync) ───────
TAB_HOME    = "home"
TAB_SPORTS  = "sports"
TAB_PROPS   = "props"
TAB_AI      = "ai"
TAB_MYBETS  = "mybets"
TAB_MODEL   = "model"
TAB_TOP     = "top"
TAB_ADMIN   = "admin"
TAB_PICKS   = "picks"
TAB_BETS    = "bets"

TABS = (TAB_HOME, TAB_SPORTS, TAB_PROPS, TAB_AI, TAB_MYBETS, TAB_MODEL, TAB_TOP, TAB_ADMIN, TAB_PICKS, TAB_BETS)


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

      /* Horizontal carousels (EV Scan + Highest Confidence on /).  Hide
         the native scrollbar entirely -- the < / > overlay arrows +
         wheel-to-horizontal JS in components/carousel_wheel.py provide
         the affordances now.  Per-browser:
           Firefox      -- scrollbar-width: none
           Chromium/WK  -- ::-webkit-scrollbar {{ display: none }}
           Edge old IE  -- -ms-overflow-style: none
         No layout reflow on Firefox because scrollbar-width:none
         removes the gutter too. */
      .carousel-scroller {{
        scrollbar-width: none;
        -ms-overflow-style: none;
      }}
      .carousel-scroller::-webkit-scrollbar {{ display: none; }}

      /* Game grid -- two columns on desktop (>768px), one on mobile.
         Cards fill left-to-right top-to-bottom; an odd final game
         occupies the left column only (grid auto-flow default).
         The mobile-breakpoint @media rule further down also bumps
         tap-target heights on the cards' Track + View Details
         controls so each tile stays comfortable on touch. */
      .game-grid {{
        display: grid;
        grid-template-columns: 1fr;
        gap: {SPACE_MD};
        width: 100%;
      }}
      @media (min-width: 769px) {{
        .game-grid {{
          grid-template-columns: 1fr 1fr;
          gap: {SPACE_LG};
        }}
      }}

      .theme-card {{
        background: {CARD};
        border: 1px solid {BORDER};
        border-radius: {RADIUS_MD};
        padding: {SPACE_MD};
        box-shadow:
          inset 0 0 0 1px rgba({PRIMARY_R}, {PRIMARY_G}, {PRIMARY_B}, 0.05),
          0 1px 0 rgba({SECONDARY_R}, {SECONDARY_G}, {SECONDARY_B}, 0.03),
          0 6px 18px rgba({PRIMARY_R}, {PRIMARY_G}, {PRIMARY_B}, 0.05);
      }}
      .theme-card-hi {{ background: {CARD_HI}; }}

      /* Card glow -- applied via an attribute selector to every element
         whose inline border matches our theme BORDER constant.  This is
         how we reach all the card-style containers without changing
         every render call: components inline-style their cards with
         the f-string `border: 1px solid {{t.BORDER}}` which produces a
         predictable substring we can target.

         The shadow stack carries the purple/emerald accent pair:
           inset 1px purple ring    -- the "tinted highlight"
           outer 1px emerald halo   -- pairs with the inset for the
                                       gradient feel the spec asks for
           outer purple bloom       -- subtle lift off the OLED background
         All three layers are weak at rest; hover bumps them and adds
         a second emerald bloom so the card visibly "warms up" without
         shifting its layout. */
      [style*="border: 1px solid {BORDER}"] {{
        box-shadow:
          inset 0 0 0 1px rgba({PRIMARY_R}, {PRIMARY_G}, {PRIMARY_B}, 0.06),
          0 0 0 1px rgba({SECONDARY_R}, {SECONDARY_G}, {SECONDARY_B}, 0.04),
          0 6px 20px rgba({PRIMARY_R}, {PRIMARY_G}, {PRIMARY_B}, 0.06);
        transition: box-shadow 180ms ease-out, border-color 180ms ease-out;
      }}
      /* Hover: gradient feel via two outer rings (purple inset, emerald
         outer) + a dual bloom.  Matches the spec's "subtle gradient
         border from purple to emerald on hover". */
      [style*="border: 1px solid {BORDER}"]:hover {{
        box-shadow:
          inset 0 0 0 1px rgba({PRIMARY_R}, {PRIMARY_G}, {PRIMARY_B}, 0.18),
          0 0 0 1px rgba({SECONDARY_R}, {SECONDARY_G}, {SECONDARY_B}, 0.14),
          0 8px 22px rgba({PRIMARY_R}, {PRIMARY_G}, {PRIMARY_B}, 0.15),
          0 4px 18px rgba({SECONDARY_R}, {SECONDARY_G}, {SECONDARY_B}, 0.10);
      }}
      /* Dashed borders (used by NO MODEL PICK chips, EV banners) shouldn't
         pick up the purple ring -- the dashed visual is the affordance. */
      [style*="border: 1px dashed {BORDER}"] {{
        box-shadow: none !important;
      }}

      /* Active navbar tab -- gets a soft purple halo so it's visibly
         distinct from inactive tabs even before the user reads the
         color cue.  The underline border-bottom from navbar.py stays
         the primary affordance; this just adds a glow.
         Selector targets the link whose color is the PRIMARY value
         (set inline by navbar._nav_link when is_active=True). */
      a[style*="color: {PRIMARY}"]:not(.q-btn) {{
        text-shadow: 0 0 8px rgba({PRIMARY_R}, {PRIMARY_G}, {PRIMARY_B}, 0.55),
                     0 0 14px rgba({PRIMARY_R}, {PRIMARY_G}, {PRIMARY_B}, 0.25);
      }}

      /* Primary buttons -- Quasar's q-btn with color="primary" picks
         t.PRIMARY from ui_app.py's ui.colors() call.  Layer a
         purple -> lighter-purple linear gradient on top so the
         button reads as "live" rather than flat.  Hover deepens the
         gradient.  Quasar's own ripple + flat/dense modifiers are
         preserved by the `:not()` guards. */
      .q-btn.bg-primary,
      .q-btn[style*="background: {PRIMARY}"] {{
        background: linear-gradient(135deg, {PRIMARY} 0%, {PRIMARY_HI} 100%) !important;
        transition: background 200ms ease-out, box-shadow 200ms ease-out;
      }}
      .q-btn.bg-primary:hover,
      .q-btn[style*="background: {PRIMARY}"]:hover {{
        background: linear-gradient(135deg, {PRIMARY_HI} 0%, {PRIMARY} 100%) !important;
        box-shadow: 0 4px 14px rgba({PRIMARY_R}, {PRIMARY_G}, {PRIMARY_B}, 0.4);
      }}

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

      /* Team logo widget -- ported from the legacy templates/index.html.
         The container is a sized rounded square that doubles as the
         fallback: it carries the team-coloured background + initials.
         When the actual logo PNG loads it sits absolutely on top with a
         white background; if it fails, onerror=this.remove() drops the
         img and the fallback shows again with no broken-image icon. */
      .team-logo {{
        position: relative;
        width:  var(--logo-size, 36px);
        height: var(--logo-size, 36px);
        border-radius: 50%;
        display: inline-flex; align-items: center; justify-content: center;
        overflow: hidden;
        flex-shrink: 0;
        color: #fff;
        font-size: var(--logo-fs, 12px); font-weight: 800;
        background: var(--logo-bg, #555);
        letter-spacing: -.2px;
      }}
      .team-logo-init {{
        position: absolute; inset: 0;
        display: flex; align-items: center; justify-content: center;
        z-index: 0;
      }}
      .team-logo-img {{
        position: absolute; inset: 0;
        width: 100%; height: 100%;
        object-fit: contain;
        background: #fff;
        z-index: 1;
      }}

      /* Live dot -- small pulsing circle next to the LIVE label on
         in-progress games.  Color is set inline at render time so the
         green can be themed; the animation is CSS-only. */
      .live-dot {{
        display: inline-block;
        width: 8px; height: 8px;
        border-radius: 50%;
        margin-right: 6px;
        animation: live-pulse 1.4s ease-in-out infinite;
        box-shadow: 0 0 6px currentColor;
        vertical-align: middle;
      }}
      @keyframes live-pulse {{
        0%, 100% {{ opacity: 1;   transform: scale(1);   }}
        50%      {{ opacity: 0.45; transform: scale(0.85); }}
      }}

      /* ── Responsive visibility ─────────────────────────────────────
         .desktop-only hides on mobile, .mobile-only hides on desktop.
         Single render path -- the browser decides which to show.    */
      .mobile-only  {{ display: none !important; }}

      @media (max-width: {MOBILE_BREAKPOINT}) {{
        .desktop-only {{ display: none !important; }}
        .mobile-only  {{ display: flex !important; }}

        /* Tighter padding everywhere on mobile.  Top padding is
           deliberately tiny (4px) so the first card sits flush under
           the navbar -- on a 667px iPhone viewport the prior 12px
           top-padding + page-title margin combined to push real
           content below the fold on landing.  Horizontal + bottom
           insets stay at 12px for readability and to keep the bottom
           nav's drop shadow off the last card. */
        .page-content {{
          padding: 4px 12px 12px 12px !important;
          gap: 12px !important;
        }}

        /* Page titles ("PLAYER PROPS", "ADMIN", etc.) shouldn't add
           extra vertical space on mobile -- the q-label default is
           already line-height 1, but Quasar can apply margins via the
           parent column gap.  Explicit 0 margins keep the title flush
           with the page-content top padding. */
        .page-title {{ margin: 0 !important; }}

        /* Hero stat cells -- wrap to 2 per row instead of 3 across */
        .hero-stats {{
          flex-wrap: wrap !important;
          gap: 12px !important;
          padding: 14px !important;
        }}
        .hero-stats > * {{ flex: 1 0 40% !important; min-width: 0 !important; }}
        .hero-stats .stat-value {{ font-size: 18px !important; }}

        /* Game card -- keep the three bet boxes side by side at every
           width.  Previously stacked vertically below 768px; users want
           horizontal layout on all screens (the three picks compare best
           when shown next to each other).  Reductions below scale font
           + padding inside each box so they fit a ~360px phone screen. */
        .bet-boxes {{
          flex-direction: row !important;
          flex-wrap:      nowrap !important;
          gap:            4px !important;
          width:          100%;
        }}
        .bet-boxes > * {{
          padding:    6px 7px !important;
          min-width:  0 !important;       /* allow boxes to shrink past content */
          flex:       1 1 0 !important;   /* equal width, allow shrink */
        }}
        /* Shrink secondary text (prob / edge / odds row + pick text) on
           mobile so the bottom row of each box doesn't wrap. */
        .bet-boxes .text-row > * {{ font-size: 10px !important; }}
        .bet-boxes .pick-text   {{ font-size: 12px !important; }}

        /* Bottom-nav clearance is handled automatically by Quasar's
           QLayout once the bar is mounted inside ui.footer() (see
           components/bottom_nav.render).  We deliberately do NOT add
           a manual padding-bottom on .q-page-container here -- doing
           so would double-count with QLayout's auto-reservation and
           push the last card up by ~70px of dead space. */

        /* Section titles a touch smaller on mobile */
        .page-title {{ font-size: 18px !important; }}

        /* iOS Human Interface Guidelines + Material Design both recommend
           a minimum 44 x 44 CSS-px tap target for primary controls on
           touch screens.  Quasar's q-btn defaults to ~36px height which
           is fine on desktop but cramped on phones -- bump everywhere
           inside our page-content tree so every CTA, Run, Track, and
           confirm-dialog button gets a comfortable touch slab on
           mobile.  Inline `ui.link` rendered as a button shape (Track
           buttons in cards) gets the same minimum via the [role=button]
           selector below. */
        .q-btn,
        button.q-btn,
        .nicegui-button,
        .q-btn-item,
        a.q-btn,
        [role="button"] {{
          min-height: 44px !important;
        }}
        /* Quasar's button-internal stretcher needs the same so the
           hit area inside the button matches its outer dimensions. */
        .q-btn__wrapper {{
          min-height: 44px !important;
        }}
        /* Game-card Track + admin section's run-button -- both use
           Quasar `dense` to look compact on desktop.  Override the
           dense reduction so they still hit the 44px floor. */
        .q-btn--dense {{
          min-height: 44px !important;
          padding-top: 4px !important;
          padding-bottom: 4px !important;
        }}

        /* AI Breakdown chat input -- the SEND button + the input row
           need the same touch slab; the input itself stretches via
           Quasar's q-input but the wrapping row's gap can squeeze it. */
        .q-input,
        .q-field__control {{
          min-height: 44px !important;
        }}
      }}

      /* EV scan carousel -- equal-width cards so exactly 3 are visible on
         desktop (>768px) and 2 on mobile.  flex-basis math:
           desktop: (100% - 2 gaps of 8px) / 3
           mobile:  (100% - 1 gap of 8px) / 2
         min-width: 0 lets the card shrink past its content (we rely on
         the calc() width below; the card's own ellipsis rules keep text
         from overflowing). */
      .ev-card {{
        flex: 0 0 calc((100% - 16px) / 3);
        max-width: calc((100% - 16px) / 3);
        min-width: 0;
      }}
      @media (max-width: {MOBILE_BREAKPOINT}) {{
        .ev-card {{
          flex: 0 0 calc((100% - 8px) / 2);
          max-width: calc((100% - 8px) / 2);
        }}
      }}
      /* Arrow buttons -- subtle hover affordance, hidden on touch via
         the existing .desktop-only / .mobile-only media-query system. */
      .ev-arrow:hover {{
        background: {CARD_HI} !important;
      }}

      /* ── Themed form controls ─────────────────────────────────────────
         Quasar's defaults are Material-Design pale.  Override them so
         filter bars on /props, /player, /admin and /mybets read as
         "ours" rather than vendor stock.  Classes ``.pill-toggle``,
         ``.styled-select``, ``.styled-switch``, ``.styled-slider`` are
         opt-in marker classes applied by ``components/controls.py``;
         the ``.q-tabs`` / ``.q-tab`` overrides further down also apply
         globally so the market tabs on the player page and the
         MLB/WNBA/PROPS tabs on /mybets all match.
                                                                        */

      /* Segmented pill toggle (q-btn-toggle) ─────────────────────── */
      .pill-toggle.q-btn-toggle {{
        background: transparent !important;
        border: 1px solid {BORDER} !important;
        border-radius: {RADIUS_PILL} !important;
        padding: 3px !important;
        box-shadow: none !important;
        display: inline-flex !important;
        gap: 2px !important;
      }}
      .pill-toggle .q-btn {{
        background: transparent !important;
        color: {TEXT_DIM} !important;
        border-radius: {RADIUS_PILL} !important;
        min-height: 28px !important;
        padding: 4px 12px !important;
        font-size: 11px !important;
        font-weight: 700 !important;
        letter-spacing: .35px !important;
        text-transform: uppercase !important;
        box-shadow: none !important;
        border: none !important;
        transition: background 150ms ease-out, color 150ms ease-out !important;
      }}
      .pill-toggle .q-btn:hover {{
        background: {CARD_HI} !important;
        color: {TEXT} !important;
      }}
      .pill-toggle .q-btn.bg-primary,
      .pill-toggle .q-btn[aria-pressed="true"] {{
        background: linear-gradient(135deg, {PRIMARY} 0%, {PRIMARY_HI} 100%) !important;
        color: {BG} !important;
        font-weight: 800 !important;
        box-shadow:
          0 0 0 1px rgba({PRIMARY_R}, {PRIMARY_G}, {PRIMARY_B}, 0.6),
          0 0 14px rgba({PRIMARY_R}, {PRIMARY_G}, {PRIMARY_B}, 0.35) !important;
      }}

      /* Dropdown select (q-select with outlined prop) ────────────── */
      .styled-select.q-field--outlined .q-field__control {{
        background: {CARD_HI} !important;
        border-radius: {RADIUS_SM} !important;
        min-height: 38px !important;
        padding: 0 12px !important;
      }}
      .styled-select.q-field--outlined .q-field__control::before {{
        border: 1px solid {BORDER} !important;
        border-radius: {RADIUS_SM} !important;
      }}
      .styled-select.q-field--outlined:hover .q-field__control::before {{
        border-color: {PRIMARY} !important;
      }}
      .styled-select.q-field--outlined .q-field__control::after {{
        border-bottom-width: 0 !important;   /* drop Material's underline */
      }}
      .styled-select.q-field--outlined.q-field--focused
        .q-field__control::before {{
        border-color: {PRIMARY} !important;
        box-shadow: 0 0 0 1px {PRIMARY},
                    0 0 12px rgba({PRIMARY_R}, {PRIMARY_G}, {PRIMARY_B}, 0.25) !important;
      }}
      .styled-select .q-field__native,
      .styled-select .q-field__input {{
        color: {TEXT} !important;
        font-size: 12.5px !important;
        font-weight: 600 !important;
      }}
      .styled-select .q-field__label {{
        color: {TEXT_DIM2} !important;
        font-size: 11px !important;
      }}
      .styled-select .q-select__dropdown-icon {{
        color: {TEXT_DIM} !important;
      }}

      /* Dropdown popup (q-menu) -- shared by every q-select.  Themed
         globally so the popups always match the trigger, even when a
         page uses ui.select directly without going through our helper.
                                                                       */
      .q-menu {{
        background: {CARD} !important;
        border: 1px solid {BORDER} !important;
        border-radius: {RADIUS_MD} !important;
        color: {TEXT} !important;
        box-shadow:
          0 12px 32px rgba(0, 0, 0, 0.55),
          inset 0 0 0 1px rgba({PRIMARY_R}, {PRIMARY_G}, {PRIMARY_B}, 0.08) !important;
      }}
      .q-menu .q-item {{
        color: {TEXT} !important;
        min-height: 36px !important;
        padding: 8px 14px !important;
        font-size: 12.5px !important;
      }}
      .q-menu .q-item:hover,
      .q-menu .q-item--active,
      .q-menu .q-item.q-manual-focusable--focused {{
        background: {CARD_HI} !important;
        color: {PRIMARY_HI} !important;
      }}
      .q-menu .q-item__section--main {{
        color: inherit !important;
      }}

      /* Switch (q-toggle) ──────────────────────────────────────── */
      .styled-switch .q-toggle__track {{
        background: {CARD_HI} !important;
        opacity: 1 !important;
        border: 1px solid {BORDER} !important;
        height: 14px !important;
      }}
      .styled-switch .q-toggle__inner--truthy .q-toggle__track {{
        background: linear-gradient(135deg, {PRIMARY} 0%, {PRIMARY_HI} 100%) !important;
        border-color: {PRIMARY} !important;
      }}
      .styled-switch .q-toggle__thumb {{
        background: #ffffff !important;
        box-shadow: 0 2px 6px rgba(0, 0, 0, 0.4) !important;
      }}
      .styled-switch .q-toggle__inner--truthy .q-toggle__thumb {{
        box-shadow:
          0 2px 6px rgba(0, 0, 0, 0.4),
          0 0 10px rgba({PRIMARY_R}, {PRIMARY_G}, {PRIMARY_B}, 0.55) !important;
      }}

      /* Slider (q-slider) ──────────────────────────────────────── */
      .styled-slider .q-slider__track {{
        background: {CARD_HI} !important;
        height: 5px !important;
        border-radius: 999px !important;
      }}
      .styled-slider .q-slider__selection {{
        background: linear-gradient(90deg, {PRIMARY} 0%, {PRIMARY_HI} 100%) !important;
        border-radius: 999px !important;
      }}
      .styled-slider .q-slider__thumb {{
        color: {PRIMARY_HI} !important;
      }}
      .styled-slider .q-slider__thumb-shape {{
        filter: drop-shadow(0 0 6px rgba({PRIMARY_R}, {PRIMARY_G}, {PRIMARY_B}, 0.55));
      }}
      .styled-slider .q-slider__pin {{
        background: linear-gradient(135deg, {PRIMARY} 0%, {PRIMARY_HI} 100%) !important;
        color: {BG} !important;
        font-weight: 800 !important;
        border-radius: {RADIUS_SM} !important;
      }}
      .styled-slider .q-slider__pin-text-container {{
        color: {BG} !important;
        font-weight: 800 !important;
      }}

      /* Tabs (q-tabs / q-tab) ──────────────────────────────────────
         The market tabs on /player and the MLB/WNBA/PROPS tabs on
         /mybets share these styles -- no per-page override needed.
                                                                    */
      .q-tabs {{
        color: {TEXT_DIM} !important;
        font-family: inherit !important;
        min-height: 38px !important;
      }}
      .q-tab {{
        color: {TEXT_DIM} !important;
        font-size: 11.5px !important;
        font-weight: 700 !important;
        letter-spacing: .4px !important;
        text-transform: uppercase !important;
        min-height: 38px !important;
        padding: 6px 14px !important;
        opacity: 1 !important;
        transition: background 150ms, color 150ms !important;
      }}
      .q-tab:hover {{
        color: {TEXT} !important;
        background: {CARD_HI} !important;
      }}
      .q-tab--active {{
        color: {PRIMARY_HI} !important;
        font-weight: 800 !important;
      }}
      .q-tab__indicator {{
        background: linear-gradient(90deg, {PRIMARY} 0%, {PRIMARY_HI} 100%) !important;
        height: 2px !important;
        border-radius: 2px !important;
        box-shadow: 0 0 10px rgba({PRIMARY_R}, {PRIMARY_G}, {PRIMARY_B}, 0.55) !important;
      }}
      .q-tab .q-focus-helper,
      .q-tab .q-ripple {{ display: none !important; }}

      /* Defensive: native <select> can sneak in from third-party
         widgets.  Render them with the same dark card look so they
         don't break the theme. */
      select:not([multiple]) {{
        appearance: none;
        -webkit-appearance: none;
        background: {CARD_HI};
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: {RADIUS_SM};
        padding: 6px 28px 6px 12px;
        font-size: 12.5px;
        font-weight: 600;
        background-image:
          linear-gradient(45deg, transparent 50%, {TEXT_DIM} 50%),
          linear-gradient(135deg, {TEXT_DIM} 50%, transparent 50%);
        background-position:
          calc(100% - 16px) 50%,
          calc(100% - 11px) 50%;
        background-size: 5px 5px, 5px 5px;
        background-repeat: no-repeat;
      }}
      select:not([multiple]):hover {{ border-color: {PRIMARY}; }}
      select:focus {{ outline: none; border-color: {PRIMARY}; }}

      /* Bet-box labels: full version is visible by default; the abbreviated
         span (ML / RL / SPR / TOT) takes over below 480px so the labels +
         optional VALUE chip fit inside a ~110px box on a portrait phone. */
      .bet-label-short {{ display: none; }}
      @media (max-width: 480px) {{
        .bet-label-full  {{ display: none !important; }}
        .bet-label-short {{ display: inline-block !important; }}

        /* Sub-480px safety net for very long strings: an unbroken team
           name, a malformed odds field, or an upstream label change
           would otherwise push the layout wider than the viewport and
           cause horizontal scroll on the whole page.  These two rules
           force breakable wrapping while keeping the per-card ellipsis
           rules from PR #29 as the primary truncation mechanism. */
        .nicegui-content,
        .nicegui-content * {{
          overflow-wrap: anywhere;
          word-break:    break-word;
        }}
        /* Outer page never scrolls horizontally on a phone.  Per-card
           ellipsis still handles long strings inside; this is just the
           safety belt. */
        body, html {{
          overflow-x: hidden !important;
        }}
      }}
    </style>
    """
