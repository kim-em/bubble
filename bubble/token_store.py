"""Shared token storage, rate limiting, and logging infrastructure.

Used by both the relay daemon (relay.py) and auth proxy (auth_proxy.py).
Provides:
- TokenStore: file-based JSON persistence with fcntl locking and mtime caching
- RateLimiter: sliding-window rate limiter with configurable windows
- setup_file_logging: timestamped file-based logging
"""

import fcntl
import json
import logging
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class RateWindow:
    """A rate limiting window: max_count requests per seconds."""

    seconds: int
    max_count: int


class TokenStore:
    """File-based JSON token storage with fcntl locking and mtime caching.

    Provides atomic read-modify-write via file locking (preventing races
    when multiple processes generate tokens concurrently) and thread-safe
    lookups via mtime-based cache invalidation (for daemon hot-reload).
    """

    def __init__(self, path: Path):
        self._path = path
        self._tokens: dict = {}
        self._mtime: float = 0
        self._thread_lock = threading.Lock()

    def _file_lock(self):
        """Acquire exclusive file lock for writes."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(".lock")
        fd = lock_path.open("w")
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    @staticmethod
    def _file_unlock(fd):
        """Release the file lock."""
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()

    def _load(self) -> dict:
        """Load tokens from disk. Caller must hold file lock for writes."""
        if self._path.exists():
            try:
                return json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save(self, tokens: dict):
        """Atomically save tokens to disk with owner-only permissions."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(tokens))
        tmp.replace(self._path)
        os.chmod(str(self._path), 0o600)

    def generate(self, value: Any) -> str:
        """Generate a new token mapped to value, with file locking.

        Returns the hex token string.
        """
        fd = self._file_lock()
        try:
            token = secrets.token_hex(32)
            tokens = self._load()
            tokens[token] = value
            self._save(tokens)
            return token
        finally:
            self._file_unlock(fd)

    def remove(self, predicate: Callable[[Any], bool]):
        """Remove all tokens whose values match predicate, with file locking."""
        fd = self._file_lock()
        try:
            tokens = self._load()
            tokens = {t: v for t, v in tokens.items() if not predicate(v)}
            self._save(tokens)
        finally:
            self._file_unlock(fd)

    def lookup(self, token: str) -> Any | None:
        """Thread-safe token lookup with mtime-based cache invalidation."""
        with self._thread_lock:
            self._maybe_reload()
            return self._tokens.get(token)

    def _maybe_reload(self):
        """Reload from disk if the file has been modified."""
        try:
            st = self._path.stat()
            if st.st_mtime != self._mtime:
                self._tokens = self._load()
                self._mtime = st.st_mtime
        except FileNotFoundError:
            self._tokens = {}
            self._mtime = 0


class RateLimiter:
    """Sliding-window rate limiter with configurable windows.

    Supports per-key rate limits with multiple time windows,
    optional global rate limiting, and automatic eviction of
    stale tracking entries.
    """

    def __init__(
        self,
        windows: list[RateWindow],
        global_per_hour: int | None = None,
        max_tracked: int = 100,
    ):
        self._windows = windows
        self._global_per_hour = global_per_hour
        self._max_tracked = max_tracked
        self._requests: dict[str, deque] = {}
        self._global: deque = deque()
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        """Check if a request is allowed. Records it if so."""
        now = time.time()
        with self._lock:
            # Global rate limit
            if self._global_per_hour is not None:
                while self._global and self._global[0] < now - 3600:
                    self._global.popleft()
                if len(self._global) >= self._global_per_hour:
                    return False

            # Evict oldest key if too many tracked
            if key not in self._requests and len(self._requests) >= self._max_tracked:
                oldest_key = min(
                    self._requests,
                    key=lambda k: self._requests[k][-1] if self._requests[k] else 0,
                )
                del self._requests[oldest_key]

            q = self._requests.setdefault(key, deque())

            # Prune entries older than the largest window
            max_seconds = max(w.seconds for w in self._windows)
            while q and q[0] < now - max_seconds:
                q.popleft()

            # Check each window
            for w in self._windows:
                if w.seconds >= max_seconds:
                    count = len(q)
                else:
                    count = sum(1 for t in q if t > now - w.seconds)
                if count >= w.max_count:
                    return False

            q.append(now)
            if self._global_per_hour is not None:
                self._global.append(now)
            return True


def setup_file_logging(target_logger: logging.Logger, log_path: Path):
    """Configure timestamped file-based request logging."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(str(log_path))
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    target_logger.addHandler(handler)
    target_logger.setLevel(logging.INFO)
