"""
web.py — веб-версия Bulk-поиска.

Отдельный процесс от Telegram-бота, переиспользует searcher.py.
Запуск:  uvicorn web:app --host 0.0.0.0 --port $PORT

Доступ по ключам из env WEB_BULK_KEYS (через запятую). Если список пуст —
сервис не пускает никого (специально, чтобы не оставить открытым по ошибке).
"""

import asyncio
import csv
import io
import os
import time
import uuid

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response

from searcher import run_bulk_search, enrich_balances, GOLDRUSH_API_KEY

WEB_BULK_KEYS = {k.strip() for k in os.getenv("WEB_BULK_KEYS", "").split(",") if k.strip()}
MAX_BULK_LINES = int(os.getenv("MAX_BULK_LINES", "10000"))
WEB_MAX_FILE_BYTES = int(os.getenv("WEB_MAX_FILE_BYTES", str(4 * 1024 * 1024)))
BULK_WORKERS = int(os.getenv("BULK_WORKERS", "20"))

app = FastAPI(title="Bulk OSINT Web")

# job_id -> dict(status, total, processed, found, csv, error, created)
JOBS: dict[str, dict] = {}


def _check_key(key: str):
    if not WEB_BULK_KEYS:
        raise HTTPException(403, "Доступ закрыт: не задан WEB_BULK_KEYS на сервере.")
    if key.strip() not in WEB_BULK_KEYS:
        raise HTTPException(403, "Неверный ключ доступа.")


def _parse_usernames(raw: bytes) -> list[str]:
    text = raw.decode("utf-8", errors="ignore")
    seen, out = set(), []
    for line in text.splitlines():
        cell = line.split(",")[0].split(";")[0].strip().lstrip("@")
        if not cell:
            continue
        low = cell.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(cell)
        if len(out) >= MAX_BULK_LINES:
            break
    return out


def _short(w: str) -> str:
    return f"{w[:6]}…{w[-4:]}" if len(w) > 12 else w


def _build_csv(results: list[dict], balances: dict) -> bytes:
    show_balance = bool(GOLDRUSH_API_KEY)
    out = io.StringIO()
    writer = csv.writer(out)
    header = ["username", "found_count", "wallets", "platforms", "matched", "errors"]
    if show_balance:
        header[3:3] = ["wallet_balances_usd", "wallet_top_tokens", "wallet_chains"]
    writer.writerow(header)
    for data in results:
        found = [r for r in data.get("results", []) if r.get("found")]
        wallets = data.get("all_wallets") or []
        platforms = list(dict.fromkeys(r.get("platform", "") for r in found if r.get("platform")))
        matched = list(dict.fromkeys(str(r.get("matched") or "") for r in found if r.get("matched")))
        errors = data.get("diagnostics", {}).get("errors", [])
        row = [
            data.get("username", ""),
            data.get("found_count", 0),
            " ".join(wallets),
            " | ".join(platforms),
            " | ".join(matched),
            " | ".join(f"{e.get('platform')}: {e.get('error')}" for e in errors),
        ]
        if show_balance:
            bal, tok, ch = [], [], []
            for w in wallets:
                info = balances.get(w) or {}
                usd = info.get("balance_usd")
                bal.append(f"{w}=${usd:,.2f}" if isinstance(usd, (int, float)) else f"{w}={info.get('note') or 'n/a'}")
                if info.get("top_tokens"):
                    tok.append(f"{_short(w)}: {', '.join(info['top_tokens'][:5])}")
                if info.get("chains"):
                    ch.append(f"{_short(w)}: {','.join(info['chains'])}")
            row[3:3] = [" | ".join(bal), " | ".join(tok), " | ".join(ch)]
        writer.writerow(row)
    return out.getvalue().encode("utf-8-sig")


async def _run_job(job_id: str, usernames: list[str]):
    job = JOBS[job_id]
    results: list[dict] = []
    sem = asyncio.Semaphore(BULK_WORKERS)
    lock = asyncio.Lock()

    async def scan_one(username: str):
        async with sem:
            try:
                data = await run_bulk_search(username)
            except Exception as exc:
                data = {"username": username, "found_count": 0, "all_wallets": [], "results": [],
                        "diagnostics": {"errors": [{"platform": "bulk", "error": str(exc)[:120]}]}}
        async with lock:
            results.append(data)
            job["processed"] += 1
            if data.get("found_count", 0) > 0:
                job["found"] += 1

    try:
        await asyncio.gather(*(scan_one(u) for u in usernames))
        job["status"] = "balances"
        balances = {}
        if GOLDRUSH_API_KEY:
            wallets = list(dict.fromkeys(w for d in results for w in (d.get("all_wallets") or [])))
            job["wallets"] = len(wallets)
            balances = await enrich_balances(wallets)
        job["csv"] = _build_csv(results, balances)
        job["status"] = "done"
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)[:200]


@app.post("/api/bulk")
async def start_bulk(key: str = Form(...), file: UploadFile = File(...)):
    _check_key(key)
    raw = await file.read()
    if len(raw) > WEB_MAX_FILE_BYTES:
        raise HTTPException(413, f"Файл больше {WEB_MAX_FILE_BYTES // 1024 // 1024} MB.")
    usernames = _parse_usernames(raw)
    if not usernames:
        raise HTTPException(400, "В файле не нашлось ни одного ника.")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "running", "total": len(usernames), "processed": 0,
                    "found": 0, "wallets": 0, "csv": None, "error": "", "created": time.time()}
    asyncio.create_task(_run_job(job_id, usernames))
    return {"job_id": job_id, "total": len(usernames)}


