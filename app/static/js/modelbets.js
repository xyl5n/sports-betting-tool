/* modelbets.js -- client for the Flask + Tailwind Model Bets page.
 * Companion to templates/modelbets.html.  Read-only dashboard mirroring
 * pages/model.py: model bankroll, record-by-bet-type, today's model picks,
 * per-classifier accuracy.  No mutations -> no SBT.apiPost; the only network
 * call is the 60 s snapshot GET (a read, same pattern as mybets.js /
 * admin.js).  SBT.toast is used only to surface a snapshot-fetch error. */
(function () {
  "use strict";

  // ── State hydration ───────────────────────────────────────────────────
  var state = { vm: loadInitial() };

  function loadInitial() {
    try {
      var node = document.getElementById("modelbets-init");
      var vm = JSON.parse((node && node.textContent) || "{}");
      return (vm && typeof vm === "object") ? vm : emptyVm();
    } catch (e) { return emptyVm(); }
  }
  function emptyVm() {
    return {
      bankroll: {start: 0, current: 0, pnl: 0, pnl_sign: "+", pnl_abs: 0,
                 pnl_color: "dim", at_risk: 0, record_w: 0, record_l: 0,
                 record_pct: "—"},
      type_records: [],
      picks: {game_picks: [], prop_picks: [], game_count: 0, prop_count: 0},
      classifiers: [],
    };
  }

  // ── Helpers ───────────────────────────────────────────────────────────
  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;",
              '"': "&quot;", "'": "&#39;"}[c];
    });
  }
  function money(n) {
    var v = Number(n) || 0;
    return "$" + Math.abs(v).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  // ── 1. Bankroll ─────────────────────────────────────────────────────────
  function renderBankroll() {
    var b = state.vm.bankroll || emptyVm().bankroll;
    if (el("mb-bk-start"))   el("mb-bk-start").textContent   = money(b.start);
    if (el("mb-bk-current")) el("mb-bk-current").textContent = money(b.current);
    var pnl = el("mb-bk-pnl");
    if (pnl) {
      pnl.textContent = (b.pnl_sign || "+") + money(b.pnl_abs);
      pnl.className = "mb-stat-value mb-text-" + (b.pnl_color || "dim");
    }
    var rec = el("mb-bk-record");
    if (rec) {
      var pctTxt = (b.record_pct && b.record_pct !== "—") ? "  (" + b.record_pct + ")" : "";
      rec.textContent = "Record  " + (b.record_w || 0) + "-" + (b.record_l || 0) + pctTxt;
    }
    if (el("mb-bk-atrisk")) el("mb-bk-atrisk").textContent = "At Risk  " + money(b.at_risk);
  }

  // ── 2. Record by bet type ────────────────────────────────────────────────
  function renderTypeRecords() {
    var host = el("mb-type-records");
    if (!host) return;
    var rows = state.vm.type_records || [];
    host.innerHTML = rows.map(function (r) {
      var pctTxt = (r.pct && r.pct !== "—") ? "  (" + esc(r.pct) + ")" : "";
      return '<div class="mb-row">' +
        '<span class="mb-text-dim text-[12px]">' + esc(r.label) + '</span>' +
        '<span class="mb-text-text mb-mono text-[12px]">' +
          (r.wins || 0) + '-' + (r.losses || 0) + pctTxt + '</span></div>';
    }).join("");
  }

  // ── 3. Today's model picks ───────────────────────────────────────────────
  function renderPicks() {
    var picks = state.vm.picks || {};
    var games = picks.game_picks || [];
    var props = picks.prop_picks || [];

    if (el("mb-game-count")) el("mb-game-count").textContent = games.length;
    if (el("mb-prop-count")) el("mb-prop-count").textContent = props.length;

    var gameCard = el("mb-game-card");
    var propCard = el("mb-prop-card");
    if (gameCard) gameCard.style.display = games.length ? "" : "none";
    if (propCard) propCard.style.display = props.length ? "" : "none";

    var gHost = el("mb-game-picks");
    if (gHost) {
      gHost.innerHTML = games.length
        ? games.map(gamePickRow).join("")
        : '<div class="mb-empty">No game picks today.</div>';
    }
    var pHost = el("mb-prop-picks");
    if (pHost) {
      pHost.innerHTML = props.length
        ? props.map(propPickRow).join("")
        : '<div class="mb-empty">No prop picks today.</div>';
    }
  }

  function gamePickRow(g) {
    var below = g.below_threshold
      ? '<span class="text-[9px] font-extrabold tracking-wide mb-text-warn">BELOW THRESHOLD</span>'
      : '';
    return '<div class="mb-pick-row">' +
      '<span class="mb-rank">' + esc(g.rank) + '</span>' +
      '<div class="flex-1 min-w-0 flex flex-col gap-[2px]">' +
        '<span class="text-[13px] font-bold truncate mb-text-' + (g.team_color || "text") + '">' +
          esc(g.team) + '</span>' + below +
      '</div>' +
      '<span class="mb-badge">' + esc(g.sport) + '</span>' +
      '<div class="flex items-center gap-[10px] mb-mono">' +
        '<span class="text-[12px] font-bold mb-text-primary">' + g.prob + '%</span>' +
        '<span class="text-[11px] mb-text-dim">' + esc(g.odds_s) + '</span>' +
        '<span class="text-[12px] font-bold mb-text-' + (g.amount_color || "text") + '">' +
          esc(g.amount_text) + '</span>' +
      '</div></div>';
  }

  function propPickRow(p) {
    return '<div class="mb-pick-row">' +
      '<span class="mb-rank">' + esc(p.rank) + '</span>' +
      '<div class="flex-1 min-w-0 flex flex-col gap-[2px]">' +
        '<span class="text-[13px] font-bold truncate mb-text-text">' + esc(p.player) + '</span>' +
        '<span class="text-[10px] mb-text-dim">' + esc(p.market) + '</span>' +
      '</div>' +
      '<span class="text-[12px] font-bold mb-text-' + (p.side_color || "dim") + '">' +
        esc(p.side) + ' ' + esc(p.line_s) + '</span>' +
      '<div class="flex items-center gap-[10px] mb-mono">' +
        '<span class="text-[11px] mb-text-dim">proj ' + esc(p.pv_s) + '</span>' +
        '<span class="text-[12px] font-bold mb-text-primary">' + p.conf + '%</span>' +
        (p.odds_s ? '<span class="text-[11px] mb-text-dim">' + esc(p.odds_s) + '</span>' : '') +
      '</div></div>';
  }

  // ── 4. Classifier accuracy ───────────────────────────────────────────────
  function renderClassifiers() {
    var host = el("mb-classifiers");
    if (!host) return;
    var cs = state.vm.classifiers || [];
    if (!cs.length) {
      host.innerHTML = '<div class="mb-empty">No classifier data yet.</div>';
      return;
    }
    host.innerHTML = cs.map(function (c) {
      var cls = "mb-clf" + (c.is_best ? " best" : (c.is_worst ? " worst" : ""));
      var pctCls = c.is_best ? "mb-text-pos" : (c.is_worst ? "mb-text-neg" : "mb-text-text");
      var byCat = (c.by_cat || []).map(function (b) {
        return '<div class="flex justify-between text-[11px]">' +
          '<span class="mb-text-dim">' + esc(b.label) + '</span>' +
          '<span class="mb-mono mb-text-text">' + esc(b.pct) +
            ' <span class="mb-text-dim">(' + b.correct + '/' + b.total + ')</span></span>' +
        '</div>';
      }).join("");
      return '<div class="' + cls + '">' +
        '<div class="flex items-center justify-between">' +
          '<span class="text-[13px] font-extrabold">' + esc(c.label) + '</span>' +
          (c.is_best ? '<span class="mb-badge mb-text-pos">BEST</span>' :
           c.is_worst ? '<span class="mb-badge mb-text-neg">WORST</span>' : '') +
        '</div>' +
        '<div class="text-[24px] font-extrabold mb-mono ' + pctCls + '">' + esc(c.pct) + '</div>' +
        '<div class="text-[10.5px] mb-text-dim mb-mono">' + c.correct + '/' + c.total + ' correct</div>' +
        '<div class="flex flex-col gap-[2px] pt-1">' + byCat + '</div>' +
      '</div>';
    }).join("");
  }

  function renderAll() {
    renderBankroll();
    renderTypeRecords();
    renderPicks();
    renderClassifiers();
  }

  // ── 60 s snapshot poll (read; SBT.apiPost is POST-only) ─────────────────
  function refreshSnapshot() {
    fetch("/api/modelbets/snapshot", {headers: {"Accept": "application/json"}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (vm) {
        if (!vm || typeof vm !== "object") return;
        state.vm = vm;
        renderAll();
      })
      .catch(function () { /* transient -- next tick retries */ });
  }

  function init() {
    renderAll();
    setInterval(refreshSnapshot, 60000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
