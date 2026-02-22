#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PixivUtil2 Multi-Account Runner (personal fork)

Goals:
- Import multiple Pixiv account tokens (cookie / refresh_token)
- Fetch each account's followed artists, merge + dedupe
- Bind each account to a stable proxy from a proxy pool (easy_proxies supported)
- Concurrent downloads via multi-process workers
- Hot reload: update tokens / proxy pool / assignments without restarting runner

Run:
  python PixivMultiRunner.py --config multi_config.json
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse

import requests

from common.PixivAppApi import PixivAppApiClient, PixivAppApiError
from common.EasyProxiesClient import EasyProxiesClient, EasyProxiesConfig
from common.RequestsProxyAdapter import mount_proxy_host_header_adapter
from common.ProxyTester import ProxyTestResult, rank_proxies
from common.ProxyUtils import ProxyParts, mask_proxy_url, normalize_proxy_url, parse_proxy_url


DEFAULT_CONFIG_PATH = "multi_config.json"
DEFAULT_BASE_CONFIG = "config.ini"
DEFAULT_RUNTIME_DIR = ".multi_runtime"
DEFAULT_PROXY_BINDING_SALT = "pixivutil2-multi-proxy-bindings"
DEFAULT_MULTI_DB_FILENAME = "db.multi.sqlite"


def _now() -> float:
    return time.time()


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fp:
        return json.load(fp) or {}


def _atomic_write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(text)
    os.replace(tmp, path)


def _atomic_write_text_if_changed(path: str, text: str) -> bool:
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fp:
                if fp.read() == text:
                    return False
    except Exception:
        pass
    _atomic_write_text(path, text)
    return True


def _stable_pick_index(seed: str, mod: int) -> int:
    digest = hashlib.md5(seed.encode("utf-8")).digest()
    n = int.from_bytes(digest[:8], "big", signed=False)
    return n % mod


def _fnv1a64(text: str) -> int:
    h = 14695981039346656037
    prime = 1099511628211
    for b in text.encode("utf-8"):
        h ^= b
        h = (h * prime) & 0xFFFFFFFFFFFFFFFF
    return h


def _rendezvous_proxy_order(*, token_key: str, proxy_keys: List[str], salt: str) -> List[str]:
    scored = [(_fnv1a64(f"{token_key}|{p}|{salt}"), p) for p in proxy_keys]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [p for _score, p in scored]


def _resolve_default_db_path_from_config(base_config_path: str) -> str:
    """
    Resolve the DB path PixivUtil2 would use for the given base config.

    If [Settings] dbPath is empty, PixivUtil2 typically uses "db.sqlite" in the repo root.
    """
    base_dir = os.path.dirname(os.path.abspath(base_config_path)) or "."
    parser = configparser.RawConfigParser()
    parser.optionxform = lambda option: option
    try:
        with open(base_config_path, "r", encoding="utf-8") as fp:
            parser.read_file(fp)
    except Exception:
        # Fall back to default in base dir.
        return os.path.join(base_dir, "db.sqlite")

    db_path = ""
    try:
        if parser.has_section("Settings") and parser.has_option("Settings", "dbPath"):
            db_path = str(parser.get("Settings", "dbPath") or "").strip()
    except Exception:
        db_path = ""
    if not db_path:
        db_path = "db.sqlite"
    if not os.path.isabs(db_path):
        db_path = os.path.abspath(os.path.join(base_dir, db_path))
    return db_path


def _load_follow_image_counts(db_path: str) -> Dict[int, int]:
    """
    Load per-artist illust counts from an existing SQLite DB (best-effort).

    Used for load-balancing: some artists have far more works than others.
    """
    if not db_path or not os.path.isfile(db_path):
        return {}
    try:
        import sqlite3

        conn = sqlite3.connect(db_path, timeout=2)
        try:
            cur = conn.cursor()
            try:
                cur.execute("""SELECT member_id, COUNT(*) FROM pixiv_follow_image GROUP BY member_id""")
                out = {}
                for row in cur.fetchall():
                    try:
                        out[int(row[0])] = int(row[1] or 0)
                    except Exception:
                        continue
                return out
            except sqlite3.OperationalError:
                return {}
            finally:
                cur.close()
        finally:
            conn.close()
    except Exception:
        return {}


def _proxy_override_ttl_seconds(*, attempt: int) -> int:
    attempt_i = int(attempt)
    if attempt_i <= 0:
        return 0

    schedule = {
        1: 20 * 60,
        2: 60 * 60,
        3: 6 * 60 * 60,
    }
    if attempt_i in schedule:
        return int(schedule[attempt_i])

    base = 6 * 60 * 60
    seconds = base * (2 ** (attempt_i - 3))
    return int(min(seconds, 24 * 60 * 60))


def _cookie_user_id(cookie: str) -> Optional[int]:
    # Pixiv PHPSESSID is usually "<user_id>_<random>"
    if not cookie:
        return None
    head = cookie.split("_", 1)[0]
    if head.isdigit():
        try:
            return int(head)
        except Exception:
            return None
    return None


def _requests_proxies(proxy_url: str) -> Optional[dict]:
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def _resolve_my_user_id(
    *,
    session: requests.Session,
    cookie: str,
    timeout: float,
    proxies: Optional[dict],
) -> Optional[int]:
    """
    Best-effort resolve logged-in user id for a cookie that doesn't start with "<id>_".
    This mirrors PixivBrowserFactory.PixivBrowser.getMyId() parsing logic.
    """
    try:
        resp = session.get(
            "https://www.pixiv.net/",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml"},
            cookies={"PHPSESSID": cookie},
            timeout=timeout,
            proxies=proxies,
        )
        if resp.status_code != 200:
            return None
        html = resp.text or ""
    except Exception:
        return None

    # Next.js: prefer __NEXT_DATA__
    try:
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
        if m:
            next_data = json.loads(m.group(1))
            page_props = (next_data.get("props") or {}).get("pageProps") or {}

            # serverSerializedPreloadedState is a JSON string
            ssp = page_props.get("serverSerializedPreloadedState", "")
            if isinstance(ssp, str) and ssp:
                try:
                    ssp_obj = json.loads(ssp)
                    self_obj = ((ssp_obj.get("userData") or {}).get("self") or {})
                    uid = self_obj.get("id")
                    if isinstance(uid, str) and uid.isdigit():
                        return int(uid)
                    if isinstance(uid, int):
                        return uid
                except Exception:
                    pass

            ga = page_props.get("gaUserData") or {}
            uid = ga.get("userId")
            if isinstance(uid, str) and uid.isdigit():
                return int(uid)
            if isinstance(uid, int):
                return uid
    except Exception:
        pass

    # Legacy fallbacks
    patterns = [
        r'pixiv\.user\.id = "(\d+)";',
        r"user_id:\s?\'(\d+)\'",
        r"_gaq\.push\(\['_setCustomVar', 6, 'user_id', \"(\d+)\", 1\]\);",
        r"var dataLayer = .*user_id: *['\"](\d+)['\"]",
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m and m.group(1).isdigit():
            return int(m.group(1))

    return None


def _fetch_following_user_ids(
    *,
    session: requests.Session,
    member_id: int,
    cookie: str,
    rest: str,
    lang: str,
    timeout: float,
    proxies: Optional[dict],
) -> Set[int]:
    limit = 48
    offset = 0
    out: Set[int] = set()

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.pixiv.net/",
    }

    while True:
        url = (
            f"https://www.pixiv.net/ajax/user/{member_id}/following"
            f"?offset={offset}&limit={limit}&rest={rest}&lang={lang}"
        )
        resp = session.get(
            url,
            headers=headers,
            cookies={"PHPSESSID": cookie},
            timeout=timeout,
            proxies=proxies,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"following list failed: {resp.status_code} {resp.text}")
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(f"following list error: {data.get('message') or data}")

        users = (data.get("body") or {}).get("users") or []
        if not users:
            break
        for u in users:
            if u.get("isAdContainer"):
                continue
            uid = u.get("userId")
            if isinstance(uid, str) and uid.isdigit():
                out.add(int(uid))
            elif isinstance(uid, int):
                out.add(uid)

        offset += limit

    return out


