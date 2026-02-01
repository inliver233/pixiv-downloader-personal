#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PixivUtil2（本修改版）Web UI：
- 同步关注画师（仅索引 URL，不全量下载）
- 每画师保留少量预览图（默认 3 张）
- 浏览本地索引库：画师列表、作品列表、URL 导出

运行：python web_ui.py
浏览器打开：http://127.0.0.1:5000
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import uuid4

from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory

from common import PixivConfig, PixivHelper
from PixivDBManager import PixivDBManager

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
WEBUI_DIR = os.path.join(ROOT_DIR, "webui")
CONFIG_PATH = os.path.join(ROOT_DIR, "config.ini")
LOG_LIMIT = 2000

app = Flask(
    __name__,
    template_folder=os.path.join(WEBUI_DIR, "templates"),
    static_folder=os.path.join(WEBUI_DIR, "static"),
)
app.config["JSON_AS_ASCII"] = False

_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()

_db_init_lock = threading.Lock()
_db_initialized = False


def _load_config() -> PixivConfig.PixivConfig:
    cfg = PixivConfig.PixivConfig()
    cfg.loadConfig(CONFIG_PATH)
    return cfg


def _serialize_config(cfg: PixivConfig.PixivConfig) -> Dict[str, Any]:
    # Secrets intentionally omitted (empty string means "keep existing").
    return {
        "rootDirectory": cfg.rootDirectory,
        "downloadListDirectory": cfg.downloadListDirectory,
        "username": cfg.username,
        "password": "",
        "cookie": "",
        "refresh_token": "",
        "hasPassword": bool(getattr(cfg, "password", "")),
        "hasCookie": bool(getattr(cfg, "cookie", "")),
        "hasRefreshToken": bool(getattr(cfg, "refresh_token", "")),
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


def _maybe_set_secret(cfg: PixivConfig.PixivConfig, payload: Dict[str, Any], field: str) -> None:
    if field not in payload:
        return
    value = payload.get(field)
    if value is None:
        return
    if isinstance(value, str) and value == "":
        return
    setattr(cfg, field, value)


def _update_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _load_config()
    cfg.rootDirectory = payload.get("rootDirectory", cfg.rootDirectory).strip() or cfg.rootDirectory
    cfg.downloadListDirectory = payload.get("downloadListDirectory", cfg.downloadListDirectory).strip() or cfg.downloadListDirectory
    cfg.username = payload.get("username", cfg.username)

    _maybe_set_secret(cfg, payload, "password")
    _maybe_set_secret(cfg, payload, "cookie")
    _maybe_set_secret(cfg, payload, "refresh_token")

    cfg.numberOfPage = _parse_int(payload.get("numberOfPage"), cfg.numberOfPage)
    cfg.dayLastUpdated = _parse_int(payload.get("dayLastUpdated"), cfg.dayLastUpdated)
    if "overwrite" in payload:
        cfg.overwrite = _parse_bool(payload.get("overwrite"))
    cfg.useMirrorHost = _parse_bool(payload.get("useMirrorHost"))
    cfg.mirrorHost = payload.get("mirrorHost", cfg.mirrorHost).strip() or cfg.mirrorHost
    cfg.mirrorMultithread = _parse_bool(payload.get("mirrorMultithread"))
    cfg.mirrorThread = max(1, _parse_int(payload.get("mirrorThread"), cfg.mirrorThread))
    cfg.mirrorChunkSizeKB = max(64, _parse_int(payload.get("mirrorChunkSizeKB"), cfg.mirrorChunkSizeKB))
    cfg.downloadDelay = max(0, _parse_int(payload.get("downloadDelay"), cfg.downloadDelay))
    cfg.writeConfig(path=CONFIG_PATH)
    return _serialize_config(cfg)


@contextmanager
def _open_db(cfg: PixivConfig.PixivConfig) -> Iterable[PixivDBManager]:
    global _db_initialized
    # Keep Web UI requests responsive if the CLI job is writing to sqlite.
    db = PixivDBManager(root_directory=cfg.rootDirectory, target=cfg.dbPath, timeout=10)
    try:
        with _db_init_lock:
            if not _db_initialized:
                db.createDatabase()
                _db_initialized = True
        yield db
    finally:
        db.close()


def _preview_root(cfg: PixivConfig.PixivConfig) -> str:
    return os.path.abspath(os.path.join(cfg.rootDirectory, "_preview"))


def _proxy(url: str, cfg: PixivConfig.PixivConfig) -> str:
    return PixivHelper.apply_mirror(url, cfg)


def _build_command(mode: str, params: Dict[str, Any]) -> List[str]:
    cmd = [sys.executable, os.path.join(ROOT_DIR, "PixivUtil2.py"), "-x"]

    bookmark_flag = params.get("bookmark_flag", "n")
    preview_per_artist = params.get("preview_per_artist", None)

    if mode == "follow_index":
        cmd.extend(["-s", "20", "-p", str(bookmark_flag)])
        if preview_per_artist not in (None, ""):
            cmd.extend(["--preview_per_artist", str(preview_per_artist)])
        return cmd

    # legacy actions (keep for compatibility)
    pages = params.get("pages")
    if pages not in (None, "", "0", 0):
        cmd.extend(["-n", str(pages)])

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


def _run_job(job_id: str, cmd: List[str]) -> None:
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
        for line in proc.stdout:  # type: ignore[union-attr]
            job_log.append(line.rstrip("\n"))
        proc.wait()
        status = "success" if proc.returncode == 0 else f"failed({proc.returncode})"
        job_log.append(f"[完成] 退出码：{proc.returncode}")
    except Exception as exc:  # noqa: BLE001
        status = "failed(exception)"
        job_log.append(f"[异常] {exc}")
    finally:
        with _jobs_lock:
            _jobs[job_id]["status"] = status


@app.get("/")
def index():
    return render_template("index.html", title="面板", page_name="dashboard")


@app.get("/artists")
def page_artists():
    return render_template("artists.html", title="画师", page_name="artists")


@app.get("/artist/<int:member_id>")
def page_artist_detail(member_id: int):
    return render_template("artist_detail.html", title=f"画师 {member_id}", page_name="artist", member_id=member_id)


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
    if mode not in {"follow_index", "follow_all", "incremental", "image_bookmark"}:
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
            return jsonify({"ok": False, "message": f"终止失败: {exc}"}), 500
        return jsonify({"ok": True, "message": "已发送终止信号"})
    return jsonify({"ok": False, "message": "任务已结束或不可用"})


@app.get("/preview/<int:member_id>/<path:filename>")
def preview_file(member_id: int, filename: str):
    cfg = _load_config()
    base = os.path.join(_preview_root(cfg), str(member_id))
    if not os.path.isdir(base):
        abort(404)
    return send_from_directory(base, filename)


@app.get("/api/follow/stats")
def api_follow_stats():
    cfg = _load_config()
    try:
        with _open_db(cfg) as db:
            stats = {
                "member_count": db.countFollowMembers(),
                "image_count": db.countFollowImagesAll(),
                "url_count": db.countFollowImageUrlsAll(),
            }
        return jsonify({"ok": True, "stats": stats})
    except Exception as exc:  # noqa: BLE001
        # sqlite can still be busy for a short window; keep UI responsive.
        return jsonify({"ok": False, "message": f"db busy: {exc}"}), 503


@app.get("/api/follow/members")
def api_follow_members():
    cfg = _load_config()
    q = (request.args.get("q") or "").strip() or None
    page = max(1, _parse_int(request.args.get("page"), 1))
    page_size = _parse_int(request.args.get("page_size"), 30)
    page_size = min(100, max(1, page_size))
    offset = (page - 1) * page_size

    with _open_db(cfg) as db:
        total = db.countFollowMembers(query=q)
        rows = db.selectFollowMembersSummary(query=q, offset=offset, limit=page_size)

    items = []
    for row in rows:
        (
            member_id,
            name,
            member_token,
            avatar_url,
            background_url,
            created_date,
            last_sync_date,
            image_count,
            url_count,
        ) = row
        items.append(
            {
                "member_id": int(member_id),
                "name": name,
                "member_token": member_token,
                "avatar_url": avatar_url,
                "background_url": background_url,
                "created_date": created_date,
                "last_sync_date": last_sync_date,
                "image_count": int(image_count or 0),
                "url_count": int(url_count or 0),
            }
        )

    return jsonify({"ok": True, "total": total, "page": page, "page_size": page_size, "items": items})


@app.get("/api/follow/member/<int:member_id>")
def api_follow_member(member_id: int):
    cfg = _load_config()
    with _open_db(cfg) as db:
        row = db.selectFollowMemberSummaryById(member_id)
    if not row:
        return jsonify({"ok": False, "message": "未找到画师"}), 404

    (
        mid,
        name,
        member_token,
        avatar_url,
        background_url,
        created_date,
        last_sync_date,
        image_count,
        url_count,
    ) = row

    preview_dir = os.path.join(_preview_root(cfg), str(mid))
    previews: List[Dict[str, str]] = []
    if os.path.isdir(preview_dir):
        try:
            files = [f for f in os.listdir(preview_dir) if os.path.isfile(os.path.join(preview_dir, f))]
            for f in sorted(files)[:12]:
                previews.append({"name": f, "url": f"/preview/{mid}/{f}"})
        except OSError:
            previews = []

    member = {
        "member_id": int(mid),
        "name": name,
        "member_token": member_token,
        "avatar_url": avatar_url,
        "background_url": background_url,
        "created_date": created_date,
        "last_sync_date": last_sync_date,
        "image_count": int(image_count or 0),
        "url_count": int(url_count or 0),
        "previews": previews,
    }
    return jsonify({"ok": True, "member": member})


@app.get("/api/follow/member/<int:member_id>/works")
def api_follow_member_works(member_id: int):
    cfg = _load_config()
    page = max(1, _parse_int(request.args.get("page"), 1))
    page_size = _parse_int(request.args.get("page_size"), 20)
    page_size = min(100, max(1, page_size))
    offset = (page - 1) * page_size

    with _open_db(cfg) as db:
        total = db.countFollowImages(member_id)
        rows = db.selectFollowImages(member_id=member_id, offset=offset, limit=page_size)
        page0 = db.selectFollowImagePage0Urls([r[0] for r in rows])

    items = []
    for row in rows:
        image_id, title, create_date, page_count, mode, bookmark_count, like_count, view_count = row
        (ori_url, reg_url) = page0.get(int(image_id), (None, None))
        thumb_url = None
        if reg_url:
            thumb_url = _proxy(reg_url, cfg)
        elif ori_url:
            thumb_url = _proxy(ori_url, cfg)
        items.append(
            {
                "image_id": int(image_id),
                "title": title,
                "create_date": create_date,
                "page_count": int(page_count or 0),
                "mode": mode,
                "bookmark_count": bookmark_count,
                "like_count": like_count,
                "view_count": view_count,
                "thumb_url": thumb_url,
            }
        )

    return jsonify({"ok": True, "total": total, "page": page, "page_size": page_size, "items": items})


@app.get("/api/follow/image/<int:image_id>/urls")
def api_follow_image_urls(image_id: int):
    cfg = _load_config()
    with _open_db(cfg) as db:
        rows = db.selectFollowImageUrls(image_id)
    urls = []
    for (page_index, original_url, regular_url) in rows:
        original_url = original_url or ""
        regular_url = regular_url or ""
        urls.append(
            {
                "page_index": int(page_index),
                "original_url": original_url,
                "regular_url": regular_url,
                "proxy_original_url": _proxy(original_url, cfg) if original_url else "",
                "proxy_regular_url": _proxy(regular_url, cfg) if regular_url else "",
            }
        )
    return jsonify({"ok": True, "urls": urls})


@app.get("/api/follow/member/<int:member_id>/export")
def api_follow_member_export(member_id: int):
    cfg = _load_config()
    kind = (request.args.get("kind") or "regular").strip().lower()
    if kind not in {"regular", "original"}:
        kind = "regular"

    with _open_db(cfg) as db:
        # join to export in proper order
        c = db.conn.cursor()
        c.execute(
            """SELECT fu.original_url, fu.regular_url
                 FROM pixiv_follow_image_url fu
                 JOIN pixiv_follow_image fi ON fi.image_id = fu.image_id
                WHERE fi.member_id = ?
                ORDER BY fi.image_id DESC, fu.page_index ASC""",
            (int(member_id),),
        )
        rows = c.fetchall()
        c.close()

    lines: List[str] = []
    for (ori, reg) in rows:
        url = reg if kind == "regular" else ori
        if not url:
            continue
        lines.append(_proxy(url, cfg))

    text = "\n".join(lines) + ("\n" if lines else "")
    filename = f"pixiv_follow_{member_id}_{kind}.txt"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(text, mimetype="text/plain; charset=utf-8", headers=headers)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
