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

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }

  // ── Top-level tabs (Pick / Game Log) ──────────────────────────────────
  function initTopTabs() {
    var tabs = document.querySelectorAll("[data-tab]");
    var pick = el("pl-tab-pick"), log = el("pl-tab-log");
    tabs.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var which = btn.getAttribute("data-tab");
        tabs.forEach(function (b) { b.classList.toggle("active", b === btn); });
        if (pick) pick.classList.toggle("hidden", which !== "pick");
        if (log)  log.classList.toggle("hidden", which !== "log");
        // Charts in a hidden container render at 0-width; resize on reveal.
        if (which === "pick" && activeMarket && chartsInit[activeMarket]) {
          chartsInit[activeMarket].resize();
        }
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

  function init() {
    initTopTabs();
    initSubTabs();
    initTrack();
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