@dataclass
class Account:
    account_id: str
    cookie: str
    refresh_token: str = ""
    download_delay: Optional[int] = None
    enabled: bool = True  # start worker / fetch urls
    follow_source: bool = True  # fetch following list source


@dataclass
class ProxyBinding:
    primary: str = ""
    override: str = ""
    override_until: float = 0.0  # epoch seconds


@dataclass
class RunnerConfig:
    base_config_path: str
    runtime_dir: str
    accounts: List[Account]

    bookmark_flag: str = "n"  # PixivUtil2 style: y/n/o
    follow_lang: str = "en"
    follow_refresh_interval_sec: int = 1800
    follow_timeout_sec: float = 20.0

    worker_poll_interval_sec: float = 10.0
    worker_mode: str = "index_urls"  # index_urls | download

    proxy_source: str = "easy_proxies"  # easy_proxies | none
    proxy_refresh_interval_sec: int = 300
    proxy_test_url: str = "https://www.pixiv.net/robots.txt"
    proxy_test_timeout_sec: float = 8.0
    proxy_test_concurrency: int = 20
    proxy_test_attempts: int = 2
    proxy_top_n: int = 20
    proxy_static_list: List[str] = None
    proxy_file: str = ""
    proxy_max_tokens_per_proxy: int = 2
    proxy_bindings_strict: bool = False
    proxy_binding_salt: str = DEFAULT_PROXY_BINDING_SALT

    easy_proxies_base_url: str = "http://127.0.0.1:9090"
    easy_proxies_password: str = ""
    easy_proxies_timeout_sec: float = 10.0
    easy_proxies_verify_ssl: bool = True
    easy_proxies_host_override: str = ""


def _load_runner_config(path: str) -> RunnerConfig:
    raw = _read_json(path)
    base_config_path = raw.get("base_config") or DEFAULT_BASE_CONFIG
    runtime_dir = raw.get("runtime_dir") or DEFAULT_RUNTIME_DIR

    accounts_raw = raw.get("accounts") or []
    accounts: List[Account] = []
    for a in accounts_raw:
        account_id = str(a.get("id") or "").strip()
        cookie = str(a.get("cookie") or "").strip()
        if not account_id:
            continue
        accounts.append(
            Account(
                account_id=account_id,
                cookie=cookie,
                refresh_token=str(a.get("refresh_token") or "").strip(),
                download_delay=(int(a["downloadDelay"]) if "downloadDelay" in a and str(a["downloadDelay"]).isdigit() else None),
                enabled=bool(a.get("enabled") if "enabled" in a else True),
                follow_source=bool(a.get("follow_source") if "follow_source" in a else True),
            )
        )

    follow = raw.get("follow") or {}
    proxy = raw.get("proxy_pool") or {}
    easy = proxy.get("easy_proxies") or {}
    worker = raw.get("worker") or {}

    cfg = RunnerConfig(
        base_config_path=str(base_config_path),
        runtime_dir=str(runtime_dir),
        accounts=accounts,
        bookmark_flag=str(follow.get("bookmark_flag") or "n").strip().lower(),
        follow_lang=str(follow.get("lang") or "en").strip().lower(),
        follow_refresh_interval_sec=int(follow.get("refresh_interval_sec") or 1800),
        follow_timeout_sec=float(follow.get("timeout_sec") or 20.0),
        worker_poll_interval_sec=float(worker.get("poll_interval_sec") or 10.0),
        worker_mode=str(worker.get("mode") or "index_urls").strip().lower(),
        proxy_source=str(proxy.get("source") or "easy_proxies").strip().lower(),
        proxy_refresh_interval_sec=int(proxy.get("refresh_interval_sec") or 300),
        proxy_test_url=str((proxy.get("test") or {}).get("target_url") or "https://www.pixiv.net/robots.txt"),
        proxy_test_timeout_sec=float((proxy.get("test") or {}).get("timeout_sec") or 8.0),
        proxy_test_concurrency=int((proxy.get("test") or {}).get("concurrency") or 20),
        proxy_test_attempts=int((proxy.get("test") or {}).get("attempts") or 2),
        proxy_top_n=int((proxy.get("test") or {}).get("top_n") or 20),
        proxy_static_list=list(proxy.get("proxies") or []),
        proxy_file=str(proxy.get("proxies_file") or "").strip(),
        proxy_max_tokens_per_proxy=int(proxy.get("max_tokens_per_proxy") or proxy.get("max_accounts_per_proxy") or 2),
        proxy_bindings_strict=bool(proxy.get("bindings_strict") if "bindings_strict" in proxy else proxy.get("strict") if "strict" in proxy else False),
        proxy_binding_salt=str(proxy.get("binding_salt") or DEFAULT_PROXY_BINDING_SALT).strip() or DEFAULT_PROXY_BINDING_SALT,
        easy_proxies_base_url=str(easy.get("base_url") or "http://127.0.0.1:9090").strip(),
        easy_proxies_password=str(easy.get("password") or ""),
        easy_proxies_timeout_sec=float(easy.get("timeout_sec") or 10.0),
        easy_proxies_verify_ssl=bool(easy.get("verify_ssl") if "verify_ssl" in easy else True),
        easy_proxies_host_override=str(easy.get("host_override") or "").strip(),
    )

    if cfg.bookmark_flag not in {"y", "n", "o"}:
        cfg.bookmark_flag = "n"
    if not cfg.follow_lang:
        cfg.follow_lang = "en"
    if cfg.follow_refresh_interval_sec < 30:
        cfg.follow_refresh_interval_sec = 30
    if cfg.proxy_refresh_interval_sec < 30:
        cfg.proxy_refresh_interval_sec = 30
    if cfg.worker_poll_interval_sec < 2:
        cfg.worker_poll_interval_sec = 2
    if cfg.worker_mode not in {"index_urls", "download"}:
        cfg.worker_mode = "index_urls"
    if cfg.proxy_test_concurrency < 1:
        cfg.proxy_test_concurrency = 1
    if cfg.proxy_test_attempts < 1:
        cfg.proxy_test_attempts = 1
    if cfg.proxy_test_attempts > 10:
        cfg.proxy_test_attempts = 10
    if cfg.proxy_max_tokens_per_proxy < 1:
        cfg.proxy_max_tokens_per_proxy = 1
    if cfg.proxy_max_tokens_per_proxy > 1000:
        cfg.proxy_max_tokens_per_proxy = 1000
    if not cfg.proxy_binding_salt:
        cfg.proxy_binding_salt = DEFAULT_PROXY_BINDING_SALT

    return cfg


