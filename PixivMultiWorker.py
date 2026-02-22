#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multi-token worker process for PixivUtil2 (personal fork).

This worker:
- Loads a per-account config.ini (cookie / proxy / delay)
- Logs in with cookie
- Reads assigned member IDs from a file (hot-reload)
- Downloads members in a loop (hot-reload config & assignments, no restart required)
"""

from __future__ import annotations

import argparse
import datetime
import os
import random
import re
import sqlite3
import signal
import sys
import time
from typing import Iterable, List, Set

from colorama import Fore, Style
import platform

import common.PixivBrowserFactory as PixivBrowserFactory
import common.PixivConfig as PixivConfig
import common.PixivConstant as PixivConstant
import common.PixivHelper as PixivHelper
from common.PixivAppApi import PixivAppApiClient, PixivAppApiError
from PixivDBManager import PixivDBManager
from common.PixivException import PixivException
import handler.PixivArtistHandler as PixivArtistHandler
from model.PixivTags import PixivTags


__config__ = PixivConfig.PixivConfig()
__dbManager__ = None
__br__: PixivBrowserFactory.PixivBrowser = None
__app_api__: PixivAppApiClient = None  # type: ignore[assignment]
__app_api_key = None
__worker_id__: str = ""
__worker_config_path__: str = ""
__last_reported_refresh_token: str = ""
__last_reported_proxy_fail_at: float = 0.0
__rate_limit_attempt: int = 0

DEBUG_SKIP_PROCESS_IMAGE = False
DEBUG_SKIP_DOWNLOAD_IMAGE = False

__blacklistTags = list()
__blacklistMembers = list()
__blacklistTitles = list()
__suppressTags = list()

UTF8_FS = None
ERROR_CODE = 0
__errorList = list()
__seriesDownloaded = []

start_iv = False
dfilename = ""
if platform.system() == "Windows":
    platform_encoding = "utf-8-sig"
else:
    platform_encoding = "utf-8"

_stop_requested = False
WORKER_MODE = "index_urls"  # default for multi-token runner; can be overridden by CLI args


def _atomic_write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(text)
    os.replace(tmp, path)


def _atomic_write_json(path: str, payload: dict) -> None:
    import json

    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _redact_secrets(text: str) -> str:
    """
    Best-effort redaction for logs/status files (avoid leaking proxy credentials).
    """
    s = str(text or "")
    # Hide basic-auth in URLs: scheme://user:pass@host -> scheme://***:***@host
    s = re.sub(r"//[^/@\\s:]+:[^@\\s]+@", "//***:***@", s)
    return s


def _runtime_dir_from_worker_config(config_path: str) -> str:
    cfg_dir = os.path.dirname(os.path.abspath(config_path))
    return os.path.abspath(os.path.join(cfg_dir, os.pardir))


def _events_dir() -> str:
    base = _runtime_dir_from_worker_config(__worker_config_path__ or ".")
    path = os.path.join(base, "events")
    os.makedirs(path, exist_ok=True)
    return path


def _workers_dir() -> str:
    base = _runtime_dir_from_worker_config(__worker_config_path__ or ".")
    path = os.path.join(base, "workers")
    os.makedirs(path, exist_ok=True)
    return path


def _progress_path(worker_id: str) -> str:
    wid = (worker_id or "worker").strip()
    safe_id = "".join(ch for ch in wid if ch.isalnum() or ch in "._-")[:64] or "worker"
    return os.path.join(_workers_dir(), f"{safe_id}.json")


def _report_rotated_refresh_token(new_refresh_token: str) -> None:
    global __last_reported_refresh_token
    rt = (new_refresh_token or "").strip()
    if not rt or rt == __last_reported_refresh_token:
        return

    wid = (__worker_id__ or "worker").strip()
    safe_id = "".join(ch for ch in wid if ch.isalnum() or ch in "._-")[:64] or "worker"
    path = os.path.join(_events_dir(), f"refresh_token_{safe_id}.txt")
    _atomic_write_text(path, rt + "\n")
    __last_reported_refresh_token = rt


def _should_report_proxy_failure(ex: BaseException) -> bool:
    proxy_addr = str(getattr(__config__, "proxyAddress", "") or "").strip()
    use_proxy = bool(getattr(__config__, "useProxy", False)) and bool(proxy_addr)
    if not use_proxy:
        return False

    if isinstance(ex, PixivAppApiError):
        if ex.status_code is None:
            return True
        if int(ex.status_code) in {407}:
            return True
        body = str(ex.body or "").lower()
        if "proxy" in body and ("407" in body or "authentication" in body or "connect" in body):
            return True
        return False

    msg = str(ex).lower()
    if "proxy" in msg and ("407" in msg or "authentication" in msg or "connect" in msg):
        return True
    return False


def _report_proxy_failure(ex: BaseException, *, context: str) -> None:
    global __last_reported_proxy_fail_at
    now = time.time()
    # Avoid spamming the runner if we're stuck with a dead proxy.
    if __last_reported_proxy_fail_at and (now - __last_reported_proxy_fail_at) < 20:
        return

    wid = (__worker_id__ or "worker").strip()
    safe_id = "".join(ch for ch in wid if ch.isalnum() or ch in "._-")[:64] or "worker"

    proxy_addr = str(getattr(__config__, "proxyAddress", "") or "").strip()
    status_code = None
    body = ""
    if isinstance(ex, PixivAppApiError):
        status_code = ex.status_code
        body = str(ex.body or "")[:300]
    payload = {
        "schema": 1,
        "ts": int(now),
        "account_id": safe_id,
        "proxy": proxy_addr,
        "context": str(context or ""),
        "status_code": status_code,
        "error": str(ex)[:300],
        "body": body,
    }

    path = os.path.join(_events_dir(), f"proxy_fail_{safe_id}.json")
    _atomic_write_json(path, payload)
    __last_reported_proxy_fail_at = now


def _pixiv_rate_limit_backoff_seconds(*, attempt: int) -> int:
    attempt_i = int(attempt)
    if attempt_i <= 0:
        return 0

    schedule = {
        1: 60,
        2: 5 * 60,
        3: 15 * 60,
        4: 60 * 60,
        5: 6 * 60 * 60,
    }
    if attempt_i in schedule:
        return int(schedule[attempt_i])

    base = 6 * 60 * 60
    seconds = base * (2 ** (attempt_i - 5))
    return int(min(seconds, 24 * 60 * 60))


def _is_pixiv_rate_limited(ex: PixivAppApiError) -> bool:
    if ex.status_code == 429:
        return True
    if ex.status_code == 403 and "rate limit" in str(ex.body or "").lower():
        return True
    return False


def _request_stop(*_args):  # pragma: no cover
    global _stop_requested
    _stop_requested = True


def _install_signal_handlers():  # pragma: no cover
    try:
        signal.signal(signal.SIGINT, _request_stop)
        signal.signal(signal.SIGTERM, _request_stop)
    except Exception:
        # Some Windows/Python combinations might not support all signals.
        pass


def set_console_title(title: str = "") -> None:
    try:
        base = f"PixivDownloader {PixivConstant.PIXIVUTIL_VERSION}"
        PixivHelper.set_console_title(f"{base} {title}".strip())
    except Exception:
        pass


def _read_id_list(path: str) -> List[int]:
    ids: List[int] = []
    if not path or not os.path.isfile(path):
        return ids
    with PixivHelper.open_text_file(path) as reader:
        for line in reader:
            s = (line or "").strip()
            if not s or s.startswith("#"):
                continue
            # Allow "123456 folder" format; take first token.
            parts = s.split()
            if not parts:
                continue
            if parts[0].isdigit():
                ids.append(int(parts[0]))
    return ids


def _read_lists():
    global __blacklistTags, __blacklistMembers, __blacklistTitles, __suppressTags

    if getattr(__config__, "useBlacklistTags", False):
        __blacklistTags = PixivTags.parseTagsList("blacklist_tags.txt")
        PixivHelper.print_and_log("info", f"Using Blacklist Tags: {len(__blacklistTags)} items.")

    if getattr(__config__, "useBlacklistMembers", False):
        __blacklistMembers = PixivTags.parseTagsList("blacklist_members.txt")
        PixivHelper.print_and_log("info", f"Using Blacklist Members: {len(__blacklistMembers)} members.")

    if getattr(__config__, "useBlacklistTitles", False):
        __blacklistTitles = PixivTags.parseTagsList("blacklist_titles.txt")
        PixivHelper.print_and_log("info", f"Using Blacklist Titles: {len(__blacklistTitles)} items.")

    if getattr(__config__, "useSuppressTags", False):
        __suppressTags = PixivTags.parseTagsList("suppress_tags.txt")
        PixivHelper.print_and_log("info", f"Using Suppress Tags: {len(__suppressTags)} items.")


def _load_config(config_path: str):
    __config__.loadConfig(path=config_path)
    PixivHelper.set_config(__config__)
    PixivHelper.set_log_level(__config__.logLevel)


def _update_download_list_path(worker_id: str) -> None:
    """
    Some download paths/logics expect caller.dfilename to exist even when not writing lists.
    Keep a per-worker path to avoid contention.
    """
    global dfilename
    base_dir = getattr(__config__, "downloadListDirectory", ".") or "."
    try:
        base_dir = os.path.abspath(base_dir)
    except Exception:
        base_dir = "."
    today = datetime.date.today().strftime("%Y-%m-%d")
    safe_id = "".join(ch for ch in str(worker_id or "worker") if ch.isalnum() or ch in "._-")[:32] or "worker"
    dfilename = os.path.join(base_dir, f"Downloaded_on_{today}.{safe_id}.txt")


def _ensure_db():
    global __dbManager__
    if __dbManager__ is None:
        __dbManager__ = PixivDBManager(root_directory=__config__.rootDirectory, target=__config__.dbPath)
        # Multi-process workers may race on schema creation; retry on SQLITE_BUSY/LOCKED.
        last_exc = None
        for attempt in range(0, 8):
            try:
                __dbManager__.createDatabase()
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if isinstance(exc, sqlite3.OperationalError):
                    msg = str(exc).lower()
                    if "database is locked" in msg or "database table is locked" in msg or "database schema is locked" in msg:
                        time.sleep(0.05 * (2 ** attempt))
                        continue
                raise
        if last_exc is not None:
            raise last_exc


def _ensure_browser():
    global __br__
    if __br__ is None:
        __br__ = PixivBrowserFactory.getBrowser(config=__config__)
    else:
        # Reconfigure existing singleton for proxy/delay hot update.
        __br__ = PixivBrowserFactory.getBrowser(config=__config__)


def _login_with_cookie() -> bool:
    if not getattr(__config__, "cookie", ""):
        PixivHelper.print_and_log("error", "No cookie found in config, cannot login.")
        return False
    try:
        return bool(__br__.loginUsingCookie())
    except Exception as ex:
        PixivHelper.print_and_log("error", f"Login failed: {ex}")
        return False


def _ensure_app_api() -> PixivAppApiClient:
    global __app_api__, __app_api_key

    refresh_token = (getattr(__config__, "refresh_token", "") or "").strip()
    if not refresh_token:
        raise PixivException("No refresh_token found in config, cannot use App API.", errorCode=PixivException.OAUTH_LOGIN_ISSUE)

    proxy_addr = str(getattr(__config__, "proxyAddress", "") or "").strip()
    use_proxy = bool(getattr(__config__, "useProxy", False)) and bool(proxy_addr)
    proxies = __config__.proxy if use_proxy else None
    verify_ssl = bool(getattr(__config__, "enableSSLVerification", True))
    timeout_sec = float(getattr(__config__, "timeout", 60))

    key = (refresh_token, proxy_addr, verify_ssl, timeout_sec)
    if __app_api__ is None or __app_api_key != key:
        __app_api__ = PixivAppApiClient(
            refresh_token=refresh_token,
            proxies=proxies,
            verify_ssl=verify_ssl,
            timeout_sec=timeout_sec,
            on_refresh_token_rotated=_report_rotated_refresh_token,
        )
        __app_api_key = key
    return __app_api__


def _iter_assigned_members(assigned_ids: Iterable[int], processed: Set[int]) -> Iterable[int]:
    for mid in assigned_ids:
        if mid in processed:
            continue
        yield mid


def _process_member(member_id: int, prefix: str) -> None:
    if str(member_id) in __blacklistMembers:
        PixivHelper.print_and_log("warn", f"Skipping member id: {member_id} by blacklist_members.txt.")
        return

    item = __dbManager__.selectMemberByMemberId2(member_id)
    PixivArtistHandler.process_member(
        sys.modules[__name__],
        __config__,
        member_id,
        user_dir=getattr(item, "path", "") or "",
        title_prefix=prefix,
    )

    try:
        __br__.clear_history()
    except Exception:
        pass


def _index_member_urls(member_id: int, prefix: str) -> None:
    """
    URL-only mode:
    - No image downloads
    - Uses refresh_token (Pixiv App API) to fetch illust details and store urls into DB
    """
    if str(member_id) in __blacklistMembers:
        PixivHelper.print_and_log("warn", f"{prefix}Skipping member id: {member_id} by blacklist_members.txt.")
        return

    client = _ensure_app_api()
    try:
        # Ensure OAuth works early so we can failover proxies quickly.
        client.get_access_token()
    except PixivAppApiError as ex:
        if _should_report_proxy_failure(ex):
            _report_proxy_failure(ex, context="oauth_refresh")
        raise
    except Exception as ex:  # noqa: BLE001
        if _should_report_proxy_failure(ex):
            _report_proxy_failure(ex, context="oauth_refresh")
        raise

    # Upsert member info (best-effort)
    try:
        detail = client.fetch_user_detail(user_id=int(member_id))
        info = client.parse_user_detail(detail)
        __dbManager__.upsertFollowMember(
            member_id=int(info.get("member_id") or member_id),
            name=info.get("name") or None,
            member_token=info.get("member_token") or None,
            avatar_url=info.get("avatar_url") or None,
            background_url=info.get("background_url") or None,
        )
    except Exception as ex:  # noqa: BLE001
        PixivHelper.print_and_log("warn", f"{prefix}Member detail failed for member_id={member_id}: {ex}")

    # Current works from App API
    try:
        current_ids = list(client.iter_user_illust_ids(user_id=int(member_id), page_sleep_sec=0.0))
    except PixivAppApiError as ex:
        PixivHelper.print_and_log("error", f"{prefix}List illusts failed for member_id={member_id}: {ex} (HTTP {ex.status_code})")
        if _should_report_proxy_failure(ex):
            _report_proxy_failure(ex, context="user_illusts")
        raise

    current_set = set(int(x) for x in current_ids)
    stored_ids = set(__dbManager__.selectFollowImageIdsByMember(int(member_id)))

    to_add = sorted(current_set - stored_ids, reverse=True)
    to_remove = sorted(stored_ids - current_set)

    if to_remove:
        PixivHelper.print_and_log("info", f"{prefix}Removing {len(to_remove)} deleted works for member_id={member_id}")
        for image_id in to_remove:
            __dbManager__.deleteFollowImage(int(image_id))

    # Resume support: images in DB but missing URL rows.
    try:
        to_fix = __dbManager__.selectFollowImageIdsNeedingUrlIndex(int(member_id))
    except Exception:  # noqa: BLE001
        to_fix = []
    to_fix = [int(x) for x in (to_fix or []) if int(x) in current_set]

    to_process: List[int] = list(to_add)
    for image_id in to_fix:
        if image_id in current_set and image_id not in to_process:
            to_process.append(int(image_id))

    total = len(to_process)
    PixivHelper.print_and_log("info", f"{prefix}Member {member_id}: to_process={total} (new={len(to_add)} repair={len(to_fix)})")

    delay = float(getattr(__config__, "downloadDelay", 0) or 0)
    consecutive_proxy_errors = 0
    for idx, illust_id in enumerate(to_process, start=1):
        if _stop_requested:
            break
        if idx == 1 or idx == total or idx % 10 == 0:
            PixivHelper.print_and_log("info", f"{prefix}Member {member_id}: progress {idx}/{total} (illust_id={illust_id})")

        try:
            detail = client.fetch_illust_detail(illust_id=int(illust_id))
            parsed = client.parse_illust_detail(detail)

            __dbManager__.upsertFollowImage(
                image_id=int(parsed.get("image_id") or illust_id),
                member_id=int(member_id),
                title=parsed.get("title") or None,
                caption=parsed.get("caption") or None,
                create_date=parsed.get("create_date") or None,
                page_count=int(parsed.get("page_count") or 1),
                mode=parsed.get("mode") or None,
                bookmark_count=parsed.get("bookmark_count"),
                like_count=None,
                view_count=parsed.get("view_count"),
            )

            url_rows = parsed.get("url_rows") or []
            if url_rows:
                __dbManager__.upsertFollowImageUrls(int(parsed.get("image_id") or illust_id), url_rows)
        except PixivAppApiError as ex:
            if _is_pixiv_rate_limited(ex):
                raise
            if _should_report_proxy_failure(ex):
                consecutive_proxy_errors += 1
                _report_proxy_failure(ex, context=f"illust_detail:{illust_id}")
                if consecutive_proxy_errors >= 3:
                    raise
            else:
                consecutive_proxy_errors = 0
            PixivHelper.print_and_log("warn", f"{prefix}Skip illust_id={illust_id}: {ex} (HTTP {ex.status_code})")
            continue
        except Exception as ex:  # noqa: BLE001
            consecutive_proxy_errors = 0
            PixivHelper.print_and_log("warn", f"{prefix}Skip illust_id={illust_id}: {ex}")
            continue

        if delay > 0:
            time.sleep(random.random() * delay)


def run_worker(*, worker_id: str, config_path: str, assign_path: str, poll_interval: float, mode: str) -> int:
    global ERROR_CODE
    global __worker_id__, __worker_config_path__
    global __rate_limit_attempt

    __worker_id__ = str(worker_id or "").strip()
    __worker_config_path__ = str(config_path or "").strip()

    mode = str(mode or "").strip().lower()
    if mode not in {"download", "index_urls"}:
        mode = "index_urls"

    if worker_id:
        # Split log files per worker to avoid contention.
        PixivConstant.PIXIVUTIL_LOG_FILE = f"pixivutil.{worker_id}.log"

    config_path = os.path.abspath(config_path)
    assign_path = os.path.abspath(assign_path)

    PixivHelper.print_and_log("info", f"[worker:{worker_id}] config={config_path}")
    PixivHelper.print_and_log("info", f"[worker:{worker_id}] assign={assign_path}")

    last_cfg_mtime = 0.0
    last_assign_mtime = 0.0
    last_progress_write_at = 0.0

    assigned_ids: List[int] = []
    processed: Set[int] = set()
    last_member_id: int = 0
    current_member_id: int = 0
    last_error: str = ""

    progress_path = _progress_path(worker_id)

    def _write_progress(*, force: bool = False) -> None:
        nonlocal last_progress_write_at, last_member_id, current_member_id, last_error
        now = time.time()
        if not force and last_progress_write_at and (now - last_progress_write_at) < 3.0:
            return
        last_progress_write_at = now
        safe_error = _redact_secrets(last_error or "")
        payload = {
            "schema": 1,
            "updated_at": int(now),
            "worker_id": str(worker_id),
            "pid": int(os.getpid()),
            "mode": str(mode),
            "assigned_total": int(len(assigned_ids or [])),
            "processed_count": int(len(processed)),
            "current_member_id": int(current_member_id or 0),
            "last_member_id": int(last_member_id or 0),
            "rate_limit_attempt": int(__rate_limit_attempt or 0),
            "last_error": str(safe_error)[:300],
        }
        try:
            _atomic_write_json(progress_path, payload)
        except Exception:
            pass

    while not _stop_requested:
        # Hot reload config.ini
        try:
            cfg_mtime = os.path.getmtime(config_path)
        except OSError:
            cfg_mtime = 0.0

        if cfg_mtime and cfg_mtime != last_cfg_mtime:
            last_cfg_mtime = cfg_mtime
            PixivHelper.print_and_log("info", f"[worker:{worker_id}] Reloading config...")
            _load_config(config_path)
            _update_download_list_path(worker_id)
            _read_lists()
            _ensure_db()
            # Worker mode is controlled by env/args (see main()).

        if __dbManager__ is None:
            # initial startup
            _load_config(config_path)
            _update_download_list_path(worker_id)
            _read_lists()
            _ensure_db()
            # Browser/App API will be initialized lazily based on mode.
            _write_progress(force=True)

        # Hot reload assignments
        try:
            assign_mtime = os.path.getmtime(assign_path)
        except OSError:
            assign_mtime = 0.0

        if assign_mtime and assign_mtime != last_assign_mtime:
            last_assign_mtime = assign_mtime
            assigned_ids = _read_id_list(assign_path)
            PixivHelper.print_and_log("info", f"[worker:{worker_id}] Loaded {len(assigned_ids)} assigned members.")
            _write_progress(force=True)

        if not assigned_ids:
            current_member_id = 0
            _write_progress()
            time.sleep(poll_interval)
            continue

        total = len(assigned_ids)
        for idx, member_id in enumerate(_iter_assigned_members(assigned_ids, processed), start=1):
            if _stop_requested:
                break
            prefix = f"[worker:{worker_id}] [{idx}/{total}] "
            try:
                current_member_id = int(member_id)
                if mode == "index_urls":
                    _index_member_urls(int(member_id), prefix)
                else:
                    _ensure_browser()
                    if not _login_with_cookie():
                        PixivHelper.print_and_log("warn", f"[worker:{worker_id}] Not logged in, will retry...")
                        time.sleep(max(2.0, poll_interval))
                        break
                    _process_member(int(member_id), prefix)
                processed.add(int(member_id))
                last_member_id = int(member_id)
                current_member_id = 0
                last_error = ""
                __rate_limit_attempt = 0
                _write_progress(force=True)
            except KeyboardInterrupt:
                _request_stop()
                break
            except PixivAppApiError as ex:
                PixivHelper.print_and_log("error", f"{prefix}App API error member_id={member_id}: {ex} (HTTP {ex.status_code})")
                last_error = f"PixivAppApiError HTTP {ex.status_code}: {ex}"
                if _is_pixiv_rate_limited(ex):
                    __rate_limit_attempt += 1
                    wait_s = _pixiv_rate_limit_backoff_seconds(attempt=__rate_limit_attempt)
                    PixivHelper.print_and_log("warn", f"{prefix}Pixiv rate limited, backoff {wait_s}s (attempt={__rate_limit_attempt})")
                    _write_progress(force=True)
                    time.sleep(min(float(wait_s), 6 * 60 * 60))
                    break
                if _should_report_proxy_failure(ex):
                    _report_proxy_failure(ex, context="member")
                    PixivHelper.print_and_log("warn", f"{prefix}Proxy failure detected, waiting for runner override...")
                    _write_progress(force=True)
                    time.sleep(max(2.0, poll_interval))
                    break
                ERROR_CODE = getattr(ex, "errorCode", -1)
            except PixivException as ex:
                PixivHelper.print_and_log("error", f"{prefix}Failed member_id={member_id}: {ex}")
                last_error = f"PixivException: {ex}"
                _write_progress(force=True)
                # Keep going for other members.
                ERROR_CODE = getattr(ex, "errorCode", -1)
            except Exception as ex:
                if _should_report_proxy_failure(ex):
                    _report_proxy_failure(ex, context="member")
                    PixivHelper.print_and_log("error", f"{prefix}Proxy failure detected, waiting for runner override...")
                    last_error = f"Proxy failure: {ex}"
                    _write_progress(force=True)
                    time.sleep(max(2.0, poll_interval))
                    break
                PixivHelper.print_and_log("error", f"{prefix}Failed member_id={member_id}: {ex}")
                last_error = f"Error: {ex}"
                _write_progress(force=True)
                ERROR_CODE = getattr(ex, "errorCode", -1)

        _write_progress()
        time.sleep(poll_interval)

    try:
        if __dbManager__ is not None:
            __dbManager__.close()
    except Exception:
        pass

    return int(ERROR_CODE or 0)


def main() -> int:  # pragma: no cover
    _install_signal_handlers()
    parser = argparse.ArgumentParser(description="Pixiv multi-token worker (hot reload)")
    parser.add_argument("--worker-id", required=True, help="Worker/account id (used for log file suffix)")
    parser.add_argument("--config", required=True, help="Per-worker config.ini path")
    parser.add_argument("--assign", required=True, help="Assigned member IDs file path")
    parser.add_argument("--poll", type=float, default=10.0, help="Poll interval seconds for hot reload")
    parser.add_argument("--mode", choices=["download", "index_urls"], default=WORKER_MODE, help="Worker mode")
    args = parser.parse_args()

    try:
        return run_worker(
            worker_id=str(args.worker_id),
            config_path=str(args.config),
            assign_path=str(args.assign),
            poll_interval=float(args.poll),
            mode=str(args.mode),
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    if not sys.version_info >= (3, 8):
        print("Require Python 3.8++")
        sys.exit(2)
    print(f"{Fore.CYAN}{Style.BRIGHT}PixivMultiWorker starting...{Style.RESET_ALL}")
    sys.exit(main())
