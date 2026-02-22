# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

from common.ProxyUtils import parse_proxy_url


@dataclass(frozen=True)
class ProxyHostHeaderConfig:
    """
    Some proxy pool deployments sit behind a virtual-host gateway and will
    hang on CONNECT when the Host header equals the *target* host.

    Workaround: force CONNECT's Host header to be the proxy host:port.
    """

    enabled: bool = True


class ProxyHostHeaderAdapter(HTTPAdapter):
    def __init__(self, *, cfg: Optional[ProxyHostHeaderConfig] = None, **kwargs):
        super().__init__(**kwargs)
        self._cfg = cfg or ProxyHostHeaderConfig()

    def proxy_headers(self, proxy):  # noqa: ANN001
        headers = super().proxy_headers(proxy)
        if not bool(getattr(self._cfg, "enabled", True)):
            return headers

        try:
            parts = parse_proxy_url(str(proxy))
            host = parts.host
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            headers.setdefault("Host", f"{host}:{int(parts.port)}")
        except Exception:
            # Best-effort; never break requests because of header munging.
            pass
        return headers


def mount_proxy_host_header_adapter(
    session: requests.Session,
    *,
    enabled: bool = True,
) -> requests.Session:
    """
    Mount adapter on http/https so urllib3 CONNECT uses our proxy headers.
    """
    env = str(os.environ.get("PIXIVUTIL_PROXY_TUNNEL_HOST_HEADER", "") or "").strip().lower()
    if env in {"0", "false", "off", "no"}:
        enabled = False

    adapter = ProxyHostHeaderAdapter(cfg=ProxyHostHeaderConfig(enabled=bool(enabled)))
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
