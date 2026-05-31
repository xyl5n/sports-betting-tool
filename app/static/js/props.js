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

  // ── Filter state ──────────────────────────────────────────────────────
  // The first three fields drive the always-visible top bar (sport / stat
  // pills + sort dropdown).  The `panel` sub-object mirrors the NiceGUI
  // filter-bar state and AND-stacks with the top-bar filters via
  // panelPasses().  Persisted to localStorage so reloads restore both
  // surfaces; matches the NiceGUI _persist_filters semantics from
  // pages/props.py.
  var state = {
    sport: "all", stat: "all", sort: "confidence",
    panel: defaultPanel(),
  };

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

        // AI model tag
        '<div class="px-4 pb-3 pt-1 mt-auto">' +
          '<span class="text-[10px] text-gray-600">Model: ' + esc(p.model || "model") + '</span>' +
        '</div>' +
      '</div>';
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
    var grid = document.getElementById("card-grid");
    var empty = document.getElementById("empty-state");
    var rows = applyFilters();

    if (!rows.length) {
      grid.innerHTML = "";
      empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden");
    grid.innerHTML = rows.map(function (r) { return cardHTML(r.p, r.i); }).join("");
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

    // Over/Under toggle (event delegation on the grid since cards re-render).
    document.getElementById("card-grid").addEventListener("click", function (e) {
      var btn = e.target.closest(".ou-btn");
      if (!btn) return;
      var idx = btn.getAttribute("data-idx");
      sideOverride[idx] = btn.getAttribute("data-side");
      render();
    });

    // ── Filter panel (Phase 2b) ───────────────────────────────────────
    initFilterPanel();

    render();
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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
