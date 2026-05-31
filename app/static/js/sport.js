/* sport.js -- client for the Flask + Tailwind /sports/<sport> page.
 *
 * Hydrates from #sp-init.  Cards re-render from JSON so the 60s snapshot
 * poll (mirrors NiceGUI's live_score 60s timer) can drop in fresh live
 * scores and late-arriving model picks without a page reload.
 *
 * Date picker navigates via location change (`?date=YYYY-MM-DD`); the
 * server re-renders with the selected date so the JS stays stateless.
 * Track buttons forward to the pre-built track_url through SBT.apiPost
 * (same 3-way-aware pattern admin / mybets / top_picks use). */
(function () {
  "use strict";

  var STATE = { vm: loadInitial() };

  function loadInitial() {
    try {
      var node = document.getElementById("sp-init");
      var vm = JSON.parse((node && node.textContent) || "{}");
      return (vm && typeof vm === "object") ? vm : empty();
    } catch (e) { return empty(); }
  }
  function empty() {
    return {cards: [], sport: "mlb", date: "", is_today: true,
            empty_msg: "No games."};
  }

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;",
              '"': "&quot;", "'": "&#39;"}[c];
    });
  }

  // ── Render ────────────────────────────────────────────────────────────
  function cardHTML(c) {
    var sportChip = '<span class="sp-chip">' + esc(STATE.vm.sport_upper || "") + '</span>';
    var timeLbl = c.when_label
      ? '<span class="sp-time">' + esc(c.when_label) + '</span>' : '';
    var matchupPill = c.matchup_url
      ? '<a href="' + esc(c.matchup_url) + '" class="sp-matchup-pill">Matchup</a>' : '';

    var stateBadge = '';
    if (c.state === "live") {
      stateBadge = '<span class="sp-state-live"><span class="sp-livedot"></span>LIVE</span>';
    } else if (c.state === "final") {
      stateBadge = '<span class="sp-state-final">FINAL</span>';
    }

    var teams =
      '<div class="sp-teams">' +
        '<span class="sp-team away">' + esc(c.away_team) + '</span>' +
        '<span class="sp-vs">@</span>' +
        '<span class="sp-team home">' + esc(c.home_team) + '</span>' +
      '</div>';

    var scoreLine = '';
    if (c.score_label) {
      var stateCls = c.state === "live" ? " live" : (c.state === "final" ? " final" : "");
      scoreLine = '<div class="sp-score' + stateCls + '">' + esc(c.score_label) + '</div>';
    }

    var pickColor = c.pick_color || "dim";
    var pickLine =
      '<div class="sp-pick-line sp-text-' + pickColor + '">' +
        esc(c.pick_summary) + '</div>';

    var trackBtn = '';
    if (c.track_url) {
      trackBtn =
        '<button class="sp-track-btn"' +
          ' data-track-url="' + esc(c.track_url) + '"' +
          ' data-track-body=\'' + esc(JSON.stringify(c.track_body || {})) + '\'' +
          ' data-track-label="' + esc(c.track_label || (c.away_team + " @ " + c.home_team)) +
          '">Track</button>';
    }
    var detailsLink = c.matchup_url
      ? '<a href="' + esc(c.matchup_url) + '" class="sp-details">View Details &rarr;</a>'
      : '<span></span>';

    return '<div class="sp-card">' +
      '<div class="sp-meta">' + sportChip + timeLbl + matchupPill + stateBadge + '</div>' +
      teams +
      scoreLine +
      pickLine +
      '<div class="sp-bottom">' + detailsLink + trackBtn + '</div>' +
    '</div>';
  }

  function renderGrid() {
    var grid = el("sp-grid");
    var emptyEl = el("sp-empty");
    if (!grid) return;
    var cards = (STATE.vm.cards || []);
    if (!cards.length) {
      grid.innerHTML = "";
      if (emptyEl) emptyEl.classList.remove("hidden");
      return;
    }
    if (emptyEl) emptyEl.classList.add("hidden");
    grid.innerHTML = cards.map(cardHTML).join("");
  }

  // ── 60s snapshot poll ────────────────────────────────────────────────
  // GET read -- SBT.apiPost is POST-only; raw fetch is correct here.
  // Mirrors NiceGUI's live_score 60s timer so live scores + late analysis
  // rows populate without a reload.
  function refresh() {
    var url = "/api/sports/" + encodeURIComponent(STATE.vm.sport) +
              "/snapshot?date=" + encodeURIComponent(STATE.vm.date);
    fetch(url, {headers: {"Accept": "application/json"}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (vm) {
        if (!vm || typeof vm !== "object" || vm.error) return;
        STATE.vm = vm;
        renderGrid();
      })
      .catch(function () { /* transient -- next tick retries */ });
  }

  // ── Wiring ────────────────────────────────────────────────────────────
  function onDateChange(e) {
    var v = (e.target.value || "").trim();
    if (!v) return;
    var sport = e.target.getAttribute("data-sport") || STATE.vm.sport || "mlb";
    window.location = "/sports/" + encodeURIComponent(sport) +
                      "?date=" + encodeURIComponent(v);
  }

  function onGridClick(e) {
    var btn = e.target.closest(".sp-track-btn");
    if (!btn || btn.disabled) return;
    e.stopPropagation();
    onTrack(btn);
  }

  function onTrack(btn) {
    var url = btn.getAttribute("data-track-url");
    var bodyAttr = btn.getAttribute("data-track-body") || "{}";
    var body;
    try { body = JSON.parse(bodyAttr) || {}; } catch (e) { body = {}; }
    // Game-pick endpoints want a bankroll; match the default the rest of
    // the migrated track dispatch uses (250 MLB / 1000 WNBA).
    if (!body.bankroll) {
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
    renderGrid();
    var dateInput = el("sp-date");
    var grid = el("sp-grid");
    if (dateInput) dateInput.addEventListener("change", onDateChange);
    if (grid) grid.addEventListener("click", onGridClick);
    setInterval(refresh, 60000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
