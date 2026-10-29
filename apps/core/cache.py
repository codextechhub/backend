"""The shared cache a deployed server keeps in Redis, and what it does when Redis is down.

Rate limits count in the cache. Kept in each process's memory, every gunicorn
worker counted on its own and a restart wiped the count, so a limit of five
sign-ins a minute was really five per worker, started afresh on each deploy.
In Redis, the one Celery's broker already uses, every worker and every
restart reads the same count.

Redis can be unreachable, and a cache is not worth an outage. Every read
answers as a miss and every write is dropped, with a warning in the log, so
the API keeps serving: a rate limit sees no history and lets the request
through, and the values cached here (a provider's token, a bank list, a
cooldown) are fetched again. ``add`` answers True, as if the key were new,
because its callers use it as a cooldown and a missing cooldown only means
asking a provider again, while a refused one would stop a payment being
checked at all.
"""
from __future__ import annotations

import logging

from django.core.cache.backends.base import DEFAULT_TIMEOUT
from django.core.cache.backends.redis import RedisCache
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


def _unreachable(operation: str, exc: Exception) -> None:
    logger.warning("Cache %s skipped, Redis is unreachable: %s", operation, exc)


class ResilientRedisCache(RedisCache):
    """Django's Redis cache, answering a miss instead of raising when Redis is down."""

    def get(self, key, default=None, version=None):
        try:
            return super().get(key, default, version)
        except RedisError as exc:
            _unreachable("read", exc)
            return default

    def get_many(self, keys, version=None):
        try:
            return super().get_many(keys, version)
        except RedisError as exc:
            _unreachable("read", exc)
            return {}

    def has_key(self, key, version=None):
        try:
            return super().has_key(key, version)
        except RedisError as exc:
            _unreachable("read", exc)
            return False

    def set(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        try:
            return super().set(key, value, timeout, version)
        except RedisError as exc:
            _unreachable("write", exc)
            return None

    def add(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        try:
            return super().add(key, value, timeout, version)
        except RedisError as exc:
            _unreachable("write", exc)
            return True

    def delete(self, key, version=None):
        try:
            return super().delete(key, version)
        except RedisError as exc:
            _unreachable("write", exc)
            return False
