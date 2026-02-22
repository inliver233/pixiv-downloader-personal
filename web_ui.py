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

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import uuid4

from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory, stream_with_context

from common import PixivConfig, PixivHelper
from PixivDBManager import PixivDBManager

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
WEBUI_DIR = os.path.join(ROOT_DIR, "webui")
CONFIG_PATH = os.path.join(ROOT_DIR, "config.ini")
MULTI_CONFIG_PATH = os.path.join(ROOT_DIR, "multi_config.json")
MULTI_DB_FILENAME = "db.multi.sqlite"
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

_multi_lock = threading.Lock()
_multi_runner_job_id: Optional[str] = None


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    """
    Stop a process and its children (best-effort).

    On Windows, proc.terminate() does NOT kill child processes. Use taskkill /T.
    """
    if not proc:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return

    try:
        pid = int(proc.pid)
    except Exception:
        pid = 0

    if os.name == "nt" and pid > 0:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return
        except Exception:
            # Fallback to terminate below.
            pass

    try:
        proc.terminate()
    except Exception:
        pass


def _default_multi_config() -> Dict[str, Any]:
    return {
        "base_config": "config.ini",
        "runtime_dir": ".multi_runtime",
        "accounts": [],
        "follow": {
            "bookmark_flag": "n",
            "lang": "en",
            "refresh_interval_sec": 1800,
            "timeout_sec": 20,
        },
        "proxy_pool": {
            "source": "easy_proxies",
            "refresh_interval_sec": 300,
            "max_tokens_per_proxy": 2,
            "bindings_strict": False,
            "easy_proxies": {
                "base_url": "http://127.0.0.1:9090",
                "host_override": "",
                "password": "",
                "timeout_sec": 10,
                "verify_ssl": True,
            },
             "test": {
                 "target_url": "https://www.pixiv.net/robots.txt",
                 "timeout_sec": 8,
                 "concurrency": 20,
                 "attempts": 2,
                 "top_n": 20,
             },
            # Optional: manual proxies (http/socks) and file-based pool.
            "proxies": [],
            "proxies_file": "",
        },
        "worker": {"poll_interval_sec": 10, "mode": "index_urls"},
        "_ui_nonce": 0,
    }


def _merge_defaults(raw: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(defaults)
    for k, v in (raw or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_defaults(v, out[k])  # type: ignore[arg-type]
        else:
            out[k] = v
    return out


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False, indent=2))
        fp.write("\n")
    os.replace(tmp, path)


def _load_multi_config_raw() -> Dict[str, Any]:
    defaults = _default_multi_config()
    if not os.path.isfile(MULTI_CONFIG_PATH):
        raw = defaults
        _atomic_write_json(MULTI_CONFIG_PATH, raw)
        return raw

    try:
        with open(MULTI_CONFIG_PATH, "r", encoding="utf-8") as fp:
            data = json.load(fp) or {}
        return _merge_defaults(data, defaults)
    except Exception:
        # If config is corrupted, fall back to defaults (but keep file for manual recovery).
        return defaults


def _save_multi_config_raw(raw: Dict[str, Any]) -> Dict[str, Any]:
    merged = _merge_defaults(raw or {}, _default_multi_config())
    _atomic_write_json(MULTI_CONFIG_PATH, merged)
    return merged


