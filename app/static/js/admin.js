/* admin.js -- client for the Flask + Tailwind Admin page (PR follow-up to
 * #339).  Companion to templates/admin.html.  Every POST goes through
 * SBT.apiPost (lib.js); every toast through SBT.toast.  Zero raw fetch()
 * for mutations -- the only plain fetch is the GET diagnostics probe (a
 * read), per the same convention mybets.js uses for its snapshot read.
 *
 * Sections: Status, Analysis, Props, AI Analysis, Models (+ perf table +
 * toggles + repick/reset), My Bets Admin, Data Resets, Supabase Explorer,
 * Diagnostics.  Destructive actions + bankroll inputs route through one
 * reusable confirmModal(). */
(function () {
  "use strict";

  // ── State hydration ───────────────────────────────────────────────────
  var INIT = (function () {
    try {
      var node = document.getElementById("admin-init");
      return JSON.parse((node && node.textContent) || "{}") || {};
    } catch (e) { return {}; }
  })();
  var SETTINGS = INIT.settings || {
    mlb_enabled: true, wnba_enabled: false,
    show_overall_chip: true, ai_daily_limit: 20,
  };
  var STATUS = INIT.status || {};

  var DEFAULT_BANKROLL = 250;     // body default for game-pick track endpoints

  // ── Small DOM helpers ─────────────────────────────────────────────────
  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;",
              '"': "&quot;", "'": "&#39;"}[c];
    });
  }
  function on(id, evt, fn) { var e = el(id); if (e) e.addEventListener(evt, fn); }

  function fmtTs(iso) {
    if (!iso) return "—";
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return String(iso).slice(0, 19);
      return new Intl.DateTimeFormat("en-US", {
        weekday: "short", month: "short", day: "numeric",
        hour: "numeric", minute: "2-digit", timeZone: "America/New_York",
      }).format(d) + " ET";
    } catch (e) { return String(iso).slice(0, 19); }
  }

  // ── Reusable confirm / input modal ────────────────────────────────────
  // confirmModal(title, message, onConfirm, opts)
  //   opts.input -- when set, renders a number input; onConfirm receives its
  //                 numeric value.  opts.inputLabel / opts.inputValue seed it.
  //   opts.confirmLabel / opts.danger -- button text + red styling.
  // Closes on Yes (after onConfirm), Cancel, Escape, or backdrop click.
  // Disables background scroll while open.
  var _modalKeyHandler = null;
  function confirmModal(title, message, onConfirm, opts) {
    opts = opts || {};
    var root = el("ad-modal-root");
    if (!root) return;
    var hasInput = !!opts.input;
    var confirmLabel = opts.confirmLabel || (hasInput ? "Save" : "Confirm");
    var confirmCls = opts.danger ? "ad-btn-danger" : "ad-btn-primary";

    root.classList.remove("hidden");
    document.body.classList.add("modal-open");
    root.innerHTML =
      '<div class="ad-modal-backdrop" data-ad-modal-backdrop="1">' +
        '<div class="ad-modal-card" role="dialog" aria-modal="true">' +
          '<div class="ad-card-title">' + esc(title) + '</div>' +
          (message
            ? '<div class="ad-card-sub" style="white-space:pre-wrap;">' +
              esc(message) + '</div>'
            : '') +
          (hasInput
            ? '<input type="number" step="' + (opts.inputStep || "0.01") +
              '" class="ad-input" id="ad-modal-input" placeholder="' +
              esc(opts.inputLabel || "") + '" value="' +
              (opts.inputValue == null ? "" : esc(opts.inputValue)) + '">'
            : '') +
          '<div class="flex justify-end gap-2">' +
            '<button class="ad-btn ad-btn-default" data-ad-modal-cancel="1">Cancel</button>' +
            '<button class="ad-btn ' + confirmCls + '" id="ad-modal-confirm">' +
              esc(confirmLabel) + '</button>' +
          '</div>' +
        '</div>' +
      '</div>';

    function close() {
      root.classList.add("hidden");
      root.innerHTML = "";
      document.body.classList.remove("modal-open");
      if (_modalKeyHandler) {
        document.removeEventListener("keydown", _modalKeyHandler);
        _modalKeyHandler = null;
      }
    }
    _modalKeyHandler = function (e) { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", _modalKeyHandler);

    root.addEventListener("click", function (e) {
      if (e.target.closest("[data-ad-modal-cancel]")) { close(); return; }
      var bd = e.target.closest("[data-ad-modal-backdrop]");
      if (bd && e.target === bd) { close(); return; }
      if (e.target.closest("#ad-modal-confirm")) {
        var val = null;
        if (hasInput) {
          var inp = el("ad-modal-input");
          val = inp ? Number(inp.value) : NaN;
          if (!isFinite(val)) { SBT.toast("Enter a valid number.", "info"); return; }
        }
        close();
        try { onConfirm(val); } catch (err) { /* swallow */ }
      }
    });

    if (hasInput) {
      var inp = el("ad-modal-input");
      if (inp) { inp.focus(); inp.select(); }
    }
  }

  // ── Generic mutate helper: POST then toast + optional refresh ─────────
  function mutate(btn, url, body, successMsg, after) {
    SBT.apiPost(url, body || {}, {
      btn: btn,
      pendingClass: "is-pending",
      onSuccess: function (data) {
        var msg = (typeof successMsg === "function") ? successMsg(data)
                : (successMsg || "Done.");
        SBT.toast(msg, "success");
        if (after) after(data);
      },
      onDedup: function (data) {
        SBT.toast((data && data.error) || "Already done.", "info");
        if (after) after(data);
      },
      onError: function (err, data, status) {
        if (btn) btn.disabled = false;
        SBT.toast("Failed: " + (
          (data && data.error) || (err && err.message) ||
          (status ? ("HTTP " + status) : "network error")
        ), "negative");
      },
    });
  }

  // ── 1. Status ─────────────────────────────────────────────────────────
  function renderStatus() {
    var host = el("ad-status-rows");
    if (!host) return;
    var db = STATUS.db || {};
    var rows = [
      ["Last MLB analyzed",  fmtTs(STATUS.mlb_analyzed_at)],
      ["Last WNBA analyzed", fmtTs(STATUS.wnba_analyzed_at)],
      ["DB mode", String(db.mode || "json")],
    ];
    if (db.supabase != null) {
      rows.push(["Supabase", db.supabase ? "connected" : "off"]);
    }
    host.innerHTML = rows.map(function (r) {
      return '<div class="ad-meta-row">' +
        '<span class="ad-meta-label">' + esc(r[0]) + '</span>' +
        '<span class="ad-meta-value">' + esc(r[1]) + '</span></div>';
    }).join("");
  }

  // ── 2. Analysis: quota + cache info (read via plain GET, like a snapshot) ──
  function loadQuota() {
    fetch("/api/odds/usage", {headers: {"Accept": "application/json"}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var host = el("ad-quota");
        if (!host) return;
        if (!d) { host.innerHTML = '<span class="ad-dim text-[11.5px]">Odds quota: could not load.</span>'; return; }
        var count = d.count || 0, limit = d.effective_limit || 500,
            remain = d.remaining || 0, reached = !!d.limit_reached;
        var cls = reached ? "ad-neg" : (remain < 50 ? "ad-warn" : "ad-pos");
        host.innerHTML =
          '<div class="flex items-center gap-3">' +
            '<span class="' + cls + ' text-[12.5px] font-bold font-mono">' +
              count + ' of ' + limit + ' requests used today</span>' +
            '<span class="' + cls + ' text-[11px] font-mono ml-auto">' +
              (reached ? "LIMIT REACHED" : "(" + remain + " remaining)") + '</span>' +
          '</div>';
      })
      .catch(function () {});
  }

  function loadCacheInfo() {
    fetch("/api/odds/cache_status?sport=both", {headers: {"Accept": "application/json"}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var host = el("ad-cache-info");
        if (!host || !d) return;
        var parts = ['<span class="ad-dim text-[11.5px]">15-min cache:</span>'];
        [["MLB", d.mlb || {}], ["WNBA", d.wnba || {}]].forEach(function (pair) {
          var fresh = !!pair[1].fresh;
          parts.push('<span class="ad-pill ' + (fresh ? "ad-pos" : "ad-warn") + '">' +
            pair[0] + ": " + (fresh ? "fresh" : "stale") + '</span>');
        });
        host.innerHTML = parts.join(" ");
      })
      .catch(function () {});
  }

  // ── 4. AI Analysis polling ─────────────────────────────────────────────
  var aiPollTimer = null;
  function renderAiProgress(data) {
    var host = el("ad-ai-progress");
    if (!host) return;
    if (data && data.running) {
      host.className = "text-[12.5px] font-semibold ad-warn";
      host.textContent = "Generating " + (data.phase || "summaries") + "… " +
        (data.done || 0) + "/" + (data.total || 0) + " complete";
    } else if (data && data.summary) {
      var s = data.summary;
      host.className = "text-[12.5px] font-semibold ad-pos";
      host.textContent = "Done in " + (s.elapsed) + "s — " +
        (s.games_generated || 0) + " games, " +
        (s.props_generated || 0) + " props, " +
        (s.breakdowns_generated || 0) + " breakdowns; " +
        (s.skipped || 0) + " skipped" +
        (s.failed ? ("; " + s.failed + " failed") : "") + ".";
    } else {
      host.textContent = "";
    }
  }
  function aiPollOnce() {
    fetch("/api/admin/ai_analysis/status", {headers: {"Accept": "application/json"}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        renderAiProgress(d);
        if (!d.running) {
          if (aiPollTimer) { clearInterval(aiPollTimer); aiPollTimer = null; }
          el("ad-ai-run").disabled = false;
          el("ad-ai-force").disabled = false;
        }
      })
      .catch(function () {});
  }
  function startAi(force, btn) {
    el("ad-ai-run").disabled = true;
    el("ad-ai-force").disabled = true;
    SBT.apiPost("/api/admin/ai_analysis/run", {force: force}, {
      btn: btn,
      onSuccess: function () {
        SBT.toast("AI analysis started.", "info");
        if (aiPollTimer) clearInterval(aiPollTimer);
        aiPollTimer = setInterval(aiPollOnce, 2000);
        aiPollOnce();
      },
      onError: function (err, data, status) {
        el("ad-ai-run").disabled = false;
        el("ad-ai-force").disabled = false;
        SBT.toast("Failed to start: " + ((data && data.error) ||
          (status ? ("HTTP " + status) : "network error")), "negative");
      },
    });
  }

  // ── 5a. Toggles + AI daily limit ──────────────────────────────────────
  function initToggles() {
    ["mlb_enabled", "wnba_enabled", "show_overall_chip"].forEach(function (field) {
      var cb = el("ad-tg-" + field);
      if (!cb) return;
      cb.checked = !!SETTINGS[field];
      cb.addEventListener("change", function () {
        var next = cb.checked;
        var body = {}; body[field] = next;
        SBT.apiPost("/api/admin/model/settings", body, {
          onSuccess: function () {
            SETTINGS[field] = next;
            SBT.toast(labelFor(field) + (next ? " enabled." : " disabled."), "success");
          },
          onError: function (err, data, status) {
            cb.checked = !next;       // revert optimistic flip
            SBT.toast("Toggle failed: " + ((data && data.error) ||
              (status ? ("HTTP " + status) : "network error")), "negative");
          },
        });
      });
    });

    var num = el("ad-ai-daily-limit");
    if (num) {
      num.value = SETTINGS.ai_daily_limit;
      var debounce = null;
      num.addEventListener("input", function () {
        if (debounce) clearTimeout(debounce);
        debounce = setTimeout(function () {
          var v = parseInt(num.value, 10);
          if (!isFinite(v)) return;
          v = Math.max(1, Math.min(500, v));
          var prev = SETTINGS.ai_daily_limit;
          SBT.apiPost("/api/admin/model/settings", {ai_daily_limit: v}, {
            onSuccess: function () {
              SETTINGS.ai_daily_limit = v;
              SBT.toast("AI daily limit set to " + v + ".", "success");
            },
            onError: function (err, data, status) {
              num.value = prev;       // revert
              SBT.toast("Save failed: " + ((data && data.error) ||
                (status ? ("HTTP " + status) : "network error")), "negative");
            },
          });
        }, 500);
      });
    }
  }
  function labelFor(field) {
    return {mlb_enabled: "MLB auto-picks", wnba_enabled: "WNBA auto-picks",
            show_overall_chip: "Home Overall chip"}[field] || field;
  }

  // ── 5b. Model Performance table ───────────────────────────────────────
  var perfState = {preset: "all", since: null, until: null};
  function loadPerf() {
    var body;
    if (perfState.preset === "custom" && perfState.since) {
      body = {since: perfState.since, until: perfState.since};
    } else {
      body = {preset: perfState.preset};
    }
    SBT.apiPost("/api/admin/model/performance", body, {
      onSuccess: function (data) { renderPerf(data); },
      onError: function (err, data, status) {
        var host = el("ad-perf-table");
        if (host) host.innerHTML = '<div class="ad-dim text-[12px] italic">' +
          'Model performance unavailable: ' + esc((data && data.error) ||
          (status ? ("HTTP " + status) : "network error")) + '</div>';
      },
    });
  }
  function renderPerf(data) {
    var rows = (data && data.rows) || [];
    var meta = el("ad-perf-meta");
    if (meta) {
      var ts = ((data && data.updated_at) || "").slice(0, 19).replace("T", " ");
      meta.textContent = "Updated " + ts + " UTC · " + rows.length + " model rows";
    }
    var host = el("ad-perf-table");
    if (!host) return;
    if (!rows.length) {
      host.innerHTML = '<div class="ad-dim text-[12px] italic">' +
        'No settled model picks in this range yet.</div>';
      return;
    }
    var head = '<tr>' +
      '<th class="l">Model</th><th class="l">Sport</th><th class="l">Type</th>' +
      '<th>W</th><th>L</th><th>Win%</th><th class="l">Last 10</th><th>Avg Conf</th></tr>';
    var body = rows.map(function (r) {
      var wp = r.win_pct;
      var wpS = (typeof wp === "number") ? wp.toFixed(1) + "%" : "—";
      var wpCls = (typeof wp === "number" && wp >= 55) ? "ad-pos"
                : (typeof wp === "number" && wp < 50) ? "ad-neg" : "";
      var last10 = (r.last10 || "").split("").map(function (c) {
        return '<span class="' + (c === "W" ? "ad-pos" : "ad-neg") + '">' + c + '</span>';
      }).join("") || "—";
      var ac = r.avg_confidence;
      var acS = (typeof ac === "number") ? Math.round(ac * 100) + "%" : "—";
      var sp = r.sport || "", mn = r.model_name || "";
      return '<tr class="clickable" data-perf-sport="' + esc(sp) +
          '" data-perf-model="' + esc(mn) + '">' +
        '<td class="l" style="color:#9F67FF;font-weight:800;">' + esc(mn) + '</td>' +
        '<td class="l ad-dim">' + esc(sp.toUpperCase()) + '</td>' +
        '<td class="l ad-dim">' + esc(r.pick_type || "") + '</td>' +
        '<td class="ad-pos">' + (r.wins || 0) + '</td>' +
        '<td class="ad-neg">' + (r.losses || 0) + '</td>' +
        '<td class="' + wpCls + '" style="font-weight:800;">' + wpS + '</td>' +
        '<td class="l" style="font-weight:800;">' + last10 + '</td>' +
        '<td class="ad-dim">' + acS + '</td></tr>';
    }).join("");
    host.innerHTML = '<table class="ad-table"><thead>' + head +
      '</thead><tbody>' + body + '</tbody></table>';
  }
  function initPerf() {
    var pills = el("ad-perf-pills");
    var dateIn = el("ad-perf-date");
    if (pills) pills.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-preset]");
      if (!btn) return;
      var preset = btn.getAttribute("data-preset");
      pills.querySelectorAll("[data-preset]").forEach(function (b) {
        b.classList.toggle("active", b === btn);
      });
      perfState.preset = preset;
      if (preset === "custom") {
        if (dateIn) dateIn.classList.remove("hidden");
        if (dateIn && dateIn.value) { perfState.since = dateIn.value; loadPerf(); }
      } else {
        if (dateIn) dateIn.classList.add("hidden");
        perfState.since = null;
        loadPerf();
      }
    });
    if (dateIn) dateIn.addEventListener("change", function () {
      perfState.preset = "custom";
      perfState.since = dateIn.value || null;
      if (perfState.since) loadPerf();
    });
    // Row click -> model history
    var table = el("ad-perf-table");
    if (table) table.addEventListener("click", function (e) {
      var tr = e.target.closest("[data-perf-model]");
      if (!tr) return;
      var sp = tr.getAttribute("data-perf-sport");
      var mn = tr.getAttribute("data-perf-model");
      if (mn) window.location = "/model-history/" + encodeURIComponent(sp) +
        "/" + encodeURIComponent(mn);
    });
  }

  // ── 8. Supabase Explorer ──────────────────────────────────────────────
  var explorerLoaded = {};   // panel -> bool
  function initExplorer() {
    var root = el("ad-explorer");
    if (!root) return;
    root.addEventListener("click", function (e) {
      var head = e.target.closest(".ad-acc-head");
      if (head) {
        var acc = head.closest(".ad-acc");
        var body = acc.querySelector(".ad-acc-body");
        var chev = head.querySelector(".ad-acc-chev");
        var opening = body.classList.contains("hidden");
        body.classList.toggle("hidden", !opening);
        chev.classList.toggle("open", opening);
        if (opening) {
          var panel = acc.getAttribute("data-panel");
          if (panel && panel !== "raw" && !explorerLoaded[panel]) {
            loadPanel(acc);
          }
        }
        return;
      }
      // Delegated controls inside loaded panels
      handleExplorerAction(e);
    });

    on("ad-explorer-refresh", "click", function () {
      root.querySelectorAll(".ad-acc").forEach(function (acc) {
        var body = acc.querySelector(".ad-acc-body");
        var panel = acc.getAttribute("data-panel");
        if (panel && panel !== "raw" && !body.classList.contains("hidden")) {
          loadPanel(acc);
        }
      });
      SBT.toast("Refreshed expanded panels.", "info");
    });

    // Raw editor buttons
    on("ad-raw-load", "click", rawLoad);
    on("ad-raw-save", "click", rawSave);
  }

  function loadPanel(acc) {
    var panel = acc.getAttribute("data-panel");
    var path  = acc.getAttribute("data-path");
    var body  = acc.querySelector(".ad-acc-body");
    body.innerHTML = '<div class="ad-dim text-[12px] italic">Loading…</div>';
    SBT.apiPost(path, {}, {
      onSuccess: function (data) {
        explorerLoaded[panel] = true;
        body.innerHTML = renderPanel(panel, data);
      },
      onError: function (err, data, status) {
        body.innerHTML = '<div class="ad-neg text-[12px]">Failed: ' +
          esc((data && data.error) || (status ? ("HTTP " + status) : "error")) + '</div>';
      },
    });
  }

  function renderPanel(panel, data) {
    if (panel === "models")      return renderModels(data);
    if (panel === "props_cache") return renderPropsCache(data);
    if (panel === "picks")       return renderPicks(data);
    if (panel === "timestamps")  return renderTimestamps(data);
    if (panel === "cache_keys")  return renderCacheKeys(data);
    return '<div class="ad-dim text-[12px]">—</div>';
  }

  function renderModels(d) {
    if (!d.supabase) return '<div class="ad-dim text-[12px]">Supabase not connected.</div>';
    var models = d.models || [];
    if (!models.length) return '<div class="ad-dim text-[12px]">No model artifacts.</div>';
    return '<table class="ad-table"><thead><tr><th class="l">Key</th><th>Size</th>' +
      '<th class="l">SHA</th><th class="l">Updated</th></tr></thead><tbody>' +
      models.map(function (m) {
        return '<tr><td class="l">' + esc(m.key) + '</td>' +
          '<td>' + fmtBytes(m.size) + '</td>' +
          '<td class="l ad-dim">' + esc((m.sha256 || "").slice(0, 10)) + '</td>' +
          '<td class="l ad-dim">' + esc((m.updated_at || "").slice(0, 19)) + '</td></tr>';
      }).join("") + '</tbody></table>';
  }

  function renderPropsCache(d) {
    var t = d.today || {};
    var head = '<div class="text-[12px] mb-2">Today: date=' + esc(t.date || "—") +
      ' · markets=' + (t.markets || 0) + ' · total=' + (t.total || 0) + '</div>';
    var rows = d.rows || [];
    if (!rows.length) return head + '<div class="ad-dim text-[12px]">No props_* rows.</div>';
    return head + '<table class="ad-table"><thead><tr><th class="l">Key</th>' +
      '<th class="l">Date</th><th>Size</th><th></th></tr></thead><tbody>' +
      rows.map(function (r) {
        return '<tr><td class="l">' + esc(r.key) + '</td>' +
          '<td class="l ad-dim">' + esc(r.date || "") + '</td>' +
          '<td>' + fmtBytes(r.size) + '</td>' +
          '<td><button class="ad-mini-btn ad-btn-danger" data-cache-del="' +
            esc(r.key) + '">Delete</button></td></tr>';
      }).join("") + '</tbody></table>';
  }

  function renderPicks(d) {
    var led = d.ledgers || {};
    var parts = ['<div class="text-[12px] mb-2 flex flex-col gap-1">'];
    ["mlb", "wnba"].forEach(function (sp) {
      var L = led[sp] || {};
      parts.push('<div class="ad-dim">' + sp.toUpperCase() +
        ': model=$' + (L.model_bankroll != null ? L.model_bankroll : "—") +
        ' personal=$' + (L.personal_bankroll != null ? L.personal_bankroll : "—") +
        ' open=' + (L.open_bets || 0) + ' settled=' + (L.settled_bets || 0) + '</div>');
    });
    parts.push('</div>');
    var open = d.open_bets || [];
    var props = (d.props && d.props.picks) || [];
    parts.push('<div class="ad-sublabel">Open game bets (' + open.length + ')</div>');
    parts.push(betRows(open, "game"));
    parts.push('<div class="ad-sublabel">Prop picks (' + props.length + ')</div>');
    parts.push(propRows(props));
    return parts.join("");
  }
  function betRows(bets, kind) {
    if (!bets.length) return '<div class="ad-dim text-[12px]">none</div>';
    return '<table class="ad-table"><tbody>' + bets.map(function (b) {
      return '<tr data-bet-id="' + esc(b.id) + '" data-bet-sport="' + esc(b.sport) +
          '" data-bet-kind="' + kind + '">' +
        '<td class="l">' + esc(b.team || "—") + '</td>' +
        '<td class="l ad-dim">' + esc(b.bet_type || "") + '</td>' +
        '<td class="ad-dim">' + esc(b.result || "pending") + '</td>' +
        '<td>' + markBtns() + '</td></tr>';
    }).join("") + '</tbody></table>';
  }
  function propRows(picks) {
    if (!picks.length) return '<div class="ad-dim text-[12px]">none</div>';
    return '<table class="ad-table"><tbody>' + picks.map(function (p) {
      return '<tr data-bet-id="' + esc(p.id) + '" data-bet-kind="prop">' +
        '<td class="l">' + esc(p.player || "—") + '</td>' +
        '<td class="l ad-dim">' + esc((p.side || "") + " " + (p.line != null ? p.line : "")) + '</td>' +
        '<td class="ad-dim">' + esc(p.result || "pending") + '</td>' +
        '<td>' + markBtns() + ' <button class="ad-mini-btn ad-btn-default" ' +
          'data-bet-remove="1">Remove</button></td></tr>';
    }).join("") + '</tbody></table>';
  }
  function markBtns() {
    return '<button class="ad-mini-btn ad-btn-default" data-mark="win">W</button> ' +
           '<button class="ad-mini-btn ad-btn-default" data-mark="loss">L</button> ' +
           '<button class="ad-mini-btn ad-btn-default" data-mark="push">P</button>';
  }

  function renderTimestamps(d) {
    var fields = [
      ["mlb", "MLB analyzed", d.mlb],
      ["wnba", "WNBA analyzed", d.wnba],
      ["props_refresh", "Props refresh", d.props_refresh],
      ["settlement", "Last settlement", d.settlement],
    ];
    return '<table class="ad-table"><tbody>' + fields.map(function (f) {
      return '<tr><td class="l">' + esc(f[1]) + '</td>' +
        '<td class="l ad-dim">' + esc(f[2] || "—") + '</td>' +
        '<td><input class="ad-input" style="width:200px;" data-ts-input="' + f[0] +
          '" placeholder="ISO-8601"> ' +
          '<button class="ad-mini-btn ad-btn-primary" data-ts-set="' + f[0] +
          '">Set</button></td></tr>';
    }).join("") + '</tbody></table>';
  }

  function renderCacheKeys(d) {
    if (!d.supabase) return '<div class="ad-dim text-[12px]">Supabase not connected.</div>';
    var keys = d.keys || [];
    if (!keys.length) return '<div class="ad-dim text-[12px]">No cache keys.</div>';
    return '<table class="ad-table"><thead><tr><th class="l">Key</th>' +
      '<th class="l">Date</th><th>Size</th><th></th></tr></thead><tbody>' +
      keys.map(function (k) {
        return '<tr><td class="l">' + esc(k.key) + '</td>' +
          '<td class="l ad-dim">' + esc(k.date || "") + '</td>' +
          '<td>' + fmtBytes(k.size) + '</td>' +
          '<td><button class="ad-mini-btn ad-btn-danger" data-cache-del="' +
            esc(k.key) + '">Delete</button></td></tr>';
      }).join("") + '</tbody></table>';
  }

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + "B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + "K";
    return (n / 1024 / 1024).toFixed(1) + "M";
  }

  // Delegated explorer row actions
  function handleExplorerAction(e) {
    // Mark bet/prop
    var mark = e.target.closest("[data-mark]");
    if (mark) {
      var row = mark.closest("[data-bet-id]");
      if (!row) return;
      var result = mark.getAttribute("data-mark");
      var bodyM = {
        kind:   row.getAttribute("data-bet-kind"),
        id:     row.getAttribute("data-bet-id"),
        sport:  row.getAttribute("data-bet-sport") || "mlb",
        result: result,
      };
      confirmModal("Mark bet " + result.toUpperCase() + "?",
        "This grades the bet and adjusts the ledger.",
        function () {
          mutate(mark, "/api/admin/explorer/mark_bet", bodyM,
            "Marked " + result + ".", function () { reloadExplorerPanelFor(row); });
        });
      return;
    }
    // Remove prop
    var rm = e.target.closest("[data-bet-remove]");
    if (rm) {
      var row2 = rm.closest("[data-bet-id]");
      if (!row2) return;
      var idR = row2.getAttribute("data-bet-id");
      confirmModal("Remove this pick?", "Permanently removes the tracked pick.",
        function () {
          mutate(rm, "/api/mybets/remove", {kind: "prop", id: idR},
            "Removed.", function () { reloadExplorerPanelFor(row2); });
        }, {danger: true, confirmLabel: "Remove"});
      return;
    }
    // Cache delete
    var del = e.target.closest("[data-cache-del]");
    if (del) {
      var key = del.getAttribute("data-cache-del");
      confirmModal("Delete cache key?", "Permanently deletes:\n" + key,
        function () {
          mutate(del, "/api/admin/explorer/cache_delete", {key: key},
            "Deleted " + key + ".", function () { reloadExplorerPanelFor(del); });
        }, {danger: true, confirmLabel: "Delete"});
      return;
    }
    // Timestamp set
    var tsSet = e.target.closest("[data-ts-set]");
    if (tsSet) {
      var field = tsSet.getAttribute("data-ts-set");
      var inp = document.querySelector('[data-ts-input="' + field + '"]');
      var val = inp ? inp.value.trim() : "";
      if (!val) { SBT.toast("Enter an ISO-8601 value.", "info"); return; }
      mutate(tsSet, "/api/admin/explorer/set_timestamp", {field: field, value: val},
        "Timestamp set.", function () { reloadExplorerPanelFor(tsSet); });
      return;
    }
  }
  function reloadExplorerPanelFor(node) {
    var acc = node.closest(".ad-acc");
    if (acc) loadPanel(acc);
  }

  // Raw editor
  function rawLoad() {
    var key = (el("ad-raw-key").value || "").trim();
    if (!key) { SBT.toast("Enter a key first.", "info"); return; }
    SBT.apiPost("/api/admin/explorer/cache_value", {key: key}, {
      btn: el("ad-raw-load"),
      onSuccess: function (d) {
        try { el("ad-raw-value").value = JSON.stringify(d.value, null, 2); }
        catch (e) { el("ad-raw-value").value = String(d.value); }
        SBT.toast("Loaded " + key, "success");
      },
      onError: function (err, data, status) {
        el("ad-raw-load").disabled = false;
        SBT.toast("Failed: " + ((data && data.error) ||
          (status ? ("HTTP " + status) : "error")), "negative");
      },
    });
  }
  function rawSave() {
    var key = (el("ad-raw-key").value || "").trim();
    if (!key) { SBT.toast("Enter a key first.", "info"); return; }
    var raw = el("ad-raw-value").value || "";
    try { JSON.parse(raw); }
    catch (exc) { SBT.toast("Invalid JSON: " + exc.message, "negative"); return; }
    confirmModal("Overwrite Supabase key?",
      "Overwrite '" + key + "' with the edited JSON? This cannot be undone.",
      function () {
        mutate(el("ad-raw-save"), "/api/admin/explorer/cache_save",
          {key: key, value: raw}, "Saved " + key + ".");
      }, {danger: true, confirmLabel: "Save"});
  }

  // ── 9. Diagnostics ─────────────────────────────────────────────────────
  var DIAG_ICON = {ok: "✓", warn: "!", err: "✗", info: "ℹ"};
  var DIAG_CLS  = {ok: "ad-pos", warn: "ad-warn", err: "ad-neg", info: "ad-info"};
  function renderDiag(results) {
    var host = el("ad-diag-results");
    if (!host) return;
    if (!results || !results.length) {
      host.innerHTML = '<div class="ad-dim text-[12px]">No results.</div>';
      return;
    }
    host.innerHTML = results.map(function (r) {
      var cls = DIAG_CLS[r.status] || "ad-dim";
      return '<div class="ad-diag-row">' +
        '<span class="ad-diag-icon ' + cls + '">' + (DIAG_ICON[r.status] || "·") + '</span>' +
        '<div class="min-w-0 flex flex-col gap-[1px]">' +
          '<span class="ad-diag-label">' + esc(r.label) + '</span>' +
          '<span class="ad-diag-detail">' + esc(r.detail) + '</span>' +
        '</div></div>';
    }).join("");
  }
  function runDiagnostics() {
    var host = el("ad-diag-results");
    if (host) host.innerHTML = '<div class="ad-dim text-[12px] italic">Running probes…</div>';
    fetch("/api/admin/diagnostics", {headers: {"Accept": "application/json"}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { renderDiag(d ? d.results : []); })
      .catch(function () {
        if (host) host.innerHTML = '<div class="ad-neg text-[12px]">Diagnostics request failed.</div>';
      });
  }
  function probeSharp(btn) {
    mutate(btn, "/api/admin/probe_sharpapi", {}, "SharpAPI probe complete.",
      function (data) { renderDiag(data.results || []); });
  }

  // ── Wiring ─────────────────────────────────────────────────────────────
  function init() {
    renderStatus();
    loadQuota();
    loadCacheInfo();
    initToggles();
    initPerf();
    initExplorer();

    // Analysis
    on("ad-approve-odds", "click", function () {
      mutate(el("ad-approve-odds"), "/api/admin/odds/approve_additional", {},
        function (d) { return "Approved +50. New limit: " + (d.effective_limit || "?") + "."; },
        function () { loadQuota(); });
    });
    on("ad-run-mlb", "click", function () {
      mutate(el("ad-run-mlb"), "/api/analyze", {bankroll: DEFAULT_BANKROLL},
        function (d) { return "MLB: analyzed " + ((d.results || []).length) + " games."; },
        function () { loadQuota(); loadCacheInfo(); });
    });
    on("ad-run-wnba", "click", function () {
      mutate(el("ad-run-wnba"), "/api/wnba/analyze", {bankroll: 1000},
        function (d) { return "WNBA: analyzed " + ((d.results || []).length) + " games."; },
        function () { loadQuota(); loadCacheInfo(); });
    });
    on("ad-run-both", "click", function () {
      // Run All = MLB then WNBA then props repull, sequentially.
      var btn = el("ad-run-both");
      btn.disabled = true; btn.classList.add("is-pending");
      SBT.apiPost("/api/analyze", {bankroll: DEFAULT_BANKROLL}, {
        onSuccess: function () {
          SBT.apiPost("/api/wnba/analyze", {bankroll: 1000}, {
            onSuccess: function () {
              SBT.apiPost("/api/admin/props/repull", {}, {
                onSuccess: function () {
                  btn.disabled = false; btn.classList.remove("is-pending");
                  SBT.toast("All models + props refreshed.", "success");
                  loadQuota(); loadCacheInfo();
                },
                onError: finishBothErr,
              });
            },
            onError: finishBothErr,
          });
        },
        onError: finishBothErr,
      });
      function finishBothErr(err, data, status) {
        btn.disabled = false; btn.classList.remove("is-pending");
        SBT.toast("Run All failed: " + ((data && data.error) ||
          (status ? ("HTTP " + status) : "network error")), "negative");
      }
    });

    // Props
    on("ad-props-refresh", "click", function () {
      mutate(el("ad-props-refresh"), "/api/admin/props/refresh_now", {},
        function (d) { return "Done — " + (d.kept || 0) + " picks above threshold."; });
    });
    on("ad-props-repull", "click", function () {
      mutate(el("ad-props-repull"), "/api/admin/props/repull", {},
        function (d) { return "Fresh re-pull — " + (d.kept || 0) + " picks."; });
    });

    // AI
    on("ad-ai-run", "click", function () { startAi(false, el("ad-ai-run")); });
    on("ad-ai-force", "click", function () {
      confirmModal("Force AI Refresh?",
        "Re-runs AI analysis on all games and props and uses API quota. Continue?",
        function () { startAi(true, el("ad-ai-force")); });
    });

    // Models
    on("ad-refresh-models", "click", function () {
      mutate(el("ad-refresh-models"), "/api/refresh_models", {},
        "Models refreshed against cached odds.");
    });
    on("ad-clear-mlb", "click", function () {
      mutate(el("ad-clear-mlb"), "/api/reset-sport", {sport: "mlb"},
        function (d) { return d.message || "MLB snapshot cleared."; });
    });
    on("ad-clear-wnba", "click", function () {
      mutate(el("ad-clear-wnba"), "/api/reset-sport", {sport: "wnba"},
        function (d) { return d.message || "WNBA snapshot cleared."; });
    });

    // Re-pick
    on("ad-repick-both", "click", function () {
      mutate(el("ad-repick-both"), "/api/admin/model/repick", {sport: "both"}, "Model picks regenerated.");
    });
    on("ad-repick-mlb", "click", function () {
      mutate(el("ad-repick-mlb"), "/api/admin/model/repick", {sport: "mlb"}, "MLB picks regenerated.");
    });
    on("ad-repick-wnba", "click", function () {
      mutate(el("ad-repick-wnba"), "/api/admin/model/repick", {sport: "wnba"}, "WNBA picks regenerated.");
    });

    // Reset today's picks (confirm)
    function resetPicks(btnId, sport, label) {
      on(btnId, "click", function () {
        confirmModal("Reset " + label + " picks?",
          "Permanently deletes today's pending " + label + " model picks from the " +
          "Supabase model_picks table and refunds their stakes. This cannot be undone.",
          function () {
            mutate(el(btnId), "/api/admin/model/reset", {sport: sport},
              "Picks reset.");
          }, {danger: true, confirmLabel: "Reset"});
      });
    }
    resetPicks("ad-reset-picks-mlb", "mlb", "MLB");
    resetPicks("ad-reset-picks-wnba", "wnba", "WNBA");
    resetPicks("ad-reset-picks-both", "both", "MLB + WNBA");

    on("ad-reset-model-bankroll", "click", function () {
      confirmModal("Reset Model Bankroll?",
        "Reset the model bankroll to its starting amount ($1000) on both ledgers " +
        "AND clear every open model bet. Settled history + personal are NOT affected. " +
        "This cannot be undone.",
        function () {
          mutate(el("ad-reset-model-bankroll"), "/api/admin/reset/model_bankroll", {},
            "Model bankroll reset.");
        }, {danger: true, confirmLabel: "Reset"});
    });
    on("ad-settle-now", "click", function () {
      confirmModal("Force Settlement?",
        "Runs the auto-settle job now, bypassing the time gate. Closes open bets " +
        "whose games have finished.",
        function () {
          mutate(el("ad-settle-now"), "/api/admin/settle_now", {}, "Settlement run.");
        });
    });

    // My Bets Admin
    function wipe(btnId, sport, label, danger) {
      on(btnId, "click", function () {
        confirmModal("Wipe " + label + " bets?",
          "Wipe all " + label + " bets (open + history) and reset " + label +
          " bankrolls? This cannot be undone.",
          function () {
            mutate(el(btnId), "/api/admin/wipe_ledger", {sport: sport},
              function (d) { return "Wiped: " + ((d.wiped || []).join(", ")) + "."; });
          }, {danger: !!danger, confirmLabel: "Wipe"});
      });
    }
    wipe("ad-wipe-mlb", "mlb", "MLB");
    wipe("ad-wipe-wnba", "wnba", "WNBA");
    wipe("ad-wipe-both", "both", "MLB + WNBA", true);

    on("ad-set-personal-bankroll", "click", function () {
      confirmModal("Set My Bankroll", "Enter the new personal bankroll amount ($).",
        function (val) {
          mutate(el("ad-set-personal-bankroll"), "/api/ledger/set_bankroll",
            {bankroll: val}, "Personal bankroll updated.");
        }, {input: true, inputLabel: "Amount ($)", confirmLabel: "Set"});
    });
    on("ad-set-model-bankroll", "click", function () {
      confirmModal("Set Model Bankroll", "Enter the new model bankroll amount ($).",
        function (val) {
          mutate(el("ad-set-model-bankroll"), "/api/ledger/set_model_bankroll",
            {bankroll: val}, "Model bankroll updated.");
        }, {input: true, inputLabel: "Amount ($)", confirmLabel: "Set"});
    });

    // Data Resets (full warning text matching NiceGUI)
    on("ad-reset-model-record", "click", function () {
      confirmModal("Reset Model Record",
        "Are you sure? This permanently deletes ALL model pick history across " +
        "MLB + WNBA from the Supabase model_picks table. The model W/L record and " +
        "units reset to 0-0 and 0U. The model bankroll, open bets, and your personal " +
        "records are NOT affected.\n\nThis acts on live persistent Supabase data and " +
        "cannot be undone.",
        function () {
          mutate(el("ad-reset-model-record"), "/api/admin/reset/model_record", {},
            "Model record cleared.");
        }, {danger: true, confirmLabel: "Reset"});
    });
    on("ad-reset-model-bankroll-2", "click", function () {
      confirmModal("Reset Model Bankroll",
        "Permanently reset the model bankroll back to its starting amount ($1000) on " +
        "both MLB + WNBA ledgers, AND clear every open model bet. The settled W/L " +
        "history, your personal bankroll, and your personal tracked bets are NOT " +
        "affected.\n\nThis cannot be undone.",
        function () {
          mutate(el("ad-reset-model-bankroll-2"), "/api/admin/reset/model_bankroll", {},
            "Model bankroll reset.");
        }, {danger: true, confirmLabel: "Reset"});
    });
    on("ad-reset-confidence", "click", function () {
      confirmModal("Reset Confidence Record",
        "Permanently clear the Confidence Performance tracker. Strong, Moderate, and " +
        "Low tier W/L records reset to 0-0 for BOTH model picks and your personal " +
        "picks.\n\nThe underlying win/loss results stay on the bets — only the " +
        "confidence-tier tagging is cleared.\n\nThis cannot be undone.",
        function () {
          mutate(el("ad-reset-confidence"), "/api/admin/reset/confidence_record", {},
            "Confidence tiers cleared.");
        }, {danger: true, confirmLabel: "Reset"});
    });
    on("ad-reset-mybets-record", "click", function () {
      confirmModal("Reset My Bets Record",
        "Permanently delete ALL personal tracked bet history across MLB + WNBA. " +
        "Personal W/L records and units reset to 0-0 and 0U. The personal bankroll, " +
        "the model's record, and open bets are NOT affected.\n\nThis cannot be undone.",
        function () {
          mutate(el("ad-reset-mybets-record"), "/api/admin/reset/my_bets_record", {},
            "My Bets record cleared.");
        }, {danger: true, confirmLabel: "Reset"});
    });

    // Diagnostics
    on("ad-run-diag", "click", runDiagnostics);
    on("ad-probe-sharp", "click", function () { probeSharp(el("ad-probe-sharp")); });

    // Reflect an already-running AI run + initial perf load + auto diagnostics.
    aiPollOnce();
    loadPerf();
    runDiagnostics();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
