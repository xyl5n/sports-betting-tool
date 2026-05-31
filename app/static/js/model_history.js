/* model_history.js -- client for the Flask + Tailwind single-model pick
 * history page (/model-history/<sport>/<model>).  Companion to
 * templates/model_history.html.  Read-only, user-driven: preset pills + a
 * native date input change the timeframe, which re-fetches the view model
 * from GET /api/model-history/<sport>/<model> and re-renders.
 *
 * No mutations -> no SBT.apiPost.  No auto-poll (this page is user-driven by
 * design, matching the NiceGUI original).  The one fetch is the GET timeframe
 * read; SBT.toast surfaces a fetch error. */
(function () {
  "use strict";

  var state = { vm: loadInitial() };

  function loadInitial() {
    try {
      var node = document.getElementById("mh-init");
      var vm = JSON.parse((node && node.textContent) || "{}");
      return (vm && typeof vm === "object") ? vm : emptyVm();
    } catch (e) { return emptyVm(); }
  }
  function emptyVm() {
    return {
      sport: "mlb", model: "combined", label: "Today",
      active: {mode: "preset", preset: "today", date: null},
      presets: [{key: "today", label: "Today"},
                {key: "yesterday", label: "Yesterday"},
                {key: "7d", label: "Last 7 Days"},
                {key: "30d", label: "Last 30 Days"}],
      record: {wins: 0, losses: 0, voids: 0, pct_s: "—", rec_color: "dim", void_s: ""},
      counts: {total: 0, finished: 0, pending: 0},
      picks: [],
    };
  }

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;",
              '"': "&quot;", "'": "&#39;"}[c];
    });
  }

  // ── Render ───────────────────────────────────────────────────────────
  function render() {
    var vm = state.vm;

    var title = el("mh-title");
    if (title) {
      title.textContent = (vm.model || "").toUpperCase() + " · " +
        (vm.sport || "").toUpperCase() + " — PICK HISTORY";
    }

    renderPills();
    syncDateInput();
    renderRecord();
    renderTable();
  }

  function renderPills() {
    var host = el("mh-pills");
    if (!host) return;
    var active = state.vm.active || {};
    host.innerHTML = (state.vm.presets || []).map(function (p) {
      var on = (active.mode === "preset" && active.preset === p.key);
      return '<button type="button" class="mh-pill' + (on ? " active" : "") +
        '" data-preset="' + esc(p.key) + '">' + esc(p.label) + '</button>';
    }).join("");
  }

  function syncDateInput() {
    var inp = el("mh-date");
    if (!inp) return;
    var active = state.vm.active || {};
    // Reflect a custom-day selection in the input; clear it when a preset is active.
    inp.value = (active.mode === "date" && active.date) ? active.date : "";
  }

  function renderRecord() {
    var r = state.vm.record || {};
    var c = state.vm.counts || {};
    var sub = el("mh-rec-sub");
    if (sub) {
      sub.textContent = (state.vm.model || "") + " · " +
        (state.vm.sport || "").toUpperCase() + " · " + (state.vm.label || "");
    }
    var val = el("mh-rec-value");
    if (val) {
      val.textContent = (r.wins || 0) + "-" + (r.losses || 0) + (r.void_s || "") +
        ", " + (r.pct_s || "—");
      val.className = "text-[22px] font-extrabold font-mono mh-text-" + (r.rec_color || "dim");
    }
    var counts = el("mh-rec-counts");
    if (counts) {
      counts.textContent = (c.total || 0) + " pick(s) in this timeframe (" +
        (c.finished || 0) + " finished, " + (c.pending || 0) + " pending)";
    }
  }

  function renderTable() {
    var wrap = el("mh-table-wrap");
    var empty = el("mh-empty");
    var picks = state.vm.picks || [];
    if (!picks.length) {
      if (wrap) wrap.innerHTML = "";
      if (empty) empty.classList.remove("hidden");
      return;
    }
    if (empty) empty.classList.add("hidden");
    var head = '<tr>' +
      ['Made', 'Player / Matchup', 'Bet', 'Side', 'Line', 'Conf', 'Status', 'Result']
        .map(function (h, i) {
          var r = (i === 4 || i === 5) ? " class='r'" : "";
          return '<th' + r + '>' + h + '</th>';
        }).join("") + '</tr>';
    var body = picks.map(function (p) {
      return '<tr>' +
        '<td class="mh-text-dim">' + esc(p.made) + '</td>' +
        '<td>' + esc(p.who) + '</td>' +
        '<td class="mh-text-dim">' + esc(p.bet) + '</td>' +
        '<td>' + esc(p.side) + '</td>' +
        '<td class="r">' + esc(p.line_s) + '</td>' +
        '<td class="r">' + esc(p.conf_s) + '</td>' +
        '<td class="mh-text-dim">' + esc(p.status) + '</td>' +
        '<td class="mh-text-' + (p.result_color || "dim") + '" style="font-weight:800;">' +
          esc(p.result_text) + '</td>' +
      '</tr>';
    }).join("");
    if (wrap) {
      wrap.innerHTML = '<table class="mh-table"><thead>' + head +
        '</thead><tbody>' + body + '</tbody></table>';
    }
  }

  // ── Timeframe change -> GET new view model ───────────────────────────
  function fetchTimeframe(query) {
    var base = "/api/model-history/" + encodeURIComponent(state.vm.sport) +
      "/" + encodeURIComponent(state.vm.model);
    // GET read -- SBT.apiPost is POST-only; raw fetch is correct here
    fetch(base + query, {headers: {"Accept": "application/json"}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (vm) {
        if (!vm || typeof vm !== "object" || vm.error) {
          SBT.toast("Could not load that timeframe.", "negative");
          return;
        }
        state.vm = vm;
        render();
      })
      .catch(function () { SBT.toast("Network error loading timeframe.", "negative"); });
  }

  function init() {
    render();

    var pills = el("mh-pills");
    if (pills) pills.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-preset]");
      if (!btn) return;
      fetchTimeframe("?preset=" + encodeURIComponent(btn.getAttribute("data-preset")));
    });

    var dateIn = el("mh-date");
    if (dateIn) dateIn.addEventListener("change", function () {
      if (dateIn.value) fetchTimeframe("?date=" + encodeURIComponent(dateIn.value));
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
