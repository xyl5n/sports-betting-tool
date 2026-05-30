"""
src/llm_client.py
=================
Local-first LLM client for the sports-betting app.

Two-pass pipeline
-----------------
  Pass 1 (fast):  Ollama qwen3.6 with think=False -- a quick JSON verdict for
                  every prop.  See ``fast_verdict``.
  Pass 2 (deep):  Ollama qwen3.6 with think=True -- deeper analysis, run after
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

import json
import logging
import os
import re
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────

OLLAMA_BASE_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL    = "qwen3.6"
OLLAMA_TIMEOUT  = 120          # seconds, per HTTP attempt
RETRY_INTERVAL  = 600          # seconds (10 min) between retry attempts

# Verdict tiers, highest priority first.  Drives sort_by_tier (Pass 2 order).
TIER_PRIORITY = ["Strong Lean", "Lean", "Slight Lean", "Neutral"]


# ── Ollama: single attempt ──────────────────────────────────────────────────--

def _call_ollama(system: str, user: str, max_tokens: int = 900,
                 think: bool = False) -> Optional[str]:
    """One POST to Ollama's /api/chat endpoint.  Returns the assistant's raw
    text, or None on any failure (connection refused, timeout, bad response).

    *think* toggles the model's reasoning mode (qwen3.6 supports it); *max_tokens*
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
