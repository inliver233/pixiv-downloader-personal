async function apiGet(url) {
  const res = await fetch(url);
  const ct = res.headers.get("content-type") || "";
  let data = null;
  if (ct.includes("application/json")) {
    try {
      data = await res.json();
    } catch {
      data = null;
    }
  }
  if (!res.ok) {
    if (data && typeof data === "object") return data;
    throw new Error(`GET ${url} failed: ${res.status}`);
  }
  if (data && typeof data === "object") return data;
  throw new Error(`GET ${url} invalid JSON`);
}

async function apiPost(url, payload) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload ?? {}),
  });
  const ct = res.headers.get("content-type") || "";
  let data = null;
  if (ct.includes("application/json")) {
    try {
      data = await res.json();
    } catch {
      data = null;
    }
  }
  if (!res.ok) {
    if (data && typeof data === "object") return data;
    throw new Error(`POST ${url} failed: ${res.status}`);
  }
  if (data && typeof data === "object") return data;
  throw new Error(`POST ${url} invalid JSON`);
}

async function apiDelete(url) {
  const res = await fetch(url, { method: "DELETE" });
  const ct = res.headers.get("content-type") || "";
  let data = null;
  if (ct.includes("application/json")) {
    try {
      data = await res.json();
    } catch {
      data = null;
    }
  }
  if (!res.ok) {
    if (data && typeof data === "object") return data;
    throw new Error(`DELETE ${url} failed: ${res.status}`);
  }
  if (data && typeof data === "object") return data;
  throw new Error(`DELETE ${url} invalid JSON`);
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
  updateSecretPlaceholder(form.querySelector('[name="proxyAddress"]'), Boolean(cfg.hasProxyAddress));
  setText("config-status", "已加载");
}

async function saveConfigForm(ev) {
  ev.preventDefault();
  const form = ev.target;
  const fd = new FormData(form);
  const payload = Object.fromEntries(fd.entries());
  payload.useMirrorHost = fd.get("useMirrorHost") ? true : false;
  payload.mirrorMultithread = fd.get("mirrorMultithread") ? true : false;
  payload.useProxy = fd.get("useProxy") ? true : false;
  payload.clearProxyAddress = fd.get("clearProxyAddress") ? true : false;
  payload.clearCookie = fd.get("clearCookie") ? true : false;
  payload.clearRefreshToken = fd.get("clearRefreshToken") ? true : false;
  payload.clearPassword = fd.get("clearPassword") ? true : false;

  const numericKeys = ["downloadDelay"];
  for (const k of numericKeys) {
    if (payload[k] === "") delete payload[k];
  }

  const data = await apiPost("/api/config", payload);
  setText("config-status", data.ok ? "已保存" : "保存失败");
  if (data.ok) {
    const clearCookie = form.querySelector('[name="clearCookie"]');
    const clearRt = form.querySelector('[name="clearRefreshToken"]');
    const clearPw = form.querySelector('[name="clearPassword"]');
    const clearProxy = form.querySelector('[name="clearProxyAddress"]');
    if (clearCookie) clearCookie.checked = false;
    if (clearRt) clearRt.checked = false;
    if (clearPw) clearPw.checked = false;
    if (clearProxy) clearProxy.checked = false;
    await loadConfigForm();
  }
}

async function testProxyConnectivity() {
  const statusEl = document.getElementById("proxy-test-status");
  if (statusEl) statusEl.textContent = "testing...";

  const form = document.getElementById("config-form");
  const proxyEl = form ? form.querySelector('[name="proxyAddress"]') : null;
  const proxyAddress = proxyEl?.value?.trim() ?? "";

  const payload = {};
  if (proxyAddress) payload.proxyAddress = proxyAddress;

  try {
    const resp = await apiPost("/api/proxy/test", payload);
    if (!resp.ok) {
      if (statusEl) statusEl.textContent = resp.message || "测试失败";
      return;
    }
    const r = resp.result || {};
    const ok = Number(r.ok_count ?? 0);
    const fail = Number(r.fail_count ?? 0);
    const total = ok + fail;
    const ms = r.avg_ms != null ? `${Number(r.avg_ms).toFixed(1)} ms` : "-";
    const p = r.proxy || "-";
    if (statusEl) statusEl.textContent = `${ok}/${total}  ${ms}  ${p}`;
  } catch (e) {
    if (statusEl) statusEl.textContent = String(e?.message || e || "测试失败");
  }
}