def _serialize_multi_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    raw = _merge_defaults(raw or {}, _default_multi_config())
    accounts = []
    for a in raw.get("accounts") or []:
        aid = str(a.get("id") or "").strip()
        if not aid:
            continue
        cookie = str(a.get("cookie") or "")
        rt = str(a.get("refresh_token") or "")
        enabled = bool(a.get("enabled") if "enabled" in a else True)
        follow_source = bool(a.get("follow_source") if "follow_source" in a else True)
        cookie_uid = ""
        if cookie and "_" in cookie:
            head = cookie.split("_", 1)[0]
            if head.isdigit():
                cookie_uid = head
        accounts.append(
            {
                "id": aid,
                "enabled": enabled,
                "follow_source": follow_source,
                "hasCookie": bool(cookie),
                "cookieUserId": cookie_uid,
                "hasRefreshToken": bool(rt),
                "downloadDelay": a.get("downloadDelay"),
            }
        )

    proxy_pool = raw.get("proxy_pool") or {}
    easy = proxy_pool.get("easy_proxies") or {}

    safe = {
        "base_config": raw.get("base_config") or "config.ini",
        "runtime_dir": raw.get("runtime_dir") or ".multi_runtime",
        "accounts": accounts,
        "follow": raw.get("follow") or {},
        "worker": raw.get("worker") or {},
        "proxy_pool": {
            **proxy_pool,
            "easy_proxies": {
                **easy,
                "password": "",
                "hasPassword": bool(str(easy.get("password") or "")),
            },
        },
    }
    return safe


def _validate_account_id(value: str) -> str:
    v = str(value or "").strip()
    if not v:
        raise ValueError("账号 ID 不能为空")
    if not re.match(r"^[A-Za-z0-9._-]{1,64}$", v):
        raise ValueError("账号 ID 只允许字母数字 . _ - 且长度 <= 64")
    return v


def _get_multi_runtime_dir(raw: Dict[str, Any]) -> str:
    runtime_dir = str((raw.get("runtime_dir") or ".multi_runtime")).strip() or ".multi_runtime"
    # Resolve relative to repo root (same as MultiRunner default).
    if not os.path.isabs(runtime_dir):
        runtime_dir = os.path.abspath(os.path.join(ROOT_DIR, runtime_dir))
    return runtime_dir


def _get_multi_db_path(raw: Dict[str, Any]) -> str:
    runtime_dir = _get_multi_runtime_dir(raw)
    return os.path.join(runtime_dir, MULTI_DB_FILENAME)


def _get_multi_base_config_path(raw: Dict[str, Any]) -> str:
    base_cfg = str((raw.get("base_config") or "config.ini")).strip() or "config.ini"
    if not os.path.isabs(base_cfg):
        base_cfg = os.path.abspath(os.path.join(ROOT_DIR, base_cfg))
    return base_cfg


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
        "useProxy": bool(getattr(cfg, "useProxy", False)),
        "proxyAddress": "",
        "hasProxyAddress": bool(getattr(cfg, "proxyAddress", "")),
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

    clear_password = _parse_bool(payload.get("clearPassword")) or _parse_bool(payload.get("clear_password"))
    clear_cookie = _parse_bool(payload.get("clearCookie")) or _parse_bool(payload.get("clear_cookie"))
    clear_refresh_token = _parse_bool(payload.get("clearRefreshToken")) or _parse_bool(payload.get("clear_refresh_token"))
    if clear_password:
        cfg.password = ""
    if clear_cookie:
        cfg.cookie = ""
    if clear_refresh_token:
        cfg.refresh_token = ""

    if "useProxy" in payload:
        cfg.useProxy = _parse_bool(payload.get("useProxy"))

    clear_proxy = _parse_bool(payload.get("clearProxyAddress")) or _parse_bool(payload.get("clear_proxy"))
    if clear_proxy:
        cfg.proxyAddress = ""
        cfg.useProxy = False
    else:
        # Secret-like: empty string means "keep existing".
        if "proxyAddress" in payload:
            value = payload.get("proxyAddress")
            if isinstance(value, str):
                value = value.strip()
            if value:
                try:
                    from common.ProxyUtils import normalize_proxy_url

                    cfg.proxyAddress = normalize_proxy_url(str(value))
                except Exception:
                    cfg.proxyAddress = str(value)

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
    # When stdout is piped, Python may buffer output heavily; enable unbuffered mode
    # so WebUI logs can stream in real-time.
    env.setdefault("PYTHONUNBUFFERED", "1")
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
            bufsize=1,
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


@app.get("/multi")
def page_multi():
    return render_template("multi.html", title="多账号并发", page_name="multi")


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


