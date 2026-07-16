"""Buffered source metrics and a PostgreSQL-backed circuit breaker."""

import asyncio
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

DATABASE_URL = os.getenv("DATABASE_URL", "")

CIRCUIT_FAILURE_THRESHOLD = max(2, int(os.getenv("SOURCE_CIRCUIT_FAILURE_THRESHOLD", "5")))
CIRCUIT_OPEN_SECONDS = max(5, int(os.getenv("SOURCE_CIRCUIT_OPEN_SECONDS", "60")))
CIRCUIT_MAX_OPEN_SECONDS = max(CIRCUIT_OPEN_SECONDS, int(os.getenv("SOURCE_CIRCUIT_MAX_OPEN_SECONDS", "900")))
CIRCUIT_DB_REFRESH_SECONDS = max(2, int(os.getenv("SOURCE_CIRCUIT_DB_REFRESH_SECONDS", "10")))
CIRCUIT_WINDOW_SIZE = max(10, int(os.getenv("SOURCE_CIRCUIT_WINDOW_SIZE", "20")))
CIRCUIT_MIN_SAMPLES = max(5, int(os.getenv("SOURCE_CIRCUIT_MIN_SAMPLES", "10")))
CIRCUIT_FAILURE_RATIO = min(1.0, max(0.1, float(os.getenv("SOURCE_CIRCUIT_FAILURE_RATIO", "0.5"))))
METRICS_FLUSH_SECONDS = max(1, int(os.getenv("SOURCE_METRICS_FLUSH_SECONDS", "5")))

METRIC_FIELDS = (
    "checks",
    "requests",
    "hits",
    "not_found",
    "http_404",
    "http_429",
    "http_5xx",
    "timeouts",
    "network_errors",
    "circuit_skips",
    "retries",
    "recovered",
    "latency_ms_sum",
    "latency_ms_max",
)


class SourceCircuitOpen(RuntimeError):
    def __init__(self, source: str, retry_after: float):
        super().__init__(f"circuit_open:{source}")
        self.source = source
        self.retry_after = max(0.0, retry_after)


_circuits: dict[str, dict[str, Any]] = {}
_circuit_lock = asyncio.Lock()
_circuit_refresh_lock = asyncio.Lock()
_circuits_refreshed_at = 0.0
_metrics: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
_flush_task: asyncio.Task | None = None


async def _get_pool():
    from database import get_pool
    return await get_pool()


def _state(source: str) -> dict[str, Any]:
    return _circuits.setdefault(source, {
        "state": "closed",
        "consecutive_failures": 0,
        "opened_until": 0.0,
        "open_count": 0,
        "probe_in_flight": False,
        "last_status": None,
        "last_error": None,
        "recent_failures": [],
    })


def _queue_metric(source: str, **values: int):
    global _flush_task
    metric = _metrics[source]
    for key, value in values.items():
        if key not in METRIC_FIELDS or not value:
            continue
        if key == "latency_ms_max":
            metric[key] = max(metric[key], int(value))
        else:
            metric[key] += int(value)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if DATABASE_URL and (_flush_task is None or _flush_task.done()):
        _flush_task = loop.create_task(_delayed_flush())


async def _delayed_flush():
    await asyncio.sleep(METRICS_FLUSH_SECONDS)
    await flush_source_metrics()


async def _refresh_circuits_from_db():
    """Refresh every circuit with one query instead of one query per source."""
    global _circuits_refreshed_at
    if not DATABASE_URL:
        return
    now = time.time()
    if now - _circuits_refreshed_at < CIRCUIT_DB_REFRESH_SECONDS:
        return

    async with _circuit_refresh_lock:
        now = time.time()
        if now - _circuits_refreshed_at < CIRCUIT_DB_REFRESH_SECONDS:
            return
        # Back off briefly even when PostgreSQL is unavailable. Otherwise every
        # outgoing API request would immediately retry the same failed refresh.
        _circuits_refreshed_at = now
        try:
            pool = await _get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT source, state, consecutive_failures, opened_until, "
                    "open_count, last_status, last_error FROM source_circuits"
                )
        except Exception:
            return

        async with _circuit_lock:
            for row in rows:
                row_state = row["state"] or "closed"
                if row_state not in ("open", "half_open"):
                    continue
                state = _state(row["source"])
                opened_until = row["opened_until"]
                opened_epoch = (
                    opened_until.replace(tzinfo=timezone.utc).timestamp()
                    if opened_until else 0.0
                )
                if opened_epoch < state["opened_until"] and state["state"] == "open":
                    continue
                state["state"] = row_state
                state["consecutive_failures"] = row["consecutive_failures"] or 0
                state["opened_until"] = opened_epoch
                state["open_count"] = row["open_count"] or 0
                state["last_status"] = row["last_status"]
                state["last_error"] = row["last_error"]


