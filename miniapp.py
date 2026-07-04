"""
FastAPI backend for Telegram Mini App bulk dashboard.

Run:
    uvicorn miniapp:app --host 0.0.0.0 --port 8000
"""

import asyncio
import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from access import check_bulk_access, get_bulk_status, get_or_create_user
from bulk_service import (
    MAX_BULK_LINES,
    create_bulk_job,
    export_job_csv,
    list_job_items,
    list_jobs,
    parse_bulk_usernames,
    process_bulk_job,
)
from database import get_pool, init_db

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MINIAPP_DEV_USER_ID = os.getenv("MINIAPP_DEV_USER_ID", "")
MAX_BULK_FILE_BYTES = int(os.getenv("BULK_MAX_FILE_BYTES", str(2 * 1024 * 1024)))
ADMIN_IDS = {
    int(part.strip())
    for part in (os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID", "")).replace(";", ",").split(",")
    if part.strip().isdigit()
}
BULK_USER_IDS = {
    int(part.strip())
    for part in os.getenv("BULK_USER_IDS", "").replace(";", ",").split(",")
    if part.strip().isdigit()
}

BASE_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = BASE_DIR / "webapp"

app = FastAPI(title="Crypto OSINT Bulk Mini App")


@app.on_event("startup")
async def startup():
    await init_db()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id
            FROM bulk_jobs
            WHERE status IN ('queued', 'running')
            ORDER BY created_at ASC
            LIMIT 5
        """)
    for row in rows:
        asyncio.create_task(process_bulk_job(row["id"]))


def _loads_json(value):
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _clean(obj):
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def verify_telegram_init_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN is not configured")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing Telegram hash")

    auth_date = int(pairs.get("auth_date", "0") or "0")
    if auth_date and time.time() - auth_date > 86400:
        raise HTTPException(status_code=401, detail="Telegram auth expired")

    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Bad Telegram auth")

    user_raw = pairs.get("user")
    if not user_raw:
        raise HTTPException(status_code=401, detail="Missing Telegram user")
    try:
        return json.loads(user_raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=401, detail="Bad Telegram user") from exc


async def current_user(
    x_telegram_init_data: str | None = Header(default=None),
    tg: str | None = Query(default=None),
):
    init_data = x_telegram_init_data or tg
    if init_data:
        tg_user = verify_telegram_init_data(init_data)
        user_id = int(tg_user["id"])
        username = tg_user.get("username") or str(user_id)
        await get_or_create_user(user_id, username)
        return {"id": user_id, "username": username}

    if MINIAPP_DEV_USER_ID:
        user_id = int(MINIAPP_DEV_USER_ID)
        await get_or_create_user(user_id, str(user_id))
        return {"id": user_id, "username": str(user_id)}

    raise HTTPException(status_code=401, detail="Telegram initData required")


def can_use_bulk(user_id: int, credits_active: bool) -> bool:
    return user_id in ADMIN_IDS or user_id in BULK_USER_IDS or credits_active


@app.get("/api/me")
async def api_me(user=Depends(current_user)):
    status = await get_bulk_status(user["id"])
    return {
        "user": user,
        "bulk": status,
        "limits": {
            "max_lines": MAX_BULK_LINES,
            "max_file_bytes": MAX_BULK_FILE_BYTES,
        },
        "admin": user["id"] in ADMIN_IDS,
    }


@app.get("/api/jobs")
async def api_jobs(user=Depends(current_user)):
    return {"jobs": _clean(await list_jobs(user["id"]))}


@app.post("/api/jobs")
async def api_create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user=Depends(current_user),
):
    filename = file.filename or "users.txt"
    if not filename.lower().endswith((".txt", ".csv")):
        raise HTTPException(status_code=400, detail="Only .txt and .csv files are supported")

    raw = await file.read(MAX_BULK_FILE_BYTES + 1)
    if len(raw) > MAX_BULK_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File is too large")

    text = raw.decode("utf-8-sig", errors="ignore")
    usernames, total_lines = parse_bulk_usernames(text)
    if not usernames:
        raise HTTPException(status_code=400, detail="No usernames found")

    status = await get_bulk_status(user["id"])
    allowed_without_credit = user["id"] in ADMIN_IDS or user["id"] in BULK_USER_IDS
    if not can_use_bulk(user["id"], status["active"]):
        raise HTTPException(status_code=402, detail="No bulk credits")

    try:
        job = await create_bulk_job(
            user["id"],
            usernames,
            consume_credit=not allowed_without_credit,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=402, detail="No bulk credits") from exc

    background_tasks.add_task(process_bulk_job, job["id"])
    return {
        "job": _clean(job),
        "parsed": {
            "total_lines": total_lines,
            "unique_usernames": len(usernames),
            "truncated": len(usernames) >= MAX_BULK_LINES and total_lines > MAX_BULK_LINES,
        },
    }


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: int, user=Depends(current_user)):
    items = await list_job_items(user["id"], job_id, limit=1, offset=0)
    if not items:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job": _clean(items["job"])}


@app.get("/api/jobs/{job_id}/items")
async def api_job_items(
    job_id: int,
    limit: int = 100,
    offset: int = 0,
    only_found: bool = False,
    q: str = "",
    user=Depends(current_user),
):
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    data = await list_job_items(user["id"], job_id, limit=limit, offset=offset, only_found=only_found, query=q.strip())
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")

    items = []
    for row in data["items"]:
        item = dict(row)
        for key in ("wallets", "platforms", "matched", "balances"):
            item[key] = _loads_json(item.get(key))
        items.append(item)
    return {
        "job": _clean(data["job"]),
        "total": data["total"],
        "items": _clean(items),
    }


@app.get("/api/jobs/{job_id}/export.csv")
async def api_export_csv(job_id: int, user=Depends(current_user)):
    data = await export_job_csv(user["id"], job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="bulk_job_{job_id}.csv"'},
    )


@app.get("/health")
async def health():
    return {"ok": True}


app.mount("/assets", StaticFiles(directory=WEBAPP_DIR), name="assets")


@app.get("/")
async def index():
    return FileResponse(WEBAPP_DIR / "index.html")