@app.post("/api/proxy/test")
def api_proxy_test():
    payload = request.get_json(force=True) or {}
    proxy_in = str(payload.get("proxy") or payload.get("proxyAddress") or "").strip()

    test_url = str(payload.get("test_url") or payload.get("target_url") or "https://www.pixiv.net/robots.txt").strip() or "https://www.pixiv.net/robots.txt"
    try:
        timeout_sec = float(payload.get("timeout_sec") or 10.0)
    except (TypeError, ValueError):
        timeout_sec = 10.0
    attempts = _parse_int(payload.get("attempts"), 2)

    proxy_value = proxy_in
    if not proxy_value:
        cfg = _load_config()
        if getattr(cfg, "useProxy", False) and getattr(cfg, "proxyAddress", ""):
            proxy_value = str(getattr(cfg, "proxyAddress", "") or "")

    if not proxy_value:
        return jsonify({"ok": False, "message": "未配置 proxyAddress（请先在“基础配置”里启用代理并保存）"}), 400

    from common.ProxyTester import test_proxy

    result = test_proxy(proxy_value, test_url=test_url, timeout=timeout_sec, attempts=attempts)
    return jsonify(
        {
            "ok": True,
            "result": {
                "proxy": result.masked,
                "ok_count": int(result.ok),
                "fail_count": int(result.fail),
                "avg_ms": result.avg_ms,
                "last_error": result.last_error,
                "test_url": test_url,
            },
        }
    )


@app.get("/api/multi/config")
def api_get_multi_config():
    raw = _load_multi_config_raw()
    return jsonify({"ok": True, "config": _serialize_multi_config(raw)})


