/* research.js -- client for the Flask + Tailwind /research dashboard.
 *
 * All filter / sort state lives in the URL query string -- the server
 * does the aggregation + sort and returns a fully rendered page.  This
 * file only translates chip / header clicks into a new URL and navigates.
 *
 * Mirrors NiceGUI's exclusive-"all" multi-select semantics from
 * pages/research.py:_set_multi -- picking any concrete value drops "all";
 * clearing all values restores "all"; "all" itself acts as a clear. */
(function () {
  "use strict";

  function readState() {
    try {
      var node = document.getElementById("rs-init");
      var vm = JSON.parse((node && node.textContent) || "{}");
      return (vm && vm.filters) ? Object.assign({}, vm.filters) : {};
    } catch (e) { return {}; }
  }

  // ── Build a /research URL from a filter-state dict ────────────────
  // Omits values equal to their defaults so the address bar stays clean
  // when only a couple of filters are non-default.
  function buildUrl(state) {
    var p = new URLSearchParams();
    function setMulti(key, val) {
      var arr = (val || []).filter(function (v) { return v && v !== "all"; });
      if (arr.length) p.set(key, arr.join(","));
    }
    function setSingle(key, val) {
      if (val && val !== "all") p.set(key, val);
    }
    setMulti("pred",   state.pred);
    setMulti("review", state.review);
    setMulti("bets",   state.bets);
    setSingle("sport",  state.sport);
    setSingle("window", state.window);
    if (state.sort_key && state.sort_key !== "win_pct") p.set("sort_key", state.sort_key);
    if (state.sort_dir && state.sort_dir !== "desc")   p.set("sort_dir", state.sort_dir);
    var qs = p.toString();
    return "/research" + (qs ? ("?" + qs) : "");
  }

  // Exclusive-"all" multi-select toggle (matches NiceGUI _set_multi).
  function toggleMulti(arr, value) {
    var set = new Set(arr || []);
    if (value === "all") {
      return ["all"];
    }
    if (set.has(value)) {
      set.delete(value);
    } else {
      set.add(value);
    }
    set.delete("all");
    if (!set.size) return ["all"];
    return Array.from(set).sort();
  }

  function onChipClick(e) {
    var btn = e.target.closest(".rs-chip");
    if (!btn) return;
    e.preventDefault();
    var key   = btn.getAttribute("data-key");
    var value = btn.getAttribute("data-value");
    var multi = btn.getAttribute("data-multi") === "1";
    var state = readState();
    if (multi) {
      state[key] = toggleMulti(state[key], value);
    } else {
      // Re-clicking the active single-select chip is a no-op.
      if (state[key] === value) return;
      state[key] = value;
    }
    window.location = buildUrl(state);
  }

  // Headers are <th tabindex="0" role="button">, so they can take focus.
  // Enter / Space trigger sort the same way a click would.
  function onHeaderActivate(e) {
    if (e.type === "keydown" && e.key !== "Enter" && e.key !== " ") return;
    var th = e.target.closest(".rs-th");
    if (!th) return;
    var key = th.getAttribute("data-sort");
    if (!key) return;
    if (e.type === "keydown") e.preventDefault();
    var state = readState();
    if (state.sort_key === key) {
      state.sort_dir = (state.sort_dir === "desc" ? "asc" : "desc");
    } else {
      state.sort_key = key;
      state.sort_dir = "desc";
    }
    window.location = buildUrl(state);
  }

  function init() {
    var filters = document.getElementById("rs-filters");
    if (filters) filters.addEventListener("click", onChipClick);
    var ths = document.querySelectorAll(".rs-th[data-sort]");
    for (var i = 0; i < ths.length; i++) {
      ths[i].addEventListener("click",   onHeaderActivate);
      ths[i].addEventListener("keydown", onHeaderActivate);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
