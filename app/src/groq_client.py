"""
Groq API client -- a single helper for generating short AI summaries.

Used by src/ai_summaries.py to produce 1-3 sentence blurbs on game cards
and prop cards.  The model is llama-3.1-8b-instant (fast, free-tier
friendly).  GROQ_API_KEY comes from the environment (Railway dashboard).

Design rule: generate_summary NEVER raises.  On any failure -- missing
key, package not installed, network error, rate limit, malformed
response -- it returns None so the caller can silently skip the summary
and the UI never breaks.
"""
from __future__ import annotations

import os
import sys

_MODEL = "llama-3.1-8b-instant"


def generate_summary(prompt: str, max_tokens: int = 150) -> str | None:
    """Call Groq's chat-completions API and return the text, or None.

    Returns None silently on ANY error (no GROQ_API_KEY, groq package
    missing, network/rate-limit failure, empty response)."""
    try:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            return None
        from groq import Groq

        client = Groq(api_key=api_key)
        resp = client.chat.completions.create(
            model=_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.4,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or None
    except Exception as exc:                                              # noqa: BLE001
        # Best-effort only -- never let a summary failure surface to the UI.
        print(f"[groq_client] generate_summary failed: {type(exc).__name__}: {exc}",
              flush=True, file=sys.stderr)
        return None
