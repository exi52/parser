const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
}

const initData = tg?.initData || "";
const state = {
  me: null,
  jobs: [],
  selectedJobId: null,
  offset: 0,
  limit: 100,
  onlyFound: false,
  query: "",
  pollTimer: null,
};

const els = {
  accountLine: document.getElementById("accountLine"),
  refreshBtn: document.getElementById("refreshBtn"),
  uploadForm: document.getElementById("uploadForm"),
  fileInput: document.getElementById("fileInput"),
  uploadStatus: document.getElementById("uploadStatus"),
  jobsList: document.getElementById("jobsList"),
  jobTitle: document.getElementById("jobTitle"),
  jobMeta: document.getElementById("jobMeta"),
  exportLink: document.getElementById("exportLink"),
  progressBar: document.getElementById("progressBar"),
  onlyFound: document.getElementById("onlyFound"),
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

function fmtDate(value) {
  if (!value) return "";
  return new Date(value).toLocaleString();
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function progress(job) {
  if (!job || !job.total_count) return 0;
  return Math.min(100, Math.round((job.processed_count / job.total_count) * 100));
}

function setUploadStatus(text) {
  els.uploadStatus.textContent = text || "";
}

async function loadMe() {
  state.me = await api("/api/me");
  const user = state.me.user;
  const bulk = state.me.bulk;
  els.accountLine.textContent = `@${user.username || user.id} · bulk credits: ${bulk.credits || 0}`;
}

async function loadJobs() {
  const data = await api("/api/jobs");
  state.jobs = data.jobs || [];
  renderJobs();
  if (!state.selectedJobId && state.jobs[0]) {
    await selectJob(state.jobs[0].id);
  }
}

function renderJobs() {
  if (!state.jobs.length) {
    els.jobsList.innerHTML = `<div class="empty">No bulk jobs yet</div>`;
    return;
  }
  els.jobsList.innerHTML = state.jobs.map(job => {
    const active = job.id === state.selectedJobId ? " active" : "";
    return `
      <button class="job${active}" data-job-id="${job.id}" type="button">
        <strong>#${job.id} · ${esc(job.status)}</strong>
        <span>${job.processed_count}/${job.total_count} checked · found ${job.found_count}</span>
        <span>${fmtDate(job.created_at)}</span>
      </button>
    `;
  }).join("");
  els.jobsList.querySelectorAll(".job").forEach(btn => {
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
    q: state.query,
  });
  const data = await api(`/api/jobs/${state.selectedJobId}/items?${params.toString()}`);
  renderJob(data.job);
  renderItems(data.items || []);
  els.prevPage.disabled = state.offset <= 0;
  els.nextPage.disabled = state.offset + state.limit >= data.total;
}

function renderJob(job) {
  els.jobTitle.textContent = `Job #${job.id} · ${job.status}`;
  els.jobMeta.textContent = `${job.processed_count}/${job.total_count} checked · found ${job.found_count} · ${progress(job)}%`;
  els.progressBar.style.width = `${progress(job)}%`;
  els.exportLink.href = `/api/jobs/${job.id}/export.csv${initData ? `?tg=${encodeURIComponent(initData)}` : ""}`;
  els.exportLink.classList.remove("disabled");
}

function renderItems(items) {
  if (!items.length) {
    els.itemsBody.innerHTML = `<tr><td colspan="6" class="empty">No rows on this page</td></tr>`;
    return;
  }
  els.itemsBody.innerHTML = items.map(item => {
    const wallets = Array.isArray(item.wallets) ? item.wallets : [];
    const platforms = Array.isArray(item.platforms) ? item.platforms : [];
    const balances = item.balances && typeof item.balances === "object" ? item.balances : {};
    const balanceText = Object.values(balances)
      .filter(v => v && typeof v === "object" && v.balance_usd !== null && v.balance_usd !== undefined)
      .map(v => `$${Number(v.balance_usd).toLocaleString()}`)
      .join("<br>");
    return `
      <tr>
        <td><strong>@${esc(item.username)}</strong></td>
        <td><span class="pill ${esc(item.status)}">${esc(item.status)}</span>${item.error ? `<p>${esc(item.error)}</p>` : ""}</td>
        <td>${wallets.length ? wallets.map(w => `<code>${esc(w)}</code>`).join("") : `<span class="pill">none</span>`}</td>
        <td>${platforms.map(p => `<span class="pill">${esc(p)}</span>`).join(" ")}</td>
        <td>${balanceText || `<span class="pill">n/a</span>`}</td>
        <td>${Number(item.elapsed_ms || 0)} ms</td>
      </tr>
    `;
  }).join("");
}

function restartPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    try {
      await loadJobs();
      if (state.selectedJobId) await loadItems();
    } catch (err) {
      console.warn(err);
    }
  }, 5000);
}

els.uploadForm.addEventListener("submit", async event => {
  event.preventDefault();
  const file = els.fileInput.files[0];
  if (!file) return;
  setUploadStatus("Uploading...");
  const form = new FormData();
  form.append("file", file);
  try {
    const data = await api("/api/jobs", { method: "POST", body: form });
    setUploadStatus(`Started job #${data.job.id}: ${data.parsed.unique_usernames} usernames`);
    els.fileInput.value = "";
    await loadMe();
    await loadJobs();
    await selectJob(data.job.id);
  } catch (err) {
    setUploadStatus(err.message);
  }
});

els.refreshBtn.addEventListener("click", async () => {
  await loadMe();
  await loadJobs();
  if (state.selectedJobId) await loadItems();
});

els.onlyFound.addEventListener("change", async () => {
  state.onlyFound = els.onlyFound.checked;
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
    els.itemsBody.innerHTML = `<tr><td colspan="6" class="empty">${esc(err.message)}</td></tr>`;
  }
})();
