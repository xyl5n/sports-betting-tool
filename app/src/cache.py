import json
import time
from pathlib import Path


class Cache:
    """Simple file-based cache with per-entry TTL."""

    def __init__(self, cache_dir: str = ".cache"):
        self.dir = Path(cache_dir)
        self.dir.mkdir(exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = key.replace("/", "_").replace("?", "_").replace("&", "_").replace("=", "_")
        return self.dir / f"{safe}.json"

    def get(self, key: str, ttl: int = 86400):
        path = self._path(key)
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > ttl:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def set(self, key: str, value) -> None:
        try:
            self._path(key).write_text(json.dumps(value, default=str), encoding="utf-8")
        except OSError:
            pass

    def invalidate(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()
