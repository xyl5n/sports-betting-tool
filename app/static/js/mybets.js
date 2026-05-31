/* mybets.js -- client for the Flask + Tailwind My Bets page (PR #339).
 *
 * Companion to app/templates/mybets.html.  All POSTs go through
 * SBT.apiPost (lib.js); all toasts go through SBT.toast.  No local fetch
 * or toast bodies anywhere in this file.
 *
 * Sections wired here:
 *   1. Add Bet button     -- opens the modal wizard
 *   2. Bankroll hero      -- snapshot poll overwrites the spans
 *   3. Open / settled list -- JS-rendered from state.vm; edit + remove
 *                             icons delegated via #mb-open-list /
 *                             #mb-settled-list
 *   4. Today's Recommendations -- 5+5 paged with "Show more"; Track
 *                             button per row dispatches to the
 *                             pre-built track_url / track_body the
 *                             view model emitted (one of /api/props/track,
 *                             /api/ledger/track_prop, or
 *                             /api/ledger/confirm/<gid>)
 *   5. Add Bet modal      -- 6-step wizard; final step POSTs
 *                             /api/mybets/add and reloads */
(function () {
  "use strict";

  // ── Boot: hydrate state from the JSON island ─────────────────────────
  var state = {
    vm:        loadInitialVm(),
    page:      0,           // recommendations pagination cursor
    modal:     null,        // null when closed; otherwise an object below
    bankroll:  0,           // current personal bankroll (used by modal Kelly)
  };

  var PAGE_SIZE = 5;

  function loadInitialVm() {
    try {
      var node = document.getElementById("mybets-data");
      var vm = JSON.parse((node && node.textContent) || "{}");
      return vm && typeof vm === "object" ? vm : emptyVm();
    } catch (e) { return emptyVm(); }
  }

  function emptyVm() {
    return {
      bankroll:     {start: 0, current: 0, pnl: 0, pnl_sign: "+", pnl_abs: 0,
                     pnl_color: "dim", at_risk: 0, budget_total: 0,
                     budget_max: 0, budget_remaining: 0, remaining_color: "dim"},
      open_bets: [], settled_bets: [], open_count: 0, settled_count: 0,
      rec_games: [], rec_props: [], rec_total: 0,
    };
  }

  // ── Helpers ──────────────────────────────────────────────────────────
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;",
              '"': "&quot;", "'": "&#39;"}[c];
    });
  }
  function fmtMoney(n, sign) {
    var v = Number(n) || 0;
    var s = (sign === "+" && v >= 0) ? "+" : (sign === "−" && v < 0) ? "−" : "";
    return s + "$" + Math.abs(v).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  function fmtBudget(n) {
    var v = Math.round(Number(n) || 0);
    return "$" + v.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  function el(id) { return document.getElementById(id); }
  function show(node, visible) {
    if (!node) return;
    if (visible) node.classList.remove("hidden");
    else         node.classList.add("hidden");
  }

  // ── Render: bankroll + budget bar ────────────────────────────────────
  function renderBankroll() {
    var b = state.vm.bankroll || emptyVm().bankroll;
    var pnl = el("mb-pnl");
    if (pnl) {
      pnl.textContent = b.pnl_sign + "$" +
        Math.abs(Number(b.pnl_abs) || 0).toFixed(2)
          .replace(/\B(?=(\d{3})+(?!\d))/g, ",");
      pnl.className = "mb-stat-value mb-text-" + (b.pnl_color || "dim");
    }
    var ar = el("mb-at-risk");
    if (ar) ar.textContent = fmtMoney(b.at_risk);
    var bl = el("mb-budget-line");
    if (bl) {
      bl.textContent = "Today's Budget: " + fmtBudget(b.budget_total) +
        " total / " + fmtBudget(b.budget_max) + " max per bet";
    }
    var br = el("mb-budget-remaining");
    if (br) {
      br.textContent = fmtBudget(b.budget_remaining) + " left today";
      br.className = "text-xs font-extrabold font-mono whitespace-nowrap mb-text-" +
        (b.remaining_color || "dim");
    }
    state.bankroll = Number(b.current) || 0;
  }

  // ── Render: open + settled bets lists ───────────────────────────────
  function renderBets() {
    var openList    = el("mb-open-list");
    var openEmpty   = el("mb-open-empty");
    var openCount   = el("mb-open-count");
    var settledList = el("mb-settled-list");
    var settledEmpty = el("mb-settled-empty");
    var settledCount = el("mb-settled-count");

    var ob = state.vm.open_bets || [];
    var sb = state.vm.settled_bets || [];
    if (openCount)    openCount.textContent    = ob.length;
    if (settledCount) settledCount.textContent = sb.length;
    show(openEmpty,    ob.length === 0);
    show(settledEmpty, sb.length === 0);

    if (openList)    openList.innerHTML    = ob.map(betCardHTML).join("");
    if (settledList) settledList.innerHTML = sb.map(betCardHTML).join("");
  }

  function betCardHTML(b) {
    var border = "mb-border-" + (b.border_color || "border");
    var pickColor = "mb-text-" + (b.pick_color || "text");
    var statusColor = "mb-text-" + (b.status_color || "dim");
    var dataAttrs = ' data-bet-kind="' + esc(b.kind) +
                    '" data-bet-id="' + esc(b.id) +
                    '" data-bet-sport="' + esc(b.sport) + '"';

    var moneyHTML;
    if (b.kind === "prop" && Array.isArray(b.money_lines) && b.money_lines.length) {
      moneyHTML = b.money_lines.map(function (m, i) {
        var cls = (i === 0)
          ? 'text-[13px] font-bold font-mono mb-text-' + (m.color || "text")
          : 'text-[10px] font-semibold font-mono mb-text-' + (m.color || "dim");
        return '<span class="' + cls + '">' + esc(m.text) + '</span>';
      }).join("");
    } else {
      moneyHTML = '<span class="text-[13px] font-bold font-mono mb-text-' +
        (b.money_color || "text") + '">' + esc(b.money_text || "") + '</span>';
    }

    return '<div class="mb-bet-row border ' + border + ' flex flex-col gap-0"' + dataAttrs + '>' +
      '<div class="flex items-start gap-3">' +
        '<span class="bg-cardhi text-gray-400 text-[9.5px] font-extrabold tracking-wide ' +
          'px-2 py-[2px] rounded-full shrink-0 mt-[2px]">' + esc(b.badge || "") + '</span>' +
        '<div class="flex-1 min-w-0 flex flex-col gap-[2px]">' +
          '<div class="text-[16px] font-extrabold truncate ' + pickColor + '">' +
            esc(b.pick || "—") + '</div>' +
          (b.sub_line
            ? '<div class="text-[11px] text-gray-400 font-mono break-words">' +
              esc(b.sub_line) + '</div>'
            : '') +
          (b.extra_line
            ? '<div class="text-[11px] text-gray-400 font-mono break-words">' +
              esc(b.extra_line) + '</div>'
            : '') +
          (b.game_dt
            ? '<div class="text-[11px] text-gray-400 font-mono break-words">' +
              esc(b.game_dt) + '</div>'
            : '') +
        '</div>' +
        '<div class="flex flex-col items-end gap-[2px] shrink-0">' +
          moneyHTML +
          '<span class="text-[10.5px] font-extrabold tracking-wide ' + statusColor + '">' +
            esc(b.status_text || "") + '</span>' +
        '</div>' +
        '<div class="flex items-center gap-1 shrink-0 ml-1">' +
          '<button class="mb-icon-btn neutral mb-edit-btn" title="Edit this bet">✎</button>' +
          '<button class="mb-icon-btn danger  mb-remove-btn" title="Remove this bet">✕</button>' +
        '</div>' +
      '</div>' +
      '<div class="mb-edit-panel hidden mt-2 pt-2 border-t border-border flex flex-col gap-2"></div>' +
    '</div>';
  }

  function editPanelHTML(b) {
    // The inline edit form.  Fields mirror NiceGUI's _edit_panel: odds,
    // line (when has_line), confidence (open bets), amount (open game
    // bets), actual_payout (settled bets).
    var isProp = b.kind === "prop";
    var hasLine = !!b.has_line;
    var hasConf = !b.settled;
    var hasAmount = (!isProp) && (!b.settled);
    var hasPayout = !!b.settled;

    function input(label, name, value, opts) {
      opts = opts || {};
      var step = opts.step || "1";
      return '<label class="flex-1 flex flex-col gap-[2px] min-w-0">' +
        '<span class="text-[9px] font-extrabold tracking-wide text-gray-500 uppercase">' +
          esc(label) + '</span>' +
        '<input type="number" step="' + step + '" name="' + name +
          '" value="' + (value == null ? "" : esc(value)) +
          '" class="mb-input" />' +
      '</label>';
    }

    var fields = [];
    fields.push(input("Odds", "odds", b.odds, {step: "1"}));
    if (hasLine)   fields.push(input("Line", "line", b.line, {step: "0.1"}));
    if (hasConf)   fields.push(input("Confidence (%)", "confidence",
                                     b.confidence_pct, {step: "1"}));
    if (hasAmount) fields.push(input("Bet amount ($)", "amount",
                                     b.amount, {step: "0.01"}));
    if (hasPayout) fields.push(input("Actual payout ($)", "actual_payout",
                                     b.actual_payout, {step: "0.01"}));

    return '<div class="flex flex-wrap gap-2">' + fields.join("") + '</div>' +
      '<div class="flex justify-end gap-2">' +
        '<button type="button" class="text-xs text-gray-400 font-semibold px-2 py-1 mb-edit-cancel">Cancel</button>' +
        '<button type="button" class="bg-accent text-black text-xs font-extrabold ' +
          'px-3 py-1 rounded mb-edit-save">Save</button>' +
      '</div>';
  }

  // ── Render: recommendations (5+5 paged) ─────────────────────────────
  function renderRecs() {
    var games = state.vm.rec_games || [];
    var props = state.vm.rec_props || [];
    var total = games.length + props.length;
    var totalEl = el("mb-rec-total");
    if (totalEl) totalEl.textContent = total;

    show(el("mb-rec-empty"), total === 0);
    show(el("mb-rec-games-wrap"), games.length > 0);
    show(el("mb-rec-props-wrap"), props.length > 0);

    var nGamePages = ceilPages(games.length);
    var nPropPages = ceilPages(props.length);
    var nPages = Math.max(nGamePages, nPropPages, 1);
    if (state.page >= nPages) state.page = 0;

    // Each list independently wraps so the page is never empty.
    var gStart = (state.page % nGamePages) * PAGE_SIZE;
    var pStart = (state.page % nPropPages) * PAGE_SIZE;
    var gameSlice = games.slice(gStart, gStart + PAGE_SIZE);
    var propSlice = props.slice(pStart, pStart + PAGE_SIZE);

    var gCountEl = el("mb-rec-games-count");
    var pCountEl = el("mb-rec-props-count");
    if (gCountEl) gCountEl.textContent = games.length;
    if (pCountEl) pCountEl.textContent = props.length;

    var gList = el("mb-rec-games-list");
    var pList = el("mb-rec-props-list");
    if (gList) gList.innerHTML = gameSlice.map(recGameHTML).join("");
    if (pList) pList.innerHTML = propSlice.map(recPropHTML).join("");

    var moreBtn = el("mb-rec-more");
    if (moreBtn) {
      if (nPages > 1) {
        moreBtn.textContent = "Show more · " + (state.page + 1) + "/" + nPages;
        show(moreBtn, true);
      } else {
        show(moreBtn, false);
      }
    }
  }

  function ceilPages(n) {
    if (n <= 0) return 1;
    return Math.max(1, Math.ceil(n / PAGE_SIZE));
  }

  function recGameHTML(p) {
    return '<div class="mb-rec-row flex items-start gap-3"' +
        ' data-rec-kind="game" data-rec-sport="' + esc(p.sport) + '"' +
        ' data-rec-game-id="' + esc(p.game_id) + '"' +
        ' data-rec-bet-type="' + esc(p.bet_type) + '">' +
      '<span class="bg-cardhi text-gray-400 text-[9.5px] font-extrabold ' +
        'tracking-wide px-2 py-[2px] rounded-full shrink-0 mt-[2px]">' +
        esc(p.sport.toUpperCase()) + '</span>' +
      '<div class="flex-1 min-w-0 flex flex-col gap-[2px]">' +
        '<div class="text-[13px] font-extrabold text-white truncate">' +
          esc(p.team) + '</div>' +
        '<div class="text-[11px] text-gray-400 font-mono break-words">' +
          esc(p.detail) + '  ·  ' + esc(p.conf_str) + '  ·  ' + esc(p.matchup) +
        '</div>' +
      '</div>' +
      '<button class="bg-accent text-black text-[10.5px] font-extrabold ' +
        'tracking-wide px-3 py-1 rounded mb-rec-track shrink-0">Track</button>' +
    '</div>';
  }

  function recPropHTML(p) {
    return '<div class="mb-rec-row flex items-start gap-3"' +
        ' data-rec-kind="prop">' +
      '<span class="bg-cardhi text-gray-400 text-[9.5px] font-extrabold ' +
        'tracking-wide px-2 py-[2px] rounded-full shrink-0 mt-[2px]">PROP</span>' +
      '<div class="flex-1 min-w-0 flex flex-col gap-[2px]">' +
        '<div class="text-[13px] font-extrabold text-white truncate">' +
          esc(p.player) + '</div>' +
        '<div class="text-[11px] text-gray-400 font-mono break-words">' +
          esc(p.detail) + '  ·  ' + esc(p.conf_str) + '  ·  ' + esc(p.matchup) +
        '</div>' +
      '</div>' +
      '<button class="bg-accent text-black text-[10.5px] font-extrabold ' +
        'tracking-wide px-3 py-1 rounded mb-rec-track shrink-0">Track</button>' +
    '</div>';
  }

  function renderAll() {
    renderBankroll();
    renderBets();
    renderRecs();
  }

  // ── 60 s snapshot poll ─────────────────────────────────────────────
  function refreshSnapshot() {
    fetch("/api/mybets/snapshot", {headers: {"Accept": "application/json"}})
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (vm) {
        if (!vm || typeof vm !== "object") return;
        state.vm = vm;
        renderAll();
      })
      .catch(function () { /* network blip -- next tick will retry */ });
  }

  // ── Edit / remove delegation (shared between open + settled lists) ──
  function findBetRow(target) {
    return target && target.closest("[data-bet-id]");
  }
  function lookupBetByRow(row) {
    if (!row) return null;
    var id = row.getAttribute("data-bet-id");
    var lists = [state.vm.open_bets || [], state.vm.settled_bets || []];
    for (var i = 0; i < lists.length; i++) {
      for (var j = 0; j < lists[i].length; j++) {
        if (String(lists[i][j].id) === String(id)) return lists[i][j];
      }
    }
    return null;
  }

  function onListClick(e) {
    var row = findBetRow(e.target);
    if (!row) return;
    var b = lookupBetByRow(row);
    if (!b) return;

    if (e.target.closest(".mb-edit-btn"))   { toggleEditPanel(row, b); return; }
    if (e.target.closest(".mb-remove-btn")) { onRemove(row, b); return; }
    if (e.target.closest(".mb-edit-cancel")){ closeEditPanel(row); return; }
    if (e.target.closest(".mb-edit-save"))  { onSaveEdit(row, b); return; }
  }

  function toggleEditPanel(row, b) {
    var panel = row.querySelector(".mb-edit-panel");
    if (!panel) return;
    if (!panel.classList.contains("hidden")) {
      closeEditPanel(row);
      return;
    }
    panel.innerHTML = editPanelHTML(b);
    panel.classList.remove("hidden");
  }

  function closeEditPanel(row) {
    var panel = row.querySelector(".mb-edit-panel");
    if (!panel) return;
    panel.classList.add("hidden");
    panel.innerHTML = "";
  }

  function onSaveEdit(row, b) {
    var panel = row.querySelector(".mb-edit-panel");
    if (!panel) return;
    var inputs = panel.querySelectorAll("input[name]");
    var body = {
      kind:  b.kind,
      id:    b.id,
      sport: b.sport,
    };
    inputs.forEach(function (inp) {
      if (inp.value === "" || inp.value == null) return;
      var v = Number(inp.value);
      if (!isFinite(v)) return;
      body[inp.name] = v;
    });
    var saveBtn = panel.querySelector(".mb-edit-save");
    SBT.apiPost("/api/mybets/edit", body, {
      btn:          saveBtn,
      pendingClass: "opacity-60",
      onSuccess: function () {
        SBT.toast("Saved", "positive");
        closeEditPanel(row);
        refreshSnapshot();
      },
      onError: function (err, data, status) {
        if (saveBtn) saveBtn.disabled = false;
        SBT.toast("Save failed: " + (
          (data && data.error) || (err && err.message) ||
          (status ? ("HTTP " + status) : "network error")
        ), "negative");
      },
    });
  }

  function onRemove(row, b) {
    if (!window.confirm("Remove this bet?")) return;
    var removeBtn = row.querySelector(".mb-remove-btn");
    SBT.apiPost("/api/mybets/remove",
      {kind: b.kind, id: b.id, sport: b.sport},
      {
        btn:          removeBtn,
        pendingClass: "opacity-50",
        onSuccess: function () {
          SBT.toast("Bet removed", "positive");
          refreshSnapshot();
        },
        onError: function (err, data, status) {
          if (removeBtn) removeBtn.disabled = false;
          SBT.toast("Remove failed: " + (
            (data && data.error) || (err && err.message) ||
            (status ? ("HTTP " + status) : "network error")
          ), "negative");
        },
      });
  }

  // ── Recommendation Track buttons (3-way dispatch) ─────────────────
  function onRecClick(e) {
    var btn = e.target.closest(".mb-rec-track");
    if (!btn) return;
    var row = btn.closest("[data-rec-kind]");
    if (!row) return;
    var kind = row.getAttribute("data-rec-kind");

    // Look up the rec dict by matching row identity to the in-memory list.
    var rec = null;
    if (kind === "game") {
      var gid = row.getAttribute("data-rec-game-id");
      var bt  = row.getAttribute("data-rec-bet-type");
      (state.vm.rec_games || []).forEach(function (r) {
        if (String(r.game_id) === gid && r.bet_type === bt) rec = r;
      });
    } else {
      // Prop: identify by the Track button's row position within the
      // current props slice (Track buttons are 1:1 with the props list).
      var list = el("mb-rec-props-list");
      if (list) {
        var rows = list.querySelectorAll("[data-rec-kind='prop']");
        for (var i = 0; i < rows.length; i++) {
          if (rows[i] === row) {
            var props = state.vm.rec_props || [];
            var nPages = ceilPages(props.length);
            var pStart = (state.page % nPages) * PAGE_SIZE;
            rec = props[pStart + i];
            break;
          }
        }
      }
    }
    if (!rec) return;

    var body = Object.assign({}, rec.track_body || {});
    // Game-pick track endpoints (/confirm, /track_prop) want bankroll in
    // the body so they can size off the personal bankroll; props track
    // doesn't use it.
    if (kind === "game") body.bankroll = state.bankroll;

    SBT.apiPost(rec.track_url, body, {
      btn:          btn,
      pendingClass: "opacity-60",
      pendingLabel: "Tracked ✓",
      onSuccess: function (data) {
        var amt = (typeof data.amount === "number")
          ? " ($" + data.amount.toFixed(2) + ")"
          : (typeof data.stake === "number"
              ? " ($" + data.stake.toFixed(2) + ")"
              : "");
        var who = (kind === "prop") ? rec.player : rec.team;
        SBT.toast("Tracked: " + (who || "") + amt, "positive");
        refreshSnapshot();
      },
      onDedup: function () {
        SBT.toast("Already tracked.", "info");
        refreshSnapshot();
      },
      onError: function (err, data, status) {
        btn.disabled = false;
        btn.textContent = "Track";
        SBT.toast("Track failed: " + (
          (data && data.error) || (err && err.message) ||
          (status ? ("HTTP " + status) : "network error")
        ), "negative");
      },
    });
  }

  // ── Add Bet wizard ─────────────────────────────────────────────────
  function openModal() {
    if (state.modal) return;
    state.modal = {
      step:     1,
      kind:     null,
      options:  null,
      loading:  true,
      team:     null,
      player:   null,
      game:     null,
      bet_type: null,
      market:   null,
      side:     "Over",
      line:     null,
      odds:     null,
      confidence: null,        // 0..1
      model_conf: null,        // 0..1
      predicted_value: null,
      submitting: false,
      prop_pick: null,
    };
    renderModal();
    // Lazy-load /api/mybets/add_options (no auth, no cache concerns).
    fetch("/api/mybets/add_options", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body:   "{}",
    }).then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (data) {
        if (!state.modal) return;
        state.modal.options = data || {games: [], props: []};
        state.modal.loading = false;
        renderModal();
      }).catch(function () {
        if (!state.modal) return;
        state.modal.options = {games: [], props: []};
        state.modal.loading = false;
        renderModal();
      });
  }

  function closeModal() {
    state.modal = null;
    var root = el("mb-modal-root");
    if (root) { root.innerHTML = ""; root.classList.add("hidden"); }
  }

  function setModal(updates) {
    if (!state.modal) return;
    Object.keys(updates).forEach(function (k) { state.modal[k] = updates[k]; });
  }

  // Auto-fill line/odds/side/confidence from the model pick for a team
  // bet.  Mirrors NiceGUI _ab_prefill_team.
  function prefillTeam(betType) {
    var s = state.modal;
    var g = s.game || {};
    var team = s.team;
    s.side = "Over";
    s.line = null;
    s.odds = null;
    if (betType === "ml") {
      s.odds = (team === g.home_team) ? g.home_odds : g.away_odds;
    } else if (betType === "run_line") {
      var rl = g.run_line || {};
      var pt = rl.run_line_point;
      if (pt != null) {
        s.line = (team === rl.pick_team) ? Number(pt) : -Number(pt);
      }
      s.odds = rl.pick_odds;
    } else if (betType === "total") {
      var tot = g.totals || {};
      s.line = tot.total_line;
      s.side = ((tot.direction || "over") + "").replace(/^./, function (c) {
        return c.toUpperCase();
      });
      s.odds = (s.side === "Over" ? tot.over_odds : tot.under_odds) || tot.pick_odds;
    }
    s.model_conf  = teamModelConf(g, betType, team, s.side);
    s.confidence  = s.model_conf;
  }

  function teamModelConf(game, betType, team, side) {
    if (betType === "ml") {
      var hwp = game.home_win_prob;
      if (hwp == null) return null;
      return (team === game.home_team) ? hwp : 1.0 - hwp;
    }
    if (betType === "run_line") {
      var rl = game.run_line || {};
      var pp = rl.pick_prob;
      if (pp == null) return null;
      return (team === rl.pick_team) ? pp : 1.0 - pp;
    }
    if (betType === "total") {
      var tot = game.totals || {};
      var pp2 = tot.pick_prob;
      if (pp2 == null) return null;
      return ((side || "").toLowerCase() === (tot.direction || "").toLowerCase())
        ? pp2 : 1.0 - pp2;
    }
    return null;
  }

  function prefillProp(pick) {
    var s = state.modal;
    s.line = pick.line;
    s.side = (pick.side || "Over").replace(/^./, function (c) {
      return c.toUpperCase();
    });
    s.odds = pick.best_odds;
    s.predicted_value = pick.predicted_value;
    s.model_conf  = pick.confidence;
    s.confidence  = pick.confidence;
    s.prop_pick   = pick;
  }

  // ½-Kelly: matches src/kelly.tracked_bet_kelly behaviour client-side
  // (the server still authoritatively sizes on POST; this is the
  // display-only number in step 6).
  function kellyHalfStake(conf, oddsAmerican, bankroll) {
    if (!(conf > 0 && conf < 1) || !oddsAmerican || !bankroll) {
      return {dollars: 0, flag: "invalid"};
    }
    var dec = (oddsAmerican > 0)
      ? (oddsAmerican / 100) + 1
      : (100 / Math.abs(oddsAmerican)) + 1;
    var b = dec - 1;
    var kelly = ((b * conf) - (1 - conf)) / b;
    if (kelly <= 0) {
      return {dollars: Math.round(bankroll * 0.01), flag: "flat"};
    }
    return {dollars: Math.round(bankroll * (kelly * 0.5)), flag: "kelly"};
  }

  // ── Modal render ──────────────────────────────────────────────────
  function renderModal() {
    var root = el("mb-modal-root");
    if (!root || !state.modal) return;
    root.classList.remove("hidden");
    var s = state.modal;
    root.innerHTML =
      '<div class="mb-modal-backdrop" data-mb-modal-close="1">' +
        '<div class="mb-modal-card" role="dialog" aria-modal="true">' +
          modalHeaderHTML(s) +
          modalBodyHTML(s) +
        '</div>' +
      '</div>';
  }

  function modalHeaderHTML(s) {
    return '<div class="flex items-center gap-2">' +
      '<span class="text-[15px] font-extrabold text-white">Add a Bet</span>' +
      '<span class="mb-step-pill">Step ' + s.step + ' of 6</span>' +
      '<span class="flex-1"></span>' +
      '<button class="mb-icon-btn neutral" data-mb-modal-close="1">✕</button>' +
    '</div>';
  }

  function modalBodyHTML(s) {
    if (s.loading) {
      return '<div class="text-gray-400 italic text-xs py-3">' +
        'Loading today\'s games and props…</div>';
    }
    if (s.step === 1) return modalStep1();
    if (s.step === 2) return modalStep2(s);
    if (s.step === 3) return modalStep3(s);
    if (s.step === 4) return modalStep4(s);
    if (s.step === 5) return modalStep5(s);
    if (s.step === 6) return modalStep6(s);
    return "";
  }

  function navHTML(showBack, rightHTML) {
    return '<div class="flex justify-between gap-2 mt-1">' +
      (showBack
        ? '<button type="button" class="text-xs text-gray-400 font-semibold" ' +
          'data-mb-step-back="1">← Back</button>'
        : '<span></span>') +
      '<span class="flex-1"></span>' +
      (rightHTML || "") +
    '</div>';
  }

  function modalStep1() {
    var s = state.modal;
    return '<div class="text-gray-400 text-xs">What kind of bet?</div>' +
      '<div class="flex gap-2">' +
        '<button class="mb-pill ' + (s.kind === "team" ? "active" : "") +
          '" data-mb-kind="team">Team Bet</button>' +
        '<button class="mb-pill ' + (s.kind === "prop" ? "active" : "") +
          '" data-mb-kind="prop">Player Prop</button>' +
      '</div>';
  }

  function modalStep2(s) {
    var listId, label, options;
    if (s.kind === "team") {
      label = "Type a team name";
      options = uniqSorted(s.options.games.reduce(function (acc, g) {
        if (g.home_team) acc.push(g.home_team);
        if (g.away_team) acc.push(g.away_team);
        return acc;
      }, []));
      listId = "mb-step2-teams";
    } else {
      label = "Type a player name";
      options = uniqSorted((s.options.props || [])
        .map(function (p) { return p.player; })
        .filter(Boolean));
      listId = "mb-step2-players";
    }
    var datalist = '<datalist id="' + listId + '">' +
      options.map(function (o) {
        return '<option value="' + esc(o) + '"></option>';
      }).join("") + '</datalist>';
    var inputName = s.kind === "team" ? "team" : "player";
    var inputVal  = s.kind === "team" ? (s.team || "") : (s.player || "");
    return '<div class="text-gray-400 text-xs">' + label + '</div>' +
      '<input class="mb-input" list="' + listId + '" name="' + inputName +
        '" value="' + esc(inputVal) +
        '" data-mb-step2-input="1" placeholder="Start typing…" />' +
      datalist +
      (options.length === 0
        ? '<div class="text-gray-500 text-[11px] italic">' +
          (s.kind === "team" ? "No games loaded for today." : "No props loaded for today.") +
          '</div>'
        : '') +
      navHTML(true,
        '<button class="bg-accent text-black text-xs font-extrabold px-3 py-1 ' +
          'rounded" data-mb-step-next="3">Next →</button>');
  }

  function modalStep3(s) {
    var matches;
    if (s.kind === "team") {
      matches = (s.options.games || []).filter(function (g) {
        return g.home_team === s.team || g.away_team === s.team;
      });
    } else {
      var seen = {};
      matches = (s.options.props || [])
        .filter(function (p) { return p.player === s.player; })
        .filter(function (p) {
          var key = p.event_id || (p.away_team + "@" + p.home_team);
          if (seen[key]) return false;
          seen[key] = true;
          return true;
        });
    }
    if (matches.length === 1 && !s.game) s.game = matches[0];
    var who = s.kind === "team" ? s.team : s.player;
    var opts = matches.map(function (m, i) {
      var label = (m.away_team || "?") + " @ " + (m.home_team || "?");
      var selected = (s.game && m === s.game) ? " selected" : "";
      return '<option value="' + i + '"' + selected + '>' +
        esc(label) + '</option>';
    }).join("");
    return '<div class="text-gray-400 text-xs">Confirm ' +
        esc(who || "this") + "'s game today</div>" +
      '<select class="mb-input" data-mb-step3-select="1">' +
        '<option value="" disabled' + (s.game ? "" : " selected") +
          '>Select…</option>' + opts + '</select>' +
      (matches.length === 0
        ? '<div class="text-gray-500 text-[11px] italic">No game found.</div>'
        : '') +
      navHTML(true, s.game
        ? '<button class="bg-accent text-black text-xs font-extrabold px-3 py-1 ' +
          'rounded" data-mb-step-next="4">Next →</button>'
        : "");
  }

  function modalStep4(s) {
    var g = s.game || {};
    var pills = "";
    if (s.kind === "team") {
      var avail = [["ml", "Moneyline"]];
      if (g.run_line) avail.push(["run_line", "Run Line"]);
      if (g.totals)   avail.push(["total",    "Total"]);
      pills = avail.map(function (pair) {
        var bt = pair[0], lbl = pair[1];
        var active = s.bet_type === bt;
        return '<button class="mb-pill ' + (active ? "active" : "") +
          '" data-mb-bet-type="' + esc(bt) + '">' + esc(lbl) + '</button>';
      }).join("");
      return '<div class="text-gray-400 text-xs">What bet are you taking?</div>' +
        '<div class="flex flex-col gap-2">' + pills + '</div>' +
        navHTML(true, "");
    }
    // prop: market pick
    var props = (s.options.props || []).filter(function (p) {
      return p.player === s.player;
    });
    var byMarket = {};
    props.forEach(function (p) {
      if (!(p.market in byMarket)) byMarket[p.market] = p;
    });
    pills = Object.keys(byMarket).map(function (mkt) {
      var pick = byMarket[mkt];
      var active = s.market === mkt;
      var lbl = (mkt || "").replace(/_/g, " ").replace(/\b\w/g, function (c) {
        return c.toUpperCase();
      });
      return '<button class="mb-pill ' + (active ? "active" : "") +
        '" data-mb-market="' + esc(mkt) +
        '" data-mb-market-idx="' + esc(props.indexOf(pick)) +
        '">' + esc(lbl) + '</button>';
    }).join("");
    return '<div class="text-gray-400 text-xs">Which market?</div>' +
      '<div class="flex flex-col gap-2">' + pills + '</div>' +
      navHTML(true, "");
  }

  function modalStep5(s) {
    var needsSide = (s.kind === "prop") || (s.bet_type === "total");
    var needsLine = (s.kind === "prop") || (s.bet_type === "run_line") ||
                    (s.bet_type === "total");
    var sideRow = "";
    if (needsSide) {
      sideRow = '<div class="flex gap-2">' +
        ["Over", "Under"].map(function (sd) {
          var active = (s.side || "Over") === sd;
          return '<button class="mb-pill ' + (active ? "active" : "") +
            '" data-mb-side="' + esc(sd) + '">' + esc(sd) + '</button>';
        }).join("") +
      '</div>';
    }
    var fields = "";
    if (needsLine) {
      fields += '<label class="flex-1 flex flex-col gap-[2px]">' +
        '<span class="text-[9px] font-extrabold tracking-wide text-gray-500 uppercase">Line</span>' +
        '<input class="mb-input" type="number" step="0.1" value="' +
          (s.line == null ? "" : esc(s.line)) +
          '" data-mb-input="line" /></label>';
    }
    fields += '<label class="flex-1 flex flex-col gap-[2px]">' +
      '<span class="text-[9px] font-extrabold tracking-wide text-gray-500 uppercase">Odds (e.g. -110)</span>' +
      '<input class="mb-input" type="number" step="1" value="' +
        (s.odds == null ? "" : esc(s.odds)) +
        '" data-mb-input="odds" /></label>';
    return '<div class="text-gray-400 text-xs">Enter the line and odds</div>' +
      sideRow +
      '<div class="flex flex-wrap gap-2">' + fields + '</div>' +
      navHTML(true,
        '<button class="bg-accent text-black text-xs font-extrabold px-3 py-1 ' +
          'rounded" data-mb-step-next="6">Next →</button>');
  }

  function modalStep6(s) {
    var modelConf = s.model_conf;
    var confInput = "";
    if (modelConf != null) {
      confInput = '<div class="text-[13px] font-bold text-white">' +
        'Model confidence: ' + Math.round(modelConf * 100) + '%</div>';
    } else {
      var confPct = (s.confidence != null) ? Math.round(s.confidence * 100) : "";
      confInput = '<div class="text-warn text-xs">No model confidence for ' +
        'this pick — enter your estimate (%)</div>' +
        '<input class="mb-input" type="number" min="1" max="99" step="1" value="' +
          esc(confPct) +
          '" data-mb-input="confidence_pct" />';
    }
    var oddsI = Number(s.odds);
    var conf  = (s.confidence != null) ? Number(s.confidence) : null;
    var kelly = kellyHalfStake(conf, isFinite(oddsI) ? oddsI : null, state.bankroll);
    var kellyText = (kelly.flag === "invalid")
      ? '<span class="text-gray-400 text-[13px]">Enter confidence + odds to size</span>'
      : '<span class="mb-kelly-value">$' + kelly.dollars.toLocaleString() +
        (kelly.flag === "flat" ? " (1% flat)" : "") + '</span>';
    return confInput +
      '<div class="mb-kelly-card">' +
        '<span class="mb-kelly-label">RECOMMENDED BET SIZE (½ KELLY)</span>' +
        kellyText +
      '</div>' +
      navHTML(true,
        '<button class="bg-pos text-black text-xs font-extrabold px-4 py-2 ' +
          'rounded" data-mb-track="1">Track Bet</button>');
  }

  function uniqSorted(arr) {
    var seen = {};
    var out = [];
    arr.forEach(function (v) { if (v && !seen[v]) { seen[v] = 1; out.push(v); } });
    out.sort();
    return out;
  }

  // ── Modal event delegation ────────────────────────────────────────
  function onModalClick(e) {
    if (!state.modal) return;
    if (e.target.closest("[data-mb-modal-close]") &&
        e.target.closest("[data-mb-modal-close]") === e.target.closest(".mb-modal-backdrop, button")) {
      // Backdrop click OR explicit close-button click.
      var src = e.target.closest("[data-mb-modal-close]");
      if (src.classList.contains("mb-modal-backdrop") || src.tagName === "BUTTON") {
        // Backdrop catches only direct clicks on itself (not card).
        if (src.classList.contains("mb-modal-backdrop") && e.target !== src) {
          // Click inside the card -- don't close.
        } else {
          closeModal();
          return;
        }
      }
    }
    var s = state.modal;

    var backBtn = e.target.closest("[data-mb-step-back]");
    if (backBtn) {
      if (s.step > 1) s.step -= 1;
      renderModal();
      return;
    }
    var nextBtn = e.target.closest("[data-mb-step-next]");
    if (nextBtn) {
      var nextStep = Number(nextBtn.getAttribute("data-mb-step-next"));
      // Capture inputs from current step before advancing.
      captureStepInputs(s);
      s.step = nextStep;
      renderModal();
      return;
    }
    var kindBtn = e.target.closest("[data-mb-kind]");
    if (kindBtn) {
      s.kind = kindBtn.getAttribute("data-mb-kind");
      s.step = 2;
      renderModal();
      return;
    }
    var betTypeBtn = e.target.closest("[data-mb-bet-type]");
    if (betTypeBtn) {
      var bt = betTypeBtn.getAttribute("data-mb-bet-type");
      prefillTeam(bt);
      s.bet_type = bt;
      s.step = 5;
      renderModal();
      return;
    }
    var marketBtn = e.target.closest("[data-mb-market]");
    if (marketBtn) {
      var mkt = marketBtn.getAttribute("data-mb-market");
      var idx = Number(marketBtn.getAttribute("data-mb-market-idx"));
      var pick = (s.options.props || []).filter(function (p) {
        return p.player === s.player;
      })[idx];
      if (pick) prefillProp(pick);
      s.market = mkt;
      s.step = 5;
      renderModal();
      return;
    }
    var sideBtn = e.target.closest("[data-mb-side]");
    if (sideBtn) {
      s.side = sideBtn.getAttribute("data-mb-side");
      renderModal();
      return;
    }
    var trackBtn = e.target.closest("[data-mb-track]");
    if (trackBtn) {
      onModalSubmit(trackBtn);
      return;
    }
  }

  function onModalChange(e) {
    if (!state.modal) return;
    var s = state.modal;
    var step2 = e.target.closest("[data-mb-step2-input]");
    if (step2) {
      if (s.kind === "team") s.team = step2.value || null;
      else                   s.player = step2.value || null;
      // Reset downstream so step 3 picks a fresh game.
      s.game = null; s.bet_type = null; s.market = null;
      return;
    }
    var step3 = e.target.closest("[data-mb-step3-select]");
    if (step3) {
      var idx = Number(step3.value);
      if (!isFinite(idx)) return;
      var matches;
      if (s.kind === "team") {
        matches = (s.options.games || []).filter(function (g) {
          return g.home_team === s.team || g.away_team === s.team;
        });
      } else {
        var seen = {};
        matches = (s.options.props || [])
          .filter(function (p) { return p.player === s.player; })
          .filter(function (p) {
            var key = p.event_id || (p.away_team + "@" + p.home_team);
            if (seen[key]) return false;
            seen[key] = true;
            return true;
          });
      }
      s.game = matches[idx] || null;
      renderModal();
      return;
    }
  }

  function captureStepInputs(s) {
    // Step 5: line + odds inputs.
    document.querySelectorAll("[data-mb-input]").forEach(function (inp) {
      var name = inp.getAttribute("data-mb-input");
      var v = inp.value;
      if (v === "" || v == null) return;
      var n = Number(v);
      if (!isFinite(n)) return;
      if (name === "confidence_pct") {
        s.confidence = n / 100.0;
      } else {
        s[name] = n;
      }
    });
  }

  function onModalSubmit(trackBtn) {
    var s = state.modal;
    captureStepInputs(s);
    var oddsI = Number(s.odds);
    if (!s.confidence || !isFinite(oddsI)) {
      SBT.toast("Enter odds and confidence first.", "info");
      return;
    }
    if (s.submitting) return;
    s.submitting = true;
    var body = buildAddBody(s);
    SBT.apiPost("/api/mybets/add", body, {
      btn:          trackBtn,
      pendingClass: "opacity-60",
      onSuccess: function () {
        SBT.toast("Bet tracked", "positive");
        closeModal();
        refreshSnapshot();
      },
      onDedup: function () {
        SBT.toast("Already tracked.", "info");
        closeModal();
        refreshSnapshot();
      },
      onError: function (err, data, status) {
        s.submitting = false;
        if (trackBtn) trackBtn.disabled = false;
        SBT.toast("Track failed: " + (
          (data && data.error) || (err && err.message) ||
          (status ? ("HTTP " + status) : "network error")
        ), "negative");
      },
      afterAttempt: function () { s.submitting = false; },
    });
  }

  function buildAddBody(s) {
    if (s.kind === "prop") {
      var pick = s.prop_pick || {};
      return {
        kind: "prop", bankroll: state.bankroll,
        player: s.player, market: s.market,
        line: s.line, side: s.side || "Over",
        odds: s.odds, confidence: s.confidence,
        predicted_value: s.predicted_value,
        team:     pick.team || "",
        event_id: pick.event_id,
        commence_time: pick.commence_time,
      };
    }
    var g = s.game || {};
    return {
      kind: "game", bankroll: state.bankroll,
      sport: g.sport || "mlb",
      game_id: g.game_id,
      home_team: g.home_team, away_team: g.away_team,
      commence_time: g.commence_time,
      bet_type: s.bet_type || "ml",
      team: s.team, side: (s.side || "over").toLowerCase(),
      line: s.line, odds: s.odds,
      confidence: s.confidence,
    };
  }

  // ── Init ──────────────────────────────────────────────────────────
  function init() {
    renderAll();

    var openList    = el("mb-open-list");
    var settledList = el("mb-settled-list");
    if (openList)    openList.addEventListener("click", onListClick);
    if (settledList) settledList.addEventListener("click", onListClick);

    var gList = el("mb-rec-games-list");
    var pList = el("mb-rec-props-list");
    if (gList) gList.addEventListener("click", onRecClick);
    if (pList) pList.addEventListener("click", onRecClick);

    var moreBtn = el("mb-rec-more");
    if (moreBtn) moreBtn.addEventListener("click", function () {
      var games = state.vm.rec_games || [];
      var props = state.vm.rec_props || [];
      var nPages = Math.max(ceilPages(games.length), ceilPages(props.length), 1);
      state.page = (state.page + 1) % nPages;
      renderRecs();
    });

    var addBtn = el("mb-add-bet");
    if (addBtn) addBtn.addEventListener("click", openModal);

    var modalRoot = el("mb-modal-root");
    if (modalRoot) {
      modalRoot.addEventListener("click",  onModalClick);
      modalRoot.addEventListener("change", onModalChange);
      modalRoot.addEventListener("input",  onModalChange);
    }

    // Backdrop click-outside closes the modal.  Capture on the root
    // because the backdrop element only exists while the modal is open.
    if (modalRoot) {
      modalRoot.addEventListener("click", function (e) {
        var backdrop = e.target.closest(".mb-modal-backdrop");
        if (backdrop && e.target === backdrop) closeModal();
      });
    }

    // 60s snapshot poll -- matches NiceGUI's ui.timer(60.0, _tick).
    setInterval(refreshSnapshot, 60000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
