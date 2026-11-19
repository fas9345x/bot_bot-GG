"""Conservative process-wide Bybit API pacing and 10006 circuit breaker.

No request is automatically retried here. In particular, order POST requests
must never be blindly replayed after an ambiguous network/API failure.
"""
from __future__ import annotations

import math
import os
import threading
import time


class RateLimitCooldown(RuntimeError):
    """A request was skipped because Bybit rate limiting is active."""


_LOCK = threading.RLock()
def _bounded_env_float(name, default, lower, upper):
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return min(upper, max(lower, value))


_MIN_REQUEST_INTERVAL = _bounded_env_float("BYBIT_MIN_REQUEST_INTERVAL_SECONDS", 0.5, 0.1, 5.0)
_BASE_COOLDOWN_SECONDS = _bounded_env_float("BYBIT_RATE_LIMIT_COOLDOWN_SECONDS", 120.0, 30.0, 600.0)
_MAX_COOLDOWN_SECONDS = _bounded_env_float("BYBIT_RATE_LIMIT_MAX_COOLDOWN_SECONDS", 900.0, 120.0, 3600.0)
_RESET_QUIET_SECONDS = 15 * 60.0
_NEXT_REQUEST_AT = 0.0
_COOLDOWN_UNTIL = 0.0
_LAST_LIMIT_AT = 0.0
_LIMIT_STREAK = 0


def _response_is_limited(response):
    if isinstance(response, dict):
        return str(response.get("retCode", "")) == "10006"
    status = getattr(response, "status_code", None)
    return status == 429


def _exception_is_limited(exc):
    if str(getattr(exc, "status_code", "")) in {"10006", "429"}:
        return True
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 429


def _start_cooldown_locked(now):
    global _COOLDOWN_UNTIL, _LAST_LIMIT_AT, _LIMIT_STREAK
    if now - _LAST_LIMIT_AT > _RESET_QUIET_SECONDS:
        _LIMIT_STREAK = 0
    _LIMIT_STREAK += 1
    delay = min(_BASE_COOLDOWN_SECONDS * (2 ** (_LIMIT_STREAK - 1)), _MAX_COOLDOWN_SECONDS)
    _LAST_LIMIT_AT = now
    _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, now + delay)
    return delay


def call_bybit(request, *, action="request", response_checker=None):
    """Serialize requests, pace them, and fail closed on rate-limit responses.

    ``request`` is a zero-argument callable. A detected 10006/HTTP 429 starts
    an exponential cooldown; the failing request is not retried.
    """
    global _NEXT_REQUEST_AT
    with _LOCK:
        now = time.monotonic()
        if now < _COOLDOWN_UNTIL:
            remaining = max(1, math.ceil(_COOLDOWN_UNTIL - now))
            raise RateLimitCooldown(
                f"Bybit rate-limit cooldown active ({remaining}s remaining); skipped {action}"
            )
        wait = max(0.0, _NEXT_REQUEST_AT - now)
        if wait:
            time.sleep(wait)
        try:
            response = request()
            limited = _response_is_limited(response)
            if response_checker is not None:
                limited = limited or bool(response_checker(response))
            if limited:
                delay = _start_cooldown_locked(time.monotonic())
                raise RateLimitCooldown(
                    f"Bybit rate limit 10006/HTTP 429 on {action}; no retry; cooldown {int(delay)}s"
                )
            return response
        except RateLimitCooldown:
            raise
        except Exception as exc:
            if not _exception_is_limited(exc):
                raise
            delay = _start_cooldown_locked(time.monotonic())
            raise RateLimitCooldown(
                f"Bybit rate limit 10006/HTTP 429 on {action}; no retry; cooldown {int(delay)}s"
            ) from exc
        finally:
            _NEXT_REQUEST_AT = max(time.monotonic(), _NEXT_REQUEST_AT) + _MIN_REQUEST_INTERVAL


def cooldown_remaining():
    with _LOCK:
        return max(0.0, _COOLDOWN_UNTIL - time.monotonic())


def _reset_for_tests():
    """Reset module state; only for isolated unit tests."""
    global _NEXT_REQUEST_AT, _COOLDOWN_UNTIL, _LAST_LIMIT_AT, _LIMIT_STREAK
    with _LOCK:
        _NEXT_REQUEST_AT = 0.0
        _COOLDOWN_UNTIL = 0.0
        _LAST_LIMIT_AT = 0.0
        _LIMIT_STREAK = 0
