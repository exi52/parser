const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
if (tg) {
  tg.ready();
  tg.expand();
}

const initData = tg && tg.initData ? tg.initData : "";
const state = {
  me: null,
  jobs: [],
  selectedJobId: null,
  offset: 0,
  limit: 100,
  onlyFound: false,
  sort: "",
  query: "",
  pollTimer: null,
};

const els = {
  accountLine: document.getElementById("accountLine"),
  creditsMetric: document.getElementById("creditsMetric"),
  processedMetric: document.getElementById("processedMetric"),
  foundMetric: document.getElementById("foundMetric"),
  statusMetric: document.getElementById("statusMetric"),
  refreshBtn: document.getElementById("refreshBtn"),
  uploadForm: document.getElementById("uploadForm"),
  fileInput: document.getElementById("fileInput"),
  fileLabel: document.getElementById("fileLabel"),
  uploadStatus: document.getElementById("uploadStatus"),
  jobsCount: document.getElementById("jobsCount"),
  jobsList: document.getElementById("jobsList"),
  jobTitle: document.getElementById("jobTitle"),
  jobMeta: document.getElementById("jobMeta"),
  exportLink: document.getElementById("exportLink"),
  tableExportLink: document.getElementById("tableExportLink"),
  progressBar: document.getElementById("progressBar"),
  onlyFound: document.getElementById("onlyFound"),
  sortSelect: document.getElementById("sortSelect"),
  searchInput: document.getElementById("searchInput"),
  prevPage: document.getElementById("prevPage"),
  nextPage: document.getElementById("nextPage"),
  itemsBody: document.getElementById("itemsBody"),
};

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (initData) headers.set("X-Telegram-Init-Data", initData);
  const res = await fetch(path, { ...options, headers });
  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      message = data.detail || message;
    } catch (_) {}
    throw new Error(message);
  }
  return res.json();
}

