"""
Persistent bulk search jobs for the Telegram Mini App.
"""

import asyncio
import csv
import io
import json
import logging
import os
import time
from typing import Any

import httpx

from database import get_pool
from searcher import (
    enrich_balances,
    extract_username,
    retry_bulk_search,
    run_bulk_search,
    start_clusters_bulk_prefetch,
    start_web3bio_bulk_prefetch,
)

log = logging.getLogger(__name__)

MAX_BULK_LINES = int(os.getenv("MAX_BULK_LINES", "10000"))
BULK_WORKERS = max(1, int(os.getenv("BULK_WORKERS", "20")))
BULK_BALANCE_WORKERS = max(1, int(os.getenv("BULK_BALANCE_WORKERS", "8")))
BULK_INCLUDE_BALANCES = os.getenv("BULK_INCLUDE_BALANCES", "true").lower() == "true"
BULK_JOB_TIMEOUT_SECONDS = int(os.getenv("BULK_JOB_TIMEOUT_SECONDS", "7200"))
BULK_MAX_ACTIVE_JOBS = int(os.getenv("BULK_MAX_ACTIVE_JOBS", "2"))
BULK_RETRY_WORKERS = max(1, int(os.getenv("BULK_RETRY_WORKERS", "5")))
BULK_PARTIAL_RETRY_DELAYS = tuple(
    max(0, int(value.strip()))
    for value in os.getenv("BULK_PARTIAL_RETRY_DELAYS", "30,90,300").split(",")
    if value.strip().isdigit()
) or (30, 90, 300)
BULK_SCHEDULER_INTERVAL_SECONDS = max(
    0.5, float(os.getenv("BULK_SCHEDULER_INTERVAL_SECONDS", "2"))
)
BULK_CONTROL_POLL_SECONDS = max(
    0.25, float(os.getenv("BULK_CONTROL_POLL_SECONDS", "0.75"))
)

_active_jobs: set[int] = set()
_job_slots = asyncio.Semaphore(max(1, BULK_MAX_ACTIVE_JOBS))
_job_tasks: dict[int, asyncio.Task] = {}
_scheduler_task: asyncio.Task | None = None
_scheduler_wakeup: asyncio.Event | None = None


def parse_bulk_usernames(text: str, limit: int = MAX_BULK_LINES) -> tuple[list[str], int]:
    usernames: list[str] = []
    seen: set[str] = set()
    total_lines = 0
    for line in text.splitlines():
        total_lines += 1
        raw = line.strip()
        if not raw:
            continue
        first_cell = raw.split(",", 1)[0].split(";", 1)[0].split("\t", 1)[0].strip()
        username = extract_username(first_cell)
        if not username:
            continue
        key = username.lower()
        if key in seen:
            continue
        seen.add(key)
        usernames.append(username)
        if len(usernames) >= limit:
            break
    return usernames, total_lines


def _json_value(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return fallback


def _csv_safe(value) -> str:
    text = str(value or "")
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


async def get_active_job_for_user(user_id: int) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT *
            FROM bulk_jobs
            WHERE user_id=$1 AND status IN ('queued', 'running', 'retrying', 'paused')
            ORDER BY created_at DESC
            LIMIT 1
        """, user_id)
    return dict(row) if row else None


async def create_bulk_job(user_id: int, usernames: list[str], consume_credit: bool = True) -> dict[str, Any]:
    if not usernames:
        raise ValueError("empty username list")
    if len(usernames) > MAX_BULK_LINES:
        usernames = usernames[:MAX_BULK_LINES]

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchrow("SELECT user_id FROM users WHERE user_id=$1 FOR UPDATE", user_id)
            active_job = await conn.fetchrow("""
                SELECT id
                FROM bulk_jobs
                WHERE user_id=$1 AND status IN ('queued', 'running', 'retrying', 'paused')
                LIMIT 1
                FOR UPDATE
            """, user_id)
            if active_job:
                raise RuntimeError("active bulk job already exists")

            if consume_credit:
                remaining = await conn.fetchval("""
                    UPDATE users
                    SET bulk_credits = bulk_credits - 1
                    WHERE user_id=$1 AND COALESCE(bulk_credits, 0) > 0
                    RETURNING bulk_credits
                """, user_id)
                if remaining is None:
                    raise PermissionError("no bulk credits")
            else:
                remaining = None

            row = await conn.fetchrow("""
                INSERT INTO bulk_jobs (user_id, status, total_count)
                VALUES ($1, 'queued', $2)
                RETURNING *
            """, user_id, len(usernames))
            job_id = row["id"]
            await conn.executemany("""
                INSERT INTO bulk_items (job_id, username)
                VALUES ($1, $2)
            """, [(job_id, username) for username in usernames])

    job = dict(row)
    job["remaining_credits"] = remaining
    return job


async def get_job(user_id: int, job_id: int) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT *
            FROM bulk_jobs
            WHERE id=$1 AND user_id=$2
        """, job_id, user_id)
    return dict(row) if row else None


