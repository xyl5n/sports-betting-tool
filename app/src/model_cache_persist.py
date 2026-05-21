"""
Persist joblib model files across Railway restarts via Supabase app_cache.

Railway's filesystem is ephemeral: every redeploy / container restart
wipes the working directory's `.cache/` subtree -- where each model's
`train_or_load` stores its joblib snapshot.  Without persistence, every
deploy triggers a full retrain on the next analyze run (slow + burns
the season-data API quota), AND the trained-from-scratch model
diverges from whatever the nightly retrain produced.

This module mirrors model joblib files to / from the `app_cache`
table (jsonb `data` column, base64-encoded blob).  Same persistence
path the analysis_cache + daily_snapshot caches already use, so a
single Supabase outage or schema change covers everything.

Public surface
--------------
  cache_key_for(local_path) -> str         # app_cache key for the file
  try_download(local_path)  -> bool        # fill the file if local missing
  upload(local_path)        -> bool        # push local file to Supabase
  inventory(local_paths)    -> list[dict]  # boot-time report rows

All public functions:
  - never raise (log + return False on any failure)
  - no-op when Supabase isn't configured (`db.is_supabase()` False)
  - print structured `MODEL CACHE:` lines to stderr so the persistence
    path is observable in Railway logs without setting any log level
"""
from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path

from . import db as _db


_CACHE_KEY_PREFIX = "model:cache:"

# app_cache.date is normally an ET YYYY-MM-DD string; the stale-cache
# cleaner (cache_delete_stale) deletes any row whose date != today.
# Use a non-date sentinel so the model cache survives the daily purge.
_MODEL_DATE_TAG = "model"


def cache_key_for(local_path: Path | str) -> str:
    """Stable app_cache key for one model file.  Uses just the file's
    name (not the parent directory) so a future refactor moving
    .cache/ to .anywhere/ keeps the key stable."""
    return f"{_CACHE_KEY_PREFIX}{Path(local_path).name}"


def _sha256_short(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:12]


def _eprint(msg: str) -> None:
    print(msg, flush=True, file=sys.stderr)


def try_download(local_path: Path) -> bool:
    """If `local_path` doesn't exist on disk, attempt to materialize it
    from Supabase app_cache.  Returns True iff a file ends up at
    `local_path` (either because it was already there, or because the
    download succeeded).

    Safe to call multiple times; cheap when the file already exists."""
    p = Path(local_path)
    if p.exists():
        return True
    if not _db.is_supabase():
        return False
    try:
        row = _db.cache_get(cache_key_for(p))
        if not row or not isinstance(row.get("data"), dict):
            return False
        b64 = row["data"].get("b64")
        if not b64:
            return False
        blob = base64.b64decode(b64)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)
        _eprint(
            f"MODEL CACHE: downloaded {p} "
            f"({len(blob):,} bytes, sha256={_sha256_short(blob)}) "
            f"from Supabase app_cache"
        )
        return True
    except Exception as exc:                                              # noqa: BLE001
        _eprint(
            f"MODEL CACHE: download failed for {p}: "
            f"{type(exc).__name__}: {exc}"
        )
        return False


def upload(local_path: Path) -> bool:
    """Push `local_path`'s bytes to Supabase app_cache under
    cache_key_for(local_path).  Returns True iff the upsert succeeded.

    Wrapped over the existing _db.cache_set(key, sport, date, data)
    helper -- same wire format as the analysis cache / snapshot, just
    a different key prefix + a non-date tag in the date column."""
    p = Path(local_path)
    if not p.exists():
        return False
    if not _db.is_supabase():
        return False
    try:
        blob = p.read_bytes()
        b64  = base64.b64encode(blob).decode("ascii")
        sha  = _sha256_short(blob)
        payload = {
            "b64":          b64,
            "size_bytes":   len(blob),
            "sha256_short": sha,
        }
        ok = _db.cache_set(cache_key_for(p), None, _MODEL_DATE_TAG, payload)
        if ok:
            _eprint(
                f"MODEL CACHE: uploaded {p} "
                f"({len(blob):,} bytes, sha256={sha}) "
                f"to Supabase app_cache"
            )
        else:
            _eprint(f"MODEL CACHE: upload skipped for {p} (Supabase off or write failed)")
        return bool(ok)
    except Exception as exc:                                              # noqa: BLE001
        _eprint(
            f"MODEL CACHE: upload failed for {p}: "
            f"{type(exc).__name__}: {exc}"
        )
        return False


def inventory(local_paths: list[Path]) -> list[dict]:
    """Return one row per path describing its presence locally + in
    Supabase.  Boot-time MODEL CACHE INVENTORY block in app.py prints
    this so the user can see at a glance what survived the restart."""
    rows: list[dict] = []
    sb_on = _db.is_supabase()
    for raw in local_paths:
        p = Path(raw)
        row = {
            "path":             str(p),
            "exists_locally":   p.exists(),
            "local_size_bytes": p.stat().st_size if p.exists() else 0,
            "supabase_present": False,
            "supabase_size":    0,
            "supabase_on":      sb_on,
        }
        if sb_on:
            try:
                rec = _db.cache_get(cache_key_for(p))
                if rec and isinstance(rec.get("data"), dict):
                    row["supabase_present"] = True
                    row["supabase_size"]    = int(rec["data"].get("size_bytes") or 0)
            except Exception:                                            # noqa: BLE001
                pass
        rows.append(row)
    return rows
