"""
FastAPI backend for Telegram Mini App bulk dashboard.

Run:
    uvicorn miniapp:app --host 0.0.0.0 --port 8000
"""

import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from access import check_bulk_access, get_bulk_status, get_or_create_user
from bulk_service import (
    MAX_BULK_LINES,
    cancel_bulk_job,
    create_bulk_job,
    export_job_csv,
    get_active_job_for_user,
    get_job,
    list_job_items,
    list_jobs,
    parse_bulk_usernames,
    pause_bulk_job,
    resume_bulk_job,
    schedule_bulk_job,
    start_bulk_scheduler,
    stop_bulk_scheduler,
)
from database import consume_rate_limit, init_db
from source_health import flush_source_metrics, get_source_stats

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MINIAPP_DEV_USER_ID = os.getenv("MINIAPP_DEV_USER_ID", "")
MINIAPP_ALLOW_DEV_AUTH = os.getenv("MINIAPP_ALLOW_DEV_AUTH", "false").lower() == "true"
MAX_BULK_FILE_BYTES = int(os.getenv("BULK_MAX_FILE_BYTES", str(2 * 1024 * 1024)))
MINIAPP_RATE_LIMIT_WINDOW = int(os.getenv("MINIAPP_RATE_LIMIT_WINDOW", "60"))
MINIAPP_RATE_LIMIT_MAX = int(os.getenv("MINIAPP_RATE_LIMIT_MAX", "120"))
MINIAPP_UPLOAD_RATE_LIMIT_MAX = int(os.getenv("MINIAPP_UPLOAD_RATE_LIMIT_MAX", "3"))
MINIAPP_UPLOAD_COOLDOWN_SECONDS = int(os.getenv("MINIAPP_UPLOAD_COOLDOWN_SECONDS", "10"))
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
    start_bulk_scheduler()


@app.on_event("shutdown")
async def shutdown():
    await stop_bulk_scheduler()
    await flush_source_metrics()


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
    now = time.time()
    if auth_date <= 0 or auth_date > now + 60 or now - auth_date > 86400:
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
):
    init_data = x_telegram_init_data
    if init_data:
        tg_user = verify_telegram_init_data(init_data)
        user_id = int(tg_user["id"])
        username = tg_user.get("username") or str(user_id)
        await get_or_create_user(user_id, username)
        return {"id": user_id, "username": username}

    if MINIAPP_DEV_USER_ID and MINIAPP_ALLOW_DEV_AUTH:
        user_id = int(MINIAPP_DEV_USER_ID)
        await get_or_create_user(user_id, str(user_id))
        return {"id": user_id, "username": str(user_id)}

    raise HTTPException(status_code=401, detail="Telegram initData required")


def can_use_bulk(user_id: int, credits_active: bool) -> bool:
    return user_id in ADMIN_IDS or user_id in BULK_USER_IDS or credits_active


async def check_rate_limit(user_id: int, bucket: str, max_requests: int, window_seconds: int | None = None):
    allowed = await consume_rate_limit(
        user_id,
        f"miniapp:{bucket}",
        max_requests,
        window_seconds or MINIAPP_RATE_LIMIT_WINDOW,
    )
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests")


@app.get("/api/me")
async def api_me(user=Depends(current_user)):
    await check_rate_limit(user["id"], "me", MINIAPP_RATE_LIMIT_MAX)
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
    await check_rate_limit(user["id"], "jobs", MINIAPP_RATE_LIMIT_MAX)
    return {"jobs": _clean(await list_jobs(user["id"]))}


@app.get("/api/admin/source-stats")
async def api_source_stats(hours: int = 24, user=Depends(current_user)):
    if user["id"] not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin access required")
    await check_rate_limit(user["id"], "source_stats", 30)
    hours = max(1, min(hours, 720))
    return {"hours": hours, "sources": _clean(await get_source_stats(hours))}


