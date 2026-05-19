"""
URL / log / traceback redactor.

Strip API keys, bearer tokens, and known env-var secret values out of any
string before it lands in stderr, JSON responses, or anywhere else a user
or operator might see it.

Use it like:

    from src.redact import redact
    _eprint(redact(traceback.format_exc()))
    jsonify({"error": redact(str(exc))})

Three layers of defense, in priority order:

1. Query-string keys -- requests like
   https://api.the-odds-api.com/...?apiKey=ABC123 surface in raw
   HTTPError messages.  We rewrite `apiKey=ABC123` -> `apiKey=REDACTED`
   (also `api_key`, `key`, `token`, `access_token`).

2. Authorization headers -- if a header somehow lands in a string we
   blank the credential after `Authorization: Bearer ` / `Authorization: Token `.

3. Verbatim secret values -- last-line defense.  We read the configured
   env-var secrets at module import time and string-replace any verbatim
   occurrence.  Catches free-form messages that don't match the URL or
   header shapes above (e.g. a credential pasted into a stack trace).
"""
from __future__ import annotations

import os
import re

# Query params (any case) that hold a credential
_QUERY_PARAM_RE = re.compile(
    r"(?i)([?&](?:apikey|api[_-]?key|key|token|access[_-]?token)=)([^&\s\"'<>]+)"
)

# Authorization: Bearer ...  /  Authorization: Token ...
_AUTH_HEADER_RE = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer|token)\s+)\S+"
)

# Env vars whose values are secrets.  Anything else in the process env is
# considered non-sensitive (e.g. SEASON, BANKROLL).  Long-enough values
# only -- avoids stripping short non-secret placeholders.
_SECRET_ENV_NAMES = (
    "ODDS_API_KEY",
    "API_SPORTS_KEY",
    "ANTHROPIC_API_KEY",
    "BALLDONTLIE_API_KEY",
    "SPORTSDATAIO_API_KEY",
    "SUPABASE_KEY",
)


def _secret_values() -> tuple[str, ...]:
    """Snapshot the env-var secrets that are currently set + long enough
    to be worth scrubbing.  Re-evaluated each call so a late-binding
    secret (rotated key, etc.) is picked up without a restart."""
    out: list[str] = []
    for name in _SECRET_ENV_NAMES:
        v = (os.environ.get(name) or "").strip()
        if len(v) >= 16:
            out.append(v)
    return tuple(out)


def redact(s: object) -> str:
    """Coerce *s* to str and remove credentials.  Never raises.

    Returns the empty string for None / falsy input.  Always returns
    str so callers can safely pipe through f-strings.
    """
    if s is None:
        return ""
    try:
        text = s if isinstance(s, str) else str(s)
    except Exception:                                                     # noqa: BLE001
        return "<unstringifiable object>"
    if not text:
        return text

    text = _QUERY_PARAM_RE.sub(r"\1REDACTED", text)
    text = _AUTH_HEADER_RE.sub(r"\1REDACTED", text)
    for v in _secret_values():
        text = text.replace(v, "REDACTED")
    return text
