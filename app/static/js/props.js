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
  var state = { sport: "all", stat: "all", sort: "confidence" };

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

    render();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
