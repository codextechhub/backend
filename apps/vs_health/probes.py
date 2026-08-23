"""Probe executors for the uptime checks.

Each ``run_*`` returns a plain dict::

    {"status", "response_ms", "status_code", "error", "meta"}

so the task layer can persist a ``UptimeCheckResult`` without knowing probe
internals. Probes are defensive: any exception becomes a CRITICAL result with
the error captured, never a raised exception.
"""
from __future__ import annotations

import socket
import ssl
import time
from datetime import datetime, timezone as dt_timezone
from urllib.parse import urlparse

from django.conf import settings
from django.db import connection

from .constants import HealthStatus


# Normalize probe outcomes into the shape persisted by uptime tasks.
def _result(status, response_ms=None, status_code=None, error="", meta=None):
    return {
        "status": status,
        "response_ms": response_ms,
        "status_code": status_code,
        "error": error,
        "meta": meta or {},
    }


# Probe an HTTP endpoint and classify both failures and slow responses.
def run_http(target: str, expected: dict) -> dict:
    """HTTP GET probe. healthy on expected status; warning if slow; critical otherwise."""
    try:
        import requests
    except ImportError:
        return _result(HealthStatus.UNKNOWN, error="requests not installed")

    want = expected.get("status", 200)
    # Per-check thresholds allow external services to be slower than internal APIs.
    warn_ms = expected.get("warn_ms", 800)
    timeout = expected.get("timeout", 10)
    start = time.perf_counter()
    try:
        resp = requests.get(target, timeout=timeout, allow_redirects=True)
        elapsed = (time.perf_counter() - start) * 1000.0
        code = resp.status_code
        if code >= 500:
            # Server errors mean the dependency is unhealthy regardless of latency.
            return _result(HealthStatus.CRITICAL, elapsed, code, error=f"HTTP {code}")
        if code != want and code >= 400:
            return _result(HealthStatus.WARNING, elapsed, code, error=f"HTTP {code}")
        status = HealthStatus.WARNING if elapsed > warn_ms else HealthStatus.HEALTHY
        return _result(status, elapsed, code)
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000.0
        return _result(HealthStatus.CRITICAL, elapsed, error=str(exc)[:500])


# Probe a raw TCP endpoint such as SMTP reachability.
def run_tcp(target: str, expected: dict) -> dict:
    """TCP connect probe against host:port."""
    host, _, port = target.partition(":")
    port = int(port or expected.get("port", 80))
    timeout = expected.get("timeout", 5)
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            elapsed = (time.perf_counter() - start) * 1000.0
            return _result(HealthStatus.HEALTHY, elapsed)
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000.0
        return _result(HealthStatus.CRITICAL, elapsed, error=str(exc)[:500])


# Probe Redis broker availability and memory saturation.
def run_redis(expected: dict) -> dict:
    """PING the broker Redis. Uses CELERY_BROKER_URL."""
    url = getattr(settings, "CELERY_BROKER_URL", "redis://localhost:6379/0")
    if not url.startswith("redis"):
        # Non-Redis brokers provide no Redis-specific signal, so do not claim failure.
        return _result(HealthStatus.UNKNOWN, error="broker is not redis")
    try:
        import redis
    except ImportError:
        return _result(HealthStatus.UNKNOWN, error="redis not installed")
    warn_ms = expected.get("warn_ms", 50)
    start = time.perf_counter()
    try:
        client = redis.from_url(url, socket_connect_timeout=expected.get("timeout", 3))
        client.ping()
        elapsed = (time.perf_counter() - start) * 1000.0
        try:
            info = client.info("memory")
            used = info.get("used_memory", 0)
            maxmem = info.get("maxmemory", 0) or 0
            meta = {"used_memory": used, "maxmemory": maxmem}
            if maxmem:
                # Memory pressure is a saturation signal even when PING is fast.
                pct = used / maxmem * 100
                meta["mem_pct"] = round(pct, 1)
                if pct >= expected.get("mem_critical_pct", 95):
                    return _result(HealthStatus.CRITICAL, elapsed, meta=meta)
                if pct >= expected.get("mem_warn_pct", 85):
                    return _result(HealthStatus.WARNING, elapsed, meta=meta)
        except Exception:
            # INFO failure should not turn a successful PING into a hard outage.
            meta = {}
        status = HealthStatus.WARNING if elapsed > warn_ms else HealthStatus.HEALTHY
        return _result(status, elapsed, meta=meta)
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000.0
        return _result(HealthStatus.CRITICAL, elapsed, error=str(exc)[:500])


# Probe default database connectivity with a minimal query.
def run_postgres(expected: dict) -> dict:
    """Run SELECT 1 on the default DB connection."""
    warn_ms = expected.get("warn_ms", 100)
    start = time.perf_counter()
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        elapsed = (time.perf_counter() - start) * 1000.0
        status = HealthStatus.WARNING if elapsed > warn_ms else HealthStatus.HEALTHY
        return _result(status, elapsed)
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000.0
        return _result(HealthStatus.CRITICAL, elapsed, error=str(exc)[:500])


# Probe TLS certificate expiry for public endpoints.
def run_ssl(target: str, expected: dict) -> dict:
    """Inspect the TLS certificate of a domain and report days to expiry."""
    parsed = urlparse(target if "//" in target else f"https://{target}")
    host = parsed.hostname or target
    port = parsed.port or 443
    warn_days = expected.get("warn_days", 14)
    critical_days = expected.get("critical_days", 5)
    start = time.perf_counter()
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=expected.get("timeout", 8)) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
        elapsed = (time.perf_counter() - start) * 1000.0
        not_after = cert.get("notAfter")
        expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=dt_timezone.utc)
        days_left = (expires - datetime.now(dt_timezone.utc)).days
        # Store certificate metadata so SLO views and alert rules can reuse the probe result.
        meta = {"ssl_days_left": days_left, "domain": host, "expires_at": expires.isoformat()}
        if days_left <= critical_days:
            return _result(HealthStatus.CRITICAL, elapsed, meta=meta, error=f"cert expires in {days_left}d")
        if days_left <= warn_days:
            return _result(HealthStatus.WARNING, elapsed, meta=meta)
        return _result(HealthStatus.HEALTHY, elapsed, meta=meta)
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000.0
        return _result(HealthStatus.CRITICAL, elapsed, error=str(exc)[:500])


# Dispatch a configured uptime check to its concrete probe implementation.
def execute(check) -> dict:
    """Dispatch a UptimeCheck to the right probe based on its type."""
    from .models import CheckType

    expected = check.expected or {}
    # Expected payloads are per-check tuning knobs, not trusted control flow.
    ct = check.check_type
    if ct == CheckType.HTTP:
        return run_http(check.target, expected)
    if ct == CheckType.TCP:
        return run_tcp(check.target, expected)
    if ct == CheckType.REDIS:
        return run_redis(expected)
    if ct == CheckType.POSTGRES:
        return run_postgres(expected)
    if ct == CheckType.SSL:
        return run_ssl(check.target, expected)
    # INTERNAL / derived checks have no probe; treated as informational.
    return _result(HealthStatus.UNKNOWN, error="no probe for internal check")
