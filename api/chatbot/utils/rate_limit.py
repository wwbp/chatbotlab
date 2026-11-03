from django.core.cache import cache


def within_rate_limit(key: str, limit: int, window_seconds: int) -> bool:
    """Count one hit on `key`; False once it has more than `limit` in the window.

    Fixed window in the shared cache. Fails open (allows) if the cache is
    unreachable: a Redis blip must not stop a running study. ponytail: fixed
    window, so a burst can straddle two windows; switch to a sliding window if
    that is ever abused.
    """
    try:
        if cache.add(key, 1, timeout=window_seconds):
            return True
        return cache.incr(key) <= limit
    # ValueError: the key expired between add() and incr(). Anything else: the
    # cache is down (with IGNORE_EXCEPTIONS, incr returns None instead).
    except Exception:
        return True
