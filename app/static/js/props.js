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

  // Swipe-mode dismissed set (in-memory; matches NiceGUI's closure-local
  // `dismissed: set`).  Both right-swipe (track + dismiss) and left-swipe
  // (dismiss-only) add the prop's key here so it's hidden from the deck for
  // the rest of the session.  Resets on page reload.
  var dismissedSwipe = {};

  function propSwipeKey(p) {
    return (p.player || "") + "|" + (p.market || "") + "|" +
           (p.line == null ? "" : p.line) + "|" + (p.side || "");
  }

  var VIEW_MODE_LS_KEY = "propsViewMode";

  function loadPersistedViewMode() {
    try {
      var v = window.localStorage.getItem(VIEW_MODE_LS_KEY);
      if (v === "list" || v === "game" || v === "xray" || v === "swipe") {
        state.viewMode = v;
      }
    } catch (e) { /* ignore */ }
  }

  function persistViewMode() {
    try { window.localStorage.setItem(VIEW_MODE_LS_KEY, state.viewMode); }
    catch (e) { /* ignore */ }
  }

  // ── X-Ray sort state (Phase 2e) ───────────────────────────────────────
  // Persisted under its own localStorage key so a user who toggles into
  // X-Ray once sees the same column sorted on the next visit.  Defaults
  // match NiceGUI's _xray_sort closure: col=conf, asc=False.
  state.xraySort = { col: "conf", asc: false };
  var XRAY_SORT_LS_KEY = "propsXraySort";
  var XRAY_SORT_COLS = ["player","line","odds","conf","ev","l5","l10","szn"];

  function loadPersistedXraySort() {
    try {
      var raw = window.localStorage.getItem(XRAY_SORT_LS_KEY);
      if (!raw) return;
      var blob = JSON.parse(raw);
      if (blob && XRAY_SORT_COLS.indexOf(blob.col) !== -1) {
        state.xraySort.col = blob.col;
        state.xraySort.asc = !!blob.asc;
      }
    } catch (e) { /* corrupted blob -> defaults */ }
  }

  function persistXraySort() {
    try {
      window.localStorage.setItem(
        XRAY_SORT_LS_KEY,
        JSON.stringify({ col: state.xraySort.col, asc: state.xraySort.asc })
      );
    } catch (e) { /* ignore */ }
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

    var playerSlug = (p.player || "").toLowerCase().replace(/ /g, "-");

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
            '<a href="/player/' + esc(p.sport) + '/' + esc(playerSlug) + '" class="player-name-link">' +
              '<div class="font-bold text-[15px] truncate">' + esc(p.player || "Unknown") + '</div>' +
            '</a>' +
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
    var xray   = document.getElementById("xray-table");
    var swipe  = document.getElementById("swipe-stage");
    var empty  = document.getElementById("empty-state");
    var rows   = applyFilters();

    // Swipe mode always renders (even when the filtered list is empty) so
    // the user can see the "All caught up!" state.  Other view modes hide
    // the grid + show the no-filter-matches empty state.
    if (!rows.length && state.viewMode !== "swipe") {
      grid.innerHTML = "";
      if (groups) groups.innerHTML = "";
      if (xray)   xray.innerHTML = "";
      if (swipe)  swipe.innerHTML = "";
      grid.classList.add("hidden");
      if (groups) groups.classList.add("hidden");
      if (xray)   xray.classList.add("hidden");
      if (swipe)  swipe.classList.add("hidden");
      empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden");

    if (state.viewMode === "game" && groups) {
      grid.classList.add("hidden");
      if (xray)  xray.classList.add("hidden");
      if (swipe) swipe.classList.add("hidden");
      groups.classList.remove("hidden");
      groups.innerHTML = renderByGameHTML(rows);
    } else if (state.viewMode === "xray" && xray) {
      grid.classList.add("hidden");
      if (groups) groups.classList.add("hidden");
      if (swipe)  swipe.classList.add("hidden");
      xray.classList.remove("hidden");
      xray.innerHTML = renderXrayHTML(rows);
    } else if (state.viewMode === "swipe" && swipe) {
      grid.classList.add("hidden");
      if (groups) groups.classList.add("hidden");
      if (xray)   xray.classList.add("hidden");
      swipe.classList.remove("hidden");
      swipe.innerHTML = renderSwipeHTML(rows);
      attachSwipeGestures();
    } else {
      if (groups) groups.classList.add("hidden");
      if (xray)   xray.classList.add("hidden");
      if (swipe)  swipe.classList.add("hidden");
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

  // ── X-Ray data table (Phase 2e) ────────────────────────────────────────
  // Condensed, sortable, one-prop-per-row.  Reads from the same applyFilters()
  // output the list / by-game views use; sort is its own dimension layered on
  // top so the user's primary "sort dropdown" doesn't fight the column sort.

  // Column defs: [header label, sort key (null = no sort), is-right-align, css class]
  var XRAY_COLS = [
    ["PLAYER", "player", false, "xr-col-player"],
    ["LINE",   "line",   true,  "xr-col-line"],
    ["ODDS",   "odds",   true,  "xr-col-odds"],
    ["CONF",   "conf",   true,  "xr-col-conf"],
    ["EV",     "ev",     true,  "xr-col-ev"],
    ["L5",     "l5",     true,  "xr-col-l5"],
    ["L10",    "l10",    true,  "xr-col-l10"],
    ["SZN",    "szn",    true,  "xr-col-szn"],
    ["TRACK",  null,     true,  "xr-col-track"],
  ];

  function xraySortRows(rows) {
    var col = state.xraySort.col || "conf";
    var asc = !!state.xraySort.asc;
    var keyFn = xraySortKey(col);
    var copy = rows.slice();
    copy.sort(function (a, b) {
      var ka = keyFn(a.p), kb = keyFn(b.p);
      if (ka < kb) return asc ? -1 :  1;
      if (ka > kb) return asc ?  1 : -1;
      return 0;
    });
    return copy;
  }

  function xraySortKey(col) {
    // Numeric columns return Number; player returns string lower-case.
    // Missing values sort to the bottom in desc order (matches NiceGUI's
    // -inf / -1 sentinels).
    if (col === "player") {
      return function (p) { return (p.player || "").toLowerCase(); };
    }
    if (col === "line") {
      return function (p) { var v = Number(p.line); return isFinite(v) ? v : 0; };
    }
    if (col === "odds") {
      return function (p) {
        var v = Number(p.best_odds); return isFinite(v) ? v : -9999;
      };
    }
    if (col === "conf") {
      return function (p) { return Number(p.confidence) || 0; };
    }
    if (col === "ev") {
      return function (p) {
        var v = p.ev_pct;
        if (v === null || v === undefined) return -Infinity;
        var n = Number(v); return isFinite(n) ? n : -Infinity;
      };
    }
    if (col === "l5") {
      return function (p) {
        if (!p.l5_games) return -1;
        return Number(p.l5_hits) / Number(p.l5_games);
      };
    }
    if (col === "l10") {
      return function (p) {
        if (!p.l10_games) return -1;
        return Number(p.l10_hits) / Number(p.l10_games);
      };
    }
    if (col === "szn") {
      return function (p) {
        var v = p.season_avg;
        if (v === null || v === undefined) return -1;
        var n = Number(v); return isFinite(n) ? n : -1;
      };
    }
    return function () { return 0; };
  }

  function xrayHeaderHTML() {
    var asc      = !!state.xraySort.asc;
    var activeCl = state.xraySort.col || "conf";
    var cells = XRAY_COLS.map(function (col) {
      var label = col[0], key = col[1], right = col[2], cls = col[3];
      var isActive = !!(key && key === activeCl);
      var arrow = isActive ? (asc ? " ▲" : " ▼") : "";
      var hClass = "xr-cell xr-h " + cls +
                   (right ? " right" : "") +
                   (key ? " sortable" : "") +
                   (isActive ? " is-active" : "");
      var attrs = key ? (' data-xr-sort="' + key + '"') : "";
      return '<div class="' + hClass + '"' + attrs + '>' +
             esc(label) + esc(arrow) + '</div>';
    }).join("");
    return '<div class="xr-row xr-header">' + cells + '</div>';
  }

  function renderXrayHTML(rows) {
    var sorted = xraySortRows(rows);
    var headerHTML = xrayHeaderHTML();
    var body = sorted.map(function (r, i) {
      return xrayRowHTML(r.p, r.i, i % 2 === 1);
    }).join("");
    return '<div class="xr-wrap">' + headerHTML + body + '</div>';
  }

  // Per-cell formatters (kept inline because they're only used here).
  function oddsStr(o) {
    if (o == null) return "—";
    var n = Number(o);
    if (!isFinite(n)) return String(o);
    n = Math.trunc(n);
    return (n > 0 ? "+" : "") + n;
  }

  function confColorClass(conf) {
    var c = Number(conf) || 0;
    if (c >= 0.65) return "xr-conf-good";
    if (c >= 0.55) return "xr-conf-mid";
    return "xr-conf-dim";
  }

  function evLabel(ev) {
    if (ev == null) return { text: "— EV", cls: "xr-ev-dim" };
    var v = Number(ev);
    if (!isFinite(v)) return { text: "— EV", cls: "xr-ev-dim" };
    var sign = v > 0 ? "+" : (v < 0 ? "-" : "");
    var text = sign + Math.abs(v).toFixed(1) + "% EV";
    var cls  = v > 0 ? "xr-ev-pos" : (v < 0 ? "xr-ev-neg" : "xr-ev-dim");
    return { text: text, cls: cls };
  }

  // Hit-rate cell background -- thresholds match NiceGUI _hr_cell_bg.
  function hrCellStyle(pct) {
    if (pct >= 0.70) return { bg: "#22c55e", colored: true };
    if (pct >= 0.55) return { bg: "#84cc16", colored: true };
    if (pct >= 0.40) return { bg: "",        colored: false };
    return { bg: "#ef4444", colored: true };
  }

  function roiColorClass(roiStr) {
    if (!roiStr) return "xr-roi-dim";
    var n = parseFloat(String(roiStr).replace("%", ""));
    if (!isFinite(n)) return "xr-roi-dim";
    return n > 0 ? "xr-roi-pos" : (n < 0 ? "xr-roi-neg" : "xr-roi-dim");
  }

  function avatarHTML(p) {
    if (p.headshot) {
      return '<img src="' + esc(p.headshot) +
        '" alt="" class="xr-avatar" loading="lazy"' +
        ' onerror="this.outerHTML=\'<div class=&quot;xr-avatar&quot;>⚾</div>\';">';
    }
    return '<div class="xr-avatar">⚾</div>';
  }

  function hrCellHTML(hits, games, roiStr) {
    if (!games) {
      return '<span class="xr-hr-dash">—</span>';
    }
    var pct = hits / games;
    var sty = hrCellStyle(pct);
    var lbl = hits + "/" + games + " · " + Math.round(pct * 100) + "%";
    var pillHTML = sty.colored
      ? '<span class="xr-hr-pill" style="background:' + sty.bg + '">' + esc(lbl) + '</span>'
      : '<span class="xr-hr-plain">' + esc(lbl) + '</span>';
    var roiHTML = roiStr
      ? '<span class="xr-roi ' + roiColorClass(roiStr) + '">' + esc(roiStr) + '</span>'
      : '';
    return pillHTML + roiHTML;
  }

  function xrayRowHTML(p, idx, alt) {
    var side    = sideOverride[idx] || p.side || "Over";
    var isOver  = side === "Over";
    var slug    = (p.player || "").toLowerCase().replace(/ /g, "-");
    var sport   = (p.sport || "mlb").toLowerCase();

    // PLAYER
    var playerCell =
      '<div class="xr-cell xr-col-player">' +
        avatarHTML(p) +
        '<div style="display:flex;flex-direction:column;min-width:0;margin-left:8px;">' +
          '<a class="xr-player-name" href="/player/' + esc(sport) + '/' + esc(slug) + '">' +
            esc(p.player || "—") +
          '</a>' +
          '<span class="xr-player-sub">' + esc((p.stat_label || "").toUpperCase()) + '</span>' +
        '</div>' +
      '</div>';

    // LINE
    var lineCell =
      '<div class="xr-cell xr-col-line center">' +
        '<span class="xr-line-pill ' + (isOver ? "over" : "under") + '">' +
          (isOver ? "O " : "U ") + esc(p.line == null ? "—" : p.line) +
        '</span>' +
      '</div>';

    // ODDS
    var oddsCell =
      '<div class="xr-cell xr-col-odds center">' +
        '<span class="xr-odds">' + esc(oddsStr(p.best_odds)) + '</span>' +
      '</div>';

    // CONF
    var confPct = Math.round((Number(p.confidence) || 0) * 100);
    var confCell =
      '<div class="xr-cell xr-col-conf center">' +
        '<span class="xr-conf ' + confColorClass(p.confidence) + '">' + confPct + '%</span>' +
      '</div>';

    // EV
    var ev = evLabel(p.ev_pct);
    var evCell =
      '<div class="xr-cell xr-col-ev center">' +
        '<span class="xr-ev-chip ' + ev.cls + '">' + esc(ev.text) + '</span>' +
      '</div>';

    // L5 + L10
    var l5Cell =
      '<div class="xr-cell xr-col-l5 col-stack">' +
        hrCellHTML(p.l5_hits, p.l5_games, p.l5_roi) +
      '</div>';
    var l10Cell =
      '<div class="xr-cell xr-col-l10 col-stack">' +
        hrCellHTML(p.l10_hits, p.l10_games, p.l10_roi) +
      '</div>';

    // SZN
    var szn = (p.season_avg == null) ? "—" : Number(p.season_avg).toFixed(2);
    var sznCell =
      '<div class="xr-cell xr-col-szn col-stack">' +
        '<span class="xr-szn">' + esc(szn) + '</span>' +
        (p.szn_roi
          ? '<span class="xr-roi ' + roiColorClass(p.szn_roi) + '">' + esc(p.szn_roi) + '</span>'
          : '') +
      '</div>';

    // TRACK -- reuse the same button + click delegation as the list/grouped views
    var trackCell =
      '<div class="xr-cell xr-col-track right">' +
        trackButtonHTML(p, idx) +
      '</div>';

    var cls = "xr-row xr-data" + (alt ? " xr-alt" : "");
    return '<div class="' + cls + '">' +
      playerCell + lineCell + oddsCell + confCell + evCell +
      l5Cell + l10Cell + sznCell + trackCell +
    '</div>';
  }

  // ── Swipe mode (Phase 2f) ─────────────────────────────────────────────
  // One card at a time, drag-to-track / drag-to-dismiss.  Reuses the same
  // prop card HTML the list view renders -- the card's own Track button
  // stays visible inside the swipe wrap.  Three input paths converge on
  // the same outcome:
  //   right swipe (≥80 px)  → trackProp(p, idx, {afterAttempt: advance})
  //   big ✓ button          → trackProp(p, idx, {afterAttempt: advance})
  //   left swipe (≥80 px)   → advance only (no POST)
  //   big ✗ button          → advance only (no POST)
  //   card's own Track btn  → trackProp(p, idx, {btnEl}) -- no advance
  //
  // Dismissed set is in-memory (matches NiceGUI) so a reload resets the
  // deck.  Gesture math: ±15° rotation at 480 px, opacity floor 0.6,
  // 80 px threshold for commit, 300 ms fly-out before the after-action.

  function visibleSwipeRows() {
    // applyFilters() output minus already-dismissed picks.  Source of
    // truth for "what card is on top" and the X reviewed / Y remaining
    // counter.
    return applyFilters().filter(function (r) {
      return !dismissedSwipe[propSwipeKey(r.p)];
    });
  }

  function renderSwipeHTML(allRows) {
    // allRows is the raw applyFilters() result; for the counter we want
    // the *post-dismiss* deck.  The list passed into render() can be
    // empty for swipe mode (filters knocked everything out) -- that path
    // is handled by render() before we get here, so allRows.length > 0.
    var deck = allRows.filter(function (r) {
      return !dismissedSwipe[propSwipeKey(r.p)];
    });
    if (!deck.length) {
      // Empty state: either the user has reviewed everything, or filters
      // killed the slate.  We can tell the difference by checking allRows.
      var reviewed = allRows.length;
      var msg = reviewed
        ? "You've reviewed all " + reviewed + " prop" +
          (reviewed === 1 ? "" : "s") + " in the current filter."
        : "No props match the current filter.";
      return '' +
        '<div class="sw-empty">' +
          '<div class="sw-empty-icon">✓</div>' +
          '<div class="sw-empty-title">All caught up!</div>' +
          '<div class="sw-empty-body">' + esc(msg) + '</div>' +
          (reviewed
            ? '<button type="button" class="sw-reset-btn" data-sw-reset="1">Reset deck</button>'
            : '') +
        '</div>';
    }

    var head      = deck[0];
    var reviewed  = allRows.length - deck.length;
    var remaining = deck.length;
    var card      = cardHTML(head.p, head.i);

    return '' +
      '<div class="sw-counter">' +
        reviewed + ' reviewed · ' + remaining + ' remaining' +
      '</div>' +
      '<div class="sw-stage" data-sw-key="' + esc(propSwipeKey(head.p)) +
                                              '" data-sw-idx="' + head.i + '">' +
        '<div class="sw-hint"></div>' +
        '<div class="sw-card-wrap">' +
          card +
        '</div>' +
      '</div>' +
      '<div class="sw-buttons">' +
        '<button type="button" class="sw-btn sw-btn-dismiss"' +
          ' aria-label="Dismiss">✗</button>' +
        '<button type="button" class="sw-btn sw-btn-track"' +
          ' aria-label="Track">✓</button>' +
      '</div>';
  }

  function swipeAdvance(swKey) {
    dismissedSwipe[swKey] = true;
    render();
  }

  function swipeResetDeck() {
    dismissedSwipe = {};
    render();
  }

  // Resolve the prop currently on top of the deck from the rendered DOM
  // (single source of truth -- avoids drift between render() and click /
  // gesture handlers).
  function currentSwipeCard() {
    var stage = document.querySelector("#swipe-stage .sw-stage");
    if (!stage) return null;
    var key = stage.getAttribute("data-sw-key");
    var idx = Number(stage.getAttribute("data-sw-idx"));
    if (!isFinite(idx)) return null;
    return { p: ALL[idx], i: idx, swKey: key };
  }

  // ── Gesture handler ─────────────────────────────────────────────────────
  // Pointer Events unify mouse + touch + pen; setPointerCapture keeps the
  // drag live even if the finger leaves the card.  We pull the threshold,
  // rotation, and fade-out constants up here so they're easy to tune.
  var SW_THRESHOLD = 80;           // px to commit a swipe
  var SW_MAX_ROTATE = 15;          // degrees at full drag
  var SW_ROT_RANGE  = 480;         // px that maps to SW_MAX_ROTATE
  var SW_FADE_RANGE = 200;         // px that maps to opacity floor
  var SW_FADE_FLOOR = 0.6;
  var SW_FLY_MS     = 300;         // fly-out animation duration

  function attachSwipeGestures() {
    var wrap = document.querySelector("#swipe-stage .sw-card-wrap");
    var hint = document.querySelector("#swipe-stage .sw-hint");
    if (!wrap) return;

    var dragging = false, pointerId = null, startX = 0, dx = 0, committed = false;

    function setHint(delta) {
      if (!hint) return;
      var abs = Math.abs(delta);
      var ratio = Math.min(abs / SW_THRESHOLD, 1);
      hint.style.opacity = String(ratio * 0.9);
      if (delta > 0) {
        hint.textContent = "✓";
        hint.style.color = "#22c55e";
      } else if (delta < 0) {
        hint.textContent = "✗";
        hint.style.color = "#ef4444";
      } else {
        hint.textContent = "";
      }
    }

    function applyDrag(delta) {
      var deg = (delta / SW_ROT_RANGE) * SW_MAX_ROTATE;
      var op  = 1 - Math.min(Math.abs(delta) / SW_FADE_RANGE,
                             1 - SW_FADE_FLOOR);
      wrap.style.transform = "translate(" + delta + "px, 0) rotate(" + deg + "deg)";
      wrap.style.opacity   = String(op);
      setHint(delta);
    }

    function resetVisuals() {
      wrap.style.transform = "";
      wrap.style.opacity   = "";
      if (hint) hint.style.opacity = "0";
    }

    wrap.addEventListener("pointerdown", function (e) {
      if (dragging || committed) return;
      // Don't start a drag from a button click -- the card's own Track
      // button and the OU pills need to receive their own clicks.
      if (e.target.closest("button, a")) return;
      dragging  = true;
      pointerId = e.pointerId;
      startX    = e.clientX;
      dx        = 0;
      wrap.classList.add("is-dragging");
      try { wrap.setPointerCapture(pointerId); } catch (_) {}
    });

    wrap.addEventListener("pointermove", function (e) {
      if (!dragging || e.pointerId !== pointerId) return;
      dx = e.clientX - startX;
      applyDrag(dx);
    });

    function finishDrag(commit) {
      if (!dragging) return;
      dragging = false;
      wrap.classList.remove("is-dragging");
      try { wrap.releasePointerCapture(pointerId); } catch (_) {}
      if (!commit || Math.abs(dx) < SW_THRESHOLD) {
        resetVisuals();
        return;
      }
      // Commit: fly out, then trigger track/dismiss + advance.
      committed = true;
      var dir = dx > 0 ? 1 : -1;
      var endX = dir * Math.max(window.innerWidth, 600);
      var endDeg = dir * (SW_MAX_ROTATE + 10);
      wrap.style.transform = "translate(" + endX + "px, 0) rotate(" + endDeg + "deg)";
      wrap.style.opacity   = "0";
      var cur = currentSwipeCard();
      setTimeout(function () {
        if (!cur) return;
        if (dir > 0) {
          trackProp(cur.p, cur.i, {
            afterAttempt: function () { swipeAdvance(cur.swKey); },
          });
        } else {
          swipeAdvance(cur.swKey);
        }
      }, SW_FLY_MS);
    }

    wrap.addEventListener("pointerup",     function (e) {
      if (!dragging || e.pointerId !== pointerId) return;
      finishDrag(true);
    });
    wrap.addEventListener("pointercancel", function (e) {
      if (!dragging || e.pointerId !== pointerId) return;
      finishDrag(false);
    });
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
    var xrayRoot = document.getElementById("xray-table");
    if (xrayRoot) {
      xrayRoot.addEventListener("click", function (e) {
        // Sortable header click -- handled first so the row-level Track
        // delegation can't accidentally swallow header clicks if a Track
        // button ever lands in a header (it doesn't today, but be safe).
        var sortHdr = e.target.closest("[data-xr-sort]");
        if (sortHdr) {
          onXraySortClick(sortHdr.getAttribute("data-xr-sort"));
          return;
        }
        cardContainerClick(e);
      });
    }
    var swipeRoot = document.getElementById("swipe-stage");
    if (swipeRoot) {
      swipeRoot.addEventListener("click", function (e) {
        // Big ✓ (track + advance) and ✗ (dismiss + advance) buttons.
        if (e.target.closest(".sw-btn-track")) {
          var cur = currentSwipeCard();
          if (!cur) return;
          trackProp(cur.p, cur.i, {
            afterAttempt: function () { swipeAdvance(cur.swKey); },
          });
          return;
        }
        if (e.target.closest(".sw-btn-dismiss")) {
          var cur2 = currentSwipeCard();
          if (cur2) swipeAdvance(cur2.swKey);
          return;
        }
        if (e.target.closest("[data-sw-reset]")) {
          swipeResetDeck();
          return;
        }
        // Any other click (the card's own Track btn, OU pills) falls
        // through to the shared card delegation.
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
    loadPersistedXraySort();
    syncViewPills();
    var row = document.getElementById("view-pills");
    if (!row) return;
    row.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-view]");
      if (!btn) return;
      var v = btn.getAttribute("data-view");
      if (v !== "list" && v !== "game" && v !== "xray" && v !== "swipe") return;
      if (v === state.viewMode) return;
      state.viewMode = v;
      persistViewMode();
      syncViewPills();
      render();
    });
  }

  function onXraySortClick(col) {
    if (XRAY_SORT_COLS.indexOf(col) === -1) return;
    var s = state.xraySort;
    if (s.col === col) {
      s.asc = !s.asc;
    } else {
      s.col = col;
      // Player → ascending by default (A–Z); numeric columns → descending.
      s.asc = (col === "player");
    }
    persistXraySort();
    render();
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
  // DOM-resolver wrapper: pulls p + idx from the clicked button element,
  // then hands off to the shared trackProp() body.  Used by the standard
  // Track button in list / by-game / x-ray views (no afterAttempt callback;
  // the button's own optimistic flip provides the user feedback).
  function onTrackClick(btn) {
    var idx = btn.getAttribute("data-track-idx");
    if (idx == null) return;
    var i = Number(idx);
    if (!isFinite(i)) return;
    var p = ALL[i];
    if (!p) return;
    trackProp(p, i, { btnEl: btn });
  }

  // Shared POST + toast + revert body.  Used by:
  //   1. onTrackClick(btn)                -- standard Track button (passes btnEl)
  //   2. swipe-right gesture / ✓ button   -- swipe mode (passes afterAttempt)
  //   3. anywhere else that needs to track a prop programmatically
  //
  // The fetch + 409-dedup + revert-on-error state machine lives in
  // /static/js/lib.js's SBT.apiPost; this function is the thin wrapper
  // that supplies the track-specific payload, success toast text, and
  // page-local state (trackedOverride / trackInflight per-index maps).
  //
  // opts:
  //   btnEl        -- if provided, the button gets the optimistic
  //                   .is-tracked + .is-pending classes (visible feedback
  //                   on the card); on real failure the button is
  //                   reverted via revertTrack
  //   afterAttempt -- callback fired regardless of outcome; swipe mode
  //                   uses this to advance to the next card after either
  //                   success, dedup, or failure
  function trackProp(p, idx, opts) {
    opts = opts || {};
    if (trackInflight[idx]) {
      if (opts.afterAttempt) opts.afterAttempt();
      return;
    }
    // Optimistic state flip -- always update the override so re-renders in
    // any view show the tracked state; the visible-button flip is opt-in
    // because swipe mode's card flies away anyway.
    trackedOverride[idx] = true;
    trackInflight[idx]   = true;
    var btn = opts.btnEl;
    if (btn) btn.classList.add("is-tracked");

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
    var sideTxt = payload.side;
    var lineTxt = (p.line == null ? "" : p.line);

    SBT.apiPost("/api/props/track", payload, {
      btn:          btn,
      pendingClass: "is-pending",
      pendingLabel: "Tracked ✓",
      onSuccess: function (data) {
        var amt = (typeof data.amount === "number")
          ? " ($" + data.amount.toFixed(2) + ")" : "";
        SBT.toast(
          "Tracked: " + (p.player || "") + " " + sideTxt + " " + lineTxt + amt,
          "positive"
        );
      },
      onDedup: function () {
        SBT.toast("Already tracked.", "info");
      },
      onError: function (err, data, status) {
        revertTrack(idx, btn);
        var msg = (data && data.error)
          || (err && err.message)
          || (status ? ("HTTP " + status) : "network error");
        SBT.toast("Track failed: " + msg, "negative");
      },
      afterAttempt: function () {
        trackInflight[idx] = false;
        if (opts.afterAttempt) opts.afterAttempt();
      },
    });
  }

  function revertTrack(idx, btn) {
    trackedOverride[idx] = false;
    if (btn) {
      btn.classList.remove("is-tracked");
      btn.disabled = false;
      btn.textContent = "Track";
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
