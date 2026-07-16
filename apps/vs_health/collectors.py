"""In-process request-metric buffer with periodic DB flush.

Recording a request on the hot path must be cheap, so the middleware only
mutates an in-memory dict. A daemon thread (one per worker process) flushes the
accumulated buckets into ``RequestMetric`` every ``HEALTH_METRICS_FLUSH_SECONDS``
using a row lock + merge, which lets multiple gunicorn workers safely fold their
counts into the same canonical row.

Everything here is best-effort: failures are logged and swallowed so
instrumentation can never degrade the application it observes.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from .constants import HISTOGRAM_SIZE, LATENCY_BUCKETS_MS, METRIC_BUCKET_SECONDS

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_buffer: dict = {}
_thread_started = False
_thread_lock = threading.Lock()


# Mutable in-memory accumulator for one metric bucket before DB flush.
@dataclass
class _Agg:
    """Mutable accumulator for one (bucket, route, method, tenant) tuple."""
    count: int = 0
    s2: int = 0
    s3: int = 0
    s4: int = 0
    s5: int = 0
    throttled: int = 0
    sum_ms: float = 0.0
    max_ms: float = 0.0
    hist: list = field(default_factory=lambda: [0] * HISTOGRAM_SIZE)


# Map latency to the compact histogram slot used by percentile estimation.
def bucket_index(latency_ms: float) -> int:
    """Map a latency in ms to its histogram bucket index (last = overflow)."""
    for i, upper in enumerate(LATENCY_BUCKETS_MS):
        if latency_ms <= upper:
            return i
    return HISTOGRAM_SIZE - 1


# Align request timestamps to the persisted metric bucket boundary.
def _floor_minute(dt):
    return dt.replace(second=0, microsecond=0)


# Record one request into the process-local buffer without touching the database.
def record(*, route: str, method: str, status_code: int, latency_ms: float,
           tenant_id=None, throttled: bool = False) -> None:
    """Fold a single request into the in-memory buffer (cheap, thread-safe)."""
    try:
        bucket = _floor_minute(timezone.now())
        key = (bucket, route, method, tenant_id)
        with _lock:
            # The lock keeps concurrent requests in the same worker from losing counts.
            agg = _buffer.get(key)
            if agg is None:
                agg = _Agg()
                _buffer[key] = agg
            agg.count += 1
            if status_code >= 500:
                # Store status families separately so error rates do not require raw logs.
                agg.s5 += 1
            elif status_code >= 400:
                agg.s4 += 1
            elif status_code >= 300:
                agg.s3 += 1
            else:
                agg.s2 += 1
            if throttled or status_code == 429:
                # 429s are tracked as saturation pressure even when middleware did not flag throttling.
                agg.throttled += 1
            agg.sum_ms += latency_ms
            if latency_ms > agg.max_ms:
                agg.max_ms = latency_ms
            agg.hist[bucket_index(latency_ms)] += 1
        _ensure_flusher()
    except Exception:
        logger.debug("vs_health.record failed", exc_info=True)


# Swap the hot-path buffer out quickly so flushing does not block request recording.
def _drain():
    """Atomically swap out the buffer and return its contents."""
    global _buffer
    with _lock:
        snapshot, _buffer = _buffer, {}
    return snapshot


# Merge buffered request aggregates into canonical RequestMetric rows.
def flush() -> int:
    """Persist buffered aggregates to RequestMetric. Returns rows touched."""
    from django.db import transaction
    from .models import RequestMetric

    snapshot = _drain()
    if not snapshot:
        return 0

    touched = 0
    for (bucket, route, method, tenant_id), agg in snapshot.items():
        try:
            with transaction.atomic():
                # Lock the row so multiple worker processes can safely merge the same bucket.
                obj, created = (
                    RequestMetric.objects.select_for_update().get_or_create(
                        bucket_start=bucket,
                        route=route[:255],
                        method=method[:10],
                        tenant_id=tenant_id,
                        defaults={
                            "request_count": agg.count,
                            "status_2xx": agg.s2,
                            "status_3xx": agg.s3,
                            "status_4xx": agg.s4,
                            "status_5xx": agg.s5,
                            "throttled_count": agg.throttled,
                            "latency_sum_ms": agg.sum_ms,
                            "latency_max_ms": agg.max_ms,
                            "latency_hist": agg.hist,
                        },
                    )
                )
                if not created:
                    # Existing rows are additive rollups; raw requests are never stored here.
                    obj.request_count += agg.count
                    obj.status_2xx += agg.s2
                    obj.status_3xx += agg.s3
                    obj.status_4xx += agg.s4
                    obj.status_5xx += agg.s5
                    obj.throttled_count += agg.throttled
                    obj.latency_sum_ms += agg.sum_ms
                    obj.latency_max_ms = max(obj.latency_max_ms, agg.max_ms)
                    existing = obj.latency_hist or [0] * HISTOGRAM_SIZE
                    obj.latency_hist = [a + b for a, b in zip(existing, agg.hist)]
                    obj.save(update_fields=[
                        "request_count", "status_2xx", "status_3xx", "status_4xx",
                        "status_5xx", "throttled_count", "latency_sum_ms",
                        "latency_max_ms", "latency_hist",
                    ])
            touched += 1
        except Exception:
            logger.warning("vs_health flush failed for %s %s", method, route, exc_info=True)
    return touched


# Background flusher loop; errors are swallowed by the caller.
def _flush_loop(interval: float):
    while True:
        time.sleep(interval)
        try:
            flush()
        except Exception:
            logger.debug("vs_health flush loop error", exc_info=True)


def _running_tests() -> bool:
    """True when invoked under the Django test runner.

    A background thread holding a DB connection blocks test-database teardown
    ("database is being accessed by other users"), so never start one in tests
    regardless of the settings flag.
    """
    return "test" in sys.argv


# Lazily start one daemon flusher thread per worker process.
def _ensure_flusher():
    """Start the background flush thread once per process (lazy, opt-out)."""
    global _thread_started
    if _thread_started:
        return
    if _running_tests():
        # Test DB teardown is fragile when daemon threads keep DB connections open.
        return
    if not getattr(settings, "HEALTH_METRICS_BACKGROUND_FLUSH", True):
        return
    with _thread_lock:
        if _thread_started:
            return
        # Default to half the bucket width so rows are reasonably fresh without hot writes.
        interval = getattr(settings, "HEALTH_METRICS_FLUSH_SECONDS", METRIC_BUCKET_SECONDS // 2)
        t = threading.Thread(target=_flush_loop, args=(interval,), name="vs-health-flush", daemon=True)
        t.start()
        _thread_started = True