@app.post("/api/multi/proxy/test")
def api_multi_proxy_test():
    payload = request.get_json(force=True) or {}

    raw = _load_multi_config_raw()
    proxy_pool = raw.get("proxy_pool") or {}
    source = str(proxy_pool.get("source") or "easy_proxies").strip().lower()

    test_cfg = proxy_pool.get("test") or {}
    attempts_default = _parse_int(test_cfg.get("attempts"), 2)
    attempts = max(1, _parse_int(payload.get("attempts"), attempts_default))
    test_url = str(test_cfg.get("target_url") or "https://www.pixiv.net/robots.txt").strip() or "https://www.pixiv.net/robots.txt"
    try:
        timeout_sec = float(test_cfg.get("timeout_sec") or 8.0)
    except (TypeError, ValueError):
        timeout_sec = 8.0
    concurrency = max(1, _parse_int(test_cfg.get("concurrency"), 20))
    top_n = max(1, _parse_int(test_cfg.get("top_n"), 20))

    raw_list: List[str] = []
    # manual proxies (always included for testing)
    manual = proxy_pool.get("proxies") or []
    if isinstance(manual, str):
        manual = [line.strip() for line in manual.splitlines() if line.strip() and not line.strip().startswith("#")]
    if isinstance(manual, list):
        raw_list.extend([str(p).strip() for p in manual if str(p).strip()])

    warning = ""
    export_error = ""
    if source == "none":
        warning = "proxy_pool.source=none：Runner 不会启用任何代理；此处仅做连通性测试"
    elif source == "easy_proxies":
        easy = proxy_pool.get("easy_proxies") or {}
        base_url = str(easy.get("base_url") or "").strip()
        password = str(easy.get("password") or "")
        host_override = str(easy.get("host_override") or "").strip()
        try:
            timeout_api = float(easy.get("timeout_sec") or 10.0)
        except (TypeError, ValueError):
            timeout_api = 10.0
        verify_ssl = bool(easy.get("verify_ssl") if "verify_ssl" in easy else True)
        if base_url:
            try:
                from common.EasyProxiesClient import EasyProxiesClient, EasyProxiesConfig

                client = EasyProxiesClient(
                    EasyProxiesConfig(
                        base_url=base_url,
                        password=password,
                        timeout=timeout_api,
                        verify_ssl=verify_ssl,
                    )
                )
                exported = client.export_http_proxies()

                # Some easy_proxies setups export placeholder hosts like 0.0.0.0/127.0.0.1/localhost.
                # Replace them with either explicit host_override or the base_url host.
                replace_host = host_override
                if not replace_host:
                    try:
                        from urllib.parse import urlparse

                        replace_host = str(urlparse(base_url).hostname or "").strip()
                    except Exception:
                        replace_host = ""

                if replace_host:
                    fixed: List[str] = []
                    try:
                        from common.ProxyUtils import ProxyParts, parse_proxy_url

                        for line in exported:
                            try:
                                parts = parse_proxy_url(line)
                                host = (parts.host or "").strip().lower()
                                if host in {"0.0.0.0", "127.0.0.1", "localhost"}:
                                    fixed.append(
                                        ProxyParts(
                                            scheme=parts.scheme,
                                            host=replace_host,
                                            port=int(parts.port),
                                            username=parts.username or "",
                                            password=parts.password or "",
                                        ).to_url()
                                    )
                                else:
                                    fixed.append(parts.to_url())
                            except Exception:
                                fixed.append(str(line).strip())
                        exported = fixed
                    except Exception:
                        # parsing helpers unavailable or failed, keep raw exported list
                        pass

                raw_list.extend(exported)
            except Exception as ex:  # noqa: BLE001
                export_error = str(ex)
        else:
            export_error = "easy_proxies.base_url 为空"

    # test & rank
    from common.ProxyTester import rank_proxies

    ranked = rank_proxies(
        raw_list,
        test_url=test_url,
        timeout=timeout_sec,
        concurrency=concurrency,
        top_n=top_n,
        attempts=attempts,
    )

    return jsonify(
        {
            "ok": True,
            "source": source,
            "warning": warning,
            "export_error": export_error,
            "input_count": int(len(raw_list)),
            "ranked_count": int(len(ranked)),
            "test": {"test_url": test_url, "timeout_sec": timeout_sec, "concurrency": concurrency, "top_n": top_n, "attempts": attempts},
            "ranked": [
                {
                    "proxy": r.masked,
                    "ok_count": int(r.ok),
                    "fail_count": int(r.fail),
                    "avg_ms": r.avg_ms,
                    "last_error": r.last_error,
                }
                for r in ranked
            ],
        }
    )


