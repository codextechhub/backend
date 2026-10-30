"""``/v1/health/live/``: the public answer to "is this API able to serve?".

Every other health route needs a signed-in platform operator, so nothing
outside the platform could tell a broken API from a working one: Render kept
sending traffic to an instance that could not reach its database, and a deploy
that came up broken went live. Render's health check and an outside uptime
monitor both call this instead.

It answers 200 when the database answers a query, and 503 when it does not.
Redis is reported but never fails the check: the platform serves on without it
(``core.cache``), so restarting instances because Redis is away would turn a
slower API into no API.

It is answered by :class:`LivenessMiddleware`, first in the stack, rather than
by a view, for two reasons. Render probes the instance directly, with its own
host name and over plain HTTP: a view would meet ``ALLOWED_HOSTS`` (a 400,
which Render reads as a dead instance and never lets a deploy go live) and
the HTTPS redirect (a 301, which Render reads as healthy whatever the
database says). And a probe every few seconds has no business in the request
metrics. The body names no school, person, version or host.
"""
from __future__ import annotations

import logging

from django.core.cache import caches
from django.db import connection
from django.http import JsonResponse

logger = logging.getLogger(__name__)

PATH = "/v1/health/live/"


def database_ok() -> bool:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            return cursor.fetchone() == (1,)
    except Exception:
        logger.warning("Liveness: the database did not answer", exc_info=True)
        return False


def cache_state() -> str:
    """``ok``, ``unavailable``, or ``not used`` (a deployment with no Redis)."""
    from django.core.cache.backends.redis import RedisCache

    cache = caches["default"]
    if not isinstance(cache, RedisCache):
        return "not used"
    try:
        cache._cache.get_client(None, write=True).ping()
        return "ok"
    except Exception:
        logger.warning("Liveness: Redis did not answer", exc_info=True)
        return "unavailable"


def liveness_response() -> JsonResponse:
    database = database_ok()
    response = JsonResponse(
        {
            "status": "ok" if database else "unavailable",
            "database": "ok" if database else "unavailable",
            "cache": cache_state(),
        },
        status=200 if database else 503,
    )
    response["Cache-Control"] = "no-store"
    return response


class LivenessMiddleware:
    """Answer ``GET``/``HEAD`` on :data:`PATH` before anything else runs."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path_info == PATH and request.method in ("GET", "HEAD"):
            return liveness_response()
        return self.get_response(request)