class MultiRunner:
    def __init__(self, *, config_path: str):
        self._config_path = os.path.abspath(config_path)
        self._cfg_mtime: float = 0.0
        self._cfg: Optional[RunnerConfig] = None
        self._config_dir = os.path.dirname(self._config_path) or "."

        self._bindings_path: Optional[str] = None
        self._bindings: Dict[str, ProxyBinding] = {}  # account_id -> binding

        self._proxy_ranked: List[ProxyTestResult] = []
        self._proxy_ranked_urls: List[str] = []
        self._proxy_last_refresh: float = 0.0
        self._proxy_input_count: int = 0
        self._proxy_normalized_count: int = 0
        self._proxy_warning: str = ""
        self._proxy_export_error: str = ""
        self._follow_last_refresh: float = 0.0

        self._workers: Dict[str, subprocess.Popen] = {}
        self._last_assign_counts: Dict[str, int] = {}
        self._last_unique_artists: int = 0
        self._last_assignment_signature: str = ""
        self._proxy_override_attempts: Dict[str, int] = {}

    def _runtime(self, *parts: str) -> str:
        assert self._cfg is not None
        return os.path.join(self._cfg.runtime_dir, *parts)

    def _load_bindings(self) -> None:
        self._bindings_path = self._runtime("bindings.json")
        self._bindings = {}
        try:
            if os.path.isfile(self._bindings_path):
                data = _read_json(self._bindings_path)
                schema = int(data.get("schema") or 1)
                if schema >= 2 and isinstance(data.get("bindings"), dict):
                    for k, v in (data.get("bindings") or {}).items():
                        if not isinstance(k, str) or not isinstance(v, dict):
                            continue
                        self._bindings[k] = ProxyBinding(
                            primary=str(v.get("primary") or "").strip(),
                            override=str(v.get("override") or "").strip(),
                            override_until=float(v.get("override_until") or 0.0),
                        )
                else:
                    for k, v in (data.get("account_proxy") or {}).items():
                        if not isinstance(k, str):
                            continue
                        self._bindings[k] = ProxyBinding(primary=str(v or "").strip())
        except Exception:
            self._bindings = {}

    def _save_bindings(self) -> None:
        if not self._bindings_path:
            return
        payload = {
            "schema": 2,
            "updated_at": int(_now()),
            "bindings": {
                k: {"primary": b.primary, "override": b.override, "override_until": float(b.override_until or 0.0)}
                for (k, b) in self._bindings.items()
            },
        }
        _atomic_write_text_if_changed(self._bindings_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def _effective_proxy(self, account_id: str, *, now_epoch: Optional[float] = None) -> str:
        now_epoch = float(_now() if now_epoch is None else now_epoch)
        b = self._bindings.get(account_id)
        if b is None:
            return ""
        if b.override and float(b.override_until or 0.0) > now_epoch:
            return b.override
        return b.primary or ""

    def _write_worker_config(
        self,
        *,
        base_config_path: str,
        out_path: str,
        cookie: str,
        refresh_token: str,
        proxy_url: str,
        download_delay: Optional[int],
        db_path: str,
    ) -> None:
        parser = configparser.RawConfigParser()
        parser.optionxform = lambda option: option  # keep case
        with open(base_config_path, "r", encoding="utf-8") as fp:
            parser.read_file(fp)

        def setv(section: str, key: str, value: str) -> None:
            if not parser.has_section(section):
                parser.add_section(section)
            parser.set(section, key, value)

        setv("Authentication", "cookie", cookie or "")
        setv("Authentication", "refresh_token", refresh_token or "")

        if proxy_url:
            setv("Network", "useProxy", "True")
            setv("Network", "proxyAddress", proxy_url)
        else:
            setv("Network", "useProxy", "False")
            setv("Network", "proxyAddress", "")

        if download_delay is not None:
            setv("Network", "downloadDelay", str(int(download_delay)))

        # Use a dedicated DB for multi-runner to avoid mixing with single-account runs.
        if db_path:
            setv("Settings", "dbPath", str(db_path))

        tmp = f"{out_path}.tmp"
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fp:
            parser.write(fp)
        os.replace(tmp, out_path)

    def _write_worker_config_if_changed(
        self,
        *,
        base_config_path: str,
        out_path: str,
        cookie: str,
        refresh_token: str,
        proxy_url: str,
        download_delay: Optional[int],
        db_path: str,
    ) -> bool:
        # Generate text and compare to avoid rewriting every loop (workers hot-reload on mtime changes).
        parser = configparser.RawConfigParser()
        parser.optionxform = lambda option: option
        with open(base_config_path, "r", encoding="utf-8") as fp:
            parser.read_file(fp)

        def setv(section: str, key: str, value: str) -> None:
            if not parser.has_section(section):
                parser.add_section(section)
            parser.set(section, key, value)

        setv("Authentication", "cookie", cookie or "")
        setv("Authentication", "refresh_token", refresh_token or "")

        if proxy_url:
            setv("Network", "useProxy", "True")
            setv("Network", "proxyAddress", proxy_url)
        else:
            setv("Network", "useProxy", "False")
            setv("Network", "proxyAddress", "")

        if download_delay is not None:
            setv("Network", "downloadDelay", str(int(download_delay)))

        # Use a dedicated DB for multi-runner to avoid mixing with single-account runs.
        if db_path:
            setv("Settings", "dbPath", str(db_path))

        from io import StringIO

        buf = StringIO()
        parser.write(buf)
        text = buf.getvalue()

        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        return _atomic_write_text_if_changed(out_path, text)

    def _ensure_runtime_dirs(self) -> None:
        os.makedirs(self._runtime("configs"), exist_ok=True)
        os.makedirs(self._runtime("assignments"), exist_ok=True)
        os.makedirs(self._runtime("events"), exist_ok=True)
        os.makedirs(self._runtime("workers"), exist_ok=True)

    def _persist_account_refresh_token(self, *, account_id: str, refresh_token: str, source: str) -> bool:
        """
        Persist rotated refresh_token back into multi_config.json (and in-memory cfg).

        This is required because Pixiv may rotate refresh_token on OAuth refresh, and the old
        refresh_token may stop working after some time.
        """
        if not self._cfg:
            return False

        aid = str(account_id or "").strip()
        rt = str(refresh_token or "").strip()
        if not aid or not rt:
            return False

        # Update in-memory config first.
        mem_changed = False
        for acc in self._cfg.accounts:
            if acc.account_id != aid:
                continue
            if acc.refresh_token == rt:
                break
            acc.refresh_token = rt
            mem_changed = True
            break

        # Update on-disk config (best-effort, merge into latest file).
        file_changed = False
        try:
            raw = _read_json(self._config_path)
            accounts = raw.get("accounts")
            if isinstance(accounts, list):
                for item in accounts:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("id") or "").strip() != aid:
                        continue
                    if str(item.get("refresh_token") or "").strip() == rt:
                        break
                    item["refresh_token"] = rt
                    raw["_ui_nonce"] = int(_now())
                    file_changed = True
                    break
            if file_changed:
                text = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
                _atomic_write_text_if_changed(self._config_path, text)
                try:
                    self._cfg_mtime = float(os.path.getmtime(self._config_path))
                except OSError:
                    pass
        except Exception as ex:
            print(f"[runner] persist refresh_token failed: account={aid} source={source} err={ex}")

        if mem_changed or file_changed:
            print(f"[runner] refresh_token rotated: account={aid} source={source}")
            return True
        return False

    def _consume_rotated_refresh_tokens(self) -> bool:
        """
        Worker processes report rotated refresh_token via:
          <runtime_dir>/events/refresh_token_<account_id>.txt
        """
        if not self._cfg:
            return False

        events_dir = self._runtime("events")
        try:
            names = os.listdir(events_dir)
        except Exception:
            return False

        changed = False
        for name in names:
            if not (name.startswith("refresh_token_") and name.endswith(".txt")):
                continue
            account_id = name[len("refresh_token_") : -len(".txt")]
            path = os.path.join(events_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    rt = (fp.read() or "").strip()
            except Exception:
                rt = ""
            try:
                os.remove(path)
            except Exception:
                pass
            if not rt:
                continue
            changed = self._persist_account_refresh_token(account_id=account_id, refresh_token=rt, source="worker") or changed
        return changed

    def _override_proxy_for_account(self, *, account_id: str, reason: str) -> bool:
        assert self._cfg is not None
        aid = str(account_id or "").strip()
        if not aid:
            return False

        ranked_urls = list(self._proxy_ranked_urls or [])
        if not ranked_urls:
            return False

        b = self._bindings.get(aid)
        if b is None:
            b = ProxyBinding()
            self._bindings[aid] = b

        current = self._effective_proxy(aid) or ""
        order = _rendezvous_proxy_order(token_key=aid, proxy_keys=ranked_urls, salt=str(self._cfg.proxy_binding_salt or DEFAULT_PROXY_BINDING_SALT))
        chosen = ""
        if current and b.override and current == b.override:
            # Avoid ping-ponging between primary <-> override when both are unhealthy.
            for cand in order:
                if cand and cand != current and cand != (b.primary or ""):
                    chosen = cand
                    break
        if not chosen:
            for cand in order:
                if cand and cand != current:
                    chosen = cand
                    break

        if not chosen or chosen == current:
            return False

        attempt = int(self._proxy_override_attempts.get(aid, 0)) + 1
        self._proxy_override_attempts[aid] = attempt
        ttl = _proxy_override_ttl_seconds(attempt=attempt)
        if ttl <= 0:
            return False

        b.override = chosen
        b.override_until = float(_now()) + float(ttl)
        self._save_bindings()

        reason_s = str(reason or "").strip().replace("\n", " ")
        if len(reason_s) > 160:
            reason_s = reason_s[:160] + "..."
        print(
            f"[runner] proxy override: account={aid} {mask_proxy_url(current) if current else '<none>'}"
            f" -> {mask_proxy_url(chosen)} ttl={ttl}s attempt={attempt} reason={reason_s}"
        )
        return True

    def _consume_proxy_failures(self) -> bool:
        """
        Workers report suspected proxy failures via:
          <runtime_dir>/events/proxy_fail_<account_id>.json
        """
        if not self._cfg:
            return False

        events_dir = self._runtime("events")
        try:
            names = os.listdir(events_dir)
        except Exception:
            return False

        changed = False
        for name in names:
            if not (name.startswith("proxy_fail_") and name.endswith(".json")):
                continue
            account_id = name[len("proxy_fail_") : -len(".json")]
            path = os.path.join(events_dir, name)
            payload: dict = {}
            try:
                if os.path.isfile(path):
                    payload = _read_json(path) or {}
            except Exception:
                payload = {}
            try:
                os.remove(path)
            except Exception:
                pass

            reason = ""
            if isinstance(payload, dict):
                ctx = str(payload.get("context") or "").strip()
                err = str(payload.get("error") or "").strip()
                sc = payload.get("status_code")
                reason = f"{ctx} {err}"
                if sc is not None:
                    reason = f"HTTP {sc} {reason}"

            changed = self._override_proxy_for_account(account_id=account_id, reason=reason) or changed

        return changed

    def _clear_expired_overrides(self) -> bool:
        if not self._cfg:
            return False
        now_epoch = float(_now())
        changed = False
        for b in self._bindings.values():
            if not b.override:
                continue
            if float(b.override_until or 0.0) <= now_epoch:
                b.override = ""
                b.override_until = 0.0
                changed = True
        if changed:
            self._save_bindings()
        return changed

    def _write_status_file(self) -> None:
        """
        Persist runner status for WebUI (no secrets).
        Path: <runtime_dir>/status.json
        """
        if not self._cfg:
            return

        proxy_ms = {r.proxy_url: r.avg_ms for r in self._proxy_ranked}
        accounts_status = []
        now_epoch = float(_now())
        assigned_total = 0
        processed_total = 0
        workers_reporting = 0
        for acc in self._cfg.accounts:
            p_raw = self._effective_proxy(acc.account_id) or ""
            p_masked = mask_proxy_url(p_raw) if p_raw else ""
            b = self._bindings.get(acc.account_id)
            primary_raw = (b.primary if b else "") or ""
            override_raw = (b.override if b else "") or ""
            override_until = float(b.override_until or 0.0) if b else 0.0
            override_active = bool(override_raw) and override_until > now_epoch
            proxy_is_override = bool(override_active and p_raw and p_raw == override_raw)
            worker = self._workers.get(acc.account_id)
            worker_pid = worker.pid if worker else None
            worker_rc = None
            worker_running = False
            if worker is not None:
                try:
                    worker_rc = worker.poll()
                    worker_running = worker_rc is None
                except Exception:
                    worker_running = False

            progress = {}
            try:
                progress_path = self._runtime("workers", f"{acc.account_id}.json")
                if os.path.isfile(progress_path):
                    progress = _read_json(progress_path) or {}
            except Exception:
                progress = {}

            processed_count = 0
            progress_updated_at = 0
            current_member_id = 0
            last_member_id = 0
            last_error = ""
            try:
                if isinstance(progress, dict):
                    processed_count = int(progress.get("processed_count") or 0)
                    progress_updated_at = int(progress.get("updated_at") or 0)
                    current_member_id = int(progress.get("current_member_id") or 0)
                    last_member_id = int(progress.get("last_member_id") or 0)
                    last_error = str(progress.get("last_error") or "")
            except Exception:
                processed_count = 0
                progress_updated_at = 0
                current_member_id = 0
                last_member_id = 0
                last_error = ""

            if bool(acc.enabled):
                assigned_total += int(self._last_assign_counts.get(acc.account_id, 0))
                processed_total += int(processed_count or 0)
            if progress_updated_at:
                workers_reporting += 1

            accounts_status.append(
                {
                    "id": acc.account_id,
                    "enabled": bool(acc.enabled),
                    "follow_source": bool(acc.follow_source),
                    "proxy": p_masked,
                    "proxy_primary": mask_proxy_url(primary_raw) if primary_raw else "",
                    "proxy_override": mask_proxy_url(override_raw) if override_active else "",
                    "proxy_override_until": int(override_until) if override_active else 0,
                    "proxy_is_override": bool(proxy_is_override),
                    "proxy_ms": proxy_ms.get(p_raw),
                    "worker_pid": worker_pid,
                    "worker_running": worker_running,
                    "worker_rc": worker_rc,
                    "assigned_artists": int(self._last_assign_counts.get(acc.account_id, 0)),
                    "processed_artists": int(processed_count or 0),
                    "progress_updated_at": int(progress_updated_at or 0),
                    "progress_current_member_id": int(current_member_id or 0),
                    "progress_last_member_id": int(last_member_id or 0),
                    "progress_last_error": str(last_error or ""),
                }
            )

        payload = {
            "schema": 1,
            "updated_at": int(_now()),
            "config_path": self._config_path,
            "runner": {"pid": os.getpid()},
            "db": {"path": os.path.join(self._cfg.runtime_dir, DEFAULT_MULTI_DB_FILENAME)},
            "work": {
                "assigned_artists_total": int(assigned_total),
                "processed_artists_total": int(processed_total),
                "workers_reporting": int(workers_reporting),
            },
            "follow": {
                "unique_artists": int(self._last_unique_artists),
                "bookmark_flag": self._cfg.bookmark_flag,
                "lang": self._cfg.follow_lang,
                "last_refresh": int(self._follow_last_refresh or 0),
                "refresh_interval_sec": int(self._cfg.follow_refresh_interval_sec),
            },
            "proxy_pool": {
                "source": self._cfg.proxy_source,
                "input_count": int(self._proxy_input_count),
                "normalized_count": int(self._proxy_normalized_count),
                "ranked_count": int(len(self._proxy_ranked)),
                "last_refresh": int(self._proxy_last_refresh or 0),
                "refresh_interval_sec": int(self._cfg.proxy_refresh_interval_sec),
                "test_url": self._cfg.proxy_test_url,
                "test_attempts": int(self._cfg.proxy_test_attempts),
                "warning": self._proxy_warning,
                "export_error": self._proxy_export_error,
            },
            "proxy_ranked": [
                {"proxy": r.masked, "ms": r.avg_ms, "ok": int(r.ok), "fail": int(r.fail)} for r in self._proxy_ranked[:50]
            ],
            "accounts": accounts_status,
        }

        try:
            _atomic_write_text(os.path.join(self._cfg.runtime_dir, "status.json"), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        except Exception:
            # status is best-effort; do not crash runner
            pass

    def _refresh_config_if_needed(self) -> bool:
        try:
            mtime = os.path.getmtime(self._config_path)
        except OSError:
            mtime = 0.0

        if not mtime or mtime == self._cfg_mtime:
            return False

        self._cfg_mtime = mtime
        self._cfg = _load_runner_config(self._config_path)
        # Resolve paths relative to multi_config.json directory.
        self._cfg.base_config_path = os.path.abspath(os.path.join(self._config_dir, self._cfg.base_config_path))
        self._cfg.runtime_dir = os.path.abspath(os.path.join(self._config_dir, self._cfg.runtime_dir))

        os.makedirs(self._cfg.runtime_dir, exist_ok=True)
        self._ensure_runtime_dirs()
        self._load_bindings()
        # Force immediate refresh on config changes.
        self._proxy_last_refresh = 0.0
        self._follow_last_refresh = 0.0
        self._write_status_file()
        return True

    def _refresh_proxies_if_needed(self) -> bool:
        assert self._cfg is not None
        if self._cfg.proxy_source in {"none", ""}:
            self._proxy_ranked = []
            self._proxy_ranked_urls = []
            self._proxy_input_count = 0
            self._proxy_normalized_count = 0
            self._proxy_warning = "proxy_pool.source=none：代理已禁用"
            self._proxy_export_error = ""
            return False

        due = (not self._proxy_last_refresh) or (_now() - self._proxy_last_refresh) >= self._cfg.proxy_refresh_interval_sec
        if not due:
            return False

        # Reset diagnostics for this refresh cycle.
        self._proxy_input_count = 0
        self._proxy_normalized_count = 0
        self._proxy_warning = ""
        self._proxy_export_error = ""

        self._proxy_last_refresh = _now()
        raw_list: List[str] = []
        export_lines: List[str] = []

        # Optional: load extra proxies from config list/file (supports http + socks).
        raw_list.extend([str(p).strip() for p in (self._cfg.proxy_static_list or []) if str(p).strip()])
        if self._cfg.proxy_file:
            try:
                proxy_file_path = os.path.abspath(os.path.join(self._cfg.runtime_dir, os.pardir, self._cfg.proxy_file))
                if not os.path.isabs(self._cfg.proxy_file):
                    # Resolve relative to multi_config.json directory.
                    proxy_file_path = os.path.abspath(os.path.join(self._config_dir, self._cfg.proxy_file))
                if os.path.isfile(proxy_file_path):
                    with open(proxy_file_path, "r", encoding="utf-8") as fp:
                        for line in fp:
                            s = (line or "").strip()
                            if not s or s.startswith("#"):
                                continue
                            raw_list.append(s)
            except Exception as ex:
                print(f"[runner] proxy file read failed: {ex}")

        if self._cfg.proxy_source == "easy_proxies":
            try:
                client = EasyProxiesClient(
                    EasyProxiesConfig(
                        base_url=self._cfg.easy_proxies_base_url,
                        password=self._cfg.easy_proxies_password,
                        timeout=self._cfg.easy_proxies_timeout_sec,
                        verify_ssl=self._cfg.easy_proxies_verify_ssl,
                    )
                )
                export_lines = client.export_http_proxies()

                # Some easy_proxies setups export placeholder hosts like 0.0.0.0/127.0.0.1/localhost.
                # Replace them with either explicit host_override or the base_url host.
                replace_host = str(self._cfg.easy_proxies_host_override or "").strip()
                if not replace_host:
                    try:
                        replace_host = str(urlparse(self._cfg.easy_proxies_base_url).hostname or "").strip()
                    except Exception:
                        replace_host = ""
                if replace_host:
                    fixed: List[str] = []
                    for line in export_lines:
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
                    export_lines = fixed
                raw_list.extend(export_lines)
            except Exception as ex:
                self._proxy_export_error = str(ex)
                print(f"[runner] easy_proxies export failed: {ex}")
        elif self._cfg.proxy_source not in {"static", "file"}:
            self._proxy_warning = f"unknown proxy source: {self._cfg.proxy_source!r}, treating as static"
            print(f"[runner] {self._proxy_warning}")

        self._proxy_input_count = int(len(raw_list))

        normalized: List[str] = []
        seen = set()
        for raw in raw_list:
            try:
                p = normalize_proxy_url(raw)
            except Exception:
                continue
            if p in seen:
                continue
            seen.add(p)
            normalized.append(p)

        self._proxy_normalized_count = int(len(normalized))

        warnings: List[str] = []
        if self._proxy_warning:
            warnings.append(self._proxy_warning)
        if self._proxy_export_error:
            warnings.append("easy_proxies 导出失败（已仅使用手工/文件代理）")

        # Detect easy_proxies pool mode: export returns same listener endpoint for all nodes.
        if export_lines and len(export_lines) > 1:
            export_norm = set()
            for line in export_lines:
                try:
                    export_norm.add(normalize_proxy_url(line))
                except Exception:
                    continue
            if len(export_norm) <= 1:
                warnings.append("easy_proxies 似乎处于 pool 模式：/api/export 返回的都是同一个入口；要做到每 token 绑定不同节点，请用 multi-port/hybrid 模式")

        # Rank by success rate + avg latency
        try:
            ranked_all = rank_proxies(
                normalized,
                test_url=self._cfg.proxy_test_url,
                timeout=self._cfg.proxy_test_timeout_sec,
                concurrency=self._cfg.proxy_test_concurrency,
                top_n=self._cfg.proxy_top_n,
                attempts=self._cfg.proxy_test_attempts,
            )
        except Exception as ex:
            print(f"[runner] proxy test failed: {ex}")
            ranked_all = []

        ranked: List[ProxyTestResult] = [r for r in (ranked_all or []) if int(getattr(r, "ok", 0)) > 0 and r.avg_ms is not None]
        if normalized and not ranked:
            warnings.append("代理测速全部失败（请检查代理可用性/目标 URL/超时设置）")
        if self._cfg.accounts and ranked and len(ranked) < len(self._cfg.accounts):
            warnings.append(f"可用代理({len(ranked)}) 少于账号({len(self._cfg.accounts)})，将会复用/部分账号无代理")
        if not normalized and raw_list:
            warnings.append("代理列表解析失败（格式不支持/缺少端口）")

        self._proxy_warning = "; ".join(warnings)

        new_urls = [r.proxy_url for r in ranked]
        changed = new_urls != self._proxy_ranked_urls
        self._proxy_ranked = ranked
        self._proxy_ranked_urls = new_urls
        return changed

    def _reconcile_proxy_bindings(self) -> bool:
        assert self._cfg is not None
        ranked_urls = list(self._proxy_ranked_urls)
        ranked_set = set(ranked_urls)
        max_per = int(self._cfg.proxy_max_tokens_per_proxy)
        strict = bool(self._cfg.proxy_bindings_strict)
        salt = str(self._cfg.proxy_binding_salt or DEFAULT_PROXY_BINDING_SALT)
        now_epoch = float(_now())

        binding_accounts = [a for a in self._cfg.accounts if bool(a.enabled) or bool(a.follow_source)]
        accounts_sorted = sorted(binding_accounts, key=lambda a: a.account_id)

        # keep existing bindings when possible
        before = {k: ProxyBinding(primary=v.primary, override=v.override, override_until=v.override_until) for k, v in self._bindings.items()}
        for acc in accounts_sorted:
            b = self._bindings.get(acc.account_id)
            if b is None:
                b = ProxyBinding()
                self._bindings[acc.account_id] = b
            if b.primary and b.primary not in ranked_set:
                b.primary = ""
            if b.override and (b.override not in ranked_set or float(b.override_until or 0.0) <= now_epoch):
                b.override = ""
                b.override_until = 0.0

        # enforce capacity for existing primaries
        used_count: Dict[str, int] = {}
        for acc in accounts_sorted:
            b = self._bindings.get(acc.account_id)
            if b is None or not b.primary:
                continue
            n = int(used_count.get(b.primary, 0))
            if n < max_per:
                used_count[b.primary] = n + 1
                continue
            # over-capacity: clear and let it be reassigned
            b.primary = ""
            b.override = ""
            b.override_until = 0.0

        # assign unbound accounts
        over_capacity = 0
        for acc in accounts_sorted:
            b = self._bindings.get(acc.account_id)
            if b is None:
                b = ProxyBinding()
                self._bindings[acc.account_id] = b

            current = b.primary or ""
            if current and current in ranked_set:
                continue
            if not ranked_urls:
                b.primary = ""
                b.override = ""
                b.override_until = 0.0
                continue

            # Deterministic: use rendezvous hashing to pick a stable proxy under capacity.
            chosen = ""
            order = _rendezvous_proxy_order(token_key=acc.account_id, proxy_keys=ranked_urls, salt=salt)
            for cand in order:
                if int(used_count.get(cand, 0)) < max_per:
                    chosen = cand
                    break
            if not chosen:
                if strict:
                    chosen = ""
                else:
                    chosen = order[0] if order else ""
                    if chosen:
                        over_capacity += 1

            b.primary = chosen
            b.override = ""
            b.override_until = 0.0
            if chosen:
                used_count[chosen] = int(used_count.get(chosen, 0)) + 1

        # drop bindings for removed accounts
        live_ids = {a.account_id for a in self._cfg.accounts}
        for dead in [k for k in self._bindings.keys() if k not in live_ids]:
            self._bindings.pop(dead, None)

        changed = before != self._bindings
        if changed:
            self._save_bindings()
        if over_capacity > 0:
            # Don't fail runner; surface warning and keep running.
            self._proxy_warning = "; ".join([w for w in [self._proxy_warning, f"代理容量不足：{over_capacity} 个账号被超额分配（可调大 max_tokens_per_proxy 或增加节点）"] if w])
        return changed

    def _write_worker_files(self) -> bool:
        assert self._cfg is not None
        any_changed = False
        db_path = os.path.join(self._cfg.runtime_dir, DEFAULT_MULTI_DB_FILENAME)
        for acc in self._cfg.accounts:
            proxy_url = self._effective_proxy(acc.account_id) or ""
            cfg_out = self._runtime("configs", f"{acc.account_id}.ini")
            changed = self._write_worker_config_if_changed(
                base_config_path=self._cfg.base_config_path,
                out_path=cfg_out,
                cookie=acc.cookie,
                refresh_token=acc.refresh_token,
                proxy_url=proxy_url,
                download_delay=acc.download_delay,
                db_path=db_path,
            )
            any_changed = any_changed or changed
        return any_changed

    def _refresh_follow_if_needed(self) -> Dict[int, Set[str]]:
        assert self._cfg is not None
        due = (not self._follow_last_refresh) or (_now() - self._follow_last_refresh) >= self._cfg.follow_refresh_interval_sec
        if not due:
            return {}

        self._follow_last_refresh = _now()

        follow_map: Dict[int, Set[str]] = {}  # member_id -> set(account_id)
        sess = requests.Session()
        mount_proxy_host_header_adapter(sess, enabled=True)

        accounts_sorted = sorted(self._cfg.accounts, key=lambda a: a.account_id)

        for acc in accounts_sorted:
            if not bool(acc.follow_source):
                continue
            proxy_url = self._effective_proxy(acc.account_id) or ""
            proxies = _requests_proxies(proxy_url) if proxy_url else None
            combined: Set[int] = set()

            # Prefer refresh_token App API (stable, no HTML parsing), fallback to cookie ajax.
            if acc.refresh_token:
                try:
                    client = PixivAppApiClient(
                        refresh_token=acc.refresh_token,
                        proxies=proxies,
                        verify_ssl=True,
                        timeout_sec=float(self._cfg.follow_timeout_sec),
                    )
                    client.get_access_token()
                    if client.refresh_token and client.refresh_token != acc.refresh_token:
                        self._persist_account_refresh_token(
                            account_id=acc.account_id,
                            refresh_token=client.refresh_token,
                            source="runner_follow",
                        )
                    my_id = client.user_id
                    if not my_id:
                        raise RuntimeError("OAuth response missing user_id")

                    restricts: List[str] = []
                    if self._cfg.bookmark_flag == "y":
                        restricts = ["public", "private"]
                    elif self._cfg.bookmark_flag == "o":
                        restricts = ["private"]
                    else:
                        restricts = ["public"]

                    for restrict in restricts:
                        try:
                            ids = client.fetch_following_user_ids(
                                user_id=int(my_id),
                                restrict=restrict,
                                page_sleep_sec=0.5,
                            )
                            combined.update(ids)
                        except PixivAppApiError as ex:
                            if proxies:
                                try:
                                    client2 = PixivAppApiClient(
                                        refresh_token=client.refresh_token,
                                        proxies=None,
                                        verify_ssl=True,
                                        timeout_sec=float(self._cfg.follow_timeout_sec),
                                    )
                                    client2.get_access_token()
                                    if client2.refresh_token and client2.refresh_token != acc.refresh_token:
                                        self._persist_account_refresh_token(
                                            account_id=acc.account_id,
                                            refresh_token=client2.refresh_token,
                                            source="runner_follow_fallback",
                                        )
                                    ids = client2.fetch_following_user_ids(
                                        user_id=int(my_id),
                                        restrict=restrict,
                                        page_sleep_sec=0.5,
                                    )
                                    combined.update(ids)
                                    continue
                                except Exception:
                                    pass
                            print(f"[runner] follow fetch failed: account={acc.account_id} restrict={restrict} err={ex}")
                            break
                except Exception as ex:
                    print(f"[runner] follow fetch failed (refresh_token): account={acc.account_id} err={ex}")

            elif acc.cookie:
                my_id = _cookie_user_id(acc.cookie) or _resolve_my_user_id(
                    session=sess,
                    cookie=acc.cookie,
                    timeout=self._cfg.follow_timeout_sec,
                    proxies=proxies,
                )
                if not my_id:
                    print(f"[runner] cannot resolve my user id for account={acc.account_id}, skip follow fetch")
                    continue

                rests: List[str] = []
                if self._cfg.bookmark_flag == "y":
                    rests = ["show", "hide"]
                elif self._cfg.bookmark_flag == "o":
                    rests = ["hide"]
                else:
                    rests = ["show"]

                for rest in rests:
                    try:
                        ids = _fetch_following_user_ids(
                            session=sess,
                            member_id=my_id,
                            cookie=acc.cookie,
                            rest=rest,
                            lang=self._cfg.follow_lang,
                            timeout=self._cfg.follow_timeout_sec,
                            proxies=proxies,
                        )
                        combined.update(ids)
                    except Exception as ex:
                        # Fallback: if proxy is flaky, retry without proxy once.
                        if proxies:
                            try:
                                ids = _fetch_following_user_ids(
                                    session=sess,
                                    member_id=my_id,
                                    cookie=acc.cookie,
                                    rest=rest,
                                    lang=self._cfg.follow_lang,
                                    timeout=self._cfg.follow_timeout_sec,
                                    proxies=None,
                                )
                                combined.update(ids)
                                continue
                            except Exception:
                                pass
                        print(f"[runner] follow fetch failed: account={acc.account_id} rest={rest} err={ex}")
                        break

            for artist_id in combined:
                follow_map.setdefault(int(artist_id), set()).add(acc.account_id)

        return follow_map

    def _eligible_worker_accounts(self) -> List[Account]:
        assert self._cfg is not None
        workers: List[Account] = []
        for a in self._cfg.accounts:
            if not bool(a.enabled):
                continue
            if self._cfg.worker_mode == "index_urls" and not a.refresh_token:
                continue
            if self._cfg.worker_mode == "download" and not a.cookie:
                continue
            workers.append(a)
        workers.sort(key=lambda x: x.account_id)
        return workers

    def _assignment_signature(self, *, follow_map: Dict[int, Set[str]], workers: List[Account]) -> str:
        assert self._cfg is not None
        h = hashlib.md5()
        h.update(str(self._cfg.worker_mode).encode("utf-8"))
        h.update(b"|")
        h.update(os.path.abspath(self._cfg.base_config_path).encode("utf-8", errors="ignore"))
        h.update(b"|")
        # Followed artists set
        for mid in sorted(follow_map.keys()):
            h.update(str(int(mid)).encode("utf-8"))
            h.update(b",")
        h.update(b"|")
        # Eligible workers + their per-account delay (affects speed)
        for acc in workers:
            delay = acc.download_delay if acc.download_delay is not None else -1
            h.update(f"{acc.account_id}:{int(delay)};".encode("utf-8"))
        return h.hexdigest()

    def _compute_assignments(self, follow_map: Dict[int, Set[str]]) -> Dict[str, List[int]]:
        assert self._cfg is not None
        worker_accounts = self._eligible_worker_accounts()
        worker_ids = [a.account_id for a in worker_accounts]
        out: Dict[str, List[int]] = {aid: [] for aid in worker_ids}
        if not worker_accounts:
            return out

        # Capacity model (best-effort):
        # - Smaller downloadDelay => faster (less sleep) => higher capacity.
        # - Faster proxy (lower avg_ms) => slightly higher capacity.
        proxy_ms = {r.proxy_url: r.avg_ms for r in self._proxy_ranked}
        ms_values = [float(ms) for ms in proxy_ms.values() if ms is not None]
        median_ms = 0.0
        try:
            if ms_values:
                ms_values.sort()
                median_ms = float(ms_values[len(ms_values) // 2])
        except Exception:
            median_ms = 0.0

        # Resolve base downloadDelay when per-account is not set.
        base_delay = 0
        try:
            parser = configparser.RawConfigParser()
            parser.optionxform = lambda option: option
            with open(self._cfg.base_config_path, "r", encoding="utf-8") as fp:
                parser.read_file(fp)
            if parser.has_section("Network") and parser.has_option("Network", "downloadDelay"):
                raw = str(parser.get("Network", "downloadDelay") or "").strip()
                if raw.isdigit():
                    base_delay = int(raw)
        except Exception:
            base_delay = 0

        capacity: Dict[str, float] = {}
        for acc in worker_accounts:
            delay = int(acc.download_delay if acc.download_delay is not None else base_delay)
            delay = max(0, delay)
            delay_factor = 1.0 / (1.0 + (float(delay) / 8.0))

            p = self._effective_proxy(acc.account_id) or ""
            ms = proxy_ms.get(p)
            proxy_factor = 1.0
            try:
                if ms is not None and median_ms > 0:
                    proxy_factor = float(median_ms) / float(ms)
                    proxy_factor = max(0.5, min(2.0, proxy_factor))
            except Exception:
                proxy_factor = 1.0

            capacity[acc.account_id] = max(0.05, float(delay_factor) * float(proxy_factor))

        # Workload model (best-effort):
        # Use existing single-thread DB (db.sqlite or configured dbPath) as a reference to estimate
        # how many works each artist has; this helps distribute "heavy" artists more evenly.
        ref_db = _resolve_default_db_path_from_config(self._cfg.base_config_path)
        follow_counts = _load_follow_image_counts(ref_db)

        jobs: List[Tuple[int, int]] = []
        for artist_id in follow_map.keys():
            mid = int(artist_id)
            w = int(follow_counts.get(mid, 0) or 0)
            if w <= 0:
                w = 1
            jobs.append((w, mid))

        # Greedy "related machines" scheduling: assign heavier artists first.
        jobs.sort(key=lambda x: (-x[0], _fnv1a64(str(x[1]))))
        loads: Dict[str, float] = {aid: 0.0 for aid in worker_ids}
        for (w, mid) in jobs:
            best_aid = worker_ids[0]
            best_score = None
            for aid in worker_ids:
                score = (loads[aid] + float(w)) / float(capacity.get(aid) or 1.0)
                if best_score is None or score < best_score or (score == best_score and aid < best_aid):
                    best_score = score
                    best_aid = aid
            out.setdefault(best_aid, []).append(int(mid))
            loads[best_aid] = float(loads.get(best_aid, 0.0)) + float(w)

        # Keep stable order in assignment files for readability/debuggability.
        for aid, ids in out.items():
            try:
                out[aid] = sorted(set(int(x) for x in (ids or [])))
            except Exception:
                out[aid] = ids or []

        return out

    def _write_assignments(self, assignments: Dict[str, List[int]]) -> None:
        assert self._cfg is not None
        self._last_assign_counts = {k: len(v or []) for (k, v) in (assignments or {}).items()}
        for acc in self._cfg.accounts:
            ids = assignments.get(acc.account_id) or []
            path = self._runtime("assignments", f"{acc.account_id}.txt")
            text = "\n".join(str(i) for i in ids) + ("\n" if ids else "")
            _atomic_write_text_if_changed(path, text)
        self._write_status_file()

    def _start_worker(self, acc: Account) -> subprocess.Popen:
        assert self._cfg is not None
        worker_script = os.path.join(os.path.dirname(__file__), "PixivMultiWorker.py")
        cfg_path = self._runtime("configs", f"{acc.account_id}.ini")
        assign_path = self._runtime("assignments", f"{acc.account_id}.txt")
        cmd = [
            sys.executable,
            worker_script,
            "--worker-id",
            acc.account_id,
            "--config",
            cfg_path,
            "--assign",
            assign_path,
            "--poll",
            str(self._cfg.worker_poll_interval_sec),
            "--mode",
            str(self._cfg.worker_mode),
        ]
        return subprocess.Popen(cmd, cwd=os.path.dirname(__file__) or ".")

    def _ensure_workers(self) -> None:
        assert self._cfg is not None
        live_ids: Set[str] = set()
        for a in self._cfg.accounts:
            if not bool(a.enabled):
                continue
            if self._cfg.worker_mode == "index_urls" and not a.refresh_token:
                continue
            if self._cfg.worker_mode == "download" and not a.cookie:
                continue
            live_ids.add(a.account_id)

        # stop removed
        for dead in [k for k in self._workers.keys() if k not in live_ids]:
            p = self._workers.pop(dead)
            try:
                p.terminate()
            except Exception:
                pass

        # start missing / restart exited
        for acc in self._cfg.accounts:
            if not bool(acc.enabled):
                continue
            if self._cfg.worker_mode == "index_urls" and not acc.refresh_token:
                # URL-only mode requires refresh_token; skip to avoid worker thrashing.
                continue
            if self._cfg.worker_mode == "download" and not acc.cookie:
                continue
            p = self._workers.get(acc.account_id)
            if p is None or p.poll() is not None:
                self._workers[acc.account_id] = self._start_worker(acc)
        self._write_status_file()

    def _print_status(self) -> None:
        assert self._cfg is not None
        proxy_count = len(self._proxy_ranked)
        print(f"[runner] accounts={len(self._cfg.accounts)} proxies_ranked={proxy_count}")
        for acc in self._cfg.accounts:
            p = self._effective_proxy(acc.account_id) or ""
            print(f"  - {acc.account_id:12} proxy={mask_proxy_url(p) if p else '<none>'}")
        if self._proxy_warning:
            print(f"[runner] proxy warning: {self._proxy_warning}")
        if self._proxy_export_error:
            print(f"[runner] easy_proxies export error: {self._proxy_export_error}")

    def run_forever(self, *, once: bool = False) -> int:
        if not os.path.isfile(self._config_path):
            # WebUI can generate this, but allow CLI start without manual file edits.
            example_path = os.path.join(os.path.dirname(__file__), "multi_config.example.json")
            data: dict
            try:
                if os.path.isfile(example_path):
                    with open(example_path, "r", encoding="utf-8") as fp:
                        data = json.load(fp) or {}
                else:
                    data = {}
            except Exception:
                data = {}

            data.setdefault("base_config", "config.ini")
            data.setdefault("runtime_dir", ".multi_runtime")
            data["accounts"] = []
            data.setdefault("follow", {}).setdefault("bookmark_flag", "n")
            data.setdefault("proxy_pool", {}).setdefault("source", "easy_proxies")
            data.setdefault("worker", {}).setdefault("poll_interval_sec", 10)
            data["_ui_nonce"] = int(_now())
            try:
                _atomic_write_text(self._config_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                print(f"[runner] Created default config: {self._config_path}")
            except Exception as ex:
                print(f"[runner] Failed to create config: {self._config_path} ({ex})")
                return 2

        self._refresh_config_if_needed()
        assert self._cfg is not None

        # initial bootstrap: create per-worker config/assignments first
        self._refresh_proxies_if_needed()
        self._reconcile_proxy_bindings()
        self._write_worker_files()
        self._ensure_workers()

        # initial follow + assignment
        follow_map = self._refresh_follow_if_needed()
        if follow_map:
            self._last_unique_artists = len(follow_map)
            workers = self._eligible_worker_accounts()
            sig = self._assignment_signature(follow_map=follow_map, workers=workers)
            if sig != self._last_assignment_signature:
                assignments = self._compute_assignments(follow_map)
                self._write_assignments(assignments)
                self._last_assignment_signature = sig

        self._print_status()
        self._write_status_file()

        if once:
            return 0

        try:
            while True:
                cfg_changed = self._refresh_config_if_needed()
                if self._consume_rotated_refresh_tokens():
                    cfg_changed = True
                proxies_changed = self._refresh_proxies_if_needed()

                bindings_changed = False
                if cfg_changed or proxies_changed:
                    bindings_changed = self._reconcile_proxy_bindings()

                if self._consume_proxy_failures():
                    bindings_changed = True
                if self._clear_expired_overrides():
                    bindings_changed = True

                if cfg_changed or bindings_changed:
                    self._write_worker_files()
                    self._ensure_workers()
                    self._print_status()

                # follow refresh
                follow_map = self._refresh_follow_if_needed()
                if follow_map:
                    self._last_unique_artists = len(follow_map)
                    workers = self._eligible_worker_accounts()
                    sig = self._assignment_signature(follow_map=follow_map, workers=workers)
                    if sig != self._last_assignment_signature:
                        assignments = self._compute_assignments(follow_map)
                        self._write_assignments(assignments)
                        self._last_assignment_signature = sig

                self._ensure_workers()
                self._write_status_file()
                time.sleep(2.0)
        except KeyboardInterrupt:
            pass
        finally:
            for p in self._workers.values():
                try:
                    p.terminate()
                except Exception:
                    pass
        return 0


def main() -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Pixiv multi-account runner (easy_proxies + hot reload)")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to multi_config.json")
    parser.add_argument("--once", action="store_true", help="Run one bootstrap cycle then exit (for testing)")
    args = parser.parse_args()

    runner = MultiRunner(config_path=str(args.config))
    return runner.run_forever(once=bool(args.once))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