@app.post("/api/jobs")
async def api_create_job(
    file: UploadFile = File(...),
    user=Depends(current_user),
):
    await check_rate_limit(user["id"], "upload", MINIAPP_UPLOAD_RATE_LIMIT_MAX)
    await check_rate_limit(user["id"], "upload_cooldown", 1, MINIAPP_UPLOAD_COOLDOWN_SECONDS)
    filename = file.filename or "users.txt"
    if not filename.lower().endswith((".txt", ".csv")):
        raise HTTPException(status_code=400, detail="Only .txt and .csv files are supported")

    raw = await file.read(MAX_BULK_FILE_BYTES + 1)
    if len(raw) > MAX_BULK_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File is too large")
    if b"\x00" in raw[:2048]:
        raise HTTPException(status_code=400, detail="Binary files are not supported")

    text = raw.decode("utf-8-sig", errors="ignore")
    usernames, total_lines = parse_bulk_usernames(text)
    if not usernames:
        raise HTTPException(status_code=400, detail="No usernames found")

    status = await get_bulk_status(user["id"])
    allowed_without_credit = user["id"] in ADMIN_IDS or user["id"] in BULK_USER_IDS
    if not can_use_bulk(user["id"], status["active"]):
        raise HTTPException(status_code=402, detail="No bulk credits")
    active_job = await get_active_job_for_user(user["id"])
    if active_job:
        raise HTTPException(status_code=409, detail=f"Bulk job #{active_job['id']} is still active")

    try:
        job = await create_bulk_job(
            user["id"],
            usernames,
            consume_credit=not allowed_without_credit,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=402, detail="No bulk credits") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    schedule_bulk_job(job["id"])
    return {
        "job": _clean(job),
        "parsed": {
            "total_lines": total_lines,
            "unique_usernames": len(usernames),
            "truncated": len(usernames) >= MAX_BULK_LINES and total_lines > MAX_BULK_LINES,
        },
    }


async def _job_action(user_id: int, job_id: int, action: str):
    handlers = {
        "pause": pause_bulk_job,
        "resume": resume_bulk_job,
        "cancel": cancel_bulk_job,
    }
    result = await handlers[action](user_id, job_id)
    if result:
        if action == "resume":
            schedule_bulk_job(job_id)
        return {"job": _clean(result)}
    existing = await get_job(user_id, job_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Job not found")
    raise HTTPException(
        status_code=409,
        detail=f"Cannot {action} job with status {existing['status']}",
    )


@app.post("/api/jobs/{job_id}/pause")
async def api_pause_job(job_id: int, user=Depends(current_user)):
    await check_rate_limit(user["id"], "job_action", 30)
    return await _job_action(user["id"], job_id, "pause")


@app.post("/api/jobs/{job_id}/resume")
async def api_resume_job(job_id: int, user=Depends(current_user)):
    await check_rate_limit(user["id"], "job_action", 30)
    return await _job_action(user["id"], job_id, "resume")


@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel_job(job_id: int, user=Depends(current_user)):
    await check_rate_limit(user["id"], "job_action", 30)
    return await _job_action(user["id"], job_id, "cancel")


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: int, user=Depends(current_user)):
    await check_rate_limit(user["id"], "job", MINIAPP_RATE_LIMIT_MAX)
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
    sort: str = "",
    user=Depends(current_user),
):
    await check_rate_limit(user["id"], "items", MINIAPP_RATE_LIMIT_MAX)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    sort = sort if sort in ("", "balance_desc") else ""
    data = await list_job_items(
        user["id"],
        job_id,
        limit=limit,
        offset=offset,
        only_found=only_found,
        query=q.strip(),
        sort=sort,
    )
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")

    items = []
    for row in data["items"]:
        item = dict(row)
        for key in ("wallets", "platforms", "matched", "balances", "partial_sources"):
            item[key] = _loads_json(item.get(key))
        items.append(item)
    return {
        "job": _clean(data["job"]),
        "total": data["total"],
        "items": _clean(items),
    }


@app.get("/api/jobs/{job_id}/export.csv")
async def api_export_csv(job_id: int, user=Depends(current_user)):
    await check_rate_limit(user["id"], "export", 20)
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
