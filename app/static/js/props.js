/* props.js — client-side filtering / sorting / rendering for the Flask +
 * Tailwind player-props page (PR #304).  Vanilla JS only, no dependencies.
 *
 * The server embeds the full prop view-model as JSON in #props-data; we read
 * it once, then every filter/sort/toggle interaction re-renders the grid in
 * place with no page reload. */
(function () {
  "use strict";

  // ── Load server data ──────────────────────────────────────────────────
  var ALL = [];
  try {
    var node = document.getElementById("props-data");
    ALL = JSON.parse((node && node.textContent) || "[]") || [];
  } catch (e) {
    ALL = [];
  }

  // Per-card UI state (the Over/Under toggle) keyed by index so re-renders
  // preserve the user's choice. Defaults to the model's recommended side.
  var sideOverride = {};

  // Per-card track state, keyed by index. Values:
  //   undefined -> use the server's initial p.tracked flag
  //   true      -> tracked (success or 409-dedup)
  //   false     -> explicit revert (network / 5xx error)
  // Pending POSTs flip the DOM class .is-pending but don't write here until
  // the response lands.
  var trackedOverride = {};
  var trackInflight   = {};       // idx -> bool (avoid double-fire)

  // ── Filter state ──────────────────────────────────────────────────────
  // The first three fields drive the always-visible top bar (sport / stat
  // pills + sort dropdown).  The `panel` sub-object mirrors the NiceGUI
  // filter-bar state and AND-stacks with the top-bar filters via
  // panelPasses().  `viewMode` ("list" | "game") swaps the flat grid for
  // collapsible per-game groups (Phase 2d).  All persisted to localStorage.
  var state = {
    sport: "all", stat: "all", sort: "confidence",
    viewMode: "list",
    panel: defaultPanel(),
  };

  // Per-game expand/collapse flags for By-Game view (in-memory only --
  // NiceGUI doesn't persist these either; default = all collapsed).
  var expandedGames = {};

  var VIEW_MODE_LS_KEY = "propsViewMode";

  function loadPersistedViewMode() {
    try {
      var v = window.localStorage.getItem(VIEW_MODE_LS_KEY);
      if (v === "list" || v === "game") state.viewMode = v;
    } catch (e) { /* ignore */ }
  }

  function persistViewMode() {
    try { window.localStorage.setItem(VIEW_MODE_LS_KEY, state.viewMode); }
    catch (e) { /* ignore */ }
  }

  function defaultPanel() {
    return {
      min_l10:   0,        // raw hit-count floor (0 = Any, 5..9 = N+/10)
      min_conf:  0.0,      // 0..1 confidence floor
      min_grade: 0.0,      // 0..1 matchup-grade floor (server-computed)
      markets:   [],       // array of market keys (empty = all)
      games:     [],       // array of game keys    (empty = all)
      show_alt:  false,    // false hides line_type != "main"
    };
  }

  var PANEL_LS_KEY = "propsFilters";

  function loadPersistedPanel() {
    try {
      var raw = window.localStorage.getItem(PANEL_LS_KEY);
      if (!raw) return;
      var blob = JSON.parse(raw);
      if (!blob || typeof blob !== "object") return;
      var p = state.panel;
      if (typeof blob.min_l10   === "number") p.min_l10   = blob.min_l10;
      if (typeof blob.min_conf  === "number") p.min_conf  = blob.min_conf;
      if (typeof blob.min_grade === "number") p.min_grade = blob.min_grade;
      if (Array.isArray(blob.markets))         p.markets   = blob.markets.slice();
      if (Array.isArray(blob.games))           p.games     = blob.games.slice();
      if (typeof blob.show_alt  === "boolean") p.show_alt  = blob.show_alt;
    } catch (e) { /* corrupted blob -> defaults */ }
  }

  function persistPanel() {
    try {
      window.localStorage.setItem(PANEL_LS_KEY, JSON.stringify({
        min_l10:   state.panel.min_l10,
        min_conf:  state.panel.min_conf,
        min_grade: state.panel.min_grade,
        markets:   state.panel.markets.slice().sort(),
        games:     state.panel.games.slice().sort(),
        show_alt:  state.panel.show_alt,
      }));
    } catch (e) { /* quota / private mode -> silently skip */ }
  }

  function activePanelCount() {
    var p = state.panel, n = 0;
    if (p.min_l10)         n++;
    if (p.min_conf)        n++;
    if (p.min_grade)       n++;
    if (p.markets.length)  n++;
    if (p.games.length)    n++;
    if (p.show_alt)        n++;          // default is hidden -- showing is the deviation
    return n;
  }

  function panelPasses(p) {
    var f = state.panel;
    // 1. Min last-10 hits (raw, matches NiceGUI semantics)
    if (f.min_l10 && (Number(p.l10_hits) || 0) < f.min_l10) return false;
    // 2. Alt lines hidden by default
    if (!f.show_alt && (p.line_type || "main").toLowerCase() !== "main") return false;
    // 3. Market multi-select (empty = all)
    if (f.markets.length && f.markets.indexOf(p.market) === -1) return false;
    // 4. Game multi-select (empty = all)
    if (f.games.length && f.games.indexOf(p.game_key) === -1) return false;
    // 5. Min confidence
    if (f.min_conf && (Number(p.confidence) || 0) < f.min_conf) return false;
    // 6. Min matchup grade (server-computed)
    if (f.min_grade && (Number(p.prop_grade) || 0) < f.min_grade) return false;
    return true;
  }

  // ── Helpers ───────────────────────────────────────────────────────────
  function confColor(pct) {
    if (pct >= 70) return "#22c55e";       // green
    if (pct >= 50) return "#eab308";       // yellow
    return "#6b7280";                       // gray
  }

  function sportTint(sport) {
    // Subtle gradient tint by sport for the card header.
    if (sport === "WNBA") return "linear-gradient(135deg, rgba(249,115,22,.16), rgba(26,26,26,0))";
    return "linear-gradient(135deg, rgba(124,58,237,.16), rgba(26,26,26,0))"; // MLB / default
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fmtRate(v) {
    return (v == null || isNaN(v)) ? "—" : (Math.round(v) + "%");
  }

  function fmtLine(v) {
    if (v == null || v === "") return "—";
    return v;
  }

  // ── Card markup ───────────────────────────────────────────────────────
  function cardHTML(p, idx) {
    var confPct = Number(p.confidence_pct) || 0;
    var color = confColor(confPct);
    var dimmed = confPct < 50 ? " dimmed" : "";
    var side = sideOverride[idx] || p.side || "Over";
    var avatar = p.headshot
      ? '<img src="' + esc(p.headshot) + '" alt="' + esc(p.player) +
        '" class="w-11 h-11 rounded-full object-cover bg-cardhi border border-border shrink-0" ' +
        'onerror="this.outerHTML=\'<div class=&quot;w-11 h-11 rounded-full bg-cardhi border border-border flex items-center justify-center text-xl shrink-0&quot;>⚾</div>\';">'
      : '<div class="w-11 h-11 rounded-full bg-cardhi border border-border flex items-center justify-center text-xl shrink-0">⚾</div>';

    var overActive = side === "Over";
    var overCls = overActive
      ? "bg-pos text-black"
      : "bg-card text-gray-400 border border-border";
    var underCls = !overActive
      ? "bg-neg text-white"
      : "bg-card text-gray-400 border border-border";

    return '' +
      '<div class="prop-card' + dimmed + ' rounded-2xl bg-card border border-border overflow-hidden flex flex-col" data-idx="' + idx + '">' +

        // Header (gradient tinted to sport)
        '<div class="px-4 pt-3 pb-2 flex items-start justify-between gap-2" style="background:' + sportTint(p.sport) + '">' +
          '<div class="flex items-center gap-2 min-w-0">' +
            '<span class="text-[10px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded bg-black/40 text-accenthi">' + esc(p.sport) + '</span>' +
            (p.game_time ? '<span class="text-[11px] text-gray-400 truncate">' + esc(p.game_time) + '</span>' : '') +
          '</div>' +
          (p.matchup ? '<span class="text-[11px] font-semibold text-gray-300 truncate max-w-[45%] text-right">' + esc(p.matchup) + '</span>' : '') +
        '</div>' +

        // Player row
        '<div class="px-4 py-2 flex items-center gap-3">' +
          avatar +
          '<div class="min-w-0">' +
            '<div class="font-bold text-[15px] truncate">' + esc(p.player || "Unknown") + '</div>' +
            '<div class="text-[11px] text-gray-500 truncate">' +
              (p.position ? esc(p.position) : '') +
              (p.team ? (p.position ? ' · ' : '') + esc(p.team) : '') +
            '</div>' +
          '</div>' +
        '</div>' +

        // Stat line + Over/Under toggle
        '<div class="px-4 py-2 flex items-center justify-between gap-3">' +
          '<div class="min-w-0">' +
            '<div class="text-[11px] uppercase tracking-wide text-gray-500 truncate">' + esc(p.stat_label || "") + '</div>' +
            '<div class="text-2xl font-extrabold leading-tight">' + esc(fmtLine(p.line)) + '</div>' +
          '</div>' +
          '<div class="flex rounded-lg overflow-hidden shrink-0 text-xs font-bold">' +
            '<button class="ou-btn px-3 py-2 ' + overCls + '" data-idx="' + idx + '" data-side="Over">OVER</button>' +
            '<button class="ou-btn px-3 py-2 ' + underCls + '" data-idx="' + idx + '" data-side="Under">UNDER</button>' +
          '</div>' +
        '</div>' +

        // Confidence bar
        '<div class="px-4 py-2">' +
          '<div class="flex items-center justify-between text-[11px] mb-1">' +
            '<span class="text-gray-500">Confidence</span>' +
            '<span class="font-bold" style="color:' + color + '">' + confPct.toFixed(0) + '%</span>' +
          '</div>' +
          '<div class="h-1.5 rounded-full bg-cardhi overflow-hidden">' +
            '<div class="h-full rounded-full" style="width:' + Math.max(0, Math.min(100, confPct)) + '%;background:' + color + '"></div>' +
          '</div>' +
        '</div>' +

        // Stats row
        '<div class="px-4 py-2 grid grid-cols-3 gap-2 text-center">' +
          statCell("Edge", (p.edge_pct == null ? "—" : (p.edge_pct > 0 ? "+" : "") + p.edge_pct + "%")) +
          statCell("L10", fmtRate(p.l10_hit_rate)) +
          statCell("Season", fmtRate(p.season_hit_rate)) +
        '</div>' +

        // AI model tag + Track button
        '<div class="px-4 pb-3 pt-1 mt-auto flex items-center justify-between gap-2">' +
          '<span class="text-[10px] text-gray-600 truncate">Model: ' + esc(p.model || "model") + '</span>' +
          trackButtonHTML(p, idx) +
        '</div>' +
      '</div>';
  }

  function isTracked(p, idx) {
    return (trackedOverride[idx] !== undefined)
      ? !!trackedOverride[idx]
      : !!p.tracked;
  }

  function trackButtonHTML(p, idx) {
    var tracked = isTracked(p, idx);
    var cls = "track-btn" + (tracked ? " is-tracked" : "");
    var label = tracked ? "Tracked ✓" : "Track";
    var dis = tracked ? " disabled" : "";
    return '<button class="' + cls + '" data-track-idx="' + idx +
      '"' + dis + '>' + label + '</button>';
  }

  function statCell(label, value) {
    return '<div class="rounded-lg bg-cardhi/60 py-1.5">' +
      '<div class="text-[13px] font-bold">' + esc(value) + '</div>' +
      '<div class="text-[10px] text-gray-500">' + esc(label) + '</div>' +
    '</div>';
  }

  // ── Filtering + sorting ───────────────────────────────────────────────
  function applyFilters() {
    var rows = ALL.map(function (p, i) { return { p: p, i: i }; });

    if (state.sport !== "all") {
      rows = rows.filter(function (r) {
        return (r.p.sport || "").toLowerCase() === state.sport;
      });
    }
    if (state.stat !== "all") {
      rows = rows.filter(function (r) { return r.p.stat_label === state.stat; });
    }
    // AND-stack the collapsible panel's six filters on top.
    rows = rows.filter(function (r) { return panelPasses(r.p); });

    rows.sort(function (a, b) {
      if (state.sort === "edge") {
        return (Number(b.p.edge) || 0) - (Number(a.p.edge) || 0);
      }
      if (state.sort === "time") {
        return String(a.p.commence_time || "").localeCompare(String(b.p.commence_time || ""));
      }
      // default: confidence desc
      return (Number(b.p.confidence) || 0) - (Number(a.p.confidence) || 0);
    });

    return rows;
  }

  function render() {
    var grid   = document.getElementById("card-grid");
    var groups = document.getElementById("game-groups");
    var empty  = document.getElementById("empty-state");
    var rows   = applyFilters();

    if (!rows.length) {
      grid.innerHTML = ""; if (groups) groups.innerHTML = "";
      grid.classList.add("hidden");
      if (groups) groups.classList.add("hidden");
      empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden");

    if (state.viewMode === "game" && groups) {
      grid.classList.add("hidden");
      groups.classList.remove("hidden");
      groups.innerHTML = renderByGameHTML(rows);
    } else {
      if (groups) groups.classList.add("hidden");
      grid.classList.remove("hidden");
      grid.innerHTML = rows.map(function (r) { return cardHTML(r.p, r.i); }).join("");
    }
  }

  // ── By-Game grouping (Phase 2d) ───────────────────────────────────────
  // Group the already-filtered/sorted rows by p.game_key, sort groups by
  // earliest commence_time, render one collapsible card per game.
  // No new data -- pure view-layer transform.
  function renderByGameHTML(rows) {
    var groups = {};
    var order = [];
    rows.forEach(function (r) {
      var k = r.p.game_key || "_";
      if (!groups[k]) {
        groups[k] = { rows: [], rep: r.p, earliest: r.p.commence_time || "9999" };
        order.push(k);
      }
      groups[k].rows.push(r);
      var ct = r.p.commence_time || "9999";
      if (ct < groups[k].earliest) {
        groups[k].earliest = ct;
        groups[k].rep      = r.p;     // earliest pick supplies the header data
      }
    });
    order.sort(function (a, b) {
      return String(groups[a].earliest).localeCompare(String(groups[b].earliest));
    });
    return order.map(function (k) { return groupHTML(k, groups[k]); }).join("");
  }

  function groupHTML(gkey, group) {
    var rep   = group.rep;
    var open  = !!expandedGames[gkey];
    var label = rep.game_label || "";
    var parts = label.split(" @ ");
    var away  = (parts[0] || "?").trim();
    var home  = (parts[1] || "?").trim();
    var n     = group.rows.length;
    var time  = etTime(rep.commence_time);
    var cards = group.rows.map(function (r) { return cardHTML(r.p, r.i); }).join("");
    return '' +
      '<div class="gg-group" data-game-key="' + esc(gkey) + '">' +
        '<div class="gg-header" data-gg-toggle="' + esc(gkey) + '"' +
              ' role="button" aria-expanded="' + (open ? "true" : "false") + '">' +
          '<span class="gg-abbr">' + esc(away) + '</span>' +
          '<span class="gg-at">@</span>' +
          '<span class="gg-abbr">' + esc(home) + '</span>' +
          '<span class="gg-spacer"></span>' +
          (time ? '<span class="gg-time">' + esc(time) + '</span>' : '') +
          '<span class="gg-count">' + n + ' prop' + (n !== 1 ? 's' : '') + '</span>' +
          '<svg class="gg-chev' + (open ? ' is-open' : '') +
            '" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">' +
            '<path d="M7 5l6 5-6 5V5z"/>' +
          '</svg>' +
        '</div>' +
        '<div class="gg-body"' + (open ? '' : ' style="display:none;"') + '>' +
          cards +
        '</div>' +
      '</div>';
  }

  // ISO UTC -> "7:05 PM ET" (matches NiceGUI _game_time_et).
  function etTime(iso) {
    if (!iso) return "";
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return "";
      var parts = new Intl.DateTimeFormat("en-US", {
        hour: "numeric", minute: "2-digit", timeZone: "America/New_York",
      }).formatToParts(d);
      var h = "", m = "", p = "";
      parts.forEach(function (pt) {
        if (pt.type === "hour")      h = pt.value;
        else if (pt.type === "minute")    m = pt.value;
        else if (pt.type === "dayPeriod") p = pt.value;
      });
      if (!h || !m) return "";
      return h + ":" + m + " " + p + " ET";
    } catch (e) { return ""; }
  }

  // ── Wiring ────────────────────────────────────────────────────────────
  function activateGroup(container, attr, value, activeCls, idleCls) {
    var btns = container.querySelectorAll("button");
    btns.forEach(function (b) {
      var on = b.getAttribute(attr) === value;
      activeCls.forEach(function (c) { b.classList.toggle(c, on); });
      idleCls.forEach(function (c) { b.classList.toggle(c, !on); });
    });
  }

  function init() {
    var sportPills = document.getElementById("sport-pills");
    sportPills.addEventListener("click", function (e) {
      var btn = e.target.closest("button[data-sport]");
      if (!btn) return;
      state.sport = btn.getAttribute("data-sport");
      activateGroup(sportPills, "data-sport", state.sport,
        ["bg-accent", "text-white"],
        ["bg-card", "text-gray-300", "border", "border-border"]);
      render();
    });

    var statPills = document.getElementById("stat-pills");
    statPills.addEventListener("click", function (e) {
      var btn = e.target.closest("button[data-stat]");
      if (!btn) return;
      state.stat = btn.getAttribute("data-stat");
      activateGroup(statPills, "data-stat", state.stat,
        ["border-accent", "text-white", "bg-accent/20"],
        ["border-border", "text-gray-300", "bg-card"]);
      render();
    });

    document.getElementById("sort-select").addEventListener("change", function (e) {
      state.sort = e.target.value;
      render();
    });

    // Card click delegation: shared between #card-grid (list view) and
    // #game-groups (By-Game view).  The same routing handles Over/Under and
    // Track button clicks no matter which container the card was rendered
    // inside, so swapping views never breaks the wiring.
    document.getElementById("card-grid").addEventListener("click", cardContainerClick);
    var groupsRoot = document.getElementById("game-groups");
    if (groupsRoot) {
      groupsRoot.addEventListener("click", function (e) {
        // Header click (chevron / toggle expand) -- routed first so a click
        // on the header background doesn't accidentally fall through to a
        // nested card handler.
        var header = e.target.closest("[data-gg-toggle]");
        if (header) {
          toggleGameGroup(header);
          return;
        }
        cardContainerClick(e);
      });
    }

    // ── Filter panel (Phase 2b) ───────────────────────────────────────
    initFilterPanel();

    // ── View-mode toggle (Phase 2d) ──────────────────────────────────
    initViewToggle();

    render();
  }

  function cardContainerClick(e) {
    var ouBtn = e.target.closest(".ou-btn");
    if (ouBtn) {
      var idx = ouBtn.getAttribute("data-idx");
      sideOverride[idx] = ouBtn.getAttribute("data-side");
      render();
      return;
    }
    var trackBtn = e.target.closest(".track-btn");
    if (trackBtn && !trackBtn.disabled) {
      onTrackClick(trackBtn);
      return;
    }
  }

  function toggleGameGroup(header) {
    var gkey = header.getAttribute("data-gg-toggle");
    if (!gkey) return;
    var willOpen = !expandedGames[gkey];
    expandedGames[gkey] = willOpen;
    // Inline DOM mutation (no full re-render) keeps the scroll position
    // and lets the chevron CSS transition play.
    header.setAttribute("aria-expanded", willOpen ? "true" : "false");
    var body = header.nextElementSibling;
    if (body) body.style.display = willOpen ? "" : "none";
    var chev = header.querySelector(".gg-chev");
    if (chev) chev.classList.toggle("is-open", willOpen);
  }

  function initViewToggle() {
    loadPersistedViewMode();
    syncViewPills();
    var row = document.getElementById("view-pills");
    if (!row) return;
    row.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-view]");
      if (!btn) return;
      var v = btn.getAttribute("data-view");
      if (v !== "list" && v !== "game") return;
      if (v === state.viewMode) return;
      state.viewMode = v;
      persistViewMode();
      syncViewPills();
      render();
    });
  }

  function syncViewPills() {
    var row = document.getElementById("view-pills");
    if (!row) return;
    row.querySelectorAll("[data-view]").forEach(function (b) {
      var on = b.getAttribute("data-view") === state.viewMode;
      b.classList.toggle("view-pill-active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  // ── Filter panel wiring ────────────────────────────────────────────────
  function syncPanelDOMFromState() {
    // Form controls
    var l10g = document.getElementById("f-min-l10");
    var cong = document.getElementById("f-min-conf");
    var grdg = document.getElementById("f-min-grade");
    var alt  = document.getElementById("f-show-alt");
    if (l10g) l10g.value = String(state.panel.min_l10);
    if (cong) cong.value = String(state.panel.min_conf);
    if (grdg) grdg.value = String(state.panel.min_grade);
    if (alt)  alt.checked = !!state.panel.show_alt;

    // Chip groups (markets, games)
    document.querySelectorAll("#f-markets [data-market-key]").forEach(function (b) {
      b.classList.toggle("active",
        state.panel.markets.indexOf(b.getAttribute("data-market-key")) !== -1);
    });
    document.querySelectorAll("#f-games [data-game-key]").forEach(function (b) {
      b.classList.toggle("active",
        state.panel.games.indexOf(b.getAttribute("data-game-key")) !== -1);
    });

    syncPanelBadge();
  }

  function syncPanelBadge() {
    var n = activePanelCount();
    var btn   = document.getElementById("filter-toggle");
    var count = document.getElementById("filter-count");
    var reset = document.getElementById("filter-reset");
    if (count) {
      if (n > 0) { count.textContent = "(" + n + ")"; count.classList.remove("hidden"); }
      else        { count.textContent = "";          count.classList.add("hidden"); }
    }
    if (btn)   btn.classList.toggle("has-active", n > 0);
    if (reset) reset.classList.toggle("hidden", n === 0);
  }

  function toggleArrayMember(arr, val) {
    var i = arr.indexOf(val);
    if (i === -1) arr.push(val);
    else          arr.splice(i, 1);
  }

  function onPanelChange() {
    persistPanel();
    syncPanelBadge();
    render();
  }

  function initFilterPanel() {
    loadPersistedPanel();
    syncPanelDOMFromState();

    var toggle = document.getElementById("filter-toggle");
    var panel  = document.getElementById("filter-panel");
    if (toggle && panel) {
      toggle.addEventListener("click", function () {
        var open = !panel.classList.contains("hidden");
        if (open) panel.classList.add("hidden");
        else      panel.classList.remove("hidden");
        toggle.setAttribute("aria-expanded", open ? "false" : "true");
      });
    }

    var reset = document.getElementById("filter-reset");
    if (reset) reset.addEventListener("click", function () {
      state.panel = defaultPanel();
      syncPanelDOMFromState();
      persistPanel();
      render();
    });

    var l10g = document.getElementById("f-min-l10");
    if (l10g) l10g.addEventListener("change", function (e) {
      state.panel.min_l10 = Number(e.target.value) || 0;
      onPanelChange();
    });
    var cong = document.getElementById("f-min-conf");
    if (cong) cong.addEventListener("change", function (e) {
      state.panel.min_conf = Number(e.target.value) || 0;
      onPanelChange();
    });
    var grdg = document.getElementById("f-min-grade");
    if (grdg) grdg.addEventListener("change", function (e) {
      state.panel.min_grade = Number(e.target.value) || 0;
      onPanelChange();
    });
    var alt = document.getElementById("f-show-alt");
    if (alt) alt.addEventListener("change", function (e) {
      state.panel.show_alt = !!e.target.checked;
      onPanelChange();
    });

    var marketsRow = document.getElementById("f-markets");
    if (marketsRow) marketsRow.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-market-key]");
      if (!btn) return;
      toggleArrayMember(state.panel.markets, btn.getAttribute("data-market-key"));
      btn.classList.toggle("active");
      onPanelChange();
    });

    var gamesRow = document.getElementById("f-games");
    if (gamesRow) gamesRow.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-game-key]");
      if (!btn) return;
      toggleArrayMember(state.panel.games, btn.getAttribute("data-game-key"));
      btn.classList.toggle("active");
      onPanelChange();
    });
  }

  // ── Track button (Phase 2c) ───────────────────────────────────────────
  // Optimistic flip: button reads as "Tracked ✓" immediately on click; the
  // POST runs in the background.  On success or 409 ("already tracked") we
  // keep the flipped state.  On any other failure we revert and toast the
  // error.  Uses the same /api/props/track endpoint + payload shape as the
  // NiceGUI props page so MyBets + ledger see picks identically.
  function onTrackClick(btn) {
    var idx = btn.getAttribute("data-track-idx");
    if (!idx || trackInflight[idx]) return;
    var p = ALL[Number(idx)];
    if (!p) return;

    // Optimistic flip
    trackedOverride[idx] = true;
    trackInflight[idx]   = true;
    btn.classList.add("is-tracked", "is-pending");
    btn.disabled = true;
    btn.textContent = "Tracked ✓";

    var payload = {
      player:          p.player || "",
      market:          p.market || "",
      line:            p.line,
      side:            sideOverride[idx] || p.side || "Over",
      odds:            p.best_odds,
      confidence:      p.confidence,
      predicted_value: p.predicted_value,
      team:            p.team || "",
      event_id:        p.event_id,
      commence_time:   p.commence_time || null,
    };

    fetch("/api/props/track", {
      method:  "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body:    JSON.stringify(payload),
    }).then(function (resp) {
      return resp.json().then(function (data) { return { resp: resp, data: data }; });
    }).then(function (r) {
      btn.classList.remove("is-pending");
      var data = r.data || {};
      if (r.resp.ok && data.success) {
        // Success -- show stake in the toast like the NiceGUI version does.
        var amt = (typeof data.amount === "number")
          ? " ($" + data.amount.toFixed(2) + ")" : "";
        toast("Tracked: " + (p.player || "") + " " +
              (payload.side || "") + " " + (p.line == null ? "" : p.line) + amt,
              "positive");
      } else if (r.resp.status === 409 ||
                 (data.error && /already tracked/i.test(data.error))) {
        // Server-side dedup -- keep the tracked state, info toast.
        toast("Already tracked.", "info");
      } else {
        // Real failure -- revert and surface the error.
        revertTrack(idx, btn);
        toast("Track failed: " + (data.error || ("HTTP " + r.resp.status)),
              "negative");
      }
    }).catch(function (err) {
      btn.classList.remove("is-pending");
      revertTrack(idx, btn);
      toast("Track failed: " + (err && err.message ? err.message : "network error"),
            "negative");
    }).finally(function () {
      trackInflight[idx] = false;
    });
  }

  function revertTrack(idx, btn) {
    trackedOverride[idx] = false;
    btn.classList.remove("is-tracked");
    btn.disabled = false;
    btn.textContent = "Track";
  }

  // Toast: simple bottom-right stack.  4s auto-dismiss; positive/info/negative
  // pick the left-border color.  No deps -- vanilla DOM.
  function toast(msg, kind) {
    var stack = document.getElementById("toast-stack");
    if (!stack) return;
    var el = document.createElement("div");
    el.className = "toast " + (kind || "info");
    el.textContent = String(msg == null ? "" : msg);
    stack.appendChild(el);
    // Trigger CSS transition on next frame so the slide-in plays.
    requestAnimationFrame(function () { el.classList.add("show"); });
    setTimeout(function () {
      el.classList.remove("show");
      setTimeout(function () {
        if (el.parentNode) el.parentNode.removeChild(el);
      }, 220);
    }, 4000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