@app.post("/api/multi/config")
def api_save_multi_config():
    payload = request.get_json(force=True) or {}
    raw = _load_multi_config_raw()

    # Update base paths (optional)
    if "base_config" in payload:
        base_cfg = str(payload.get("base_config") or "").strip()
        if base_cfg:
            raw["base_config"] = base_cfg
    if "runtime_dir" in payload:
        runtime_dir = str(payload.get("runtime_dir") or "").strip()
        if runtime_dir:
            raw["runtime_dir"] = runtime_dir

    # Follow settings
    follow_in = payload.get("follow") or {}
    if isinstance(follow_in, dict):
        follow = raw.get("follow") or {}
        bookmark_flag = str(follow_in.get("bookmark_flag") or "").strip().lower()
        if bookmark_flag in {"y", "n", "o"}:
            follow["bookmark_flag"] = bookmark_flag
        if "lang" in follow_in:
            follow["lang"] = str(follow_in.get("lang") or "en").strip() or "en"
        if "refresh_interval_sec" in follow_in:
            follow["refresh_interval_sec"] = max(30, _parse_int(follow_in.get("refresh_interval_sec"), int(follow.get("refresh_interval_sec") or 1800)))
        if "timeout_sec" in follow_in:
            try:
                follow["timeout_sec"] = float(follow_in.get("timeout_sec") or follow.get("timeout_sec") or 20.0)
            except (TypeError, ValueError):
                pass
        raw["follow"] = follow

    # Proxy pool settings
    proxy_in = payload.get("proxy_pool") or {}
    if isinstance(proxy_in, dict):
        proxy_pool = raw.get("proxy_pool") or {}
        if "source" in proxy_in:
            proxy_pool["source"] = str(proxy_in.get("source") or "easy_proxies").strip().lower()
        if "refresh_interval_sec" in proxy_in:
            proxy_pool["refresh_interval_sec"] = max(30, _parse_int(proxy_in.get("refresh_interval_sec"), int(proxy_pool.get("refresh_interval_sec") or 300)))
        if "max_tokens_per_proxy" in proxy_in or "max_accounts_per_proxy" in proxy_in:
            raw_val = proxy_in.get("max_tokens_per_proxy")
            if raw_val in (None, ""):
                raw_val = proxy_in.get("max_accounts_per_proxy")
            proxy_pool["max_tokens_per_proxy"] = max(1, _parse_int(raw_val, int(proxy_pool.get("max_tokens_per_proxy") or 2)))
        if "bindings_strict" in proxy_in:
            proxy_pool["bindings_strict"] = _parse_bool(proxy_in.get("bindings_strict"))
        if "binding_salt" in proxy_in:
            salt = str(proxy_in.get("binding_salt") or "").strip()
            if salt:
                proxy_pool["binding_salt"] = salt

        # manual proxies and file
        if "proxies" in proxy_in:
            proxies = proxy_in.get("proxies") or []
            if isinstance(proxies, str):
                proxies = [line.strip() for line in proxies.splitlines() if line.strip() and not line.strip().startswith("#")]
            if isinstance(proxies, list):
                proxy_pool["proxies"] = [str(p).strip() for p in proxies if str(p).strip()]
        if "proxies_file" in proxy_in:
            proxy_pool["proxies_file"] = str(proxy_in.get("proxies_file") or "").strip()

        test_in = proxy_in.get("test") or {}
        if isinstance(test_in, dict):
            test = proxy_pool.get("test") or {}
            if "target_url" in test_in:
                test["target_url"] = str(test_in.get("target_url") or "https://www.pixiv.net/robots.txt").strip() or "https://www.pixiv.net/robots.txt"
            if "timeout_sec" in test_in:
                try:
                    test["timeout_sec"] = float(test_in.get("timeout_sec") or test.get("timeout_sec") or 8.0)
                except (TypeError, ValueError):
                    pass
            if "concurrency" in test_in:
                test["concurrency"] = max(1, _parse_int(test_in.get("concurrency"), int(test.get("concurrency") or 20)))
            if "attempts" in test_in:
                test["attempts"] = max(1, _parse_int(test_in.get("attempts"), int(test.get("attempts") or 2)))
            if "top_n" in test_in:
                test["top_n"] = max(1, _parse_int(test_in.get("top_n"), int(test.get("top_n") or 20)))
            proxy_pool["test"] = test

        easy_in = proxy_in.get("easy_proxies") or {}
        if isinstance(easy_in, dict):
            easy = proxy_pool.get("easy_proxies") or {}
            if "base_url" in easy_in:
                easy["base_url"] = str(easy_in.get("base_url") or "").strip()
            if "host_override" in easy_in:
                easy["host_override"] = str(easy_in.get("host_override") or "").strip()
            # secret: empty string means keep
            if "password" in easy_in:
                pw = easy_in.get("password")
                if pw is not None and str(pw) != "":
                    easy["password"] = str(pw)
            if "timeout_sec" in easy_in:
                try:
                    easy["timeout_sec"] = float(easy_in.get("timeout_sec") or easy.get("timeout_sec") or 10.0)
                except (TypeError, ValueError):
                    pass
            if "verify_ssl" in easy_in:
                easy["verify_ssl"] = bool(easy_in.get("verify_ssl"))
            proxy_pool["easy_proxies"] = easy

        raw["proxy_pool"] = proxy_pool

    # Worker settings
    worker_in = payload.get("worker") or {}
    if isinstance(worker_in, dict):
        worker = raw.get("worker") or {}
        if "mode" in worker_in:
            mode = str(worker_in.get("mode") or "").strip().lower()
            if mode in {"index_urls", "download"}:
                worker["mode"] = mode
        if "poll_interval_sec" in worker_in:
            try:
                worker["poll_interval_sec"] = max(2.0, float(worker_in.get("poll_interval_sec") or worker.get("poll_interval_sec") or 10.0))
            except (TypeError, ValueError):
                pass
        raw["worker"] = worker

    # Touch nonce so the runner can reload immediately (no restart).
    raw["_ui_nonce"] = int(time.time())

    saved = _save_multi_config_raw(raw)
    return jsonify({"ok": True, "config": _serialize_multi_config(saved)})


