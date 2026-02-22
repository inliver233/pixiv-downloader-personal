# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional

import requests


@dataclass
class EasyProxiesConfig:
    base_url: str
    password: str = ""
    timeout: float = 10.0
    verify_ssl: bool = True


class EasyProxiesClient:
    """
    Client for https://github.com/jasonwong1991/easy_proxies management API.

    We use:
    - POST /api/auth {password}  -> returns session token
    - GET  /api/export           -> returns proxies (one per line)
    """

    def __init__(self, cfg: EasyProxiesConfig):
        self._cfg = cfg
        self._sess = requests.Session()
        self._token: Optional[str] = None
        self._token_at: float = 0.0

    def _normalize_base(self) -> str:
        return (self._cfg.base_url or "").rstrip("/")

    def _auth_if_needed(self) -> None:
        if not self._cfg.password:
            return

        # easy_proxies sessions default to 24h; refresh conservatively.
        if self._token and (time.time() - self._token_at) < 6 * 60 * 60:
            return

        url = f"{self._normalize_base()}/api/auth"
        resp = self._sess.post(
            url,
            json={"password": self._cfg.password},
            timeout=self._cfg.timeout,
            verify=self._cfg.verify_ssl,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"easy_proxies auth failed: {resp.status_code} {resp.text}")

        data = resp.json()
        token = data.get("token") or ""
        if not token:
            # When password is disabled, server returns {no_password:true}.
            # But that should be handled by checking cfg.password above.
            raise RuntimeError("easy_proxies auth did not return token")

        self._token = token
        self._token_at = time.time()

    def export_http_proxies(self) -> List[str]:
        self._auth_if_needed()

        base = self._normalize_base()
        url = f"{base}/api/export"
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        resp = self._sess.get(
            url,
            headers=headers,
            timeout=self._cfg.timeout,
            verify=self._cfg.verify_ssl,
        )

        if resp.status_code == 401 and self._cfg.password:
            # token expired, retry once
            self._token = None
            self._token_at = 0.0
            self._auth_if_needed()
            headers["Authorization"] = f"Bearer {self._token}"
            resp = self._sess.get(
                url,
                headers=headers,
                timeout=self._cfg.timeout,
                verify=self._cfg.verify_ssl,
            )

        if resp.status_code != 200:
            raise RuntimeError(f"easy_proxies export failed: {resp.status_code} {resp.text}")

        lines = []
        for line in (resp.text or "").splitlines():
            s = line.strip()
            if not s:
                continue
            lines.append(s)
        return lines