function esc(value) {
  return String(value == null ? "" : value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function fmtDate(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function compact(value) {
  return Number(value || 0).toLocaleString();
}

function shortWallet(wallet) {
  if (!wallet || wallet.length <= 16) return wallet || "";
  return `${wallet.slice(0, 6)}...${wallet.slice(-5)}`;
}

function progress(job) {
  if (!job || !job.total_count) return 0;
  return Math.min(100, Math.round((job.processed_count / job.total_count) * 100));
}

function statusClass(status) {
  return `status-${String(status || "idle").toLowerCase()}`;
}

function setUploadStatus(text, tone = "") {
  els.uploadStatus.textContent = text || "";
  els.uploadStatus.dataset.tone = tone;
}

function selectedJob() {
  return state.jobs.find(job => job.id === state.selectedJobId) || null;
}

function updateMetrics(job = selectedJob()) {
  const bulk = state.me && state.me.bulk ? state.me.bulk : {};
  els.creditsMetric.textContent = compact(bulk.credits || 0);
  if (!job) {
    els.processedMetric.textContent = "-";
    els.foundMetric.textContent = "-";
    els.statusMetric.textContent = "Idle";
    return;
  }
  els.processedMetric.textContent = `${compact(job.processed_count)}/${compact(job.total_count)}`;
  els.foundMetric.textContent = compact(job.found_count);
  els.statusMetric.textContent = job.status || "idle";
}

async function loadMe() {
  state.me = await api("/api/me");
  const user = state.me.user;
  const bulk = state.me.bulk;
  els.accountLine.textContent = `@${user.username || user.id}`;
  els.creditsMetric.textContent = compact(bulk.credits || 0);
}

async function loadJobs() {
  const data = await api("/api/jobs");
  state.jobs = data.jobs || [];
  renderJobs();
  if (!state.selectedJobId && state.jobs[0]) {
    await selectJob(state.jobs[0].id);
  } else {
    updateMetrics();
  }
}

function renderJobs() {
  els.jobsCount.textContent = state.jobs.length ? `${state.jobs.length} saved runs` : "No jobs";
  if (!state.jobs.length) {
    els.jobsList.innerHTML = `<div class="emptyState small">No bulk runs yet</div>`;
    return;
  }
  els.jobsList.innerHTML = state.jobs.map(job => {
    const active = job.id === state.selectedJobId ? " active" : "";
    const pct = progress(job);
    return `
      <button class="jobCard${active}" data-job-id="${job.id}" type="button">
        <span class="jobTop">
          <strong>#${job.id}</strong>
          <span class="statusPill ${statusClass(job.status)}">${esc(job.status)}</span>
        </span>
        <span class="jobNumbers">${compact(job.processed_count)}/${compact(job.total_count)} checked · ${compact(job.found_count)} found</span>
        <span class="miniTrack"><span style="width:${pct}%"></span></span>
        <span class="jobDate">${fmtDate(job.created_at)}</span>
      </button>
    `;
  }).join("");
  els.jobsList.querySelectorAll(".jobCard").forEach(btn => {
    btn.addEventListener("click", () => selectJob(Number(btn.dataset.jobId)));
  });
}

async function selectJob(jobId) {
  state.selectedJobId = jobId;
  state.offset = 0;
  renderJobs();
  await loadItems();
  restartPolling();
}

async function loadItems() {
  if (!state.selectedJobId) return;
  const params = new URLSearchParams({
    limit: String(state.limit),
    offset: String(state.offset),
    only_found: String(state.onlyFound),
    sort: state.sort,
    q: state.query,
  });
  const data = await api(`/api/jobs/${state.selectedJobId}/items?${params.toString()}`);
  renderJob(data.job);
  renderItems(data.items || []);
  els.prevPage.disabled = state.offset <= 0;
  els.nextPage.disabled = state.offset + state.limit >= data.total;
}

function renderJob(job) {
  const pct = progress(job);
  els.jobTitle.textContent = `Run #${job.id}`;
  els.jobMeta.textContent = `${compact(job.processed_count)} of ${compact(job.total_count)} checked · ${compact(job.found_count)} found · ${pct}%`;
  els.progressBar.style.width = `${pct}%`;
  const exportUrl = `/api/jobs/${job.id}/export.csv${initData ? `?tg=${encodeURIComponent(initData)}` : ""}`;
  els.exportLink.href = exportUrl;
  els.exportLink.classList.remove("disabled");
  els.tableExportLink.href = exportUrl;
  els.tableExportLink.classList.remove("disabled");
  updateMetrics(job);
}

function balanceLine(balances) {
  if (!balances || typeof balances !== "object") return "";
  return Object.entries(balances).map(([wallet, info]) => {
    if (!info || typeof info !== "object") return "";
    const usd = info.balance_usd;
    const tokens = Array.isArray(info.top_tokens) ? info.top_tokens.slice(0, 3) : [];
    const chains = Array.isArray(info.chains) ? info.chains : [];
    const parts = [];
    if (usd !== null && usd !== undefined) {
      parts.push(`<strong>$${Number(usd).toLocaleString()}</strong>`);
    }
    if (tokens.length) parts.push(esc(tokens.join(", ")));
    if (chains.length) parts.push(`<span>${esc(chains.join(", "))}</span>`);
    return parts.length ? `<div class="balanceRow"><code>${esc(shortWallet(wallet))}</code>${parts.join(" · ")}</div>` : "";
  }).filter(Boolean).join("");
}

function walletLinks(wallet) {
  if (!wallet) return "";
  const safe = encodeURIComponent(wallet);
  if (wallet.startsWith("0x")) {
    return `
      <a href="https://etherscan.io/address/${safe}" target="_blank" rel="noreferrer">Etherscan</a>
      <a href="https://debank.com/profile/${safe}" target="_blank" rel="noreferrer">DeBank</a>
      <a href="https://zapper.xyz/account/${safe}" target="_blank" rel="noreferrer">Zapper</a>
    `;
  }
  if (wallet.length > 30) {
    return `<a href="https://solscan.io/account/${safe}" target="_blank" rel="noreferrer">Solscan</a>`;
  }
  return "";
}

function renderItems(items) {
  if (!items.length) {
    els.itemsBody.innerHTML = `<div class="emptyState">No rows on this page</div>`;
    return;
  }
  els.itemsBody.innerHTML = items.map(item => {
    const wallets = Array.isArray(item.wallets) ? item.wallets : [];
    const platforms = Array.isArray(item.platforms) ? item.platforms : [];
    const balances = item.balances && typeof item.balances === "object" ? item.balances : {};
    return `
      <article class="resultCard">
        <div class="resultMain">
          <div>
            <h3>@${esc(item.username)}</h3>
            <div class="platforms">
              ${platforms.length ? platforms.map(p => `<span>${esc(p)}</span>`).join("") : `<span>no source</span>`}
            </div>
          </div>
          <span class="statusPill ${statusClass(item.status)}">${esc(item.status)}</span>
        </div>
        <div class="walletStack">
          ${wallets.length ? wallets.map(w => `
            <div class="walletRow">
              <code>${esc(w)}</code>
              <div class="walletActions">${walletLinks(w)}</div>
            </div>
          `).join("") : `<span class="muted">No public wallet</span>`}
        </div>
        ${balanceLine(balances) ? `<div class="balances">${balanceLine(balances)}</div>` : ""}
        <div class="resultFoot">
          <span>${Number(item.elapsed_ms || 0).toLocaleString()} ms</span>
          ${item.error ? `<span class="errorText">${esc(item.error)}</span>` : ""}
        </div>
      </article>
    `;
  }).join("");
}

function restartPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    try {
      const before = state.selectedJobId;
      await loadJobs();
      if (before) {
        state.selectedJobId = before;
        await loadItems();
      }
    } catch (err) {
      console.warn(err);
    }
  }, 5000);
}

