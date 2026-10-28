"""Login rate limiting and token helpers."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import defaultdict, deque


def constant_time_equals(left: str, right: str) -> bool:
    """Compare two strings without leaking timing information."""

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def valid_csrf(expected: str | None, provided: str | None) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(
        expected.encode("utf-8"), provided.encode("utf-8")
    )


def safe_next(target: str | None, fallback: str = "/") -> str:
    """Only allow local, absolute paths as post-login redirects."""

    if not target or not target.startswith("/"):
        return fallback
    if target.startswith("//") or "\\" in target:
        return fallback
    return target


class LoginLimiter:
    """Sliding-window limiter for failed login attempts per client."""

    def __init__(self, max_attempts: int = 10, window: int = 900) -> None:
        self.max_attempts = max_attempts
        self.window = window
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str) -> deque[float]:
        cutoff = time.monotonic() - self.window
        attempts = self._failures[key]
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        return attempts

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key)) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._prune(key).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
