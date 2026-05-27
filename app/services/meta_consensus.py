"""
services/meta_consensus.py
==========================
Meta-Consensus: ONE batched compound-beta review of all of today's scored
props, layered on top of the in-house model signals to produce a consensus
tier per prop.

Why compound-beta, one request
------------------------------
compound-beta has no output-token limit (it can return ~50 verdict lines in a
single response) and search capability, and costs exactly 1 of the 250 daily
requests regardless of prop count.  So instead of N per-prop calls we send one
batched prompt and parse the response line by line.

Real-signals design (IMPORTANT)
-------------------------------
This codebase does NOT store 4 independent model votes per prop.  Each prop has
ONE consolidated model pick (props_model: side + confidence) plus ONE cached AI
breakdown whose ``verdict_tier`` states that model's stance on the pick.  So the
per-prop *base* signals used here are:

  * the model pick                              -- always present
  * the AI breakdown verdict_tier, when cached  -- Strong Lean/Lean = agree,
                                                   Fade = disagree, Neutral =
                                                   abstain (no vote)

compound-beta (this job) is the independent meta-reviewer on top of those.
``calculate_tier`` still implements the spec's 4-model table verbatim for the
canonical ``total_models == 4`` case and generalises by ratio for fewer
signals (with a guard so a lone model pick can't masquerade as consensus).

Storage: app_cache["meta_consensus_today"] (+ a local-file mirror so it works
without Supabase), regenerated daily by the 8:30 AM job.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

_CACHE_KEY        = "meta_consensus_today"
_LOCAL_PATH       = Path(".cache/meta_consensus_today.json")
_COMPOUND_VERSION = "V2"          # groq_models tier for compound-beta
_MAX_TOKENS       = 4000          # ample for ~50 short verdict lines
_STALE_HOURS      = 24
_LOAD_TTL         = 120.0         # in-process memo for UI reads

# In-process memo so prop-card rendering doesn't hit the cache once per card.
_memo: dict = {"at": 0.0, "data": None}


def _log(msg: str) -> None:
    print(f"META-CONSENSUS: {msg}", flush=True, file=sys.stderr)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Prop identity + labels ────────────────────────────────────────────────────

def _slug(player: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_",
                  (player or "").strip().lower())).strip("_")


def _market_token(market: str) -> str:
    """'pitcher_strikeouts' -> 'strikeouts', 'batter_home_runs' -> 'home_runs'."""
    m = (market or "").strip().lower()
    for pre in ("pitcher_", "batter_"):
        if m.startswith(pre):
            return m[len(pre):]
    return m


_MARKET_LABELS = {
    "pitcher_strikeouts": "Strikeouts", "pitcher_outs": "Outs Recorded",
    "pitcher_hits_allowed": "Hits Allowed", "pitcher_walks": "Walks Allowed",
    "pitcher_earned_runs": "Earned Runs Allowed",
    "batter_hits": "Hits", "batter_home_runs": "Home Runs",
    "batter_rbis": "RBIs", "batter_total_bases": "Total Bases",
    "batter_runs_scored": "Runs", "batter_walks": "Walks",
    "batter_strikeouts": "Strikeouts", "batter_stolen_bases": "Stolen Bases",
    "points": "Points", "rebounds": "Rebounds", "assists": "Assists",
}


def _market_label(market: str) -> str:
    return _MARKET_LABELS.get(market) or _market_token(market).replace("_", " ").title()


def _consensus_key(prop: dict) -> str:
    """Stable per-prop key, e.g. 'logan_gilbert_strikeouts'.  Computed the same
    way at write time (the job) and read time (the UI) so lookups match."""
    return f"{_slug(prop.get('player') or '')}_{_market_token(prop.get('market') or '')}"


def _opp(side: str) -> str:
    return "Under" if (side or "").title() == "Over" else "Over"


def _pct(conf) -> Optional[int]:
    try:
        c = float(conf)
    except (TypeError, ValueError):
        return None
    return int(round(c * 100)) if c <= 1.0 else int(round(c))


# ── Real per-prop base signals ────────────────────────────────────────────────

def _peek_breakdown(prop: dict) -> Optional[dict]:
    try:
        from src.player_ai_breakdown import peek_breakdown
        return peek_breakdown(prop)
    except Exception:                                                     # noqa: BLE001
        return None


def _model_name(version: str) -> str:
    try:
        from src.groq_models import model_name
        return model_name(version) or ""
    except Exception:                                                     # noqa: BLE001
        return ""


def _base_votes(prop: dict) -> list[dict]:
    """The real, independent in-house opinions on this prop's pick, as votes.

    Always includes the model pick; adds the cached AI breakdown verdict as a
    second vote (agree -> pick side, fade -> opposite, neutral -> abstain)."""
    pick_side = (prop.get("side") or "Over").strip().title()
    votes = [{"model": "props-model", "side": pick_side,
              "confidence": _pct(prop.get("confidence"))}]

    bd = _peek_breakdown(prop) or {}
    tier = (bd.get("verdict_tier") or "").strip().lower()
    if tier:
        mname = _model_name((bd.get("model_version") or "").strip()) or "ai-verdict"
        if tier in ("strong lean", "lean"):
            votes.append({"model": mname, "side": pick_side, "confidence": None})
        elif tier == "fade":
            votes.append({"model": mname, "side": _opp(pick_side), "confidence": None})
        # "neutral" -> abstain (no vote)
    return votes


def _majority(votes: list[dict]) -> tuple[str, int, int]:
    """Return (majority_side, majority_count, total_votes).  Ties favour Over
    (the model pick is the anchor and listed first)."""
    over  = sum(1 for v in votes if v["side"] == "Over")
    under = sum(1 for v in votes if v["side"] == "Under")
    total = len(votes)
    return ("Over", over, total) if over >= under else ("Under", under, total)


def _enumerate_props(scored_props: list[dict]) -> list[dict]:
    """Assign sequential PROP_IDs and attach the base votes + majority for each
    prop.  Single source of truth shared by build_batched_prompt and the
    prop_map used to parse the response."""
    entries: list[dict] = []
    for i, prop in enumerate(scored_props or [], 1):
        votes = _base_votes(prop)
        side, count, total = _majority(votes)
        entries.append({
            "prop_id":        f"PROP_{i:03d}",
            "prop":           prop,
            "key":            _consensus_key(prop),
            "votes":          votes,
            "majority_side":  side,
            "majority_count": count,
            "total":          total,
            "line":           prop.get("line"),
        })
    return entries


# ── Prompt ────────────────────────────────────────────────────────────────────

_PROMPT_HEADER = (
    "You are a sports betting analyst reviewing today's player props.\n"
    "Below is a list of props that have already been scored by our models. "
    "For each prop, I show you: the player, prop type, line, and what each "
    "model recommended (OVER or UNDER) with their confidence %.\n\n"
    "Your job: for each prop, independently evaluate whether you AGREE or "
    "DISAGREE with the majority model pick. Use your search capability to "
    "check any relevant recent news, injury reports, or matchup context.\n\n"
    "Respond ONLY in this exact format, one line per prop, nothing else:\n"
    "PROP_ID | AGREE | One sentence reason.\n"
    "PROP_ID | DISAGREE | One sentence reason."
)


def build_batched_prompt(scored_props: list) -> str:
    """Build the single batched compound-beta prompt from all scored props."""
    entries = _enumerate_props(scored_props)
    lines = [_PROMPT_HEADER, "", "Props to review:", ""]
    for e in entries:
        prop = e["prop"]
        side = (prop.get("side") or "Over").upper()
        lines.append(
            f"{e['prop_id']} | {prop.get('player')} | "
            f"{_market_label(prop.get('market') or '')} | {side} {prop.get('line')}"
        )
        for v in e["votes"]:
            conf = f" | {v['confidence']}%" if v.get("confidence") is not None else ""
            lines.append(f"  {v['model']}: {v['side'].upper()}{conf}")
        lines.append(
            f"  Majority: {e['majority_side'].upper()} "
            f"({e['majority_count']}/{e['total']})"
        )
        lines.append("")
    return "\n".join(lines)


# ── Tier logic ────────────────────────────────────────────────────────────────

def calculate_tier(majority_count: int, compound_agrees: bool,
                   total_models: int = 4) -> str:
    """Consensus tier from how many base models agree + whether compound agrees.

    For the canonical ``total_models == 4`` this reproduces the spec table
    exactly:
        4/4 + compound  -> UNANIMOUS
        3/4 + compound  -> STRONG
        3/4, no compound-> LEAN
        2/4             -> SPLIT
        <=1/4           -> FADE
    For fewer real signals it generalises by ratio.  A lone model pick
    (total < 2) can never reach UNANIMOUS/STRONG -- there's no second
    independent opinion to corroborate it."""
    if total_models <= 0:
        return "FADE"
    if total_models < 2:
        return "SPLIT" if compound_agrees else "FADE"
    ratio = majority_count / total_models
    if majority_count == total_models and compound_agrees:
        return "UNANIMOUS"
    if ratio >= 0.75:
        return "STRONG" if compound_agrees else "LEAN"
    if ratio >= 0.5:
        return "SPLIT"
    return "FADE"


# ── Response parsing ──────────────────────────────────────────────────────────

_LINE_RE = re.compile(
    r"^\s*(PROP_\d+)\s*\|\s*(AGREE|DISAGREE)\s*\|\s*(.*?)\s*$", re.IGNORECASE
)


def parse_consensus_response(response: str, prop_map: dict) -> dict:
    """Parse compound-beta's response line by line into a consensus dict keyed
    by each prop's consensus key.  Lines that don't match the expected
    'PROP_ID | AGREE/DISAGREE | reason' shape, or reference an unknown
    PROP_ID, are skipped (malformed-tolerant)."""
    out: dict = {}
    for raw in (response or "").splitlines():
        m = _LINE_RE.match(raw)
        if not m:
            continue
        pid, verdict, reason = m.group(1).upper(), m.group(2).upper(), m.group(3)
        entry = prop_map.get(pid)
        if not entry:
            continue
        compound_agrees = verdict == "AGREE"
        tier = calculate_tier(entry["majority_count"], compound_agrees,
                              entry.get("total", 4))
        out[entry["key"]] = {
            "tier":            tier,
            "side":            entry["majority_side"],
            "majority_side":   entry["majority_side"],
            "majority_count":  entry["majority_count"],
            "total_models":    entry.get("total", 4),
            "compound_agrees": compound_agrees,
            "compound_reason": reason,
            "line":            entry.get("line"),
        }
    return out


# ── Persistence ───────────────────────────────────────────────────────────────

def _store(result: dict) -> None:
    try:
        _LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LOCAL_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except Exception as exc:                                              # noqa: BLE001
        _log(f"local store failed: {exc}")
    try:
        from src import db
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        db.cache_set(_CACHE_KEY, None, today, result)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"supabase store failed: {exc}")
    _memo["data"] = result
    _memo["at"] = time.time()


def _read_raw() -> dict:
    try:
        from src import db
        row = db.cache_get(_CACHE_KEY)
        data = row.get("data") if isinstance(row, dict) else None
        if isinstance(data, dict):
            return data
    except Exception as exc:                                              # noqa: BLE001
        _log(f"supabase read failed: {exc}")
    try:
        if _LOCAL_PATH.exists():
            data = json.loads(_LOCAL_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:                                              # noqa: BLE001
        _log(f"local read failed: {exc}")
    return {}


def _is_stale(data: dict) -> bool:
    gen = data.get("generated_at")
    if not gen:
        return False
    try:
        dt = datetime.fromisoformat(str(gen).replace("Z", "+00:00"))
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt) > timedelta(hours=_STALE_HOURS)
    except Exception:                                                     # noqa: BLE001
        return False


def load_consensus() -> dict:
    """Today's consensus payload, or {} when missing/stale (>24h).  Memoised
    in-process for _LOAD_TTL so prop-card rendering doesn't refetch per card."""
    if _memo["data"] is not None and (time.time() - _memo["at"]) <= _LOAD_TTL:
        data = _memo["data"]
    else:
        data = _read_raw()
        _memo["data"] = data
        _memo["at"] = time.time()
    if not data or _is_stale(data):
        return {}
    return data


