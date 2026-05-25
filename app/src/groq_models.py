"""
groq_models.py
==============
Multi-model Groq generation with budget-aware cascading + version labels.

Three models form a quality hierarchy; every breakdown is labelled with the
model that produced it (V1 / V2 / V3):

  V3  llama-3.3-70b-versatile               premium  (games, Pass-2 top props)
  V2  llama-3.1-8b-instant                  volume   (all props in Pass 1)
  V1  meta-llama/llama-4-scout-17b-16e-instruct  FALLBACK only

Why this exists: the old path always hit 8B and, once a model was rate
limited, kept hammering it and collecting 429s.  This module instead:
  * tracks each model's per-ET-day request + token budget in Supabase (so
    it survives Railway redeploys mid-run -- PostgREST only),
  * BEFORE each call checks the target model's remaining daily budget and
    CASCADES to the Scout fallback (V3->V1, V2->V1) when the target is
    exhausted, rather than calling an exhausted model,
  * enforces per-model spacing AND a rolling 60-second request/token window
    so we stay safely under the per-minute caps,
  * on a 429 it marks that model day-exhausted and cascades immediately.

`generate()` returns (text, version_label) so callers can store + display
which model produced the breakdown.  Best-effort: never raises.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone, timedelta

_ET = timezone(timedelta(hours=-4))


def _log(msg: str) -> None:
    print(f"GROQ-MODELS: {msg}", flush=True, file=sys.stderr)


def _today_et() -> str:
    return datetime.now(_ET).strftime("%Y-%m-%d")


# ── Model registry + designed-against limits ─────────────────────────────────
# Daily limits are the budgets we design AGAINST (slightly under the documented
# caps to leave headroom).  rpm/tpm drive the rolling-minute limiter; min_gap is
# the floor between consecutive calls to that model.
MODELS: dict[str, dict] = {
    "V3": {  # 70B premium
        "name": "llama-3.3-70b-versatile",
        "rpm": 30, "tpm": 12_000, "rpd": 1_000, "tpd": 100_000,
        "min_gap": 0.45,            # >= ~400ms between 70B calls (per spec)
    },
    "V2": {  # 8B volume (existing higher limits)
        "name": "llama-3.1-8b-instant",
        "rpm": 30, "tpm": 6_000, "rpd": 14_400, "tpd": 500_000,
        "min_gap": 0.15,            # existing safe 8B spacing
    },
    "V1": {  # Scout fallback
        "name": "meta-llama/llama-4-scout-17b-16e-instruct",
        "rpm": 30, "tpm": 30_000, "rpd": 1_000, "tpd": 500_000,
        "min_gap": 0.35,
    },
}
# Both premium and volume cascade DOWN to Scout when day-exhausted.
_FALLBACK = {"V3": "V1", "V2": "V1", "V1": None}

_VALID_VERSIONS = tuple(MODELS.keys())


def model_name(version: str) -> str:
    return (MODELS.get(version) or {}).get("name", "")


# ── Budget state (per ET day, mirrored to Supabase) ──────────────────────────
_BUDGET_KEY_PREFIX = "groq_budget_"
_lock = threading.RLock()
_state: dict = {"date": None, "usage": {}, "exhausted": set()}
# Rolling 60s windows of (ts, tokens) per model for the per-minute caps.
_window: dict[str, deque] = {v: deque() for v in MODELS}
_last_call: dict[str, float] = {v: 0.0 for v in MODELS}


def _budget_key(date_str: str) -> str:
    return f"{_BUDGET_KEY_PREFIX}{date_str}"


def _ensure_day_loaded() -> None:
    """Load today's budget from Supabase once (and on ET rollover).  Lets a
    redeployed process continue the same day's running totals."""
    today = _today_et()
    if _state["date"] == today:
        return
    usage = {v: {"req": 0, "tokens": 0} for v in MODELS}
    try:
        from . import db
        if db.is_supabase():
            row = db.cache_get(_budget_key(today))
            data = (row.get("data") if isinstance(row, dict) else None) or {}
            saved = data.get("usage") if isinstance(data, dict) else None
            if isinstance(saved, dict):
                for v in MODELS:
                    u = saved.get(v) or {}
                    usage[v] = {"req": int(u.get("req") or 0),
                                "tokens": int(u.get("tokens") or 0)}
    except Exception as exc:                                              # noqa: BLE001
        _log(f"budget load failed: {exc}")
    _state.update({"date": today, "usage": usage, "exhausted": set()})


def _flush_budget() -> None:
    try:
        from . import db
        if db.is_supabase():
            db.cache_set(_budget_key(_state["date"]), None, _state["date"],
                         {"usage": _state["usage"]})
    except Exception as exc:                                              # noqa: BLE001
        _log(f"budget flush failed: {exc}")


