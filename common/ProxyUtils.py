# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from urllib.parse import quote


SUPPORTED_PROXY_SCHEMES = {
    "http",
    "https",
    "socks4",
    "socks4a",
    "socks5",
    "socks5h",
}


@dataclass(frozen=True)
class ProxyParts:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    def to_url(self) -> str:
        auth = ""
        if self.username:
            username = quote(self.username, safe="%")
            password = quote(self.password, safe="%")
            auth = f"{username}:{password}@" if self.password != "" else f"{username}@"
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{self.scheme}://{auth}{host}:{self.port}"


def _strip_url_path(authority_and_maybe_path: str) -> str:
    return re.split(r"[/?#]", authority_and_maybe_path, maxsplit=1)[0]


def _split_host_port(hostport: str) -> Tuple[str, int]:
    hp = hostport.strip()
    if not hp:
        raise ValueError("Empty host:port")

    if hp.startswith("["):
        end = hp.find("]")
        if end <= 1:
            raise ValueError(f"Invalid IPv6 host:port: {hostport!r}")
        host = hp[1:end]
        rest = hp[end + 1 :]
        if not rest.startswith(":"):
            raise ValueError(f"Missing port in host:port: {hostport!r}")
        port_str = rest[1:]
    else:
        if ":" not in hp:
            raise ValueError(f"Missing port in host:port: {hostport!r}")
        host, port_str = hp.rsplit(":", 1)

    if not port_str.isdigit():
        raise ValueError(f"Invalid port in host:port: {hostport!r}")
    port = int(port_str)
    if not (0 < port < 65536):
        raise ValueError(f"Port out of range in host:port: {hostport!r}")
    if not host:
        raise ValueError(f"Empty host in host:port: {hostport!r}")

    return host, port


def parse_proxy_url(raw: str, *, default_scheme: str = "http") -> ProxyParts:
    """
    Parse proxy URL (http/https/socks4/socks5) and normalize auth safely.

    Supports non-URL-safe credentials from some proxy pool exports, e.g.
    `http://user:pass@123@1.2.3.4:2323` (password contains '@'):
    we split by the *rightmost* '@' to find the host:port part.
    """
    if raw is None:
        raise ValueError("Proxy is None")
    value = str(raw).strip()
    if not value:
        raise ValueError("Proxy is empty")

    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1].strip()
        if not value:
            raise ValueError("Proxy is empty after stripping quotes")

    if "://" not in value:
        value = f"{default_scheme}://{value}"

    scheme, rest = value.split("://", 1)
    scheme = scheme.strip().lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError(f"Unsupported proxy scheme: {scheme!r}")

    rest = _strip_url_path(rest)
    if not rest:
        raise ValueError("Proxy authority is empty")

    username = ""
    password = ""
    hostport = rest
    if "@" in rest:
        userinfo, hostport = rest.rsplit("@", 1)
        if ":" in userinfo:
            username, password = userinfo.split(":", 1)
        else:
            username = userinfo
            password = ""

    host, port = _split_host_port(hostport)
    return ProxyParts(
        scheme=scheme,
        host=host,
        port=port,
        username=username,
        password=password,
    )


def normalize_proxy_url(raw: str, *, default_scheme: str = "http") -> str:
    return parse_proxy_url(raw, default_scheme=default_scheme).to_url()


def proxies_dict(proxy_url: str) -> Dict[str, str]:
    return {"http": proxy_url, "https": proxy_url}


def mask_proxy_url(proxy_url: str) -> str:
    """
    Hide password for logs: http://user:***@host:port
    """
    try:
        parts = parse_proxy_url(proxy_url)
    except Exception:
        return "<invalid proxy>"

    if not parts.username:
        return parts.to_url()

    host = parts.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    username = quote(parts.username, safe="%")
    if parts.password != "":
        return f"{parts.scheme}://{username}:***@{host}:{parts.port}"
    return f"{parts.scheme}://{username}@{host}:{parts.port}"
