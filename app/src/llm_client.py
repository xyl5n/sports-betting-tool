"""
src/llm_client.py
=================
Local-first LLM client for the sports-betting app.

Two-pass pipeline
-----------------
  Pass 1 (fast):  Ollama qwen3:8b with think=False -- a quick JSON verdict for
                  every prop.  See ``fast_verdict``.
  Pass 2 (deep):  Ollama qwen3:8b with think=True -- deeper analysis, run after
                  Pass 1, highest verdict tier first (see ``sort_by_tier``).
                  See ``deep_analysis``.
  Fallback:       Groq (the existing budget-aware multi-model client) -- a
                  silent fallback used when an Ollama response can't be parsed
                  into JSON.

Ollama unreachability is handled by an infinite retry loop in
``_call_ollama_with_retry`` (it waits RETRY_INTERVAL between attempts and only
returns once Ollama answers); the Groq fallback kicks in on a *parse* failure.

No dependencies beyond ``requests`` and the standard library.  The only
project import is a lazy ``from .groq_models import generate`` inside
``_call_groq`` so this module stays import-cheap and free of import cycles.
"""
from __future__ import annotations

import heapq
import itertools
import json
import logging
import os
import re
import threading
import time
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────

OLLAMA_BASE_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL    = "qwen3:8b"
OLLAMA_TIMEOUT  = 120          # seconds, per HTTP attempt
RETRY_INTERVAL  = 600          # seconds (10 min) between retry attempts

# Verdict tiers, highest priority first.  Drives sort_by_tier (Pass 2 order).
TIER_PRIORITY = ["Strong Lean", "Lean", "Slight Lean", "Neutral"]


# ── Ollama: single attempt ──────────────────────────────────────────────────--