@app.post("/api/multi/account")
def api_upsert_multi_account():
    payload = request.get_json(force=True) or {}
    account_id = _validate_account_id(payload.get("id"))
    cookie = str(payload.get("cookie") or "")
    refresh_token = str(payload.get("refresh_token") or "")
    clear_cookie = _parse_bool(payload.get("clearCookie")) or _parse_bool(payload.get("clear_cookie"))
    clear_refresh_token = _parse_bool(payload.get("clearRefreshToken")) or _parse_bool(payload.get("clear_refresh_token"))
    enabled_in = payload.get("enabled") if "enabled" in payload else None
    follow_source_in = payload.get("follow_source") if "follow_source" in payload else payload.get("followSource") if "followSource" in payload else None

    download_delay = None
    if "downloadDelay" in payload:
        raw_val = payload.get("downloadDelay")
        if raw_val in (None, ""):
            download_delay = None
        else:
            try:
                download_delay = int(raw_val)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "message": "downloadDelay 必须是整数"}), 400

    raw = _load_multi_config_raw()
    accounts = raw.get("accounts") or []
    if not isinstance(accounts, list):
        accounts = []

    existing = None
    for a in accounts:
        if str(a.get("id") or "").strip() == account_id:
            existing = a
            break

    if existing is None:
        if not cookie and not refresh_token:
            return jsonify({"ok": False, "message": "新增账号必须填写 cookie(PHPSESSID) 或 refresh_token"}), 400
        item: Dict[str, Any] = {
            "id": account_id,
            "cookie": cookie or "",
            "enabled": _parse_bool(enabled_in) if enabled_in is not None else True,
            "follow_source": _parse_bool(follow_source_in) if follow_source_in is not None else True,
        }
        if refresh_token:
            item["refresh_token"] = refresh_token
        if download_delay is not None:
            item["downloadDelay"] = int(download_delay)
        accounts.append(item)
    else:
        if enabled_in is not None:
            existing["enabled"] = _parse_bool(enabled_in)
        if follow_source_in is not None:
            existing["follow_source"] = _parse_bool(follow_source_in)

        if clear_cookie:
            existing["cookie"] = ""
        elif cookie:
            existing["cookie"] = cookie

        if clear_refresh_token:
            existing.pop("refresh_token", None)
        elif refresh_token:
            existing["refresh_token"] = refresh_token
        if "downloadDelay" in payload:
            if download_delay is None:
                existing.pop("downloadDelay", None)
            else:
                existing["downloadDelay"] = int(download_delay)

    raw["accounts"] = accounts
    raw["_ui_nonce"] = int(time.time())
    saved = _save_multi_config_raw(raw)
    return jsonify({"ok": True, "config": _serialize_multi_config(saved)})