@app.get("/api/status/{job_id}")
async def status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена.")
    return JSONResponse({k: job[k] for k in ("status", "total", "processed", "found", "wallets", "error")})


@app.get("/api/result/{job_id}")
async def result(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done" or not job.get("csv"):
        raise HTTPException(404, "Результат ещё не готов.")
    return Response(
        content=job["csv"],
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=bulk_results.csv"},
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


HTML_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bulk OSINT</title>
<style>
:root{color-scheme:dark}
body{margin:0;background:#0b0e14;color:#e6e6e6;font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
.wrap{max-width:560px;margin:8vh auto;padding:0 20px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:#8a94a6;margin:0 0 28px}
.card{background:#121723;border:1px solid #1f2738;border-radius:14px;padding:22px}
label{display:block;font-size:13px;color:#9aa4b6;margin:14px 0 6px}
input[type=text],input[type=password]{width:100%;box-sizing:border-box;background:#0b0e14;border:1px solid #28324a;border-radius:9px;color:#e6e6e6;padding:11px 12px;font-size:14px}
.file{border:1px dashed #2c3650;border-radius:10px;padding:18px;text-align:center;color:#8a94a6;cursor:pointer}
.file.drag{border-color:#4f7cff;color:#cdd7ea}
button{margin-top:18px;width:100%;background:#4f7cff;border:0;border-radius:10px;color:#fff;font-size:15px;font-weight:600;padding:13px;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
.bar{height:8px;background:#1b2235;border-radius:6px;overflow:hidden;margin-top:18px;display:none}
.bar>i{display:block;height:100%;width:0;background:#4f7cff;transition:width .3s}
.stat{display:flex;justify-content:space-between;color:#9aa4b6;font-size:13px;margin-top:10px}
a.dl{display:none;margin-top:16px;text-align:center;background:#1f8a4c;color:#fff;text-decoration:none;border-radius:10px;padding:13px;font-weight:600}
.err{color:#ff6b6b;font-size:13px;margin-top:12px;display:none}
</style></head><body><div class="wrap">
<h1>📦 Bulk OSINT</h1>
<p class="sub">Список @username → кошельки и балансы в CSV</p>
<div class="card">
  <label>Ключ доступа</label>
  <input id="key" type="password" placeholder="ваш ключ">
  <label>Файл .txt / .csv (по одному нику на строку)</label>
  <div class="file" id="drop">перетащи файл сюда или нажми<br><small id="fname"></small></div>
  <input id="file" type="file" accept=".txt,.csv" style="display:none">
  <button id="go" disabled>Запустить</button>
  <div class="bar" id="bar"><i id="fill"></i></div>
  <div class="stat" id="stat" style="display:none"><span id="prog">0 / 0</span><span id="found">найдено 0</span></div>
  <a class="dl" id="dl">⬇ Скачать CSV</a>
  <div class="err" id="err"></div>
</div></div>
<script>
const $=id=>document.getElementById(id);
let chosen=null;
$('drop').onclick=()=>$('file').click();
$('file').onchange=e=>{chosen=e.target.files[0];$('fname').textContent=chosen?chosen.name:'';check()};
['dragover','dragleave','drop'].forEach(ev=>$('drop').addEventListener(ev,e=>{e.preventDefault();
 $('drop').classList.toggle('drag',ev==='dragover');
 if(ev==='drop'){chosen=e.dataTransfer.files[0];$('fname').textContent=chosen?chosen.name:'';check()}}));
$('key').oninput=check;
function check(){$('go').disabled=!(chosen&&$('key').value.trim())}
function fail(m){$('err').style.display='block';$('err').textContent=m;$('go').disabled=false}
$('go').onclick=async()=>{
  $('err').style.display='none';$('dl').style.display='none';$('go').disabled=true;
  const fd=new FormData();fd.append('key',$('key').value.trim());fd.append('file',chosen);
  let r;try{r=await fetch('/api/bulk',{method:'POST',body:fd})}catch(e){return fail('Сеть недоступна')}
  if(!r.ok){const j=await r.json().catch(()=>({}));return fail(j.detail||'Ошибка запуска')}
  const {job_id,total}=await r.json();
  $('bar').style.display='block';$('stat').style.display='flex';
  poll(job_id,total);
};
async function poll(id,total){
  const r=await fetch('/api/status/'+id);const j=await r.json();
  $('fill').style.width=(total?100*j.processed/total:0)+'%';
  $('prog').textContent=j.processed+' / '+j.total+(j.status==='balances'?' · считаю балансы':'');
  $('found').textContent='найдено '+j.found;
  if(j.status==='done'){$('fill').style.width='100%';$('dl').href='/api/result/'+id;$('dl').style.display='block';$('go').disabled=false;return}
  if(j.status==='error'){return fail(j.error||'Ошибка обработки')}
  setTimeout(()=>poll(id,total),1500);
}
</script></body></html>"""
