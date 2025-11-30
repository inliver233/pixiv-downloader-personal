#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
简易中文可视化交互层，用浏览器操作 PixivUtil2。
 - 修改登录/下载配置（cookie、账号、下载目录等）
 - 一键启动常用下载任务（关注画师全量/增量）
 - 查看实时日志，避免控制台乱码
运行：python web_ui.py
然后在浏览器打开 http://127.0.0.1:5000
"""

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Any, Dict
from uuid import uuid4

from flask import Flask, jsonify, render_template_string, request

from common import PixivConfig

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(ROOT_DIR, "config.ini")
LOG_LIMIT = 1200

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _load_config() -> PixivConfig.PixivConfig:
    cfg = PixivConfig.PixivConfig()
    cfg.loadConfig(CONFIG_PATH)
    return cfg


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}***{value[-3:]}"


def _serialize_config(cfg: PixivConfig.PixivConfig) -> Dict[str, Any]:
    return {
        "rootDirectory": cfg.rootDirectory,
        "downloadListDirectory": cfg.downloadListDirectory,
        "username": cfg.username,
        "password": _mask_secret(cfg.password),
        "cookie": _mask_secret(cfg.cookie),
        "cookieFanbox": _mask_secret(cfg.cookieFanbox),
        "refresh_token": _mask_secret(cfg.refresh_token),
        "cf_clearance": _mask_secret(cfg.cf_clearance),
        "cf_bm": _mask_secret(cfg.cf_bm),
        "numberOfPage": cfg.numberOfPage,
        "dayLastUpdated": cfg.dayLastUpdated,
        "overwrite": bool(cfg.overwrite),
        "useMirrorHost": bool(getattr(cfg, "useMirrorHost", False)),
        "mirrorHost": getattr(cfg, "mirrorHost", ""),
        "mirrorMultithread": bool(getattr(cfg, "mirrorMultithread", False)),
        "mirrorThread": getattr(cfg, "mirrorThread", 4),
        "mirrorChunkSizeKB": getattr(cfg, "mirrorChunkSizeKB", 1024),
        "downloadDelay": getattr(cfg, "downloadDelay", 5),
    }


def _parse_bool(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _update_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _load_config()
    cfg.rootDirectory = payload.get("rootDirectory", cfg.rootDirectory).strip() or cfg.rootDirectory
    cfg.downloadListDirectory = payload.get("downloadListDirectory", cfg.downloadListDirectory).strip() or cfg.downloadListDirectory
    cfg.username = payload.get("username", cfg.username)
    cfg.password = payload.get("password", cfg.password)
    cfg.cookie = payload.get("cookie", cfg.cookie)
    cfg.cookieFanbox = payload.get("cookieFanbox", cfg.cookieFanbox)
    cfg.refresh_token = payload.get("refresh_token", cfg.refresh_token)
    cfg.cf_clearance = payload.get("cf_clearance", cfg.cf_clearance)
    cfg.cf_bm = payload.get("cf_bm", cfg.cf_bm)
    cfg.numberOfPage = _parse_int(payload.get("numberOfPage"), cfg.numberOfPage)
    cfg.dayLastUpdated = _parse_int(payload.get("dayLastUpdated"), cfg.dayLastUpdated)
    cfg.overwrite = _parse_bool(payload.get("overwrite"))
    cfg.useMirrorHost = _parse_bool(payload.get("useMirrorHost"))
    cfg.mirrorHost = payload.get("mirrorHost", cfg.mirrorHost).strip() or cfg.mirrorHost
    cfg.mirrorMultithread = _parse_bool(payload.get("mirrorMultithread"))
    cfg.mirrorThread = max(1, _parse_int(payload.get("mirrorThread"), cfg.mirrorThread))
    cfg.mirrorChunkSizeKB = max(64, _parse_int(payload.get("mirrorChunkSizeKB"), cfg.mirrorChunkSizeKB))
    cfg.downloadDelay = max(0, _parse_int(payload.get("downloadDelay"), cfg.downloadDelay))
    cfg.writeConfig(path=CONFIG_PATH)
    return _serialize_config(cfg)


def _build_command(mode: str, params: Dict[str, Any]) -> Any:
    cmd = [sys.executable, os.path.join(ROOT_DIR, "PixivUtil2.py"), "-x"]
    pages = params.get("pages")
    if pages not in (None, "", "0", 0):
        cmd.extend(["-n", str(pages)])
    bookmark_flag = params.get("bookmark_flag", "y")
    bookmark_count = params.get("bookmark_count")
    start_page = params.get("start_page")
    end_page = params.get("end_page")
    if start_page not in (None, "", "0", 0):
        cmd.extend(["--start_page", str(start_page)])
    if end_page not in (None, "", "0", 0):
        cmd.extend(["--end_page", str(end_page)])

    if mode == "follow_all":
        cmd.extend(["-s", "5", "-p", bookmark_flag])
        if bookmark_count not in (None, "", -1):
            cmd.extend(["--bookmark_count_limit", str(bookmark_count)])
    elif mode == "incremental":
        cmd.extend(["-s", "8", "-p", bookmark_flag])
        if bookmark_count not in (None, "", -1):
            cmd.extend(["--bookmark_count_limit", str(bookmark_count)])
    elif mode == "image_bookmark":
        cmd.extend(["-s", "6", "-p", bookmark_flag])
        if bookmark_count not in (None, "", -1):
            cmd.extend(["--bookmark_count_limit", str(bookmark_count)])
    else:
        raise ValueError("不支持的任务类型")
    return cmd


def _run_job(job_id: str, cmd: Any) -> None:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    job_log = deque(maxlen=LOG_LIMIT)
    with _jobs_lock:
        _jobs[job_id]["status"] = "running"
        _jobs[job_id]["logs"] = job_log
    job_log.append(f"[{time.strftime('%H:%M:%S')}] 启动命令: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=ROOT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        with _jobs_lock:
            _jobs[job_id]["pid"] = proc.pid
            _jobs[job_id]["process"] = proc
        for line in proc.stdout:
            line = line.rstrip("\n")
            job_log.append(line)
        proc.wait()
        status = "success" if proc.returncode == 0 else f"failed({proc.returncode})"
        job_log.append(f"[完成] 退出码：{proc.returncode}")
    except Exception as exc:  # noqa: BLE001
        status = "failed(exception)"
        job_log.append(f"[异常] {exc}")
    finally:
        with _jobs_lock:
            _jobs[job_id]["status"] = status


@app.get("/api/config")
def api_get_config():
    cfg = _load_config()
    return jsonify({"ok": True, "config": _serialize_config(cfg)})


@app.post("/api/config")
def api_save_config():
    data = request.get_json(force=True) or {}
    cfg = _update_config(data)
    return jsonify({"ok": True, "config": cfg})


@app.post("/api/run")
def api_run():
    data = request.get_json(force=True) or {}
    mode = data.get("mode")
    if mode not in {"follow_all", "incremental", "image_bookmark"}:
        return jsonify({"ok": False, "message": "未知任务类型"}), 400
    try:
        cmd = _build_command(mode, data)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400

    job_id = uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "cmd": cmd,
            "status": "pending",
            "logs": deque(maxlen=LOG_LIMIT),
            "created_at": time.time(),
        }

    thread = threading.Thread(target=_run_job, args=(job_id, cmd), daemon=True)
    thread.start()
    return jsonify({"ok": True, "job_id": job_id})


@app.get("/api/job/<job_id>")
def api_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "message": "未找到任务"}), 404
        logs = list(job["logs"]) if "logs" in job else []
        status = job.get("status", "pending")
    return jsonify({"ok": True, "status": status, "logs": logs, "cmd": job.get("cmd")})


@app.post("/api/job/<job_id>/stop")
def api_stop_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "message": "未找到任务"}), 404
        proc = job.get("process")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "message": f'终止失败: {exc}'}), 500
        return jsonify({"ok": True, "message": "已发送终止信号"})
    return jsonify({"ok": False, "message": "任务已结束或不可用"})


INDEX_TEMPLATE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>PixivUtil2 中文交互面板</title>
  <style>
    :root { color-scheme: light; }
    body { font-family: "Segoe UI", "Microsoft YaHei", sans-serif; background: linear-gradient(135deg, #0f172a, #111827); margin: 0; color: #f8fafc; }
    .wrap { max-width: 1100px; margin: 0 auto; padding: 24px; }
    h1 { margin: 0 0 12px; font-size: 24px; }
    h2 { margin: 24px 0 8px; font-size: 18px; border-left: 4px solid #38bdf8; padding-left: 8px; }
    p, label, input, button, small { color: #e2e8f0; }
    a { color: #7dd3fc; }
    .card { background: rgba(15, 23, 42, 0.6); border: 1px solid #1e293b; border-radius: 12px; padding: 16px 18px; box-shadow: 0 6px 30px rgba(0,0,0,0.25); }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
    .field { display: flex; flex-direction: column; gap: 6px; }
    input[type="text"], input[type="password"], input[type="number"] { padding: 10px; border-radius: 8px; border: 1px solid #334155; background: #0b1222; color: #e2e8f0; }
    input::placeholder { color: #94a3b8; }
    .actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 8px; }
    button { border: none; padding: 10px 14px; border-radius: 10px; cursor: pointer; font-weight: 600; }
    .primary { background: linear-gradient(135deg, #22d3ee, #2563eb); color: #0b1222; }
    .ghost { background: rgba(226, 232, 240, 0.08); color: #e2e8f0; border: 1px solid #334155; }
    .success { color: #4ade80; }
    .warn { color: #fbbf24; }
    .log { background: #0b1222; border-radius: 10px; padding: 12px; min-height: 240px; max-height: 360px; overflow-y: auto; white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
    .status { display: inline-block; padding: 4px 10px; border-radius: 999px; background: #1e293b; border: 1px solid #334155; }
    .badges { display: flex; gap: 8px; flex-wrap: wrap; margin: 4px 0 10px; }
    .badge { padding: 4px 10px; background: #1e293b; border-radius: 999px; border: 1px solid #334155; font-size: 12px; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>PixivUtil2 中文交互面板</h1>
    <p>按顺序：先填写配置，再点任务按钮。日志实时滚动，防止控制台乱码。推荐使用 Cookie 登录（PHPSESSID）。</p>

    <div class="card">
      <h2>① 配置账户与下载目录</h2>
      <div class="badges">
        <span class="badge">必填：PHPSESSID</span>
        <span class="badge">可选：refresh_token / Fanbox</span>
        <span class="badge">点击保存后会写入 config.ini</span>
      </div>
      <form id="config-form">
        <div class="grid">
          <label class="field">
            下载根目录 (rootDirectory)
            <input name="rootDirectory" type="text" placeholder="例如 D:\\PixivDownload">
          </label>
          <label class="field">
            下载清单目录 (downloadListDirectory)
            <input name="downloadListDirectory" type="text" placeholder=".">
          </label>
          <label class="field">
            PHPSESSID Cookie
            <input name="cookie" type="text" placeholder="PHPSESSID=xxxxx; path=/; domain=.pixiv.net">
          </label>
          <label class="field">
            Fanbox Cookie (可选)
            <input name="cookieFanbox" type="text" placeholder="FANBOXSESSID=...">
          </label>
          <label class="field">
            refresh_token (可选)
            <input name="refresh_token" type="text" placeholder="用于 OAuth 刷新">
          </label>
          <label class="field">
            Cloudflare cf_clearance (可选)
            <input name="cf_clearance" type="text" placeholder="cf_clearance=...">
          </label>
          <label class="field">
            Cloudflare cf_bm (可选)
            <input name="cf_bm" type="text" placeholder="cf_bm=...">
          </label>
          <label class="field">
            备用账号 (不推荐密码登录)
            <input name="username" type="text" placeholder="用户名或邮箱">
          </label>
          <label class="field">
            密码 (可留空)
            <input name="password" type="password" placeholder="仅当必须时填写">
          </label>
          <label class="field">
            每个任务最大页数 (0 为全部)
            <input name="numberOfPage" type="number" min="0" placeholder="0">
          </label>
          <label class="field">
            间隔天数过滤 dayLastUpdated (默认 7)
            <input name="dayLastUpdated" type="number" min="0" placeholder="7">
          </label>
          <label class="field">
            <input name="overwrite" type="checkbox"> 覆盖已存在文件（重新下载）
          </label>
          <label class="field">
            <input name="useMirrorHost" type="checkbox"> 使用反代域名（默认 i.pixiv.cat）
          </label>
          <label class="field">
            反代域名
            <input name="mirrorHost" type="text" placeholder="i.pixiv.cat">
          </label>
          <label class="field">
            <input name="mirrorMultithread" type="checkbox"> 反代分片多线程下载
          </label>
          <label class="field">
            分片线程数
            <input name="mirrorThread" type="number" min="1" placeholder="4">
          </label>
          <label class="field">
            分片大小 (KB)
            <input name="mirrorChunkSizeKB" type="number" min="64" placeholder="1024">
          </label>
          <label class="field">
            下载随机延迟 (秒) 默认 5，可设 0 关闭
            <input name="downloadDelay" type="number" min="0" placeholder="5">
          </label>
        </div>
        <div class="actions">
          <button class="primary" type="submit">保存配置到 config.ini</button>
          <span id="config-status" class="status">尚未保存</span>
        </div>
      </form>
    </div>

    <div class="card" style="margin-top:16px;">
      <h2>② 选择任务（自动调用 PixivUtil2）</h2>
      <div class="grid">
        <label class="field">
          目标页数/数量 (0=全部)
          <input id="task-pages" type="number" min="0" placeholder="0">
        </label>
        <label class="field">
          只取书签数 ≥ N (可选)
          <input id="task-bookmark-count" type="number" min="0" placeholder="不限">
        </label>
        <label class="field">
          <input id="task-private" type="checkbox" checked> 包含私密关注/书签
        </label>
      </div>
      <div class="actions">
        <button class="primary" data-mode="follow_all">关注画师：全量下载（选项 5）</button>
        <button class="ghost" data-mode="incremental">关注画师：增量更新（选项 8）</button>
        <button class="ghost" data-mode="image_bookmark">图像书签导出/下载（选项 6）</button>
      </div>
      <small>提示：全量下载会按关注列表把所有作品抓取；增量更新只拉取新作。若需重新下载旧文件，请在上方勾选“覆盖已存在文件”。</small>
      <div class="actions" style="margin-top:8px;">
        <span class="status">当前任务状态：<span id="job-status">空闲</span></span>
        <button id="stop-btn" class="ghost" type="button">停止当前任务</button>
      </div>
      <div class="log" id="log-box"></div>
    </div>

    <div class="card" style="margin-top:16px;">
      <h2>使用小贴士</h2>
      <ul>
        <li>Cookie 登录：在浏览器已登录 Pixiv，复制 <strong>PHPSESSID</strong> 值（开发者工具-应用/存储-Cookie）。</li>
        <li>增量更新：保持 dayLastUpdated=7 或按需调大，只抓取最近更新的作者。</li>
        <li>加速下载：勾选“使用反代”可将 i.pximg.net 替换为 i.pixiv.cat；再勾“分片多线程”可并行分片，线程数/分片大小可在上方调整。</li>
        <li>乱码：本页面和后台均强制 UTF-8；日志仍乱码时，可检查系统区域设置。</li>
        <li>FFmpeg：若需要转码动图，请安装 ffmpeg 并确保 PATH 中可调用。</li>
      </ul>
    </div>
  </div>

  <script>
    const logBox = document.getElementById("log-box");
    const statusSpan = document.getElementById("job-status");
    const configStatus = document.getElementById("config-status");
    let polling = null;
    let currentJobId = null;

    async function loadConfig() {
      const res = await fetch("/api/config");
      const data = await res.json();
      if (!data.ok) return;
      const cfg = data.config;
      for (const [key, value] of Object.entries(cfg)) {
        const el = document.querySelector(`[name="${key}"]`);
        if (!el) continue;
        if (el.type === "checkbox") {
          el.checked = Boolean(value);
        } else {
          el.value = value ?? "";
        }
      }
      configStatus.textContent = "已加载";
    }

    async function saveConfig(ev) {
      ev.preventDefault();
      const formData = new FormData(ev.target);
      const payload = Object.fromEntries(formData.entries());
      payload.overwrite = formData.get("overwrite") ? true : false;
      payload.useMirrorHost = formData.get("useMirrorHost") ? true : false;
      payload.mirrorMultithread = formData.get("mirrorMultithread") ? true : false;
      // 强制数值字段为数字/字符串，避免空字符串被当作 0
      ["numberOfPage","dayLastUpdated","mirrorThread","mirrorChunkSizeKB","downloadDelay"].forEach(k=>{
        if (payload[k] === "") delete payload[k];
      });
      const res = await fetch("/api/config", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
      const data = await res.json();
      configStatus.textContent = data.ok ? "已保存" : "保存失败";
    }

    async function startTask(mode) {
      const includePrivate = document.getElementById("task-private").checked ? "y" : "n";
      const pages = document.getElementById("task-pages").value;
      const bookmarkCount = document.getElementById("task-bookmark-count").value;
      const res = await fetch("/api/run", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          mode,
          pages,
          bookmark_flag: includePrivate,
          bookmark_count: bookmarkCount
        })
      });
      const data = await res.json();
      if (!data.ok) {
        statusSpan.textContent = data.message || "启动失败";
        return;
      }
      currentJobId = data.job_id;
      statusSpan.textContent = "运行中...";
      logBox.textContent = "";
      beginPolling(data.job_id);
    }

    function beginPolling(jobId) {
      if (polling) clearInterval(polling);
      polling = setInterval(async () => {
        const res = await fetch(`/api/job/${jobId}`);
        if (!res.ok) { statusSpan.textContent = "查询失败"; return; }
        const data = await res.json();
        if (!data.ok) { statusSpan.textContent = "查询失败"; return; }
        statusSpan.textContent = data.status;
        logBox.textContent = (data.logs || []).join("\\n");
        logBox.scrollTop = logBox.scrollHeight;
        if (!data.status.startsWith("running") && !data.status.startsWith("pending")) {
          clearInterval(polling);
          polling = null;
        }
      }, 1500);
    }

    async function stopTask() {
      if (!currentJobId) { statusSpan.textContent = "无正在运行的任务"; return; }
      const res = await fetch(`/api/job/${currentJobId}/stop`, { method: "POST" });
      const data = await res.json();
      statusSpan.textContent = data.message || (data.ok ? "已停止" : "停止失败");
    }

    document.getElementById("config-form").addEventListener("submit", saveConfig);
    document.querySelectorAll("button[data-mode]").forEach(btn => {
      btn.addEventListener("click", () => startTask(btn.dataset.mode));
    });
    document.getElementById("stop-btn").addEventListener("click", stopTask);

    loadConfig();
  </script>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(INDEX_TEMPLATE)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