@app.post("/api/multi/accounts/bulk")
def api_multi_accounts_bulk():
    payload = request.get_json(force=True) or {}
    clear_delay = _parse_bool(payload.get("clearDownloadDelay")) or _parse_bool(payload.get("clear_download_delay"))

    has_delay = "downloadDelay" in payload
    download_delay = None
    if has_delay and not clear_delay:
        raw_val = payload.get("downloadDelay")
        if raw_val in (None, ""):
            clear_delay = True
        else:
            try:
                download_delay = int(raw_val)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "message": "downloadDelay 必须是整数"}), 400

    raw = _load_multi_config_raw()
    accounts = raw.get("accounts") or []
    if not isinstance(accounts, list):
        accounts = []

    for a in accounts:
        if not isinstance(a, dict):
            continue
        if clear_delay:
            a.pop("downloadDelay", None)
        elif has_delay and download_delay is not None:
            a["downloadDelay"] = int(download_delay)

    raw["accounts"] = accounts
    raw["_ui_nonce"] = int(time.time())
    saved = _save_multi_config_raw(raw)
    return jsonify({"ok": True, "config": _serialize_multi_config(saved)})


@app.delete("/api/multi/account/<account_id>")
def api_delete_multi_account(account_id: str):
    account_id = _validate_account_id(account_id)
    raw = _load_multi_config_raw()
    accounts = raw.get("accounts") or []
    if not isinstance(accounts, list):
        accounts = []
    raw["accounts"] = [a for a in accounts if str(a.get("id") or "").strip() != account_id]
    raw["_ui_nonce"] = int(time.time())
    saved = _save_multi_config_raw(raw)
    return jsonify({"ok": True, "config": _serialize_multi_config(saved)})


def _job_is_running(job: Dict[str, Any]) -> bool:
    proc = job.get("process")
    try:
        return bool(proc and proc.poll() is None)
    except Exception:
        return False


@app.get("/api/multi/runner")
def api_multi_runner_status():
    with _multi_lock:
        job_id = _multi_runner_job_id
    if not job_id:
        return jsonify({"ok": True, "running": False, "job_id": None})

    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"ok": True, "running": False, "job_id": None})
        return jsonify(
            {
                "ok": True,
                "running": _job_is_running(job),
                "job_id": job_id,
                "status": job.get("status", "pending"),
                "pid": job.get("pid"),
            }
        )


@app.post("/api/multi/runner/start")
def api_multi_runner_start():
    global _multi_runner_job_id
    raw = _load_multi_config_raw()
    _save_multi_config_raw(raw)  # ensure file exists

    with _multi_lock:
        existing_job_id = _multi_runner_job_id

    if existing_job_id:
        with _jobs_lock:
            job = _jobs.get(existing_job_id)
            if job and _job_is_running(job):
                return jsonify({"ok": True, "job_id": existing_job_id, "message": "已在运行"})

    cmd = [sys.executable, os.path.join(ROOT_DIR, "PixivMultiRunner.py"), "--config", MULTI_CONFIG_PATH]
    job_id = uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "cmd": cmd,
            "type": "multi_runner",
            "status": "pending",
            "logs": deque(maxlen=LOG_LIMIT),
            "created_at": time.time(),
        }
    with _multi_lock:
        _multi_runner_job_id = job_id

    thread = threading.Thread(target=_run_job, args=(job_id, cmd), daemon=True)
    thread.start()
    return jsonify({"ok": True, "job_id": job_id})


@app.post("/api/multi/runner/stop")
def api_multi_runner_stop():
    with _multi_lock:
        job_id = _multi_runner_job_id
    if not job_id:
        return jsonify({"ok": False, "message": "未运行"}), 400

    with _jobs_lock:
        job = _jobs.get(job_id)
        proc = job.get("process") if job else None
    if proc and proc.poll() is None:
        try:
            _terminate_process_tree(proc)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "message": f"终止失败: {exc}"}), 500
        return jsonify({"ok": True, "message": "已终止（含子进程）", "job_id": job_id})
    return jsonify({"ok": False, "message": "任务已结束或不可用", "job_id": job_id}), 400


@app.post("/api/multi/refresh")
def api_multi_refresh_now():
    raw = _load_multi_config_raw()
    raw["_ui_nonce"] = int(time.time())
    saved = _save_multi_config_raw(raw)
    return jsonify({"ok": True, "message": "已触发刷新（等待 runner 轮询）", "config": _serialize_multi_config(saved)})


