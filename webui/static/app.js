async function apiGet(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`GET ${url} failed: ${res.status}`);
  return await res.json();
}

async function apiPost(url, payload) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload ?? {}),
  });
  if (!res.ok) throw new Error(`POST ${url} failed: ${res.status}`);
  return await res.json();
}

function escapeHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function fmtDate(s) {
  if (!s) return "-";
  return String(s).replace("T", " ").replace("+00:00", "Z");
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value ?? "";
}

function updateSecretPlaceholder(inputEl, hasSecret) {
  if (!inputEl) return;
  if (inputEl.dataset.placeholderOriginal === undefined) {
    inputEl.dataset.placeholderOriginal = inputEl.placeholder || "";
  }
  inputEl.placeholder = hasSecret ? "已保存（不回显）" : inputEl.dataset.placeholderOriginal;
}

let polling = null;
let currentJobId = null;

async function startJob(mode, data) {
  const statusSpan = document.getElementById("job-status");
  const logBox = document.getElementById("log-box");
  const stopBtn = document.getElementById("btn-stop");

  const resp = await apiPost("/api/run", { mode, ...(data ?? {}) });
  if (!resp.ok) {
    statusSpan.textContent = resp.message || "启动失败";
    return;
  }
  currentJobId = resp.job_id;
  statusSpan.textContent = "running";
  stopBtn.disabled = false;
  if (logBox) logBox.textContent = "";

  if (polling) clearInterval(polling);
  let statsTick = 0;
  polling = setInterval(async () => {
    try {
      const job = await apiGet(`/api/job/${currentJobId}`);
      if (!job.ok) return;
      statusSpan.textContent = job.status;
      if (logBox) {
        logBox.textContent = (job.logs || []).join("\n");
        logBox.scrollTop = logBox.scrollHeight;
      }
      // Keep dashboard counters fresh during long-running jobs.
      statsTick += 1;
      if (statsTick % 3 === 0) {
        loadStats().catch(() => {});
      }
      if (!String(job.status).startsWith("running") && !String(job.status).startsWith("pending")) {
        clearInterval(polling);
        polling = null;
        stopBtn.disabled = true;
      }
    } catch {
      // ignore polling errors
    }
  }, 1200);
}

async function stopJob() {
  const statusSpan = document.getElementById("job-status");
  const stopBtn = document.getElementById("btn-stop");
  if (!currentJobId) return;
  const resp = await apiPost(`/api/job/${currentJobId}/stop`, {});
  statusSpan.textContent = resp.message || (resp.ok ? "已停止" : "停止失败");
  stopBtn.disabled = true;
}

async function loadStats() {
  const data = await apiGet("/api/follow/stats");
  if (!data.ok) return;
  setText("stat-members", data.stats?.member_count ?? "-");
  setText("stat-images", data.stats?.image_count ?? "-");
  setText("stat-urls", data.stats?.url_count ?? "-");
}

async function loadConfigForm() {
  const data = await apiGet("/api/config");
  if (!data.ok) return;
  const cfg = data.config || {};
  const form = document.getElementById("config-form");
  if (!form) return;
  for (const [key, value] of Object.entries(cfg)) {
    const el = form.querySelector(`[name="${key}"]`);
    if (!el) continue;
    if (el.type === "checkbox") el.checked = Boolean(value);
    else el.value = value ?? "";
  }
  updateSecretPlaceholder(form.querySelector('[name="password"]'), Boolean(cfg.hasPassword));
  updateSecretPlaceholder(form.querySelector('[name="cookie"]'), Boolean(cfg.hasCookie));
  updateSecretPlaceholder(form.querySelector('[name="refresh_token"]'), Boolean(cfg.hasRefreshToken));
  setText("config-status", "已加载");
}

async function saveConfigForm(ev) {
  ev.preventDefault();
  const form = ev.target;
  const fd = new FormData(form);
  const payload = Object.fromEntries(fd.entries());
  payload.useMirrorHost = fd.get("useMirrorHost") ? true : false;
  payload.mirrorMultithread = fd.get("mirrorMultithread") ? true : false;

  const numericKeys = ["downloadDelay"];
  for (const k of numericKeys) {
    if (payload[k] === "") delete payload[k];
  }

  const data = await apiPost("/api/config", payload);
  setText("config-status", data.ok ? "已保存" : "保存失败");
  if (data.ok) {
    await loadConfigForm();
  }
}

