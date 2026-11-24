"""chatbot.utils.rate_limit: a fixed-window counter in the shared cache."""

from unittest.mock import patch

import pytest

from chatbot.utils.rate_limit import within_rate_limit


@pytest.fixture
def fresh_cache(settings):
    settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    }
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def test_allows_up_to_the_limit_then_blocks(fresh_cache):
    assert all(within_rate_limit("k", 3, 60) for _ in range(3))
    assert not within_rate_limit("k", 3, 60)


def test_keys_are_independent(fresh_cache):
    assert within_rate_limit("a", 1, 60)
    assert not within_rate_limit("a", 1, 60)
    assert within_rate_limit("b", 1, 60)


def test_fails_open_when_the_cache_raises(fresh_cache):
    from django.core.cache import cache

    with patch.object(cache, "add", side_effect=ConnectionError("redis down")):
        assert within_rate_limit("k", 0, 60)