def _call_ollama(system: str, user: str, max_tokens: int = 900,
                 think: bool = False) -> Optional[str]:
    """One POST to Ollama's /api/chat endpoint.  Returns the assistant's raw
    text, or None on any failure (connection refused, timeout, bad response).

    *think* toggles the model's reasoning mode (qwen3:8b supports it); *max_tokens*
    maps to Ollama's ``options.num_predict``.
    """
    url = f"{OLLAMA_BASE_URL}/api/chat"
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "stream":  False,
        "think":   think,
        "options": {"num_predict": max_tokens},
    }
    try:
        resp = requests.post(url, json=payload, timeout=OLLAMA_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        # /api/chat (stream=False) returns {"message": {"role", "content"}, ...}.
        text = ((data.get("message") or {}).get("content") or "").strip()
        return text or None
    except requests.exceptions.ConnectionError as exc:
        log.warning("Ollama connection error (%s): %s", url, exc)
        return None
    except requests.exceptions.Timeout as exc:
        log.warning("Ollama timeout after %ss (%s): %s", OLLAMA_TIMEOUT, url, exc)
        return None
    except Exception as exc:                                              # noqa: BLE001
        log.warning("Ollama call failed (%s): %s: %s",
                    url, type(exc).__name__, exc)
        return None


# ── Ollama: infinite retry wrapper ────────────────────────────────────────────

def _call_ollama_with_retry(system: str, user: str, max_tokens: int = 900,
                            think: bool = False) -> str:
    """Call _call_ollama, retrying forever (sleeping RETRY_INTERVAL between
    attempts) until Ollama returns a non-empty response.  Returns that raw text.

    Used because Ollama may be temporarily down (e.g. model still loading); the
    pipeline waits it out rather than failing.  Parse-level failures are handled
    by the caller's Groq fallback, not here.
    """
    attempt = 0
    while True:
        attempt += 1
        mode = "think" if think else "fast"
        log.info("Ollama attempt #%d (mode=%s, model=%s)",
                 attempt, mode, OLLAMA_MODEL)
        text = _call_ollama(system, user, max_tokens=max_tokens, think=think)
        if text is not None:
            return text
        log.warning("Ollama unreachable on attempt #%d (mode=%s); retrying in %ds",
                    attempt, mode, RETRY_INTERVAL)
        time.sleep(RETRY_INTERVAL)


# ── JSON extraction ───────────────────────────────────────────────────────────

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _extract_json(text: Optional[str]) -> Optional[dict]:
    """Strip any <think>...</think> blocks (safety net for reasoning models),
    then extract and parse the FIRST balanced {...} JSON object in *text*.
    Returns the parsed dict, or None when nothing parseable is found.
    """
    if not text:
        return None
    cleaned = _THINK_RE.sub("", text)

    start = cleaned.find("{")
    if start == -1:
        return None

    # Walk forward tracking brace depth (ignoring braces inside strings) to find
    # the close of the first object -- more robust than a greedy last-} grab.
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = cleaned[start:i + 1]
                try:
                    obj = json.loads(candidate)
                    return obj if isinstance(obj, dict) else None
                except (ValueError, TypeError) as exc:
                    log.warning("JSON parse failed: %s", exc)
                    return None
    return None


# ── Groq fallback ─────────────────────────────────────────────────────────────

def _call_groq(system: str, user: str, max_tokens: int = 900) -> Optional[dict]:
    """Silent Groq fallback via the existing budget-aware multi-model client.
    Returns a parsed dict (through _extract_json) or None on any failure.
    """
    try:
        from .groq_models import generate          # lazy: avoid import cycles
        text, _version = generate(f"{system}\n\n{user}",
                                  prefer="V4", max_tokens=max_tokens)
        return _extract_json(text)
    except Exception as exc:                                              # noqa: BLE001
        log.warning("Groq fallback failed: %s: %s", type(exc).__name__, exc)
        return None


# ── Pass 1: fast verdict ──────────────────────────────────────────────────────

def fast_verdict(system: str, user: str,
                 max_tokens: int = 900) -> tuple[Optional[dict], str]:
    """Pass 1 entry point: fast JSON verdict via Ollama (think=False).

    Returns ``(parsed_dict, source_label)`` where source_label is one of:
      "ollama_fast"   -- parsed from the Ollama response
      "groq_fallback" -- Ollama text didn't parse; Groq fallback succeeded
      "parse_error"   -- neither produced parseable JSON (dict is None)
    """
    raw = _call_ollama_with_retry(system, user, max_tokens=max_tokens, think=False)
    parsed = _extract_json(raw)
    if parsed is not None:
        return parsed, "ollama_fast"

    log.info("fast_verdict: Ollama response unparseable; trying Groq fallback")
    parsed = _call_groq(system, user, max_tokens=max_tokens)
    if parsed is not None:
        return parsed, "groq_fallback"

    log.warning("fast_verdict: both Ollama and Groq failed to parse")
    return None, "parse_error"


# ── Pass 2: deep analysis ─────────────────────────────────────────────────────

def deep_analysis(system: str, user: str,
                  max_tokens: int = 1200) -> tuple[Optional[dict], str]:
    """Pass 2 entry point: deep analysis via Ollama (think=True).

    Returns ``(parsed_dict, source_label)`` where source_label is one of:
      "ollama_deep"   -- parsed from the Ollama response
      "groq_fallback" -- Ollama text didn't parse; Groq fallback succeeded
      "parse_error"   -- neither produced parseable JSON (dict is None)
    """
    raw = _call_ollama_with_retry(system, user, max_tokens=max_tokens, think=True)
    parsed = _extract_json(raw)
    if parsed is not None:
        return parsed, "ollama_deep"

    log.info("deep_analysis: Ollama response unparseable; trying Groq fallback")
    parsed = _call_groq(system, user, max_tokens=max_tokens)
    if parsed is not None:
        return parsed, "groq_fallback"

    log.warning("deep_analysis: both Ollama and Groq failed to parse")
    return None, "parse_error"


# ── Tier ordering (Pass 2 runs highest tier first) ────────────────────────────

def sort_by_tier(props: list[dict]) -> list[dict]:
    """Sort fast_verdict result dicts by ``verdict_tier`` per TIER_PRIORITY
    (highest first).  Unrecognized / missing tiers sort to the end.  Stable."""
    rank = {tier: i for i, tier in enumerate(TIER_PRIORITY)}
    end = len(TIER_PRIORITY)
    return sorted(props, key=lambda p: rank.get((p or {}).get("verdict_tier"), end))


# ── Pass-2 scheduling queue ──────────────────────────────────────────────────-
# A single shared priority queue feeds Pass 2 (deep analysis).  On every 15-min
# pull, Pass 1 runs the new picks (fast) and pushes them onto this heap; the
# Pass-2 loop pops the best-ranked pick each iteration.  Because it's a heap,
# a fresh high-confidence pick lands at the front the moment Pass 2 reaches for
# its next item -- but Pass 2 is only ever interrupted BETWEEN picks: it always
# finishes the analysis in flight before re-checking the queue.
#
# Priority order (smaller == earlier out of the heap):
#   1. verdict tier rank, reusing TIER_PRIORITY (the module's single source of
#      truth; "Slight Lean" and unknown tiers handled the same as sort_by_tier);
#   2. then higher confidence first (re-sort by confidence within a tier);
#   3. then a monotonic sequence number -- stable FIFO tie-break that also keeps
#      heapq from ever having to compare the (unorderable) pick dicts.

_pass2_queue: list = []                 # heap of (rank, -confidence, seq, pick)
_queue_lock = threading.Lock()          # guards _pass2_queue
_queue_seq = itertools.count()          # monotonic tie-break counter
_pass2_stop = threading.Event()         # set -> Pass-2 loop exits between picks

# Worker hooks: callables that actually run an analysis for ONE pick.  Injected
# by the orchestrator (e.g. player_ai_breakdown) so this module stays decoupled
# from prompt-building -- no import cycle.  A worker takes the pick dict and
# does the work (calling fast_verdict / deep_analysis with the right prompts).
_pass1_worker: Optional[Callable] = None
_pass2_worker: Optional[Callable] = None

# Thread handle for the background Pass-2 loop (start_pass2_worker).
_pass2_thread: Optional[threading.Thread] = None
_pass2_thread_lock = threading.Lock()   # guards _pass2_thread (not the queue)


def set_workers(pass1: Optional[Callable] = None,
                pass2: Optional[Callable] = None) -> None:
    """Register the default per-pick worker callables.  pass1(pick) runs the
    fast verdict; pass2(pick) runs the deep analysis.  Either can also be passed
    per-call to run_pass1 / run_pass2_loop / on_api_pull to override these."""
    global _pass1_worker, _pass2_worker
    if pass1 is not None:
        _pass1_worker = pass1
    if pass2 is not None:
        _pass2_worker = pass2


def _pick_tier(pick: dict) -> Optional[str]:
    """The verdict tier on a pick, tolerating either key the pipeline uses."""
    if not isinstance(pick, dict):
        return None
    return pick.get("tier") or pick.get("verdict_tier")


def _tier_rank(tier: Optional[str]) -> int:
    """Heap rank for *tier* from TIER_PRIORITY; unknown/missing -> end."""
    try:
        return TIER_PRIORITY.index(tier)
    except ValueError:
        return len(TIER_PRIORITY)


def _pick_confidence(pick: dict) -> float:
    try:
        return float((pick or {}).get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def add_to_pass2_queue(picks: list[dict]) -> None:
    """Push picks onto the shared Pass-2 heap.  The heap re-sorts on every push,
    so merging new picks mid-run naturally re-prioritises by tier then
    confidence -- the next pop reflects the merged order."""
    with _queue_lock:
        for pick in picks or []:
            rank = _tier_rank(_pick_tier(pick))
            conf = _pick_confidence(pick)
            heapq.heappush(_pass2_queue, (rank, -conf, next(_queue_seq), pick))


def pass2_queue_size() -> int:
    with _queue_lock:
        return len(_pass2_queue)


def run_pass1(new_picks: list[dict],
              worker: Optional[Callable] = None) -> list[dict]:
    """Pass 1: run the fast verdict for each new pick (synchronously), then add
    the completed picks to the Pass-2 queue.  Returns the completed picks.
    *worker* defaults to the one registered via set_workers."""
    worker = worker or _pass1_worker
    completed: list[dict] = []
    for pick in new_picks or []:
        if worker is not None:
            try:
                worker(pick)
            except Exception as exc:                                      # noqa: BLE001
                log.warning("pass1 worker failed for pick %s: %s: %s",
                            (pick or {}).get("id"), type(exc).__name__, exc)
        completed.append(pick)
    add_to_pass2_queue(completed)
    log.info("pass1: ran %d pick(s); pass2 queue size now %d",
             len(completed), pass2_queue_size())
    return completed


def run_pass2_loop(worker: Optional[Callable] = None) -> None:
    """Pass 2: drain the shared queue, deep-analysing the best-ranked pick each
    iteration.  Interruptible ONLY between picks -- the current analysis always
    finishes before the queue is re-checked.  Exits when the queue is empty or
    _pass2_stop is set.  *worker* defaults to the one registered via
    set_workers."""
    worker = worker or _pass2_worker
    while not _pass2_stop.is_set():
        with _queue_lock:
            if not _pass2_queue:
                break
            _, _, _, pick = heapq.heappop(_pass2_queue)
        # Outside the lock: this is the only point Pass 2 can be "interrupted"
        # (between picks).  run_deep_analysis runs to completion here.
        if worker is not None:
            try:
                worker(pick)
            except Exception as exc:                                      # noqa: BLE001
                log.warning("pass2 worker failed for pick %s: %s: %s",
                            (pick or {}).get("id"), type(exc).__name__, exc)


def start_pass2_worker(worker: Optional[Callable] = None) -> threading.Thread:
    """Ensure the Pass-2 loop is running in a background daemon thread.  If it's
    already alive this is a no-op (returns the existing thread); if it drained
    and exited, a new one is started so a later pull's picks get processed."""
    global _pass2_thread
    with _pass2_thread_lock:
        if _pass2_thread is not None and _pass2_thread.is_alive():
            return _pass2_thread
        _pass2_stop.clear()
        _pass2_thread = threading.Thread(
            target=run_pass2_loop, kwargs={"worker": worker},
            name="pass2-loop", daemon=True,
        )
        _pass2_thread.start()
        return _pass2_thread


def stop_pass2_worker() -> None:
    """Signal the background Pass-2 loop to exit after its current pick."""
    _pass2_stop.set()


def on_api_pull(new_picks: list[dict],
                pass1_worker: Optional[Callable] = None,
                pass2_worker: Optional[Callable] = None) -> None:
    """Entry point for each 15-min pull.  Pass 1 runs first (synchronous) so the
    new picks are scored and merged into the Pass-2 queue (re-sorted by tier
    then confidence); then the background Pass-2 loop is (re)started so it picks
    them up -- resuming from the top of the queue on its next iteration."""
    run_pass1(new_picks, worker=pass1_worker)
    start_pass2_worker(worker=pass2_worker)