async def list_jobs(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT *
            FROM bulk_jobs
            WHERE user_id=$1
            ORDER BY created_at DESC
            LIMIT $2
        """, user_id, limit)
    return [dict(r) for r in rows]


async def pause_bulk_job(user_id: int, job_id: int) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE bulk_jobs
            SET status='paused', paused_at=NOW(), updated_at=NOW(), error=NULL
            WHERE id=$1 AND user_id=$2
              AND status IN ('queued', 'running', 'retrying')
            RETURNING *
        """, job_id, user_id)
    return dict(row) if row else None


async def resume_bulk_job(user_id: int, job_id: int) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE bulk_jobs
            SET status='queued', paused_at=NULL, finished_at=NULL,
                updated_at=NOW(), error=NULL
            WHERE id=$1 AND user_id=$2 AND status='paused'
            RETURNING *
        """, job_id, user_id)
    if row:
        wake_bulk_scheduler()
    return dict(row) if row else None


async def cancel_bulk_job(user_id: int, job_id: int) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE bulk_jobs
            SET status='canceled', canceled_at=NOW(), finished_at=NOW(),
                updated_at=NOW(), error=NULL
            WHERE id=$1 AND user_id=$2
              AND status IN ('queued', 'running', 'retrying', 'paused')
            RETURNING *
        """, job_id, user_id)
    return dict(row) if row else None


async def _job_status(job_id: int) -> str | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT status FROM bulk_jobs WHERE id=$1", job_id)


