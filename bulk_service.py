"""
Persistent bulk search jobs for the Telegram Mini App.
"""

import asyncio
import csv
import io
import json
import os
import time
from typing import Any

import httpx

from database import get_pool
from searcher import extract_username, run_bulk_search, enrich_balances, GOLDRUSH_API_KEY

MAX_BULK_LINES = int(os.getenv("MAX_BULK_LINES", "10000"))
BULK_WORKERS = max(1, int(os.getenv("BULK_WORKERS", "20")))
BULK_BALANCE_WORKERS = max(1, int(os.getenv("BULK_BALANCE_WORKERS", "8")))
BULK_INCLUDE_BALANCES = os.getenv("BULK_INCLUDE_BALANCES", "true").lower() == "true"
BULK_JOB_TIMEOUT_SECONDS = int(os.getenv("BULK_JOB_TIMEOUT_SECONDS", "7200"))
BULK_MAX_ACTIVE_JOBS = int(os.getenv("BULK_MAX_ACTIVE_JOBS", "2"))

_active_jobs: set[int] = set()
_job_slots = asyncio.Semaphore(max(1, BULK_MAX_ACTIVE_JOBS))


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
            WHERE user_id=$1 AND status IN ('queued', 'running')
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
                WHERE user_id=$1 AND status IN ('queued', 'running')
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
            SELECT id, username, status, wallets, platforms, matched, balances, error, elapsed_ms, updated_at
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
                SET status='running', started_at=COALESCE(started_at, NOW()), error=NULL
                WHERE id=$1
            """, job_id)
        elif status in ("done", "failed", "canceled"):
            await conn.execute("""
                UPDATE bulk_jobs
                SET status=$2, finished_at=NOW(), error=$3
                WHERE id=$1
            """, job_id, status, error)
        else:
            await conn.execute("UPDATE bulk_jobs SET status=$2, error=$3 WHERE id=$1", job_id, status, error)


async def _store_item_result(job_id: int, item_id: int, data: dict[str, Any], balances: dict[str, Any] | None = None):
    balances = balances or {}
    found = [r for r in data.get("results", []) if r.get("found")]
    wallets = data.get("all_wallets") or []
    platforms = list(dict.fromkeys(r.get("platform", "") for r in found if r.get("platform")))
    matched = list(dict.fromkeys(str(r.get("matched") or "") for r in found if r.get("matched")))
    elapsed_ms = data.get("diagnostics", {}).get("elapsed_ms", 0)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchval("""
                UPDATE bulk_items
                SET status='done',
                    wallets=$3::jsonb,
                    platforms=$4::jsonb,
                    matched=$5::jsonb,
                    balances=$6::jsonb,
                    result=$7::jsonb,
                    elapsed_ms=$8,
                    updated_at=NOW()
                WHERE id=$1 AND job_id=$2 AND status='pending'
                RETURNING 1
            """,
                item_id,
                job_id,
                json.dumps(wallets),
                json.dumps(platforms),
                json.dumps(matched),
                json.dumps(balances),
                json.dumps(data),
                int(elapsed_ms or 0),
            )
            if updated:
                await conn.execute("""
                    UPDATE bulk_jobs
                    SET processed_count = processed_count + 1,
                        found_count = found_count + CASE WHEN $2 THEN 1 ELSE 0 END
                    WHERE id=$1
                """, job_id, bool(wallets))


async def _store_item_error(job_id: int, item_id: int, error: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchval("""
                UPDATE bulk_items
                SET status='failed', error=$3, updated_at=NOW()
                WHERE id=$1 AND job_id=$2 AND status='pending'
                RETURNING 1
            """, item_id, job_id, error[:300])
            if updated:
                await conn.execute("""
                    UPDATE bulk_jobs
                    SET processed_count = processed_count + 1
                    WHERE id=$1
                """, job_id)


async def process_bulk_job(job_id: int):
    if job_id in _active_jobs:
        return
    _active_jobs.add(job_id)
    started = time.perf_counter()
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
                if status not in ("queued", "running"):
                    return

                async with _job_slots:
                    await _set_job_status(job_id, "running")
                    async with pool.acquire() as conn:
                        rows = await conn.fetch("""
                            SELECT id, username
                            FROM bulk_items
                            WHERE job_id=$1 AND status='pending'
                            ORDER BY id ASC
                        """, job_id)

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
                                if time.perf_counter() - started > BULK_JOB_TIMEOUT_SECONDS:
                                    await _store_item_error(job_id, item["id"], "job timeout")
                                    continue

                                data = await run_bulk_search(item["username"])
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
                    await _set_job_status(job_id, "done")
            finally:
                await lock_conn.execute("SELECT pg_advisory_unlock($1, $2)", 52026, job_id)
    except Exception as exc:
        await _set_job_status(job_id, "failed", str(exc)[:300])
    finally:
        _active_jobs.discard(job_id)


async def export_job_csv(user_id: int, job_id: int) -> bytes | None:
    job = await get_job(user_id, job_id)
    if not job:
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT username, status, wallets, platforms, matched, balances, error, elapsed_ms
            FROM bulk_items
            WHERE job_id=$1
            ORDER BY id ASC
        """, job_id)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["username", "status", "wallets", "platforms", "matched", "balance_usd", "top_tokens", "chains", "elapsed_ms", "error"])
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
            row["elapsed_ms"] or 0,
            _csv_safe(row["error"] or ""),
        ])
    return output.getvalue().encode("utf-8-sig")
