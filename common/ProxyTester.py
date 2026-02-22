# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import re
import threading
from typing import List, Optional

import requests

from common.RequestsProxyAdapter import mount_proxy_host_header_adapter
from common.ProxyUtils import mask_proxy_url, normalize_proxy_url


@dataclass(frozen=True)
class ProxyTestResult:
    proxy_url: str
    ok: int
    fail: int
    avg_ms: Optional[float]
    last_error: str = ""

    @property
    def masked(self) -> str:
        return mask_proxy_url(self.proxy_url)


_thread_local = threading.local()


def _get_session() -> requests.Session:
    sess = getattr(_thread_local, "sess", None)
    if sess is None:
        sess = requests.Session()
        # Compatible with some proxy pool frontends that hang on CONNECT with target Host header.
        mount_proxy_host_header_adapter(sess, enabled=True)
        _thread_local.sess = sess
    return sess


def _redact_secrets(text: str) -> str:
    s = str(text or "")
    return re.sub(r"//[^/@\\s:]+:[^@\\s]+@", "//***:***@", s)


def test_proxy_once(
    proxy_url: str,
    *,
    test_url: str,
    timeout: float,
    verify_ssl: bool = True,
) -> tuple[bool, Optional[float], int, str]:
    """
    Return (ok, latency_ms, status_code, error_message).
    ok means: request succeeded and status_code < 500.
    """
    proxy_url = normalize_proxy_url(proxy_url)
    proxies = {"http": proxy_url, "https": proxy_url}

    start = time.monotonic()
    try:
        sess = _get_session()
        resp = sess.get(
            test_url,
            timeout=timeout,
            proxies=proxies,
            headers={"User-Agent": "Mozilla/5.0"},
            allow_redirects=False,
            verify=verify_ssl,
        )
        status = int(resp.status_code)
        ok = status < 500
        ms = (time.monotonic() - start) * 1000.0
        return ok, ms, status, ""
    except Exception as ex:  # noqa: BLE001
        return False, None, 0, _redact_secrets(str(ex))


def test_proxy(
    proxy_url: str,
    *,
    test_url: str,
    timeout: float,
    attempts: int = 2,
    verify_ssl: bool = True,
) -> ProxyTestResult:
    proxy_url = normalize_proxy_url(proxy_url)
    attempts = max(1, int(attempts))

    ok = 0
    fail = 0
    ms_list: List[float] = []
    last_err = ""

    for _ in range(attempts):
        is_ok, ms, _status, err = test_proxy_once(
            proxy_url,
            test_url=test_url,
            timeout=timeout,
            verify_ssl=verify_ssl,
        )
        if is_ok and ms is not None:
            ok += 1
            ms_list.append(float(ms))
        else:
            fail += 1
            last_err = err or last_err

    avg_ms = (sum(ms_list) / len(ms_list)) if ms_list else None
    return ProxyTestResult(proxy_url=proxy_url, ok=ok, fail=fail, avg_ms=avg_ms, last_error=last_err)


def rank_proxies(
    proxies: List[str],
    *,
    test_url: str,
    timeout: float,
    concurrency: int = 20,
    top_n: int = 20,
    attempts: int = 2,
    verify_ssl: bool = True,
    max_input: int = 500,
) -> List[ProxyTestResult]:
    if not proxies:
        return []

    # Normalize + dedupe early to avoid wasting threads.
    normalized: List[str] = []
    seen = set()
    for raw in proxies[: max(0, int(max_input))]:
        try:
            p = normalize_proxy_url(raw)
        except Exception:
            continue
        if p in seen:
            continue
        seen.add(p)
        normalized.append(p)

    if not normalized:
        return []

    concurrency = max(1, int(concurrency))
    top_n = max(1, int(top_n))
    attempts = max(1, int(attempts))

    results: List[ProxyTestResult] = []
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        fut_map = {
            ex.submit(test_proxy, p, test_url=test_url, timeout=timeout, attempts=attempts, verify_ssl=verify_ssl): p
            for p in normalized
        }
        for fut in as_completed(fut_map):
            try:
                r = fut.result()
            except Exception as ex2:  # noqa: BLE001
                p = fut_map.get(fut, "<unknown>")
                results.append(
                    ProxyTestResult(proxy_url=str(p), ok=0, fail=attempts, avg_ms=None, last_error=_redact_secrets(str(ex2)))
                )
                continue
            results.append(r)

    # Sort: higher ok first, then lower avg latency.
    def _sort_key(r: ProxyTestResult):
        avg = r.avg_ms if r.avg_ms is not None else 9e18
        return (-int(r.ok), float(avg))

    results.sort(key=_sort_key)
    return results[:top_n]
