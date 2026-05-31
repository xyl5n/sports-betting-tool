/* top_picks.js -- client for the Flask + Tailwind /top-picks page.
 *
 * Hydrates from #tp-init.  Filter pills are client-side (no fetch).
 * 20s snapshot poll re-renders rows + scorecard so pending AI verdicts
 * populate without a page reload (mirrors NiceGUI's ui.timer(20.0)).
 *
 * Card click navigates to /player/mlb/<slug> (props) or /matchup/<sport>/
 * <gid> (game picks) -- both already-migrated.  Track button does the same
 * 3-way SBT.apiPost dispatch as admin/mybets/game_detail/player, with the
 * track_url + track_body pre-built server-side (no per-bet-type branching
 * in the client). */
(function () {
  "use strict";

  var STATE = {
    vm: loadInitial(),
    filter: "all",
  };

  function loadInitial() {
    try {
      var node = document.getElementById("tp-init");
      var vm = JSON.parse((node && node.textContent) || "{}");
      return (vm && typeof vm === "object") ? vm : empty();
    } catch (e) { return empty(); }
  }
  function empty() {
    return {rows: [], filters: [], scorecard: {}};
  }

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;",
              '"': "&quot;", "'": "&#39;"}[c];
    });
  }

  // ── Render ────────────────────────────────────────────────────────────
  function renderScorecard() {
    var sc = STATE.vm.scorecard || {};
    var pct = el("tp-pct"), rec = el("tp-record"), un = el("tp-units");
    if (pct) {
      pct.textContent = sc.pct_str || "—";
      pct.className = "text-[26px] font-extrabold font-mono tp-text-" + (sc.pct_color || "dim");
    }
    if (rec) rec.textContent = sc.record || "0-0";
    if (un) {
      un.textContent = sc.units_str || "0.00u";
      un.className = "text-[26px] font-extrabold font-mono tp-text-" + (sc.units_color || "dim");
    }
  }

  function filteredRows() {
    var rows = STATE.vm.rows || [];
    if (STATE.filter === "game")  return rows.filter(function (r) { return r.kind === "game"; });
    if (STATE.filter === "props") return rows.filter(function (r) { return r.kind === "prop"; });
    return rows;
  }

  function renderList() {
    var list = el("tp-list");
    var emptyEl = el("tp-empty");
    if (!list) return;
    var rows = filteredRows();
    if (!rows.length) {
      list.innerHTML = "";
      if (emptyEl) emptyEl.classList.remove("hidden");
      return;
    }
    if (emptyEl) emptyEl.classList.add("hidden");
    // The display rank is the row's original rank from the server; renumbering
    // after filter would lie about combined-score order, so we keep r.rank.
    list.innerHTML = rows.map(cardHTML).join("");
  }

  function cardHTML(r) {
    var outline = "tp-border-" + (r.outline_color || "border");
    var clickAttr = r.href
      ? ' data-card-href="' + esc(r.href) + '"'
      : "";
    var clickCls = r.href ? " clickable" : "";

    var verdictBadge =
      '<span class="tp-chip tp-bg-' + (r.verdict_color || "dim") + '">' +
        esc(r.verdict_label) + '</span>';
    var confBadge =
      '<span class="text-[10.5px] font-bold font-mono tp-text-dim">Model ' + r.conf_pct + '%</span>';
    var agreeBadge = r.agree ? '<span class="tp-chip tp-chip-agree">MODEL + AI AGREE</span>' : "";
    var fadeBadge  = r.fade  ? '<span class="tp-chip tp-chip-fade">AI FADE</span>' : "";

    var body;
    if (r.pending) {
      body = '<div class="flex items-center gap-1.5 text-[11.5px] italic tp-text-dim2">' +
        '<span class="tp-spinner"></span> AI analysis generating…</div>';
    } else if (r.reasoning) {
      body = '<div class="text-[12px] tp-text-dim leading-relaxed">' + esc(r.reasoning) + '</div>';
    } else {
      body = '';
    }

    // Track button: pre-built server-side so client just forwards via SBT.apiPost.
    var trackBtn = "";
    if (r.track_url) {
      trackBtn = '<div class="flex justify-end mt-0.5" data-track-stop="1">' +
        '<button class="tp-track-btn"' +
          ' data-track-url="' + esc(r.track_url) + '"' +
          ' data-track-body=\'' + esc(JSON.stringify(r.track_body || {})) + '\'' +
          ' data-track-kind="' + esc(r.kind) + '"' +
          ' data-track-label="' + esc(r.name) + '">Track</button>' +
      '</div>';
    }

    return '<div class="tp-card ' + outline + clickCls + '"' + clickAttr + '>' +
      '<div class="flex items-center gap-2.5">' +
        '<span class="text-[14px] font-extrabold font-mono tp-text-dim2 flex-shrink-0" style="min-width:30px;">#' + r.rank + '</span>' +
        '<div class="flex-1 min-w-0 flex flex-col gap-[1px]">' +
          '<span class="text-[14.5px] font-extrabold tp-text-text truncate">' + esc(r.name) + '</span>' +
          '<span class="text-[11px] font-mono tp-text-dim">' +
            esc(r.pick_type) + ' · ' + esc(r.side) + '</span>' +
        '</div>' +
        '<div class="flex flex-col items-end gap-[1px] flex-shrink-0">' +
          '<span class="text-[18px] font-extrabold font-mono tp-text-' + (r.verdict_color || "dim") + '">' +
            r.combined_pct + '%</span>' +
          '<span class="text-[8.5px] font-extrabold tracking-wider tp-text-dim2">' +
            esc(r.model_version_label) + '</span>' +
        '</div>' +
      '</div>' +
      '<div class="flex items-center flex-wrap gap-2">' + verdictBadge + confBadge +
        agreeBadge + fadeBadge + '</div>' +
      body + trackBtn +
    '</div>';
  }

  function renderAll() {
    renderScorecard();
    renderList();
  }

  // ── 20 s snapshot poll ────────────────────────────────────────────────
  // GET read -- SBT.apiPost is POST-only; raw fetch is correct here.
  // Mirrors NiceGUI's ui.timer(20.0) so pending AI verdicts populate
  // without a page reload.
  function refresh() {
    fetch("/api/top-picks/snapshot", {headers: {"Accept": "application/json"}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (vm) {
        if (!vm || typeof vm !== "object" || vm.error) return;
        STATE.vm = vm;
        renderAll();
      })
      .catch(function () { /* transient -- next tick retries */ });
  }

  // ── Wiring ────────────────────────────────────────────────────────────
  function onFilterClick(e) {
    var btn = e.target.closest("[data-filter]");
    if (!btn) return;
    var key = btn.getAttribute("data-filter");
    if (key === STATE.filter) return;
    STATE.filter = key;
    var filters = el("tp-filters");
    if (filters) filters.querySelectorAll("[data-filter]").forEach(function (b) {
      var on = b === btn;
      b.classList.toggle("active", on);
      b.classList.toggle("inactive", !on);
    });
    renderList();
  }

  function onListClick(e) {
    // Track button -- short-circuit so it doesn't navigate the card.
    var trackBtn = e.target.closest(".tp-track-btn");
    if (trackBtn && !trackBtn.disabled) {
      e.stopPropagation();
      onTrack(trackBtn);
      return;
    }
    // Anything else inside .track-stop also short-circuits.
    if (e.target.closest("[data-track-stop]")) {
      e.stopPropagation();
      return;
    }
    // Card click -> navigate (if href present).
    var card = e.target.closest("[data-card-href]");
    if (card) {
      window.location = card.getAttribute("data-card-href");
    }
  }

  function onTrack(btn) {
    var url = btn.getAttribute("data-track-url");
    var bodyAttr = btn.getAttribute("data-track-body") || "{}";
    var body;
    try { body = JSON.parse(bodyAttr) || {}; } catch (e) { body = {}; }
    // Game-pick endpoints want a bankroll; props don't.  Match the default
    // the rest of the migrated track-dispatch uses (250 MLB / 1000 WNBA).
    var kind = btn.getAttribute("data-track-kind");
    if (kind === "game" && !body.bankroll) {
      body.bankroll = (url.indexOf("/wnba/") >= 0) ? 1000 : 250;
    }
    var label = btn.getAttribute("data-track-label") || "";
    SBT.apiPost(url, body, {
      btn: btn,
      pendingClass: "is-pending",
      pendingLabel: "Tracked ✓",
      onSuccess: function (data) {
        btn.classList.add("is-tracked");
        var amt = (typeof data.amount === "number")
          ? " ($" + data.amount.toFixed(2) + ")"
          : (typeof data.stake === "number"
              ? " ($" + data.stake.toFixed(2) + ")" : "");
        SBT.toast("Tracked: " + label + amt, "positive");
      },
      onDedup: function () {
        btn.classList.add("is-tracked");
        SBT.toast("Already tracked.", "info");
      },
      onError: function (err, data, status) {
        btn.disabled = false;
        btn.textContent = "Track";
        btn.classList.remove("is-tracked");
        var msg = (data && data.error) || (err && err.message) ||
          (status ? ("HTTP " + status) : "network error");
        SBT.toast("Track failed: " + msg, "negative");
      },
    });
  }

  function init() {
    renderAll();
    var filters = el("tp-filters");
    var list = el("tp-list");
    if (filters) filters.addEventListener("click", onFilterClick);
    if (list)    list.addEventListener("click", onListClick);
    setInterval(refresh, 20000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
