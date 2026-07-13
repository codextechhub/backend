"""Request-timing middleware that feeds the golden signals.

Sits *after* the tenant-context middleware so ``request.tenant`` is resolved.
It times every resolved request and hands the result to the in-process
collector. It deliberately:

  * records the *resolved route pattern* (not the raw path) to bound cardinality,
  * skips unmatched paths and its own metrics endpoints, and
  * never raises — a metrics failure must not affect the response.
"""
from __future__ import annotations

import logging
import time

from .collectors import record

logger = logging.getLogger(__name__)


class RequestMetricsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.perf_counter()
        response = self.get_response(request)
        try:
            self._record(request, response, (time.perf_counter() - start) * 1000.0)
        except Exception:  # pragma: no cover - never break the response
            logger.debug("RequestMetricsMiddleware record failed", exc_info=True)
        return response

    @staticmethod
    def _route_for(request) -> str | None:
        match = getattr(request, "resolver_match", None)
        if match is None:
            return None  # unmatched (404 on no route) — skip to avoid path-cardinality noise
        # ``route`` is the pattern string on Django 2.2+ (e.g. "v1/finance/invoices/").
        route = getattr(match, "route", None)
        if route:
            return "/" + route.lstrip("/")
        # Fall back to the view's qualified name when no route string is exposed.
        return getattr(match, "view_name", None) or request.path

    def _record(self, request, response, latency_ms: float) -> None:
        route = self._route_for(request)
        if not route:
            return
        # Don't measure our own observability surface (avoids self-reference noise).
        if route.startswith("/v1/health/"):
            return

        tenant = getattr(request, "tenant", None)
        tenant_id = getattr(tenant, "id", None) if tenant else None

        record(
            route=route,
            method=request.method,
            status_code=getattr(response, "status_code", 0),
            latency_ms=latency_ms,
            tenant_id=tenant_id,
            throttled=getattr(response, "status_code", 0) == 429,
        )