function setFile(file) {
  if (!file) return;
  els.fileLabel.textContent = file.name;
  setUploadStatus(`${(file.size / 1024).toFixed(1)} KB ready`, "ready");
}

["dragenter", "dragover"].forEach(name => {
  els.uploadForm.addEventListener(name, event => {
    event.preventDefault();
    els.uploadForm.classList.add("dragging");
  });
});

["dragleave", "drop"].forEach(name => {
  els.uploadForm.addEventListener(name, event => {
    event.preventDefault();
    els.uploadForm.classList.remove("dragging");
  });
});

els.uploadForm.addEventListener("drop", event => {
  const file = event.dataTransfer && event.dataTransfer.files ? event.dataTransfer.files[0] : null;
  if (!file) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  els.fileInput.files = transfer.files;
  setFile(file);
});

els.fileInput.addEventListener("change", () => setFile(els.fileInput.files[0]));

els.uploadForm.addEventListener("submit", async event => {
  event.preventDefault();
  const file = els.fileInput.files[0];
  if (!file) return;
  setUploadStatus("Uploading...", "busy");
  const form = new FormData();
  form.append("file", file);
  try {
    const data = await api("/api/jobs", { method: "POST", body: form });
    setUploadStatus(`Run #${data.job.id} started · ${compact(data.parsed.unique_usernames)} usernames`, "ready");
    els.fileInput.value = "";
    els.fileLabel.textContent = "Drop TXT or CSV";
    await loadMe();
    await loadJobs();
    await selectJob(data.job.id);
  } catch (err) {
    setUploadStatus(err.message, "error");
  }
});

els.refreshBtn.addEventListener("click", async () => {
  els.refreshBtn.classList.add("spinning");
  try {
    await loadMe();
    await loadJobs();
    if (state.selectedJobId) await loadItems();
  } finally {
    setTimeout(() => els.refreshBtn.classList.remove("spinning"), 300);
  }
});

els.onlyFound.addEventListener("change", async () => {
  state.onlyFound = els.onlyFound.checked;
  state.offset = 0;
  await loadItems();
});

els.sortSelect.addEventListener("change", async () => {
  state.sort = els.sortSelect.value;
  state.offset = 0;
  await loadItems();
});

let searchTimer;
els.searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    state.query = els.searchInput.value.trim();
    state.offset = 0;
    await loadItems();
  }, 300);
});

els.prevPage.addEventListener("click", async () => {
  state.offset = Math.max(0, state.offset - state.limit);
  await loadItems();
});

els.nextPage.addEventListener("click", async () => {
  state.offset += state.limit;
  await loadItems();
});

(async function init() {
  try {
    await loadMe();
    await loadJobs();
    restartPolling();
  } catch (err) {
    els.accountLine.textContent = err.message;
    els.itemsBody.innerHTML = `<div class="emptyState">${esc(err.message)}</div>`;
  }
})();