def _day_exhausted(version: str) -> bool:
    """True iff the model has hit its daily request OR token budget (or was
    flagged exhausted by a 429 earlier today)."""
    if version in _state["exhausted"]:
        return True
    u = _state["usage"].get(version) or {"req": 0, "tokens": 0}
    m = MODELS[version]
    return u["req"] >= m["rpd"] or u["tokens"] >= m["tpd"]


def remaining(version: str) -> dict:
    """Remaining daily budget for diagnostics: {req, tokens, exhausted}."""
    with _lock:
        _ensure_day_loaded()
        u = _state["usage"].get(version) or {"req": 0, "tokens": 0}
        m = MODELS[version]
        return {"req": max(0, m["rpd"] - u["req"]),
                "tokens": max(0, m["tpd"] - u["tokens"]),
                "exhausted": _day_exhausted(version)}


def _respect_spacing(version: str, est_tokens: int) -> None:
    """Sleep so we honour the per-model min gap AND the rolling 60s rpm/tpm
    caps before issuing a call."""
    m = MODELS[version]
    now = time.monotonic()
    # 1. per-model floor between consecutive calls
    gap = m["min_gap"] - (now - _last_call[version])
    if gap > 0:
        time.sleep(gap)
    # 2. rolling 60s window: drop stale, then wait if at the rpm/tpm edge
    win = _window[version]
    while True:
        cutoff = time.monotonic() - 60.0
        while win and win[0][0] < cutoff:
            win.popleft()
        req_in_win = len(win)
        tok_in_win = sum(t for _, t in win)
        if req_in_win < m["rpm"] and (tok_in_win + est_tokens) <= m["tpm"]:
            break
        # wait until the oldest entry ages out of the window
        sleep_for = max(0.05, (win[0][0] + 60.0) - time.monotonic()) if win else 0.5
        time.sleep(min(sleep_for, 2.0))


def _call_model(version: str, prompt: str, max_tokens: int) -> tuple:
    """Raw Groq call.  Returns (text|None, total_tokens, rate_limited_bool)."""
    try:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            return None, 0, False
        from groq import Groq
        client = Groq(api_key=api_key)
        resp = client.chat.completions.create(
            model=MODELS[version]["name"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.4,
        )
        text = (resp.choices[0].message.content or "").strip()
        toks = 0
        try:
            toks = int(getattr(resp.usage, "total_tokens", 0) or 0)
        except Exception:                                                 # noqa: BLE001
            toks = 0
        if not toks:                       # estimate when usage is absent
            toks = len(prompt) // 4 + max_tokens
        return (text or None), toks, False
    except Exception as exc:                                              # noqa: BLE001
        msg = str(exc).lower()
        rate_limited = ("429" in msg or "rate limit" in msg
                        or "too many requests" in msg
                        or type(exc).__name__ == "RateLimitError")
        _log(f"{version} call failed ({type(exc).__name__}): {exc}"
             + (" [rate-limited -> cascade]" if rate_limited else ""))
        return None, 0, rate_limited


def generate(prompt: str, *, prefer: str = "V2",
             max_tokens: int = 900) -> tuple:
    """Generate text on the preferred model, cascading to the Scout fallback
    when the preferred model is day-exhausted or rate-limited.

    Returns (text, version_label) on success, or (None, None) when every
    candidate is exhausted/failing.  Never raises."""
    if prefer not in MODELS:
        prefer = "V2"
    est = len(prompt) // 4 + max_tokens
    # Candidate order: preferred, then its fallback (Scout), de-duped.
    order, seen = [], set()
    for v in (prefer, _FALLBACK.get(prefer)):
        if v and v not in seen:
            order.append(v); seen.add(v)

    with _lock:
        _ensure_day_loaded()
        for version in order:
            if _day_exhausted(version):
                _log(f"{version} day-exhausted -> cascading")
                continue
            _respect_spacing(version, est)
            text, toks, rate_limited = _call_model(version, prompt, max_tokens)
            ts = time.monotonic()
            _last_call[version] = ts
            if rate_limited:
                _state["exhausted"].add(version)   # don't hammer it again today
                continue
            if text is None:
                continue                            # transient error -> fallback
            # record usage (req + tokens), update window + persist
            u = _state["usage"].setdefault(version, {"req": 0, "tokens": 0})
            u["req"] += 1
            u["tokens"] += toks
            _window[version].append((ts, toks))
            _flush_budget()
            return text, version
    return None, None


def usage_snapshot() -> dict:
    """Per-model usage + remaining for the day (diagnostics / admin)."""
    with _lock:
        _ensure_day_loaded()
        out = {}
        for v, m in MODELS.items():
            u = _state["usage"].get(v) or {"req": 0, "tokens": 0}
            out[v] = {"name": m["name"], "req_used": u["req"],
                      "req_limit": m["rpd"], "tokens_used": u["tokens"],
                      "tokens_limit": m["tpd"], "exhausted": _day_exhausted(v)}
        return out
