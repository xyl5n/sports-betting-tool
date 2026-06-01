/* admin.js — vanilla client for the simplified /admin dashboard.
 *
 * Two responsibilities:
 *   1. Render the STATUS grid from #admin-init JSON (storage backend +
 *      last-analyzed timestamps per sport) into #ad-status-grid.
 *   2. Wire every [data-endpoint] button in the ACTIONS grid to a POST
 *      fetch with credentials, swap the label to a spinner while in
 *      flight, and surface success / error via a slide-in toast.
 *
 * Dark theme tokens (bg-card, border-border, accent, pos/neg/warn) come
 * from base.html's tailwind.config — no extra CSS needed beyond the page
 * spinner + toast slide keyframes in admin.html. */

(function () {
  "use strict";

  // ── Hydrate ────────────────────────────────────────────────────────
  // Parse the data island once.  If anything goes wrong (missing tag,
  // malformed JSON) fall back to a shape that renders an honest
  // "unknown" status so the page never throws on init.
  var INIT = (function () {
    try {
      var node = document.getElementById("admin-init");
      return JSON.parse((node && node.textContent) || "{}") || {};
    } catch (e) { return {}; }
  })();
  var STATUS = (INIT && INIT.status) || {};

  // ── Helpers ────────────────────────────────────────────────────────
  function fmtTs(iso) {
    if (!iso) return "Never";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso).slice(0, 19);
    try {
      return new Intl.DateTimeFormat("en-US", {
        weekday: "short", month: "short", day: "numeric",
        hour: "numeric", minute: "2-digit", timeZone: "America/New_York",
      }).format(d) + " ET";
    } catch (e) { return String(iso).slice(0, 19); }
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }

  // ── Toast ──────────────────────────────────────────────────────────
  // Bottom-right slide-in stack.  .ad-toast + .show transition lives in
  // admin.html's <style> block.  3 s auto-dismiss matches the spec.
  function toast(msg, kind) {
    var stack = document.getElementById("ad-toast-stack");
    if (!stack) return;
    var borderClass = ({
      ok:    "border-l-pos",
      error: "border-l-neg",
      info:  "border-l-accent",
    })[kind] || "border-l-gray-500";

    var el = document.createElement("div");
    el.className =
      "ad-toast pointer-events-auto bg-card text-gray-100 " +
      "border border-border " + borderClass + " border-l-4 " +
      "rounded-lg px-4 py-2.5 text-[12.5px] font-semibold shadow-xl " +
      "min-w-[220px] max-w-[360px]";
    el.textContent = String(msg == null ? "" : msg);
    stack.appendChild(el);

    requestAnimationFrame(function () { el.classList.add("show"); });
    setTimeout(function () {
      el.classList.remove("show");
      setTimeout(function () {
        if (el.parentNode) el.parentNode.removeChild(el);
      }, 220);
    }, 3000);
  }

  // ── STATUS grid ────────────────────────────────────────────────────
  // 3 cards from init_data.status:
  //   • Storage backend (Supabase on/off)
  //   • MLB last analyzed
  //   • WNBA last analyzed
  // Each card: dot + LABEL above + value text.  Tailwind only.
  function renderStatus() {
    var grid = document.getElementById("ad-status-grid");
    if (!grid) return;

    var db = STATUS.db || {};
    var dbOk = !!db.supabase;
    var rows = [
      {
        label: "STORAGE",
        value: dbOk ? "Supabase connected" : "JSON file (Supabase off)",
        ok: dbOk,
      },
      {
        label: "MLB LAST ANALYZED",
        value: fmtTs(STATUS.mlb_analyzed_at),
        ok: !!STATUS.mlb_analyzed_at,
      },
      {
        label: "WNBA LAST ANALYZED",
        value: fmtTs(STATUS.wnba_analyzed_at),
        ok: !!STATUS.wnba_analyzed_at,
      },
    ];

    grid.innerHTML = rows.map(function (r) {
      var dotCls = r.ok ? "bg-pos" : "bg-neg";
      return (
        '<div class="bg-card border border-border rounded-xl shadow-sm p-5 ' +
              'flex flex-col gap-2">' +
          '<div class="flex items-center gap-2">' +
            '<span class="inline-block w-2 h-2 rounded-full ' + dotCls + '" ' +
                  'aria-hidden="true"></span>' +
            '<span class="text-[10px] font-extrabold tracking-widest text-gray-500">' +
              esc(r.label) +
            '</span>' +
          '</div>' +
          '<div class="text-[14px] font-bold text-white font-mono truncate">' +
            esc(r.value) +
          '</div>' +
        '</div>'
      );
    }).join("");
  }

  // ── ACTIONS ────────────────────────────────────────────────────────
  // Every button with [data-endpoint] POSTs to that path on click.  While
  // in flight the label is swapped to a spinner and the button is
  // disabled; toast announces the outcome.  Network/HTTP errors are
  // caught and surfaced — never throw up to the page.
  function postAction(btn) {
    var url   = btn.getAttribute("data-endpoint");
    var label = btn.getAttribute("data-label") || "Done";
    if (!url || btn.disabled) return;

    var original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="ad-spin"></span>';

    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "Accept":       "application/json",
      },
      body: JSON.stringify({}),
    }).then(function (resp) {
      return resp.json()
        .catch(function () { return {}; })
        .then(function (data) { return { ok: resp.ok, status: resp.status, data: data }; });
    }).then(function (r) {
      if (r.ok && r.data && r.data.success !== false) {
        toast(label, "ok");
      } else {
        var msg = (r.data && (r.data.error || r.data.message))
                  || ("Request failed (HTTP " + r.status + ")");
        toast(msg, "error");
      }
    }).catch(function (err) {
      toast("Network error: " + (err && err.message ? err.message : "unknown"),
            "error");
    }).finally(function () {
      btn.disabled = false;
      btn.innerHTML = original;
    });
  }

  function wireActions() {
    var btns = document.querySelectorAll("button[data-endpoint]");
    Array.prototype.forEach.call(btns, function (btn) {
      btn.addEventListener("click", function () { postAction(btn); });
    });
  }

  // ── Init ───────────────────────────────────────────────────────────
  function init() {
    renderStatus();
    wireActions();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