async def _monitor_job_control(job_id: int, stop_event: asyncio.Event):
    while not stop_event.is_set():
        status = await _job_status(job_id)
        if status not in ("running", "retrying"):
            stop_event.set()
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=BULK_CONTROL_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def list_job_items(
    user_id: int,
    job_id: int,
    limit: int = 100,
    offset: int = 0,
    only_found: bool = False,
    query: str = "",
    sort: str = "",
) -> dict[str, Any] | None:
    job = await get_job(user_id, job_id)
    if not job:
        return None

    where = ["job_id=$1"]
    args: list[Any] = [job_id]
    if only_found:
        where.append("jsonb_array_length(wallets) > 0")
    if query:
        args.append(f"%{query.lower()}%")
        where.append("(lower(username) LIKE $" + str(len(args)) + " OR lower(wallets::text) LIKE $" + str(len(args)) + ")")

    args.extend([limit, offset])
    limit_pos = len(args) - 1
    offset_pos = len(args)
    where_sql = " AND ".join(where)
    order_sql = "id ASC"
    if sort == "balance_desc":
        order_sql = """
            COALESCE((
                SELECT SUM((balance.value->>'balance_usd')::numeric)
                FROM jsonb_each(balances) AS balance(key, value)
                WHERE jsonb_typeof(balance.value) = 'object'
                  AND (balance.value->>'balance_usd') ~ '^-?[0-9]+(\\.[0-9]+)?$'
            ), -1) DESC,
            id ASC
        """

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT id, username, status, wallets, platforms, matched, balances,
                   partial_sources, retry_count, error, elapsed_ms, updated_at
            FROM bulk_items
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ${limit_pos} OFFSET ${offset_pos}
        """, *args)
        total = await conn.fetchval(f"SELECT COUNT(*) FROM bulk_items WHERE {where_sql}", *args[: len(args) - 2])

    return {
        "job": job,
        "total": total or 0,
        "items": [dict(r) for r in rows],
    }


async def _set_job_status(job_id: int, status: str, error: str | None = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if status == "running":
            await conn.execute("""
                UPDATE bulk_jobs
                SET status='running', started_at=COALESCE(started_at, NOW()),
                    updated_at=NOW(), error=NULL
                WHERE id=$1 AND status IN ('queued', 'running', 'retrying')
            """, job_id)
        elif status in ("done", "done_partial", "failed", "canceled"):
            await conn.execute("""
                UPDATE bulk_jobs
                SET status=$2, finished_at=NOW(), updated_at=NOW(), error=$3
                WHERE id=$1
                  AND ($2 IN ('canceled') OR status IN ('running', 'retrying'))
            """, job_id, status, error)
        else:
            await conn.execute("""
                UPDATE bulk_jobs
                SET status=$2, updated_at=NOW(), error=$3
                WHERE id=$1 AND status IN ('running', 'retrying')
            """, job_id, status, error)


async def _store_item_result(
    job_id: int,
    item_id: int,
    data: dict[str, Any],
    balances: dict[str, Any] | None = None,
    *,
    is_retry: bool = False,
    final_partial: bool = False,
) -> bool:
    found = [r for r in data.get("results", []) if r.get("found")]
    wallets = data.get("all_wallets") or []
    platforms = list(dict.fromkeys(r.get("platform", "") for r in found if r.get("platform")))
    matched = list(dict.fromkeys(str(r.get("matched") or "") for r in found if r.get("matched")))
    elapsed_ms = data.get("diagnostics", {}).get("elapsed_ms", 0)
    partial_sources = list(data.get("partial_sources") or [])
    complete = bool(data.get("bulk_complete", not partial_sources))
    target_status = "done" if complete else ("done_partial" if final_partial else "partial")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow("""
                SELECT i.status, i.wallets, i.balances, i.retry_count, j.status AS job_status
                FROM bulk_items i
                JOIN bulk_jobs j ON j.id=i.job_id
                WHERE i.id=$1 AND i.job_id=$2
                FOR UPDATE OF i, j
            """, item_id, job_id)
            if not current or current["job_status"] not in ("running", "retrying"):
                return False
            expected = "partial" if is_retry else "pending"
            if current["status"] != expected:
                return False

            old_wallets = _json_value(current["wallets"], [])
            old_balances = _json_value(current["balances"], {})
            merged_balances = dict(old_balances) if isinstance(old_balances, dict) else {}
            if balances:
                merged_balances.update(balances)
            retry_count = int(current["retry_count"] or 0) + (1 if is_retry else 0)
            error = None if complete else "temporary sources: " + ", ".join(partial_sources)

            await conn.execute("""
                UPDATE bulk_items
                SET status=$3,
                    wallets=$4::jsonb,
                    platforms=$5::jsonb,
                    matched=$6::jsonb,
                    balances=$7::jsonb,
                    result=$8::jsonb,
                    partial_sources=$9::jsonb,
                    retry_count=$10,
                    error=$11,
                    elapsed_ms=$12,
                    updated_at=NOW()
                WHERE id=$1 AND job_id=$2
            """,
                item_id,
                job_id,
                target_status,
                json.dumps(wallets),
                json.dumps(platforms),
                json.dumps(matched),
                json.dumps(merged_balances),
                json.dumps(data),
                json.dumps(partial_sources),
                retry_count,
                error,
                int(elapsed_ms or 0),
            )

            processed_delta = 1 if current["status"] == "pending" else 0
            found_delta = 1 if wallets and not old_wallets else 0
            partial_delta = 0
            if current["status"] == "pending" and not complete:
                partial_delta = 1
            elif current["status"] == "partial" and complete:
                partial_delta = -1
            await conn.execute("""
                UPDATE bulk_jobs
                SET processed_count = processed_count + $2,
                    found_count = found_count + $3,
                    partial_count = GREATEST(0, partial_count + $4),
                    updated_at=NOW()
                WHERE id=$1
            """, job_id, processed_delta, found_delta, partial_delta)
    return True


async def _store_item_error(job_id: int, item_id: int, error: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchval("""
                UPDATE bulk_items AS item
                SET status='failed', error=$3, updated_at=NOW()
                FROM bulk_jobs AS job
                WHERE item.id=$1 AND item.job_id=$2 AND item.status='pending'
                  AND job.id=item.job_id AND job.status IN ('running', 'retrying')
                RETURNING 1
            """, item_id, job_id, error[:300])
            if updated:
                await conn.execute("""
                    UPDATE bulk_jobs
                    SET processed_count = processed_count + 1
                    WHERE id=$1
                """, job_id)


async def _retry_partial_items(
    job_id: int,
    started: float,
    stop_event: asyncio.Event | None = None,
) -> int:
    """Retry only temporary source failures and keep every earlier hit."""
    pool = await get_pool()
    for round_index, delay in enumerate(BULK_PARTIAL_RETRY_DELAYS):
        if stop_event and stop_event.is_set():
            break
        async with pool.acquire() as conn:
            partial_count = await conn.fetchval(
                "SELECT COUNT(*) FROM bulk_items WHERE job_id=$1 AND status='partial'",
                job_id,
            )
        if not partial_count:
            return 0
        if time.perf_counter() - started >= BULK_JOB_TIMEOUT_SECONDS:
            break

        if delay:
            if stop_event:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                if stop_event.is_set():
                    break
            else:
                await asyncio.sleep(delay)

        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, username, result
                FROM bulk_items
                WHERE job_id=$1 AND status='partial'
                ORDER BY id ASC
            """, job_id)

        retry_usernames = [row["username"] for row in rows]
        prefetch_tasks = [task for task in (
            start_web3bio_bulk_prefetch(retry_usernames, force_errors=True),
            start_clusters_bulk_prefetch(retry_usernames, force_errors=True),
        ) if task]

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        for row in rows:
            await queue.put(dict(row))
        worker_count = min(BULK_RETRY_WORKERS, max(1, len(rows)))
        for _ in range(worker_count):
            await queue.put(None)
        final_round = round_index == len(BULK_PARTIAL_RETRY_DELAYS) - 1

        async def retry_worker(balance_client: httpx.AsyncClient):
            while True:
                item = await queue.get()
                try:
                    if item is None:
                        return
                    if stop_event and stop_event.is_set():
                        continue
                    if time.perf_counter() - started >= BULK_JOB_TIMEOUT_SECONDS:
                        continue
                    previous = _json_value(item.get("result"), {})
                    data = await retry_bulk_search(item["username"], previous)
                    balances: dict[str, Any] = {}
                    if BULK_INCLUDE_BALANCES and data.get("all_wallets"):
                        try:
                            balances = await enrich_balances(
                                data["all_wallets"],
                                client=balance_client,
                            )
                        except Exception:
                            balances = {}
                    await _store_item_result(
                        job_id,
                        item["id"],
                        data,
                        balances,
                        is_retry=True,
                        final_partial=final_round,
                    )
                except Exception:
                    # Keep the item partial so a later round can still recover it.
                    if final_round and item is not None:
                        previous = _json_value(item.get("result"), {})
                        await _store_item_result(
                            job_id,
                            item["id"],
                            previous,
                            {},
                            is_retry=True,
                            final_partial=True,
                        )
                finally:
                    queue.task_done()

        limits = httpx.Limits(
            max_connections=max(worker_count, 5),
            max_keepalive_connections=max(worker_count, 5),
        )
        async with httpx.AsyncClient(follow_redirects=True, limits=limits) as balance_client:
            workers = [
                asyncio.create_task(retry_worker(balance_client))
                for _ in range(worker_count)
            ]
            await queue.join()
            await asyncio.gather(*workers, return_exceptions=True)
        if prefetch_tasks:
            await asyncio.gather(*prefetch_tasks, return_exceptions=True)

    async with pool.acquire() as conn:
        remaining = await conn.fetchval(
            "SELECT COUNT(*) FROM bulk_items WHERE job_id=$1 AND status='partial'",
            job_id,
        )
        if remaining and not (stop_event and stop_event.is_set()):
            await conn.execute("""
                UPDATE bulk_items
                SET status='done_partial', updated_at=NOW()
                WHERE job_id=$1 AND status='partial'
            """, job_id)
    return int(remaining or 0)


