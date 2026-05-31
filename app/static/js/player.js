/* player.js -- client for the Flask + Tailwind player-profile page (PR #1:
 * Pick + Game Log tabs).  Companion to templates/player.html.
 *
 * The Pick tab content + Game Log table are server-rendered; this file wires:
 *   - top-level tab toggle (Pick / Game Log)
 *   - per-market sub-tab toggle inside Pick.  ACTIVE SUB-TAB IS TRACKED IN A
 *     VARIABLE (activeMarket), never inferred from the DOM -- so a Track
 *     click (which does an optimistic flip + toast, no re-render) can't reset
 *     it, and any future re-render restores from the variable.
 *   - ECharts init per market chart (from server-built option dicts)
 *   - Track button -> SBT.apiPost('/api/props/track', ...)
 *   - lazy AI breakdown per market (GET /api/player/<sport>/<slug>/ai)
 *
 * No mutations beyond Track (SBT.apiPost); no 60s poll. */
(function () {
  "use strict";

  var INIT = (function () {
    try { return JSON.parse(document.getElementById("pl-init").textContent) || {}; }
    catch (e) { return {}; }
  })();
  if (!INIT.found) return;   // not-found state is server-rendered

  var SPORT = INIT.sport, SLUG = INIT.slug;

  // Source of truth for the active Pick sub-tab.  Seeded to the first
  // market; mutated only by an explicit sub-tab click.  Track/AI never
  // touch it.
  var activeMarket = null;
  var aiLoaded = {};           // market -> bool (lazy AI fired once)
  var chartsInit = {};         // market -> echarts instance (init once)
  // Lazy tab load state (PR #2): each loads once on first reveal.
  var overviewLoaded = false;
  var matchupLoaded  = false;

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }

  // ── Top-level tabs (Pick / Overview / Matchup / Game Log) ────────────
  function initTopTabs() {
    var tabs = document.querySelectorAll("[data-tab]");
    var panes = {
      pick:     el("pl-tab-pick"),
      overview: el("pl-tab-overview"),
      matchup:  el("pl-tab-matchup"),
      log:      el("pl-tab-log"),
    };
    tabs.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var which = btn.getAttribute("data-tab");
        tabs.forEach(function (b) { b.classList.toggle("active", b === btn); });
        Object.keys(panes).forEach(function (k) {
          var p = panes[k];
          if (p) p.classList.toggle("hidden", k !== which);
        });
        // Charts in a hidden container render at 0-width; resize on reveal.
        if (which === "pick" && activeMarket && chartsInit[activeMarket]) {
          chartsInit[activeMarket].resize();
        }
        if (which === "overview" && !overviewLoaded) loadOverview();
        if (which === "matchup"  && !matchupLoaded)  loadMatchup();
      });
    });
  }

  // ── Per-market sub-tabs (state in activeMarket variable) ──────────────
  function initSubTabs() {
    var subtabs = document.querySelectorAll("[data-market]");
    if (!subtabs.length) return;
    activeMarket = subtabs[0].getAttribute("data-market");
    subtabs.forEach(function (btn) {
      btn.addEventListener("click", function () {
        setActiveMarket(btn.getAttribute("data-market"));
      });
    });
    // Init the first market's chart + lazy AI.
    showMarketChart(activeMarket);
    loadAi(activeMarket);
  }

  function setActiveMarket(market) {
    if (market === activeMarket) return;
    activeMarket = market;
    document.querySelectorAll("[data-market]").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-market") === market);
    });
    document.querySelectorAll("[data-market-panel]").forEach(function (p) {
      p.classList.toggle("hidden", p.getAttribute("data-market-panel") !== market);
    });
    showMarketChart(market);
    loadAi(market);
  }

  // ── ECharts (server-built options) ────────────────────────────────────
  function showMarketChart(market) {
    if (chartsInit[market]) { chartsInit[market].resize(); return; }
    var panel = document.querySelector('[data-market-panel="' + cssEsc(market) + '"]');
    if (!panel) return;
    var holder = panel.querySelector(".pl-chart");
    if (!holder || typeof echarts === "undefined") return;
    var opts;
    try { opts = JSON.parse(holder.getAttribute("data-chart")); }
    catch (e) { return; }
    var inst = echarts.init(holder, null, { renderer: "canvas" });
    inst.setOption(opts);
    chartsInit[market] = inst;
  }
  function cssEsc(s) { return String(s).replace(/"/g, '\\"'); }

  // ── Lazy AI breakdown per market ──────────────────────────────────────
  function loadAi(market) {
    if (aiLoaded[market]) return;
    aiLoaded[market] = true;
    var panel = document.querySelector('[data-market-panel="' + cssEsc(market) + '"]');
    if (!panel) return;
    var out = panel.querySelector("[data-ai-market]");
    if (!out) return;   // gamelog-only markets have no AI host
    var url = "/api/player/" + encodeURIComponent(SPORT) + "/" +
      encodeURIComponent(SLUG) + "/ai?market=" + encodeURIComponent(market);
    // GET read -- SBT.apiPost is POST-only; raw fetch is correct here
    fetch(url, { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var text = (d && d.analysis || "").trim();
        if (!text) {
          out.classList.add("hidden");
          return;
        }
        out.classList.remove("pl-skeleton");
        out.style.color = "#d1d5db";
        out.style.lineHeight = "1.5";
        out.textContent = text;
      })
      .catch(function () { out.classList.add("hidden"); });
  }

  // ── Track button ──────────────────────────────────────────────────────
  function initTrack() {
    var pick = el("pl-tab-pick");
    if (!pick) return;
    pick.addEventListener("click", function (e) {
      var btn = e.target.closest(".pl-track-btn");
      if (!btn || btn.disabled) return;
      onTrack(btn);
    });
  }

  function onTrack(btn) {
    var body;
    try { body = JSON.parse(btn.getAttribute("data-track-body")) || {}; }
    catch (e) { body = {}; }
    var label = btn.getAttribute("data-track-label") || "";
    SBT.apiPost("/api/props/track", body, {
      btn: btn,
      pendingClass: "is-pending",
      pendingLabel: "Tracked ✓",
      onSuccess: function (data) {
        btn.classList.add("is-tracked");
        var amt = (typeof data.amount === "number")
          ? " ($" + data.amount.toFixed(2) + ")" : "";
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

  // ── Overview tab (lazy) ───────────────────────────────────────────────
  function loadOverview() {
    overviewLoaded = true;
    var host = el("pl-overview-host");
    if (!host) return;
    var url = "/api/player/" + encodeURIComponent(SPORT) + "/" +
      encodeURIComponent(SLUG) + "/overview";
    // GET read -- SBT.apiPost is POST-only; raw fetch is correct here
    fetch(url, { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || d.error) {
          host.innerHTML = unavailableCard("Overview unavailable.");
          return;
        }
        host.className = "flex flex-col gap-4";
        host.innerHTML =
          (d.is_pitcher ? "" : section("SEASON AVERAGES", renderSeasonAvgs(d.season_avgs))) +
          section("STATCAST PERCENTILES", renderPercentiles(d.percentiles));
      })
      .catch(function () {
        host.innerHTML = unavailableCard("Network error loading overview.");
      });
  }

  function renderSeasonAvgs(s) {
    if (!s || s.available === false) {
      return noteRow((s && s.note) || "Season averages unavailable.");
    }
    var rows = [["AVG","avg"],["OBP","obp"],["SLG","slg"],["OPS","ops"]];
    if (!rows.some(function (r) { return s[r[1]] != null; })) {
      return noteRow("No season batting stats yet for this player.");
    }
    return '<div class="flex gap-2 flex-nowrap">' +
      rows.map(function (r) {
        var v = s[r[1]];
        var txt = "—";
        if (typeof v === "number") {
          txt = v.toFixed(3).replace(/^0/, "");
        }
        return statBox(r[0], txt);
      }).join("") +
    '</div>';
  }

  function renderPercentiles(p) {
    if (!p || p.available === false) {
      return noteRow((p && p.note) || "Statcast data unavailable.");
    }
    var splits = p.splits || {};
    var keys = ["all", "rhp", "lhp"].filter(function (k) {
      return splits[k] && splits[k].rows && splits[k].rows.length;
    });
    if (!keys.length) {
      return noteRow("Not enough data for any split.");
    }
    var labels = {all: "SZN", rhp: "vs RHP", lhp: "vs LHP"};
    // Split pills — vanilla JS state, same pattern as the Pick sub-tabs.
    var pills = keys.map(function (k, i) {
      return '<button class="pl-subtab' + (i === 0 ? " active" : "") +
        '" data-pct-split="' + esc(k) + '">' + esc(labels[k] || k) + '</button>';
    }).join("");
    var bodies = keys.map(function (k, i) {
      var sp = splits[k];
      var rows = (sp.rows || []).map(function (r) { return percentileBar(r); }).join("");
      return '<div class="pl-pct-body' + (i === 0 ? "" : " hidden") +
        '" data-pct-body="' + esc(k) + '">' + (rows || noteRow("No rows.")) + '</div>';
    }).join("");
    return '<div class="flex gap-1 border-b border-border mb-3">' + pills + '</div>' + bodies;
  }

  function percentileBar(r) {
    var pct = Number(r.percentile);
    var v = (typeof r.value === "number") ? r.value.toFixed(r.value < 10 ? 2 : 1) : "—";
    var pctTxt = isFinite(pct) ? Math.round(pct) : "—";
    var col = !isFinite(pct) ? "#6b7280" :
      (pct >= 75 ? "#22c55e" : (pct >= 50 ? "#eab308" : "#ef4444"));
    var width = isFinite(pct) ? Math.max(0, Math.min(100, pct)) : 0;
    return '<div class="flex items-center gap-2 py-[5px] border-b border-[#161616] last:border-0">' +
      '<span class="text-[11px] font-bold pl-text-dim min-w-[110px]">' + esc(r.label || "") + '</span>' +
      '<span class="text-[12px] font-mono font-bold pl-text-text min-w-[50px] text-right">' + esc(v) + '</span>' +
      '<div style="flex:1;height:6px;background:#1a1a1a;border-radius:3px;overflow:hidden;">' +
        '<div style="height:100%;width:' + width + '%;background:' + col + ';"></div>' +
      '</div>' +
      '<span class="text-[11px] font-mono font-bold min-w-[28px] text-right" style="color:' + col + ';">' +
        pctTxt + '</span>' +
    '</div>';
  }

  // ── Matchup tab (lazy) ────────────────────────────────────────────────
  function loadMatchup() {
    matchupLoaded = true;
    var host = el("pl-matchup-host");
    if (!host) return;
    var url = "/api/player/" + encodeURIComponent(SPORT) + "/" +
      encodeURIComponent(SLUG) + "/matchup";
    // GET read -- SBT.apiPost is POST-only; raw fetch is correct here
    fetch(url, { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || d.error) {
          host.innerHTML = unavailableCard("Matchup unavailable.");
          return;
        }
        if (d.has_prop === false) {
          host.innerHTML = unavailableCard(d.note || "No upcoming game to grade.");
          return;
        }
        host.className = "flex flex-col gap-4";
        var blocks = [
          section("MATCHUP GRADE", renderGrade(d.grade)),
          // Weather + Park side by side
          '<div class="flex flex-wrap gap-2">' +
            '<div class="flex-1 min-w-[240px]">' +
              section("WEATHER", renderWeather(d.weather)) +
            '</div>' +
            '<div class="flex-1 min-w-[240px]">' +
              section("PARK FACTORS", renderPark(d.park)) +
            '</div>' +
          '</div>',
        ];
        if (d.is_pitcher) {
          blocks.push(section("OPPOSING LINEUP", renderLineup(d.opposing_lineup)));
        } else {
          blocks.push(section("TONIGHT'S STARTER", renderStarter(d.starter)));
          blocks.push(section("CAREER H2H", renderH2H(d.h2h)));
          blocks.push(section("BATTER VS PITCH TYPE", renderBvp(d.bvp)));
          blocks.push(section("PITCHER ARSENAL", renderArsenal(d.arsenal)));
        }
        host.innerHTML = blocks.join("");
      })
      .catch(function () {
        host.innerHTML = unavailableCard("Network error loading matchup.");
      });
  }

  function renderGrade(g) {
    if (!g || g.available === false) {
      return noteRow((g && g.note) || "Matchup grade unavailable.");
    }
    var score = g.score;
    var head = (typeof score === "number") ? Math.round(score) : "—";
    var col = colorToken(g.color || "dim");
    return '<div class="flex items-baseline gap-1">' +
      '<span class="font-mono font-extrabold" style="font-size:40px;line-height:1;color:' + col + ';">' +
        esc(head) + '</span>' +
      '<span class="font-mono font-extrabold pl-text-dim2" style="font-size:18px;">/100</span>' +
    '</div>';
  }

  function renderWeather(w) {
    if (!w || w.available === false) {
      return noteRow((w && w.note) || "Weather unavailable.");
    }
    var temp = (typeof w.temperature === "number") ? Math.round(w.temperature) + "°F" : "—";
    var wind = (typeof w.wind_speed === "number")
      ? (Math.round(w.wind_speed) + " mph " + (w.wind_dir || "")).trim() : "—";
    return '<div class="text-[15px] font-extrabold pl-text-text mb-2">' +
        esc(w.conditions || "—") + '</div>' +
      '<div class="flex gap-2 flex-nowrap">' +
        statBox("TEMP", temp) + statBox("WIND", wind) +
      '</div>';
  }

  function renderPark(p) {
    if (!p || p.available === false) {
      return noteRow((p && p.note) || "Park data unavailable.");
    }
    function factorColor(f) {
      if (typeof f !== "number") return "#fff";
      return f > 1.02 ? "#22c55e" : (f < 0.98 ? "#ef4444" : "#9ca3af");
    }
    function fmt(f) { return (typeof f === "number") ? f.toFixed(2) : "—"; }
    return '<div class="text-[14px] font-extrabold pl-text-text leading-tight mb-2">' +
        esc(p.park_name || "—") + '</div>' +
      '<div class="flex gap-2 flex-nowrap">' +
        statBoxColor("RUN FACTOR", fmt(p.run_factor), factorColor(p.run_factor)) +
        statBoxColor("HR FACTOR",  fmt(p.hr_factor),  factorColor(p.hr_factor)) +
      '</div>';
  }

  function renderStarter(s) {
    if (!s || s.available === false) {
      return noteRow((s && s.note) || "Opposing starter not announced yet.");
    }
    var hand = (s.hand || "").toUpperCase();
    var handTxt = (hand === "L" || hand === "R") ? " (" + hand + ")" : "";
    var record = (s.wins || 0) + "-" + (s.losses || 0);
    var rows = [
      ["ERA", "era", 2], ["WHIP", "whip", 2],
      ["K/9", "k9", 1], ["BB/9", "bb9", 1],
    ];
    var rowsHtml = rows.map(function (r) {
      var val = s[r[1]];
      var txt = (typeof val === "number") ? val.toFixed(r[2]) : "—";
      return '<div class="flex items-center gap-2 py-[6px] border-b border-[#161616] last:border-0">' +
        '<span class="text-[11px] font-extrabold pl-text-dim2 min-w-[48px]">' + esc(r[0]) + '</span>' +
        '<span class="text-[14px] font-extrabold font-mono pl-text-text flex-1">' + esc(txt) + '</span>' +
      '</div>';
    }).join("");
    return '<div class="flex items-center gap-2 mb-2">' +
        '<span class="text-[15px] font-extrabold pl-text-text">' +
          esc((s.name || "—") + handTxt) + '</span>' +
        '<span class="text-[12px] font-mono pl-text-dim">  ' + esc(record) + '</span>' +
      '</div>' + rowsHtml;
  }

  function renderH2H(h) {
    if (!h || h.available === false) {
      return noteRow((h && h.note) || "No prior matchups (fewer than 5 PA).");
    }
    var ab = Number(h.ab) || 0;
    if (ab < 5) return noteRow("No prior matchups (fewer than 5 PA).");
    var h_ = Number(h.h) || 0;
    var hr = Number(h.hr) || 0;
    var so = Number(h.so) || 0;
    var kPct = ab ? (Math.round((so / ab) * 100) + "%") : "—";
    return '<div class="flex gap-2 flex-nowrap">' +
      statBox("PA",  String(ab)) +
      statBox("AVG", h.avg || "—") +
      statBox("HR",  String(hr)) +
      statBox("K%",  kPct) +
    '</div>';
  }

  function renderBvp(b) {
    if (!b || b.available === false) {
      return noteRow((b && b.note) || "Pitch-type data unavailable.");
    }
    var rows = b.rows || [];
    if (!rows.length) return noteRow("No pitch-type data available.");
    var head = '<tr>' +
      ['Pitch', '%', 'AVG', 'wOBA', 'K%'].map(function (h, i) {
        return '<th style="text-align:' + (i === 0 ? "left" : "right") + ';">' + h + '</th>';
      }).join("") + '</tr>';
    var body = rows.map(function (r) {
      var avg = (typeof r.avg === "number") ? r.avg.toFixed(3).replace(/^0/, "") : "—";
      var woba = (typeof r.woba === "number") ? r.woba.toFixed(3).replace(/^0/, "") : "—";
      var kpct = (typeof r.k_pct === "number") ? Math.round(r.k_pct) + "%" : "—";
      var pct = (typeof r.usage === "number") ? Math.round(r.usage * 100) + "%" : "—";
      return '<tr>' +
        '<td style="text-align:left;">' + esc(r.label || r.type || "") + '</td>' +
        '<td style="text-align:right;">' + esc(pct) + '</td>' +
        '<td style="text-align:right;">' + esc(avg) + '</td>' +
        '<td style="text-align:right;">' + esc(woba) + '</td>' +
        '<td style="text-align:right;">' + esc(kpct) + '</td>' +
      '</tr>';
    }).join("");
    return '<div class="overflow-x-auto"><table class="pl-table">' +
      '<thead>' + head + '</thead><tbody>' + body + '</tbody></table></div>';
  }

  function renderArsenal(a) {
    if (!a || a.available === false) {
      return noteRow((a && a.note) || "Arsenal unavailable.");
    }
    var rows = a.rows || a.pitches || [];
    if (!rows.length) return noteRow("No arsenal data available.");
    return rows.map(function (r) {
      var usage = (typeof r.usage === "number") ? Math.round(r.usage * 100) + "%" : "—";
      return '<div class="flex items-center gap-2 py-[5px] border-b border-[#161616] last:border-0">' +
        '<span class="text-[12px] font-bold pl-text-text flex-1">' + esc(r.label || r.type || "") + '</span>' +
        '<span class="text-[12px] font-mono pl-text-dim">' + esc(usage) + '</span>' +
      '</div>';
    }).join("");
  }

  function renderLineup(d) {
    if (!d || d.available === false) {
      return noteRow((d && d.note) || "Opposing lineup not posted yet.");
    }
    var batters = d.batters || [];
    if (!batters.length) return noteRow("Opposing lineup not posted yet.");
    var splitLabel = d.split_label || "vs RHP";
    function r3(v) {
      return (typeof v === "number") ? v.toFixed(3).replace(/^0/, "") : "—";
    }
    return batters.map(function (b) {
      var hand = (["L","R","S"].indexOf(b.hand) >= 0) ? " (" + b.hand + ")" : "";
      var pa = b.split_pa;
      var splitLine;
      if (typeof pa === "number" && pa > 0) {
        var kPctTxt = (typeof b.split_k_pct === "number")
          ? "K% " + Math.round(b.split_k_pct) + "%" : "K% —";
        splitLine = '<div class="flex flex-wrap gap-[10px] text-[11px] font-mono pl-text-text" style="padding-left:24px;">' +
          '<span class="pl-text-dim2">' + esc(splitLabel) + '</span>' +
          '<span>PA ' + Math.round(pa) + '</span>' +
          '<span>AVG '  + esc(r3(b.split_avg))  + '</span>' +
          '<span>wOBA ' + esc(r3(b.split_woba)) + '</span>' +
          '<span>ISO '  + esc(r3(b.split_iso))  + '</span>' +
          '<span>' + esc(kPctTxt) + '</span>' +
        '</div>';
      } else {
        splitLine = '<span class="text-[10.5px] italic pl-text-dim2" style="padding-left:24px;">' +
          esc(splitLabel) + ': no split data</span>';
      }
      return '<div class="flex flex-col gap-[3px] py-2 border-b border-[#1a1a1a] last:border-0">' +
        '<div class="flex items-center gap-2">' +
          '<span class="text-[11px] font-extrabold font-mono pl-text-dim2 min-w-[16px]">' +
            esc(String(b.order || "")) + '</span>' +
          '<span class="text-[13px] font-bold pl-text-text flex-1 truncate">' +
            esc((b.name || "") + hand) + '</span>' +
          '<span class="text-[11px] font-bold font-mono pl-text-dim flex-shrink-0">' +
            esc(b.avg || "—") + '/' + esc(b.obp || "—") + '/' + esc(b.slg || "—") +
          '</span>' +
        '</div>' + splitLine +
      '</div>';
    }).join("");
  }

  // ── Shared render helpers ─────────────────────────────────────────────
  function section(title, bodyHtml) {
    return '<div class="pl-card">' +
      '<div class="text-[10px] font-extrabold tracking-wider" style="color:#9F67FF;letter-spacing:.8px;">' +
        esc(title) + '</div>' +
      bodyHtml +
    '</div>';
  }
  function noteRow(msg) {
    return '<div class="text-[12px] italic pl-text-dim2" style="padding:4px 2px;">' +
      esc(msg) + '</div>';
  }
  function unavailableCard(msg) {
    return '<div class="pl-empty">' + esc(msg) + '</div>';
  }
  function statBox(label, value) {
    return '<div class="flex-1 flex flex-col items-center gap-[2px] py-[10px] px-3" ' +
      'style="background:#222;border:1px solid #2a2a2a;border-radius:10px;">' +
      '<span class="text-[10px] font-bold tracking-wider pl-text-dim2">' + esc(label) + '</span>' +
      '<span class="text-[18px] font-extrabold font-mono pl-text-text">' + esc(value) + '</span>' +
    '</div>';
  }
  function statBoxColor(label, value, color) {
    return '<div class="flex-1 flex flex-col items-center gap-[2px] py-[10px] px-3" ' +
      'style="background:#222;border:1px solid #2a2a2a;border-radius:10px;">' +
      '<span class="text-[10px] font-bold tracking-wider pl-text-dim2">' + esc(label) + '</span>' +
      '<span class="text-[18px] font-extrabold font-mono" style="color:' + color + ';">' +
        esc(value) + '</span>' +
    '</div>';
  }
  function colorToken(name) {
    return {pos: "#22c55e", neg: "#ef4444", warn: "#eab308",
            dim: "#9ca3af", primary: "#7C3AED",
            primary_hi: "#9F67FF"}[name] || "#fff";
  }

  // ── Percentile split-pill delegation (event-delegated on the host) ────
  function initPctSplitDelegation() {
    var host = el("pl-overview-host");
    if (!host) return;
    host.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-pct-split]");
      if (!btn) return;
      var key = btn.getAttribute("data-pct-split");
      host.querySelectorAll("[data-pct-split]").forEach(function (b) {
        b.classList.toggle("active", b === btn);
      });
      host.querySelectorAll("[data-pct-body]").forEach(function (p) {
        p.classList.toggle("hidden",
          p.getAttribute("data-pct-body") !== key);
      });
    });
  }

  function init() {
    initTopTabs();
    initSubTabs();
    initTrack();
    initPctSplitDelegation();
    window.addEventListener("resize", function () {
      if (activeMarket && chartsInit[activeMarket]) chartsInit[activeMarket].resize();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