async function initDashboard() {
  await loadConfigForm();
  await loadStats();

  const btnSync = document.getElementById("btn-sync");
  const stopBtn = document.getElementById("btn-stop");
  const cfgForm = document.getElementById("config-form");

  if (cfgForm) cfgForm.addEventListener("submit", saveConfigForm);
  if (stopBtn) stopBtn.addEventListener("click", stopJob);
  if (btnSync) {
    btnSync.addEventListener("click", async () => {
      const flag = document.getElementById("sync-bookmark-flag")?.value ?? "n";
      const preview = document.getElementById("sync-preview")?.value ?? "3";
      await startJob("follow_index", { bookmark_flag: flag, preview_per_artist: preview });
      // Refresh stats in a bit (best-effort)
      setTimeout(() => loadStats().catch(() => {}), 2000);
    });
  }
}

async function initArtists() {
  const tbody = document.querySelector("#artist-table tbody");
  const queryEl = document.getElementById("artist-query");
  const infoEl = document.getElementById("artist-pagination-info");
  const prevBtn = document.getElementById("artist-prev");
  const nextBtn = document.getElementById("artist-next");

  let page = 1;
  const pageSize = 30;
  let query = "";
  let total = 0;

  async function load() {
    tbody.innerHTML = `<tr><td colspan="8" class="muted">加载中...</td></tr>`;
    const data = await apiGet(`/api/follow/members?page=${page}&page_size=${pageSize}&q=${encodeURIComponent(query)}`);
    total = data.total ?? 0;
    const items = data.items ?? [];

    if (!items.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="muted">暂无数据</td></tr>`;
    } else {
      tbody.innerHTML = items
        .map((it) => {
          const avatar = it.avatar_url ? `<img class="avatar" src="${escapeHtml(it.avatar_url)}" />` : `<div class="avatar"></div>`;
          return `<tr>
            <td>${avatar}</td>
            <td>${escapeHtml(it.member_id)}</td>
            <td>${escapeHtml(it.name || "-")}</td>
            <td class="muted">${escapeHtml(it.member_token || "-")}</td>
            <td>${escapeHtml(it.image_count ?? 0)}</td>
            <td>${escapeHtml(it.url_count ?? 0)}</td>
            <td class="muted">${escapeHtml(fmtDate(it.last_sync_date))}</td>
            <td><a class="btn" href="/artist/${encodeURIComponent(it.member_id)}">查看</a></td>
          </tr>`;
        })
        .join("");
    }

    const start = (page - 1) * pageSize + 1;
    const end = Math.min(page * pageSize, total);
    infoEl.textContent = total ? `${start}-${end} / ${total}` : "0 / 0";
    prevBtn.disabled = page <= 1;
    nextBtn.disabled = page * pageSize >= total;
  }

  document.getElementById("artist-search")?.addEventListener("click", async () => {
    query = queryEl.value?.trim() ?? "";
    page = 1;
    await load();
  });
  queryEl?.addEventListener("keydown", async (ev) => {
    if (ev.key === "Enter") {
      query = queryEl.value?.trim() ?? "";
      page = 1;
      await load();
    }
  });
  prevBtn?.addEventListener("click", async () => {
    if (page > 1) page -= 1;
    await load();
  });
  nextBtn?.addEventListener("click", async () => {
    page += 1;
    await load();
  });

  await load();
}

async function initArtist() {
  const memberId = window.__ARTIST_ID__;
  const previewsEl = document.getElementById("artist-previews");
  const worksBody = document.querySelector("#works-table tbody");
  const worksMeta = document.getElementById("works-meta");
  const worksInfo = document.getElementById("works-pagination-info");
  const prevBtn = document.getElementById("works-prev");
  const nextBtn = document.getElementById("works-next");

  const modal = document.getElementById("urls-modal");
  const urlsBox = document.getElementById("urls-box");
  const modalTitle = document.getElementById("modal-title");
  const modalSubtitle = document.getElementById("modal-subtitle");
  const modalClose = document.getElementById("modal-close");
  const copyBtn = document.getElementById("copy-urls");

  let urlsToCopy = "";

  let page = 1;
  const pageSize = 20;
  let total = 0;

  const data = await apiGet(`/api/follow/member/${memberId}`);
  if (!data.ok) return;
  const m = data.member;
  document.getElementById("artist-name").textContent = m.name || `画师 ${m.member_id}`;
  document.getElementById("artist-meta").textContent = `ID: ${m.member_id}  |  Token: ${m.member_token || "-"}  |  作品: ${m.image_count}  |  URL: ${m.url_count}`;

  const pixivUrl = `https://www.pixiv.net/users/${encodeURIComponent(m.member_id)}`;
  document.getElementById("btn-open-pixiv").href = pixivUrl;
  document.getElementById("btn-export-regular").href = `/api/follow/member/${encodeURIComponent(m.member_id)}/export?kind=regular`;
  document.getElementById("btn-export-original").href = `/api/follow/member/${encodeURIComponent(m.member_id)}/export?kind=original`;

  const previews = m.previews || [];
  if (!previews.length) {
    previewsEl.innerHTML = `<div class="muted">暂无预览图（同步时会自动下载）</div>`;
  } else {
    previewsEl.innerHTML = previews
      .slice(0, 6)
      .map((p) => `<a href="${escapeHtml(p.url)}" target="_blank" rel="noreferrer"><img class="thumb" src="${escapeHtml(p.url)}" /></a>`)
      .join("");
  }

  async function loadWorks() {
    worksBody.innerHTML = `<tr><td colspan="7" class="muted">加载中...</td></tr>`;
    const d = await apiGet(`/api/follow/member/${memberId}/works?page=${page}&page_size=${pageSize}`);
    total = d.total ?? 0;
    const items = d.items ?? [];
    worksMeta.textContent = `共 ${total} 个作品`;

    if (!items.length) {
      worksBody.innerHTML = `<tr><td colspan="7" class="muted">暂无数据</td></tr>`;
    } else {
      worksBody.innerHTML = items
        .map((it) => {
          const thumb = it.thumb_url ? `<a href="${escapeHtml(it.thumb_url)}" target="_blank" rel="noreferrer"><img class="thumb" src="${escapeHtml(it.thumb_url)}" /></a>` : `<div class="thumb"></div>`;
          const title = it.title ? escapeHtml(it.title) : "-";
          return `<tr>
            <td>${thumb}</td>
            <td>${escapeHtml(it.image_id)}</td>
            <td>${title}</td>
            <td>${escapeHtml(it.page_count ?? 1)}</td>
            <td class="muted">${escapeHtml(fmtDate(it.create_date))}</td>
            <td>${escapeHtml(it.bookmark_count ?? "-")}</td>
            <td><button class="btn" data-image-id="${escapeHtml(it.image_id)}">URL</button></td>
          </tr>`;
        })
        .join("");
    }

    worksBody.querySelectorAll("button[data-image-id]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const imageId = btn.dataset.imageId;
        modalTitle.textContent = `作品 ${imageId}`;
        modalSubtitle.textContent = "加载中...";
        urlsBox.textContent = "(加载中...)";
        urlsToCopy = "";
        modal.showModal();

        const u = await apiGet(`/api/follow/image/${imageId}/urls`);
        if (!u.ok) {
          urlsBox.textContent = "加载失败";
          return;
        }
        const rows = u.urls || [];
        const lines = rows.map((r) => r.proxy_regular_url || r.proxy_original_url || r.regular_url || r.original_url).filter(Boolean);
        urlsToCopy = lines.join("\n");
        modalSubtitle.textContent = `共 ${rows.length} 条 URL`;
        urlsBox.textContent = urlsToCopy || "(空)";
      });
    });

    const start = (page - 1) * pageSize + 1;
    const end = Math.min(page * pageSize, total);
    worksInfo.textContent = total ? `${start}-${end} / ${total}` : "0 / 0";
    prevBtn.disabled = page <= 1;
    nextBtn.disabled = page * pageSize >= total;
  }

  prevBtn.addEventListener("click", async () => {
    if (page > 1) page -= 1;
    await loadWorks();
  });
  nextBtn.addEventListener("click", async () => {
    page += 1;
    await loadWorks();
  });

  modalClose.addEventListener("click", () => modal.close());
  copyBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(urlsToCopy || "");
      copyBtn.textContent = "已复制";
      setTimeout(() => (copyBtn.textContent = "复制（镜像/反代优先）"), 1200);
    } catch {
      // fallback
    }
  });

  await loadWorks();
}

window.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;
  if (page === "dashboard") initDashboard().catch(() => {});
  if (page === "artists") initArtists().catch(() => {});
  if (page === "artist") initArtist().catch(() => {});
});