async def process_bulk_job(job_id: int):
    if job_id in _active_jobs:
        return
    _active_jobs.add(job_id)
    started = time.perf_counter()
    stop_event: asyncio.Event | None = None
    control_task: asyncio.Task | None = None
    try:
        pool = await get_pool()
        async with pool.acquire() as lock_conn:
            lock_acquired = await lock_conn.fetchval(
                "SELECT pg_try_advisory_lock($1, $2)", 52026, job_id
            )
            if not lock_acquired:
                return
            try:
                status = await lock_conn.fetchval(
                    "SELECT status FROM bulk_jobs WHERE id=$1", job_id
                )
                if status not in ("queued", "running", "retrying"):
                    return

                async with _job_slots:
                    await _set_job_status(job_id, "running")
                    if await _job_status(job_id) != "running":
                        return
                    stop_event = asyncio.Event()
                    control_task = asyncio.create_task(
                        _monitor_job_control(job_id, stop_event)
                    )
                    async with pool.acquire() as conn:
                        rows = await conn.fetch("""
                            SELECT id, username
                            FROM bulk_items
                            WHERE job_id=$1 AND status='pending'
                            ORDER BY id ASC
                        """, job_id)

                    initial_usernames = [row["username"] for row in rows]
                    prefetch_tasks = [task for task in (
                        start_web3bio_bulk_prefetch(initial_usernames),
                        start_clusters_bulk_prefetch(initial_usernames),
                    ) if task]

                    search_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
                    balance_queue: asyncio.Queue[tuple[dict[str, Any], dict[str, Any]] | None] = asyncio.Queue(
                        maxsize=max(BULK_BALANCE_WORKERS * 4, 100)
                    )
                    for row in rows:
                        await search_queue.put(dict(row))
                    for _ in range(BULK_WORKERS):
                        await search_queue.put(None)

                    async def search_worker():
                        while True:
                            item = await search_queue.get()
                            try:
                                if item is None:
                                    return
                                if stop_event.is_set():
                                    continue
                                if time.perf_counter() - started > BULK_JOB_TIMEOUT_SECONDS:
                                    await _store_item_error(job_id, item["id"], "job timeout")
                                    continue

                                data = await run_bulk_search(item["username"])
                                if stop_event.is_set():
                                    continue
                                if BULK_INCLUDE_BALANCES and data.get("all_wallets"):
                                    await balance_queue.put((item, data))
                                else:
                                    await _store_item_result(job_id, item["id"], data, {})
                            except Exception as exc:
                                if item is not None:
                                    await _store_item_error(job_id, item["id"], str(exc))
                            finally:
                                search_queue.task_done()

                    async def balance_worker(balance_client: httpx.AsyncClient):
                        while True:
                            payload = await balance_queue.get()
                            try:
                                if payload is None:
                                    return
                                if stop_event.is_set():
                                    continue
                                item, data = payload
                                if time.perf_counter() - started > BULK_JOB_TIMEOUT_SECONDS:
                                    await _store_item_result(job_id, item["id"], data, {})
                                    continue
                                try:
                                    balances = await enrich_balances(
                                        data["all_wallets"],
                                        client=balance_client,
                                    )
                                except Exception:
                                    balances = {}
                                await _store_item_result(job_id, item["id"], data, balances)
                            finally:
                                balance_queue.task_done()

                    limits = httpx.Limits(
                        max_connections=max(BULK_BALANCE_WORKERS, 5),
                        max_keepalive_connections=max(BULK_BALANCE_WORKERS, 5),
                    )
                    async with httpx.AsyncClient(follow_redirects=True, limits=limits) as balance_client:
                        search_workers = [
                            asyncio.create_task(search_worker())
                            for _ in range(BULK_WORKERS)
                        ]
                        balance_workers = [
                            asyncio.create_task(balance_worker(balance_client))
                            for _ in range(BULK_BALANCE_WORKERS)
                        ]

                        await search_queue.join()
                        await asyncio.gather(*search_workers, return_exceptions=True)
                        for _ in balance_workers:
                            await balance_queue.put(None)
                        await balance_queue.join()
                        await asyncio.gather(*balance_workers, return_exceptions=True)
                    if prefetch_tasks:
                        await asyncio.gather(*prefetch_tasks, return_exceptions=True)
                    if stop_event.is_set() or await _job_status(job_id) != "running":
                        return
                    async with pool.acquire() as conn:
                        partial_count = await conn.fetchval(
                            "SELECT COUNT(*) FROM bulk_items WHERE job_id=$1 AND status='partial'",
                            job_id,
                        )
                    if partial_count:
                        await _set_job_status(job_id, "retrying")
                        await _retry_partial_items(job_id, started, stop_event)

                    if stop_event.is_set() or await _job_status(job_id) not in ("running", "retrying"):
                        return

                    async with pool.acquire() as conn:
                        partial_count = await conn.fetchval("""
                            SELECT COUNT(*)
                            FROM bulk_items
                            WHERE job_id=$1 AND status IN ('partial', 'done_partial')
                        """, job_id)
                        await conn.execute("""
                            UPDATE bulk_jobs
                            SET partial_count=$2, updated_at=NOW()
                            WHERE id=$1
                        """, job_id, int(partial_count or 0))
                    await _set_job_status(
                        job_id,
                        "done_partial" if partial_count else "done",
                    )
            finally:
                await lock_conn.execute("SELECT pg_advisory_unlock($1, $2)", 52026, job_id)
    except Exception as exc:
        await _set_job_status(job_id, "failed", str(exc)[:300])
    finally:
        if stop_event:
            stop_event.set()
        if control_task:
            control_task.cancel()
            await asyncio.gather(control_task, return_exceptions=True)
        _active_jobs.discard(job_id)


