/* game_detail.js -- client for the Flask + Tailwind matchup page.
 *
 * 5 core sections render server-side (header, picks, venue, game context,
 * upset).  4 lazy sections fetch their own GET endpoints on init (AI,
 * pitching, lineups, team context) so first paint never blocks on a
 * generation or external-API call.  Mutations: track buttons (3-way
 * dispatch via SBT.apiPost) + per-pick Analyze (POST /api/ai/pick_analysis,
 * client-cached per game/bet_type).  Zero raw fetch for mutations -- every
 * POST goes through SBT.apiPost (lib.js); every toast through SBT.toast. */
(function () {
  "use strict";

  // ── State hydration ───────────────────────────────────────────────────
  var INIT = (function () {
    try {
      var node = document.getElementById("gd-init");
      return JSON.parse((node && node.textContent) || "{}") || {};
    } catch (e) { return {}; }
  })();
  if (!INIT.found) {
    // Not-found state is pure server-rendered HTML; no JS work needed.
    return;
  }
  var SPORT   = INIT.sport;
  var GAME_ID = INIT.game_id;
  // Per-pick Analyze cache: (bet_type -> rendered text).
  var analyzeCache = {};

  // ── Small DOM helpers ─────────────────────────────────────────────────
  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }

  // ── Lazy endpoints: AI / pitching / lineups / team-context ────────────
  function lazyLoad(slug, hostId, renderFn, skeletonText) {
    var host = el(hostId);
    if (!host) return;
    if (skeletonText) host.textContent = skeletonText;
    var url = "/api/matchup/" + encodeURIComponent(SPORT) + "/" +
      encodeURIComponent(GAME_ID) + "/" + slug;
    // GET read -- SBT.apiPost is POST-only; raw fetch is correct here
    fetch(url, {headers: {"Accept": "application/json"}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || data.error) {
          host.innerHTML = '<div class="gd-empty">' +
            esc((data && data.error) || "Unavailable.") + '</div>';
          return;
        }
        renderFn(host, data);
      })
      .catch(function () {
        host.innerHTML = '<div class="gd-empty">Network error — try again.</div>';
      });
  }

  // ── AI section (3 tabbed takes) ───────────────────────────────────────
  function renderAi(host, data) {
    var labels = SPORT === "mlb"
      ? [["moneyline","Moneyline"], ["run_line","Run Line"], ["run_total","Run Total"]]
      : [["moneyline","Moneyline"], ["run_line","Spread"],   ["run_total","Total"]];
    var anyText = labels.some(function (p) { return (data[p[0]] || "").trim(); });
    if (!anyText) {
      host.innerHTML = '<div class="gd-empty">AI analysis unavailable for this game.</div>';
      return;
    }
    var tabsHost = el("gd-ai-tabs");
    if (tabsHost) {
      tabsHost.innerHTML = labels.map(function (p, i) {
        return '<button class="gd-tab' + (i === 0 ? " active" : "") +
          '" data-ai-tab="' + esc(p[0]) + '">' + esc(p[1]) + '</button>';
      }).join("");
      tabsHost.addEventListener("click", function (e) {
        var b = e.target.closest("[data-ai-tab]");
        if (!b) return;
        var key = b.getAttribute("data-ai-tab");
        tabsHost.querySelectorAll("[data-ai-tab]").forEach(function (x) {
          x.classList.toggle("active", x === b);
        });
        host.textContent = (data[key] || "").trim() ||
          "No analysis available for this bet type.";
      });
    }
    // Initial tab body
    host.classList.remove("gd-skeleton");
    host.style.lineHeight = "1.5";
    host.style.color = "#d1d5db";
    host.style.padding = "8px 2px 2px 2px";
    host.textContent = (data[labels[0][0]] || "").trim() ||
      "No analysis available for this bet type.";
  }

  // ── Pitching (MLB only) ───────────────────────────────────────────────
  function renderPitching(host, data) {
    if (!data || (!data.away && !data.home)) {
      host.innerHTML = '<div class="gd-empty">Starting pitchers not yet announced.</div>';
      return;
    }
    var away = data.away || {};
    var home = data.home || {};
    if (!away.full_name && !home.full_name) {
      host.innerHTML = '<div class="gd-empty">Starting pitchers TBD.</div>';
      return;
    }
    host.classList.remove("gd-skeleton");
    host.innerHTML = '<div class="grid grid-cols-2 gap-3">' +
      pitcherCard(away, data.away_team, "Away") +
      pitcherCard(home, data.home_team, "Home") +
    '</div>';
  }
  function pitcherCard(sp, team, side) {
    var name = sp.full_name || "TBD";
    var hand = sp.hand ? " (" + esc(sp.hand) + ")" : "";
    function stat(label, val, fmt) {
      var v = (val == null || val === "") ? "N/A" :
        (fmt === "ip" ? Number(val).toFixed(1) :
         fmt === "pct" ? (Number(val) * 100).toFixed(1) + "%" :
         Number(val).toFixed(fmt === "2" ? 2 : 1));
      return '<div class="flex justify-between text-[11.5px] font-mono py-[2px] border-b border-[#161616]">' +
        '<span class="gd-text-dim">' + esc(label) + '</span>' +
        '<span class="gd-text-text">' + esc(v) + '</span></div>';
    }
    return '<div class="rounded-lg p-3" style="background:#222;border:1px solid #2a2a2a;">' +
      '<div class="text-[10px] font-extrabold tracking-wider text-gray-500 mb-1">' +
        esc(side) + ' · ' + esc(team || "") + '</div>' +
      '<div class="text-[13px] font-bold mb-2 truncate">' + esc(name) + esc(hand) + '</div>' +
      stat("ERA", sp.era, "2") +
      stat("WHIP", sp.whip, "2") +
      stat("K/9", sp.k_per_9, "1") +
      stat("Last 3 ERA", sp.last3_era, "2") +
      stat("Rest", sp.rest, "0") +
    '</div>';
  }

  // ── Lineups (MLB only) ────────────────────────────────────────────────
  function renderLineups(host, data) {
    var away = (data && data.away) || [];
    var home = (data && data.home) || [];
    if (!away.length && !home.length) {
      host.innerHTML = '<div class="gd-empty">Lineups not yet posted.</div>';
      return;
    }
    function listFor(label, players) {
      if (!players.length) {
        return '<div class="text-[11px] gd-text-dim italic">' + esc(label) +
          ': not yet posted</div>';
      }
      var rows = players.slice(0, 9).map(function (p) {
        return '<div class="flex items-center gap-2 text-[11.5px] py-[2px]">' +
          '<span class="font-mono text-gray-500 w-[16px] text-right">' + (p.order || "") + '</span>' +
          '<span class="flex-1 truncate">' + esc(p.name || "—") + '</span>' +
          '<span class="font-mono gd-text-dim">' + esc(p.position || "") + '</span></div>';
      }).join("");
      return '<div class="flex flex-col gap-[1px]"><div class="text-[10px] ' +
        'font-extrabold tracking-wider text-gray-500 mb-1">' + esc(label) +
        '</div>' + rows + '</div>';
    }
    host.classList.remove("gd-skeleton");
    host.innerHTML = '<div class="grid grid-cols-2 gap-4">' +
      listFor("AWAY", away) + listFor("HOME", home) + '</div>';
  }

  // ── Team context ──────────────────────────────────────────────────────
  function renderTeamCtx(host, data) {
    var away = (data && data.away) || {};
    var home = (data && data.home) || {};
    if (!Object.keys(away).length && !Object.keys(home).length) {
      host.innerHTML = '<div class="gd-empty">Team context not available.</div>';
      return;
    }
    function row(label, av, hv, header) {
      var weight = header ? "800" : "700";
      var lbl = header ? "ad-text-text" : "gd-text-dim";
      return '<div class="flex items-center gap-2 py-[5px] border-b border-[#1a1a1a]">' +
        '<span class="flex-[0_0_38%] text-[11px] gd-text-dim" style="font-weight:' +
          (header ? "800" : "600") + ';">' + esc(label) + '</span>' +
        '<span class="flex-1 text-center text-[12px] font-mono gd-text-text" style="font-weight:' + weight + ';">' +
          esc(av || "—") + '</span>' +
        '<span class="flex-1 text-center text-[12px] font-mono gd-text-text" style="font-weight:' + weight + ';">' +
          esc(hv || "—") + '</span></div>';
    }
    function abbr(t) {
      return (t || "—").split(" ").map(function (w) { return w[0]; }).join("").toUpperCase().slice(0, 4);
    }
    var aw = abbr(data.away_team), hm = abbr(data.home_team);
    host.classList.remove("gd-skeleton");
    host.innerHTML =
      row("", aw, hm, true) +
      row("Last 10", away.l10, home.l10) +
      row("Streak",  away.streak, home.streak) +
      row("Home",    away.home, home.home) +
      row("Away",    away.away, home.away) +
      '<div class="flex items-center gap-2 pt-2"><span class="flex-[0_0_60%] text-[11px] gd-text-dim">' +
        'Head-to-head (' + esc(hm) + '-' + esc(aw) + ')</span>' +
        '<span class="flex-1 text-center text-[12.5px] font-mono font-extrabold">' +
          esc(data.h2h || "—") + '</span></div>';
  }

  // ── Picks: Analyze + Track ────────────────────────────────────────────
  function wirePicks() {
    var host = el("gd-picks");
    if (!host) return;
    host.addEventListener("click", function (e) {
      var aBtn = e.target.closest("[data-analyze]");
      if (aBtn) { onAnalyze(aBtn); return; }
      var tBtn = e.target.closest(".gd-track-btn");
      if (tBtn && !tBtn.disabled) { onTrack(tBtn); return; }
    });
  }

  function onAnalyze(btn) {
    var bet_type = btn.getAttribute("data-analyze");
    var row = btn.closest("[data-pick-bet-type]");
    var outEl = row && row.querySelector(".gd-analyze-out");
    if (!outEl) return;
    if (analyzeCache[bet_type] != null) {
      outEl.classList.remove("hidden");
      outEl.textContent = analyzeCache[bet_type];
      return;
    }
    btn.disabled = true;
    outEl.classList.remove("hidden");
    outEl.textContent = "…loading…";
    SBT.apiPost("/api/ai/pick_analysis",
      {game_id: GAME_ID, bet_type: bet_type, sport: SPORT},
      {
        btn: btn,
        pendingClass: "is-pending",
        onSuccess: function (data) {
          var text = (data && data.analysis) || "(no response)";
          analyzeCache[bet_type] = text;
          outEl.textContent = text;
          btn.disabled = false;
        },
        onError: function (err, data, status) {
          btn.disabled = false;
          var msg = (data && data.error) || (err && err.message) ||
            (status ? ("HTTP " + status) : "network error");
          outEl.textContent = "Error: " + msg;
          SBT.toast("Analyze failed: " + msg, "negative");
        },
      });
  }

  function onTrack(btn) {
    var url = btn.getAttribute("data-track-url");
    var bodyAttr = btn.getAttribute("data-track-body") || "{}";
    var body;
    try { body = JSON.parse(bodyAttr) || {}; } catch (e) { body = {}; }
    // Game-pick endpoints want a bankroll; props track doesn't.  Defer to
    // the same default the home + admin track-dispatch uses (250 MLB / 1000
    // WNBA).  The server's actual size lands via the existing Kelly path.
    if (!body.bankroll) body.bankroll = (SPORT === "wnba" ? 1000 : 250);
    var label = btn.getAttribute("data-pick-label") || "";
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
    // Fire all 4 lazy loaders.  No 60 s poll (NiceGUI has none here).
    lazyLoad("ai",            "gd-ai-body",      renderAi);
    if (SPORT === "mlb") {
      lazyLoad("pitching",    "gd-pitching",     renderPitching);
      lazyLoad("lineups",     "gd-lineups",      renderLineups);
    }
    lazyLoad("team-context",  "gd-team-context", renderTeamCtx);
    wirePicks();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