@app.get("/api/multi/status")
def api_multi_status():
    raw = _load_multi_config_raw()
    runtime_dir = _get_multi_runtime_dir(raw)
    status_path = os.path.join(runtime_dir, "status.json")
    if not os.path.isfile(status_path):
        return jsonify({"ok": False, "message": "status.json 不存在（请先启动 runner）"}), 404
    try:
        with open(status_path, "r", encoding="utf-8") as fp:
            data = json.load(fp) or {}
        return jsonify({"ok": True, "status": data})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": f"读取 status.json 失败: {exc}"}), 500


@app.get("/api/multi/db/stats")
def api_multi_db_stats():
    raw = _load_multi_config_raw()
    db_path = _get_multi_db_path(raw)
    if not os.path.isfile(db_path):
        return jsonify(
            {
                "ok": True,
                "exists": False,
                "db_path": db_path,
                "stats": {"member_count": 0, "image_count": 0, "url_count": 0},
            }
        )

    import sqlite3

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            cur = conn.cursor()

            def _count(table: str) -> int:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    row = cur.fetchone()
                    return int(row[0]) if row else 0
                except sqlite3.OperationalError:
                    return 0

            stats = {
                "member_count": _count("pixiv_follow_member"),
                "image_count": _count("pixiv_follow_image"),
                "url_count": _count("pixiv_follow_image_url"),
            }
            cur.close()
        finally:
            conn.close()
        return jsonify({"ok": True, "exists": True, "db_path": db_path, "stats": stats})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": f"db busy: {exc}"}), 503


@app.get("/api/multi/export")
def api_multi_export_all_urls():
    """
    Export ALL URLs from multi-runner DB as a plain text file (one URL per line).
    This is intentionally separate from single-thread follow export to avoid confusion.
    """
    raw = _load_multi_config_raw()
    db_path = _get_multi_db_path(raw)
    kind = (request.args.get("kind") or "regular").strip().lower()
    if kind not in {"regular", "original"}:
        kind = "regular"

    # Best-effort: load base config to apply mirror host rewriting (same behavior as other exports).
    base_cfg_path = _get_multi_base_config_path(raw)
    base_cfg: Optional[PixivConfig.PixivConfig] = None
    try:
        cfg = PixivConfig.PixivConfig()
        if os.path.isfile(base_cfg_path):
            cfg.loadConfig(base_cfg_path)
        base_cfg = cfg
    except Exception:
        base_cfg = None

    def _maybe_proxy(url: str) -> str:
        if not url:
            return ""
        if base_cfg is None:
            return url
        return _proxy(url, base_cfg)

    filename = f"pixiv_multi_all_{kind}.txt"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}

    # If DB does not exist yet, return an empty file instead of a JSON error (nicer UX for direct download button).
    if not os.path.isfile(db_path):
        return Response("", mimetype="text/plain; charset=utf-8", headers=headers)

    import sqlite3

    def _generate() -> Iterable[str]:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    """SELECT fu.original_url, fu.regular_url
                         FROM pixiv_follow_image_url fu
                         JOIN pixiv_follow_image fi ON fi.image_id = fu.image_id
                        ORDER BY fi.member_id ASC, fi.image_id DESC, fu.page_index ASC"""
                )
            except sqlite3.OperationalError:
                # Table missing or schema not initialized yet.
                return

            while True:
                rows = cur.fetchmany(2000)
                if not rows:
                    break
                for (ori, reg) in rows:
                    url = (reg if kind == "regular" else ori) or ""
                    url = _maybe_proxy(url)
                    if not url:
                        continue
                    yield f"{url}\n"
            cur.close()
        finally:
            conn.close()

    return Response(stream_with_context(_generate()), mimetype="text/plain; charset=utf-8", headers=headers)


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
            _terminate_process_tree(proc)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "message": f"终止失败: {exc}"}), 500
        return jsonify({"ok": True, "message": "已终止（含子进程）"})
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