def wake_bulk_scheduler():
    if _scheduler_wakeup:
        _scheduler_wakeup.set()


def schedule_bulk_job(job_id: int) -> asyncio.Task | None:
    existing = _job_tasks.get(job_id)
    if existing and not existing.done():
        return existing
    if job_id in _active_jobs:
        return None
    running_tasks = sum(1 for task in _job_tasks.values() if not task.done())
    if running_tasks >= max(1, BULK_MAX_ACTIVE_JOBS):
        wake_bulk_scheduler()
        return None
    task = asyncio.create_task(process_bulk_job(job_id))
    _job_tasks[job_id] = task

    def finished(done_task: asyncio.Task):
        if _job_tasks.get(job_id) is done_task:
            _job_tasks.pop(job_id, None)
        wake_bulk_scheduler()

    task.add_done_callback(finished)
    return task


async def _bulk_scheduler_loop():
    global _scheduler_wakeup
    _scheduler_wakeup = asyncio.Event()
    while True:
        try:
            running_tasks = sum(1 for task in _job_tasks.values() if not task.done())
            capacity = max(0, BULK_MAX_ACTIVE_JOBS - running_tasks)
            if capacity > 0:
                pool = await get_pool()
                async with pool.acquire() as conn:
                    rows = await conn.fetch("""
                        SELECT id
                        FROM bulk_jobs
                        WHERE status IN ('queued', 'running', 'retrying')
                        ORDER BY created_at ASC
                        LIMIT $1
                    """, capacity)
                for row in rows:
                    schedule_bulk_job(int(row["id"]))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("bulk scheduler tick failed")

        _scheduler_wakeup.clear()
        try:
            await asyncio.wait_for(
                _scheduler_wakeup.wait(),
                timeout=BULK_SCHEDULER_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            pass


def start_bulk_scheduler() -> asyncio.Task:
    global _scheduler_task
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(_bulk_scheduler_loop())
    return _scheduler_task


async def stop_bulk_scheduler():
    global _scheduler_task, _scheduler_wakeup
    if _scheduler_task:
        _scheduler_task.cancel()
        await asyncio.gather(_scheduler_task, return_exceptions=True)
        _scheduler_task = None
    tasks = [task for task in _job_tasks.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _job_tasks.clear()
    _scheduler_wakeup = None


async def export_job_csv(user_id: int, job_id: int) -> bytes | None:
    job = await get_job(user_id, job_id)
    if not job:
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT username, status, wallets, platforms, matched, balances,
                   partial_sources, retry_count, error, elapsed_ms
            FROM bulk_items
            WHERE job_id=$1
            ORDER BY id ASC
        """, job_id)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["username", "status", "wallets", "platforms", "matched", "balance_usd", "top_tokens", "chains", "partial_sources", "retry_count", "elapsed_ms", "error"])
    for row in rows:
        balances = _json_value(row["balances"], {})
        wallets = _json_value(row["wallets"], [])
        platforms = _json_value(row["platforms"], [])
        matched = _json_value(row["matched"], [])
        wallet_balances = []
        top_tokens = []
        chains = []
        if isinstance(balances, dict):
            for wallet, info in balances.items():
                if isinstance(info, dict):
                    if info.get("balance_usd") is not None:
                        wallet_balances.append(f"{wallet}:{info.get('balance_usd')}")
                    top_tokens.extend(info.get("top_tokens") or [])
                    chains.extend(info.get("chains") or [])
        writer.writerow([
            _csv_safe(row["username"]),
            _csv_safe(row["status"]),
            _csv_safe(" ".join(wallets)),
            _csv_safe(" | ".join(platforms)),
            _csv_safe(" | ".join(matched)),
            _csv_safe(" | ".join(wallet_balances)),
            _csv_safe(" | ".join(top_tokens[:10])),
            _csv_safe(" | ".join(list(dict.fromkeys(chains)))),
            ",".join(_json_value(row["partial_sources"], [])),
            row["retry_count"] or 0,
            row["elapsed_ms"] or 0,
            _csv_safe(row["error"] or ""),
        ])
    return output.getvalue().encode("utf-8-sig")