async function initDashboard() {
  await loadConfigForm();
  await loadStats();

  const btnSync = document.getElementById("btn-sync");
  const stopBtn = document.getElementById("btn-stop");
  const cfgForm = document.getElementById("config-form");
  const proxyTestBtn = document.getElementById("btn-proxy-test");

  if (cfgForm) cfgForm.addEventListener("submit", saveConfigForm);
  if (stopBtn) stopBtn.addEventListener("click", stopJob);
  if (proxyTestBtn) proxyTestBtn.addEventListener("click", () => testProxyConnectivity().catch(() => {}));
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

let multiPolling = null;
let multiJobId = null;

function setDisabled(id, disabled) {
  const el = document.getElementById(id);
  if (el) el.disabled = Boolean(disabled);
}

function setValue(id, value) {
  const el = document.getElementById(id);
  if (el) el.value = value ?? "";
}

function joinLines(arr) {
  return (arr ?? []).filter(Boolean).join("\n");
}

function renderMultiAccounts(cfg, status) {
  const body = document.getElementById("multi-accounts-body");
  if (!body) return;
  const accounts = cfg?.accounts ?? [];
  const stAccounts = new Map();
  for (const a of status?.accounts ?? []) stAccounts.set(a.id, a);

  if (!accounts.length) {
    body.innerHTML = `<tr><td colspan="11" class="muted">暂无账号，请先新增</td></tr>`;
    return;
  }

  body.innerHTML = accounts
    .map((a) => {
      const sa = stAccounts.get(a.id) || {};
      const proxy = sa.proxy || "-";
      const worker = sa.worker_pid ? `${sa.worker_pid}${sa.worker_running === false ? " (stopped)" : ""}` : "-";
      const assigned = sa.assigned_artists ?? "-";
      const processed = sa.processed_artists ?? "-";
      return `<tr data-account-id="${escapeHtml(a.id)}" style="cursor:pointer;">
        <td>${escapeHtml(a.id)}</td>
        <td class="muted">${a.enabled ? "yes" : "no"}</td>
        <td class="muted">${a.follow_source ? "yes" : "no"}</td>
        <td>${escapeHtml(a.cookieUserId || "-")}</td>
        <td class="muted">${a.hasCookie ? "yes" : "no"}</td>
        <td class="muted">${a.hasRefreshToken ? "yes" : "no"}</td>
        <td>${escapeHtml(a.downloadDelay ?? "-")}</td>
        <td class="muted">${escapeHtml(proxy)}</td>
        <td class="muted">${escapeHtml(worker)}</td>
        <td>${escapeHtml(assigned)}</td>
        <td>${escapeHtml(processed)}</td>
      </tr>`;
    })
    .join("");

  body.querySelectorAll("tr[data-account-id]").forEach((tr) => {
    tr.addEventListener("click", () => {
      const id = tr.dataset.accountId;
      if (!id) return;
      setValue("multi-acc-id", id);
      const hit = accounts.find((x) => x.id === id);
      if (hit) {
        const enEl = document.getElementById("multi-acc-enabled");
        const fsEl = document.getElementById("multi-acc-follow-source");
        if (enEl) enEl.checked = Boolean(hit.enabled);
        if (fsEl) fsEl.checked = Boolean(hit.follow_source);
        setValue("multi-acc-delay", hit.downloadDelay ?? "");
      }
      setText("multi-acc-status", `已选择 ${id}`);
    });
  });
}

function renderMultiProxyTop(status) {
  const box = document.getElementById("multi-proxy-top");
  if (!box) return;
  const rows = status?.proxy_ranked ?? [];
  if (!rows.length) {
    box.textContent = "(暂无)";
    return;
  }
  const attempts = Number(status?.proxy_pool?.test_attempts ?? 2);
  box.textContent = rows
    .map((r) => {
      const ms = r.ms != null ? `${Number(r.ms).toFixed(1)} ms` : "-";
      if (r.ok != null) return `${Number(r.ok)}/${attempts}  ${ms}  ${r.proxy}`;
      return `${ms}  ${r.proxy}`;
    })
    .join("\n");
}

function fmtEpoch(ts) {
  if (!ts) return "-";
  const ms = Number(ts) * 1000;
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleString();
}

function renderMultiSummary(status, db) {
  const updatedAt = status?.updated_at ?? 0;
  const followUnique = status?.follow?.unique_artists ?? "-";
  const assigned = status?.work?.assigned_artists_total ?? "-";
  const processed = status?.work?.processed_artists_total ?? "-";
  const reporting = status?.work?.workers_reporting ?? 0;

  const dbStats = db?.stats ?? null;
  const memberCount = dbStats?.member_count ?? "-";
  const imageCount = dbStats?.image_count ?? "-";
  const urlCount = dbStats?.url_count ?? "-";
  let progressDone = memberCount !== "-" ? memberCount : processed;
  const progressTotal = followUnique !== "-" ? followUnique : assigned;
  const doneNum = Number(progressDone);
  const totalNum = Number(progressTotal);
  if (!Number.isNaN(doneNum) && !Number.isNaN(totalNum) && totalNum > 0 && doneNum > totalNum) {
    progressDone = String(totalNum);
  }

  setText("multi-status-updated", `status: ${fmtEpoch(updatedAt)}`);
  setText("multi-follow-unique", `关注画师: ${followUnique}`);
  setText("multi-work-progress", `进度: ${progressDone}/${progressTotal} (reporting=${reporting})`);
  setText("multi-db-stats", `DB: members=${memberCount} images=${imageCount} urls=${urlCount}`);
  setText("multi-db-path", `db_path: ${db?.db_path || status?.db?.path || "-"}`);
}

async function loadMultiIntoForm(cfg) {
  const proxy = cfg?.proxy_pool ?? {};
  const easy = proxy?.easy_proxies ?? {};
  const test = proxy?.test ?? {};
  const worker = cfg?.worker ?? {};

  setValue("multi-proxy-source", proxy.source ?? "easy_proxies");
  setValue("multi-proxy-refresh", proxy.refresh_interval_sec ?? 300);
  setValue("multi-proxy-max-per", proxy.max_tokens_per_proxy ?? 2);
  setValue("multi-worker-mode", worker.mode ?? "index_urls");
  setValue("multi-easy-base-url", easy.base_url ?? "http://127.0.0.1:9090");
  setValue("multi-easy-host-override", easy.host_override ?? "");
  setValue("multi-proxy-test-url", test.target_url ?? "https://www.pixiv.net/robots.txt");
  setValue("multi-proxy-concurrency", test.concurrency ?? 20);
  setValue("multi-proxy-attempts", test.attempts ?? 2);
  setValue("multi-proxy-topn", test.top_n ?? 20);
  setValue("multi-proxy-manual", joinLines(proxy.proxies ?? []));
  const strictEl = document.getElementById("multi-proxy-strict");
  if (strictEl) strictEl.checked = Boolean(proxy.bindings_strict);

  const pwEl = document.getElementById("multi-easy-password");
  updateSecretPlaceholder(pwEl, Boolean(easy.hasPassword));
}

async function refreshMultiView() {
  const cfgResp = await apiGet("/api/multi/config");
  const cfg = cfgResp.config || {};

  let status = null;
  try {
    const st = await apiGet("/api/multi/status");
    if (st.ok) status = st.status;
  } catch {
    // ignore if runner not started yet
  }

  let db = null;
  try {
    const resp = await apiGet("/api/multi/db/stats");
    if (resp.ok) db = resp;
  } catch {
    // ignore
  }

  await loadMultiIntoForm(cfg);
  renderMultiAccounts(cfg, status);
  renderMultiProxyTop(status);
  renderMultiSummary(status, db);
  const warn = status?.proxy_pool?.warning || "";
  const exportErr = status?.proxy_pool?.export_error || "";
  setText("multi-proxy-warn", exportErr ? `export: ${exportErr}` : warn || "-");

  const runner = await apiGet("/api/multi/runner");
  const running = Boolean(runner.running);
  multiJobId = runner.job_id || multiJobId;
  setDisabled("btn-multi-stop", !running);
  setText("multi-runner-state", running ? (runner.status || "running") : "stopped");

  const logBox = document.getElementById("multi-log");
  if (running && multiJobId) {
    try {
      const job = await apiGet(`/api/job/${multiJobId}`);
      if (job.ok && logBox) {
        logBox.textContent = (job.logs || []).join("\n") || "(暂无)";
        logBox.scrollTop = logBox.scrollHeight;
      }
    } catch {
      // ignore
    }
  }
}

async function bulkSetMultiDelay(clear) {
  const statusEl = document.getElementById("multi-bulk-status");
  if (statusEl) statusEl.textContent = "saving...";

  const delayRaw = document.getElementById("multi-bulk-delay")?.value ?? "";
  const payload = {};
  if (clear) {
    payload.clearDownloadDelay = true;
  } else if (delayRaw !== "") {
    payload.downloadDelay = delayRaw;
  } else {
    if (statusEl) statusEl.textContent = "请先填写 delay 或点击清空";
    return;
  }

  try {
    const resp = await apiPost("/api/multi/accounts/bulk", payload);
    if (!resp.ok) {
      if (statusEl) statusEl.textContent = resp.message || "保存失败";
      return;
    }
    if (statusEl) statusEl.textContent = "已保存";
    await refreshMultiView();
  } catch (e) {
    if (statusEl) statusEl.textContent = String(e?.message || e || "保存失败");
  }
}

async function startMultiRunner() {
  const resp = await apiPost("/api/multi/runner/start", {});
  if (resp.ok) {
    multiJobId = resp.job_id;
  }
  await refreshMultiView();
}

async function stopMultiRunner() {
  try {
    await apiPost("/api/multi/runner/stop", {});
  } catch {
    // ignore
  }
  await refreshMultiView();
}

async function multiRefreshNow() {
  try {
    await apiPost("/api/multi/refresh", {});
    setText("multi-runner-state", "refreshing");
  } catch {
    // ignore
  }
}

async function testMultiProxyPool() {
  setText("multi-proxy-test-status", "testing...");
  try {
    const resp = await apiPost("/api/multi/proxy/test", {});
    if (!resp.ok) {
      setText("multi-proxy-test-status", resp.message || "测试失败");
      return;
    }

    const warn = resp.warning || "";
    const exportErr = resp.export_error || "";
    const summary = exportErr
      ? `export 失败：${exportErr}`
      : warn
        ? warn
        : `已测速：${resp.ranked_count ?? 0}/${resp.input_count ?? 0}`;
    setText("multi-proxy-test-status", summary);

    const box = document.getElementById("multi-proxy-top");
    if (box) {
      const rows = resp.ranked ?? [];
      if (!rows.length) {
        box.textContent = "(暂无)";
      } else {
        const attempts = Number(resp.test?.attempts ?? 2);
        box.textContent = rows
          .map((r) => {
            const ok = Number(r.ok_count ?? 0);
            const ms = r.avg_ms != null ? `${Number(r.avg_ms).toFixed(1)} ms` : "-";
            return `${ok}/${attempts}  ${ms}  ${r.proxy}`;
          })
          .join("\n");
      }
    }
  } catch (e) {
    setText("multi-proxy-test-status", String(e?.message || e || "测试失败"));
  }
}

async function saveMultiAccount() {
  const id = document.getElementById("multi-acc-id")?.value?.trim();
  const cookie = document.getElementById("multi-acc-cookie")?.value ?? "";
  const rt = document.getElementById("multi-acc-rt")?.value ?? "";
  const delayRaw = document.getElementById("multi-acc-delay")?.value ?? "";
  const clearCookie = document.getElementById("multi-acc-clear-cookie")?.checked ?? false;
  const clearRt = document.getElementById("multi-acc-clear-rt")?.checked ?? false;
  const enabled = document.getElementById("multi-acc-enabled")?.checked ?? true;
  const followSource = document.getElementById("multi-acc-follow-source")?.checked ?? true;

  if (!id) {
    setText("multi-acc-status", "请先填写账号 ID");
    return;
  }

  const payload = { id };
  if (cookie !== "") payload.cookie = cookie;
  if (rt !== "") payload.refresh_token = rt;
  if (clearCookie) payload.clearCookie = true;
  if (clearRt) payload.clearRefreshToken = true;
  if (delayRaw !== "") payload.downloadDelay = delayRaw;
  payload.enabled = Boolean(enabled);
  payload.follow_source = Boolean(followSource);

  const resp = await apiPost("/api/multi/account", payload);
  setText("multi-acc-status", resp.ok ? "已保存" : resp.message || "保存失败");
  // Clear secrets to avoid accidental resubmits.
  setValue("multi-acc-cookie", "");
  setValue("multi-acc-rt", "");
  const c1 = document.getElementById("multi-acc-clear-cookie");
  const c2 = document.getElementById("multi-acc-clear-rt");
  if (c1) c1.checked = false;
  if (c2) c2.checked = false;
  await refreshMultiView();
}

async function deleteMultiAccount() {
  const id = document.getElementById("multi-acc-id")?.value?.trim();
  if (!id) return;
  const resp = await apiDelete(`/api/multi/account/${encodeURIComponent(id)}`);
  setText("multi-acc-status", resp.ok ? "已删除" : resp.message || "删除失败");
  await refreshMultiView();
}

async function saveMultiProxy(ev) {
  ev.preventDefault();
  const source = document.getElementById("multi-proxy-source")?.value ?? "easy_proxies";
  const refresh = document.getElementById("multi-proxy-refresh")?.value ?? "";
  const maxPer = document.getElementById("multi-proxy-max-per")?.value ?? "";
  const strict = document.getElementById("multi-proxy-strict")?.checked ?? false;
  const workerMode = document.getElementById("multi-worker-mode")?.value ?? "index_urls";
  const baseUrl = document.getElementById("multi-easy-base-url")?.value ?? "";
  const hostOverride = document.getElementById("multi-easy-host-override")?.value ?? "";
  const password = document.getElementById("multi-easy-password")?.value ?? "";
  const testUrl = document.getElementById("multi-proxy-test-url")?.value ?? "";
  const concurrency = document.getElementById("multi-proxy-concurrency")?.value ?? "";
  const attempts = document.getElementById("multi-proxy-attempts")?.value ?? "";
  const topn = document.getElementById("multi-proxy-topn")?.value ?? "";
  const manual = document.getElementById("multi-proxy-manual")?.value ?? "";

  const payload = {
    proxy_pool: {
      source,
      refresh_interval_sec: refresh,
      max_tokens_per_proxy: maxPer,
      bindings_strict: Boolean(strict),
      easy_proxies: { base_url: baseUrl, host_override: hostOverride, password },
      test: { target_url: testUrl, concurrency, attempts, top_n: topn },
      proxies: manual,
    },
    worker: { mode: workerMode },
  };

  const resp = await apiPost("/api/multi/config", payload);
  setText("multi-proxy-status", resp.ok ? "已保存" : resp.message || "保存失败");
  setValue("multi-easy-password", "");
  await refreshMultiView();
}

async function initMulti() {
  await refreshMultiView();

  const startBtn = document.getElementById("btn-multi-start");
  const stopBtn = document.getElementById("btn-multi-stop");
  const refreshBtn = document.getElementById("btn-multi-refresh");
  const accSaveBtn = document.getElementById("btn-multi-acc-save");
  const accDelBtn = document.getElementById("btn-multi-acc-delete");
  const bulkBtn = document.getElementById("btn-multi-bulk-delay");
  const bulkClearBtn = document.getElementById("btn-multi-bulk-delay-clear");
  const proxyForm = document.getElementById("multi-proxy-form");
  const proxyTestBtn = document.getElementById("btn-multi-proxy-test");

  if (startBtn) startBtn.addEventListener("click", () => startMultiRunner().catch(() => {}));
  if (stopBtn) stopBtn.addEventListener("click", () => stopMultiRunner().catch(() => {}));
  if (refreshBtn) refreshBtn.addEventListener("click", () => multiRefreshNow().catch(() => {}));
  if (accSaveBtn) accSaveBtn.addEventListener("click", () => saveMultiAccount().catch(() => {}));
  if (accDelBtn) accDelBtn.addEventListener("click", () => deleteMultiAccount().catch(() => {}));
  if (bulkBtn) bulkBtn.addEventListener("click", () => bulkSetMultiDelay(false).catch(() => {}));
  if (bulkClearBtn) bulkClearBtn.addEventListener("click", () => bulkSetMultiDelay(true).catch(() => {}));
  if (proxyForm) proxyForm.addEventListener("submit", (ev) => saveMultiProxy(ev).catch(() => {}));
  if (proxyTestBtn) proxyTestBtn.addEventListener("click", () => testMultiProxyPool().catch(() => {}));

  if (multiPolling) clearInterval(multiPolling);
  multiPolling = setInterval(() => refreshMultiView().catch(() => {}), 1500);
}

window.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;
  if (page === "dashboard") initDashboard().catch(() => {});
  if (page === "multi") initMulti().catch(() => {});
  if (page === "artists") initArtists().catch(() => {});
  if (page === "artist") initArtist().catch(() => {});
});