def consensus_for(prop: dict) -> Optional[dict]:
    """The stored consensus entry for *prop*, or None (graceful: missing/stale
    data simply yields no badge).  Safe to call from UI render paths."""
    try:
        return (load_consensus().get("props") or {}).get(_consensus_key(prop))
    except Exception:                                                     # noqa: BLE001
        return None


# ── Orchestration ─────────────────────────────────────────────────────────────

def _generate(prompt: str) -> tuple:
    """Single compound-beta (V2) request through the existing groq_models
    client (budget-aware, cascades to V4 only if V2 is exhausted)."""
    try:
        from src import groq_models
        return groq_models.generate(prompt, prefer=_COMPOUND_VERSION,
                                    max_tokens=_MAX_TOKENS)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"generate failed: {type(exc).__name__}: {exc}")
        return None, None


def run_meta_consensus() -> dict:
    """Full flow: read scored props -> build one batched prompt -> ONE
    compound-beta request -> parse -> store -> return.  Never raises; on a
    malformed/empty response it stores partial (or empty) results."""
    try:
        from src.props_scored_cache import load_scored_props
        cache = load_scored_props() or {}
    except Exception as exc:                                              # noqa: BLE001
        _log(f"scored cache read failed: {exc}")
        cache = {}

    props = list(cache.get("picks") or [])
    if not props:
        _log("today_props_all empty -- nothing to review, skipping")
        return {"generated_at": _now_iso(), "props": {}, "prop_count": 0,
                "skipped": "no_scored_props"}

    entries = _enumerate_props(props)
    prompt  = build_batched_prompt(props)
    _log(f"sending 1 batched request for {len(entries)} props to compound-beta")

    text, version = _generate(prompt)
    if not text:
        _log("compound-beta returned no text -- storing empty consensus")
        result = {"generated_at": _now_iso(), "model": version, "props": {},
                  "prop_count": len(entries), "parsed": 0,
                  "note": "empty_or_failed_response"}
        _store(result)
        return result

    prop_map  = {e["prop_id"]: e for e in entries}
    props_out = parse_consensus_response(text, prop_map)
    if not props_out:
        _log("compound-beta response parsed to 0 verdicts (malformed) -- "
             "storing empty consensus, not crashing")

    result = {
        "generated_at": _now_iso(),
        "model":        version,
        "prop_count":   len(entries),
        "parsed":       len(props_out),
        "props":        props_out,
    }
    _store(result)
    _log(f"stored consensus: {len(props_out)}/{len(entries)} props "
         f"(model={version})")
    return result