async def before_source_request(source: str):
    await _refresh_circuits_from_db()
    async with _circuit_lock:
        state = _state(source)
        now = time.time()
        if state["state"] == "open":
            if now < state["opened_until"]:
                _queue_metric(source, circuit_skips=1)
                raise SourceCircuitOpen(source, state["opened_until"] - now)
            state["state"] = "half_open"
            state["probe_in_flight"] = False
        if state["state"] == "half_open":
            if state["probe_in_flight"]:
                _queue_metric(source, circuit_skips=1)
                raise SourceCircuitOpen(source, 1.0)
            state["probe_in_flight"] = True


async def _persist_circuit(source: str, state: dict[str, Any]):
    if not DATABASE_URL:
        return
    opened_until = None
    if state["opened_until"]:
        opened_until = datetime.fromtimestamp(state["opened_until"], tz=timezone.utc).replace(tzinfo=None)
    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO source_circuits (
                    source, state, consecutive_failures, opened_until, open_count,
                    last_status, last_error, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                ON CONFLICT (source) DO UPDATE SET
                    state=EXCLUDED.state,
                    consecutive_failures=EXCLUDED.consecutive_failures,
                    opened_until=EXCLUDED.opened_until,
                    open_count=EXCLUDED.open_count,
                    last_status=EXCLUDED.last_status,
                    last_error=EXCLUDED.last_error,
                    updated_at=NOW()
            """,
                source,
                state["state"],
                state["consecutive_failures"],
                opened_until,
                state["open_count"],
                state["last_status"],
                state["last_error"],
            )
    except Exception:
        return


async def report_source_result(
    source: str,
    *,
    status: int | None = None,
    error: str | None = None,
    latency_ms: int = 0,
    retry_after: float | None = None,
):
    values: dict[str, int] = {
        "requests": 1,
        "latency_ms_sum": max(0, latency_ms),
        "latency_ms_max": max(0, latency_ms),
    }
    if status == 404:
        values["http_404"] = 1
    elif status == 429:
        values["http_429"] = 1
    elif status is not None and status >= 500:
        values["http_5xx"] = 1
    if error:
        if "timeout" in error.lower():
            values["timeouts"] = 1
        else:
            values["network_errors"] = 1
    _queue_metric(source, **values)

    failed = status in (408, 425, 429) or (status is not None and status >= 500) or bool(error)
    async with _circuit_lock:
        state = _state(source)
        state["last_status"] = status
        state["last_error"] = error
        state["probe_in_flight"] = False
        state["recent_failures"].append(1 if failed else 0)
        if len(state["recent_failures"]) > CIRCUIT_WINDOW_SIZE:
            del state["recent_failures"][:-CIRCUIT_WINDOW_SIZE]
        if failed:
            state["consecutive_failures"] += 1
            recent = state["recent_failures"]
            ratio_open = (
                len(recent) >= CIRCUIT_MIN_SAMPLES
                and sum(recent) / len(recent) >= CIRCUIT_FAILURE_RATIO
            )
            if state["consecutive_failures"] >= CIRCUIT_FAILURE_THRESHOLD or ratio_open:
                state["open_count"] += 1
                multiplier = min(8, 2 ** max(0, state["open_count"] - 1))
                duration = min(CIRCUIT_MAX_OPEN_SECONDS, CIRCUIT_OPEN_SECONDS * multiplier)
                if retry_after is not None:
                    duration = max(duration, int(retry_after))
                state["state"] = "open"
                state["opened_until"] = time.time() + duration
                asyncio.create_task(_persist_circuit(source, dict(state)))
        else:
            was_open = state["state"] in ("open", "half_open") or state["consecutive_failures"] > 0
            state["consecutive_failures"] = 0
            if state["state"] in ("open", "half_open"):
                state["state"] = "closed"
                state["opened_until"] = 0.0
                state["recent_failures"].clear()
            if was_open and state["state"] == "closed":
                _queue_metric(source, recovered=1)
                asyncio.create_task(_persist_circuit(source, dict(state)))


def record_platform_result(source: str, *, found: bool, error: str | None, elapsed_ms: int, retried: bool = False):
    values = {
        "checks": 1,
        "hits": 1 if found else 0,
        "not_found": 1 if not found and not error else 0,
        "timeouts": 1 if error == "timeout" else 0,
        "network_errors": 1 if error and error != "timeout" else 0,
        "retries": 1 if retried else 0,
        "latency_ms_sum": max(0, elapsed_ms),
        "latency_ms_max": max(0, elapsed_ms),
    }
    _queue_metric(f"platform:{source}", **values)


def record_source_retry(source: str):
    _queue_metric(source, retries=1)


async def flush_source_metrics():
    if not DATABASE_URL or not _metrics:
        return
    pending = dict(_metrics)
    _metrics.clear()
    rows = []
    for source, values in pending.items():
        rows.append((source, *[int(values.get(field, 0)) for field in METRIC_FIELDS]))
    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.executemany("""
                INSERT INTO source_metrics_hourly (
                    bucket, source, checks, requests, hits, not_found,
                    http_404, http_429, http_5xx, timeouts, network_errors,
                    circuit_skips, retries, recovered, latency_ms_sum, latency_ms_max
                ) VALUES (
                    date_trunc('hour', NOW()), $1, $2, $3, $4, $5,
                    $6, $7, $8, $9, $10, $11, $12, $13, $14, $15
                )
                ON CONFLICT (bucket, source) DO UPDATE SET
                    checks=source_metrics_hourly.checks + EXCLUDED.checks,
                    requests=source_metrics_hourly.requests + EXCLUDED.requests,
                    hits=source_metrics_hourly.hits + EXCLUDED.hits,
                    not_found=source_metrics_hourly.not_found + EXCLUDED.not_found,
                    http_404=source_metrics_hourly.http_404 + EXCLUDED.http_404,
                    http_429=source_metrics_hourly.http_429 + EXCLUDED.http_429,
                    http_5xx=source_metrics_hourly.http_5xx + EXCLUDED.http_5xx,
                    timeouts=source_metrics_hourly.timeouts + EXCLUDED.timeouts,
                    network_errors=source_metrics_hourly.network_errors + EXCLUDED.network_errors,
                    circuit_skips=source_metrics_hourly.circuit_skips + EXCLUDED.circuit_skips,
                    retries=source_metrics_hourly.retries + EXCLUDED.retries,
                    recovered=source_metrics_hourly.recovered + EXCLUDED.recovered,
                    latency_ms_sum=source_metrics_hourly.latency_ms_sum + EXCLUDED.latency_ms_sum,
                    latency_ms_max=GREATEST(source_metrics_hourly.latency_ms_max, EXCLUDED.latency_ms_max)
            """, rows)
    except Exception:
        for source, values in pending.items():
            metric = _metrics[source]
            for field, value in values.items():
                if field == "latency_ms_max":
                    metric[field] = max(metric[field], value)
                else:
                    metric[field] += value


async def get_source_stats(hours: int = 24) -> list[dict[str, Any]]:
    await flush_source_metrics()
    if not DATABASE_URL:
        return []
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                m.source,
                SUM(m.checks)::bigint AS checks,
                SUM(m.requests)::bigint AS requests,
                SUM(m.hits)::bigint AS hits,
                SUM(m.not_found)::bigint AS not_found,
                SUM(m.http_404)::bigint AS http_404,
                SUM(m.http_429)::bigint AS http_429,
                SUM(m.http_5xx)::bigint AS http_5xx,
                SUM(m.timeouts)::bigint AS timeouts,
                SUM(m.network_errors)::bigint AS network_errors,
                SUM(m.circuit_skips)::bigint AS circuit_skips,
                SUM(m.retries)::bigint AS retries,
                SUM(m.recovered)::bigint AS recovered,
                SUM(m.latency_ms_sum)::bigint AS latency_ms_sum,
                MAX(m.latency_ms_max)::bigint AS latency_ms_max,
                c.state AS circuit_state,
                c.opened_until,
                c.last_status,
                c.last_error
            FROM source_metrics_hourly m
            LEFT JOIN source_circuits c ON c.source=m.source
            WHERE m.bucket >= date_trunc('hour', NOW()) - ($1::int * INTERVAL '1 hour')
            GROUP BY m.source, c.state, c.opened_until, c.last_status, c.last_error
            ORDER BY SUM(m.http_429 + m.http_5xx + m.timeouts + m.network_errors) DESC,
                     SUM(m.requests + m.checks) DESC
        """, max(1, hours))
    result = []
    for row in rows:
        item = dict(row)
        denominator = int(item.get("requests") or item.get("checks") or 0)
        item["avg_latency_ms"] = round((item.get("latency_ms_sum") or 0) / max(1, denominator), 1)
        result.append(item)
    return result
