/* lib.js -- shared utilities for the Flask/Tailwind pages.
 *
 * Exposes two functions on the global window.SBT namespace:
 *   SBT.toast(msg, kind)      -- bottom-right stack, 4 s auto-dismiss
 *   SBT.apiPost(url, body, opts) -- canonical POST flow with optimistic
 *                                   button flip, 409-as-dedup, revert on
 *                                   error, and lifecycle callbacks
 *
 * Designed to be loaded BEFORE any page-specific JS via a <script>
 * tag (no module system; vanilla globals).  Both /props and /mybets
 * include this file and call into it; props.js trackProp() is a thin
 * wrapper that hands /api/props/track over to SBT.apiPost with the
 * track-specific payload and toast text. */
;(function (global) {
  "use strict";
  if (global.SBT) return;           // idempotent: safe to load twice
  var SBT = {};

  // ── toast ─────────────────────────────────────────────────────────────
  // Requires a <div id="toast-stack"> in the page (any container with
  // that id works; the page's CSS owns the visual styling).  No-op when
  // the stack element is missing so this is safe to call on any page.
  function toast(msg, kind) {
    var stack = document.getElementById("toast-stack");
    if (!stack) return;
    var el = document.createElement("div");
    el.className = "toast " + (kind || "info");
    el.textContent = String(msg == null ? "" : msg);
    stack.appendChild(el);
    // Trigger CSS transition on next frame so the slide-in plays.
    requestAnimationFrame(function () { el.classList.add("show"); });
    setTimeout(function () {
      el.classList.remove("show");
      setTimeout(function () {
        if (el.parentNode) el.parentNode.removeChild(el);
      }, 220);
    }, 4000);
  }

  // ── apiPost ───────────────────────────────────────────────────────────
  // Canonical "POST something to a Flask endpoint, optimistically flip a
  // button, route success / 409-dedup / error through page callbacks"
  // body.  Extracted from props.js trackProp() (PR #337) so both /props
  // and /mybets share one fetch + state-machine implementation.
  //
  // Args:
  //   url   -- string, the /api/... path to POST to
  //   body  -- object, the JSON payload (stringified here)
  //   opts:
  //     btn          -- optional DOM button element.  When set, opts adds
  //                     `pendingClass` to it on flight, removes when the
  //                     response (or error) lands.  Page-specific success /
  //                     error visuals (textContent flip, .is-tracked etc)
  //                     are the caller's responsibility via the callbacks
  //                     below -- this keeps page palettes / labels out
  //                     of the shared lib
  //     pendingClass -- class added to btn while POST is in flight
  //                     (default "is-pending"; pages share the CSS name)
  //     pendingLabel -- optional textContent to set on btn while pending
  //                     (e.g. "Tracked ✓" -- pages opt in)
  //     dedupRegex   -- regex tested against data.error for the dedup
  //                     branch (default /already tracked/i -- the
  //                     ledger / props_picks_tracker convention).  HTTP
  //                     409 always routes to onDedup regardless
  //     onSuccess(data)       -- 2xx + data.success !== false
  //     onDedup(data, status) -- 409 OR error matches dedupRegex
  //     onError(err, data, status) -- everything else; `err` is the
  //                                   fetch reject reason (null for HTTP
  //                                   errors), `status` is the HTTP code
  //                                   (0 for network failure)
  //     afterAttempt() -- finally callback fired regardless of outcome
  //
  // Returns the Promise so callers can chain `.then` if they want, but
  // the callback-based API is the primary surface.
  function apiPost(url, body, opts) {
    opts = opts || {};
    var btn          = opts.btn || null;
    var pendingClass = opts.pendingClass || "is-pending";
    var dedupRegex   = opts.dedupRegex || /already tracked/i;

    if (btn) {
      btn.classList.add(pendingClass);
      btn.disabled = true;
      if (opts.pendingLabel != null) btn.textContent = opts.pendingLabel;
    }

    return fetch(url, {
      method:  "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept":       "application/json",
      },
      body: JSON.stringify(body || {}),
    }).then(function (resp) {
      return resp.json()
        .catch(function () { return {}; })
        .then(function (data) { return { resp: resp, data: data }; });
    }).then(function (r) {
      if (btn) btn.classList.remove(pendingClass);
      var data = r.data || {};
      var status = r.resp.status;
      // Server "success" convention: 2xx + (no `success` key OR success
      // !== false).  The Flask routes return `{success: true, ...}` on
      // ok and `{error: ...}` on failure; some legacy routes omit the
      // success flag entirely on 200, so we treat absent as ok.
      if (r.resp.ok && data.success !== false) {
        if (opts.onSuccess) opts.onSuccess(data);
      } else if (status === 409 ||
                 (data.error && dedupRegex.test(String(data.error)))) {
        if (opts.onDedup) opts.onDedup(data, status);
      } else {
        if (opts.onError) opts.onError(null, data, status);
      }
    }).catch(function (err) {
      if (btn) btn.classList.remove(pendingClass);
      if (opts.onError) opts.onError(err, null, 0);
    }).finally(function () {
      if (opts.afterAttempt) opts.afterAttempt();
    });
  }

  SBT.toast   = toast;
  SBT.apiPost = apiPost;
  global.SBT  = SBT;
})(window);
