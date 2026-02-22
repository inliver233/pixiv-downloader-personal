import pytest

from common.ProxyUtils import mask_proxy_url, normalize_proxy_url, parse_proxy_url


def test_normalize_host_port_defaults_to_http():
    assert normalize_proxy_url("127.0.0.1:8888") == "http://127.0.0.1:8888"


def test_normalize_http_with_auth():
    assert normalize_proxy_url("http://user:pass@1.2.3.4:2323") == "http://user:pass@1.2.3.4:2323"


def test_normalize_http_with_at_in_password():
    raw = "http://inliver:inliverBAIPIAO@123@152.53.91.30:2323"
    assert normalize_proxy_url(raw) == "http://inliver:inliverBAIPIAO%40123@152.53.91.30:2323"


def test_normalize_socks_with_at_in_password():
    raw = "socks5://user:p@ss@127.0.0.1:1080"
    assert normalize_proxy_url(raw) == "socks5://user:p%40ss@127.0.0.1:1080"


def test_ipv6_host_kept():
    assert normalize_proxy_url("http://[::1]:8080") == "http://[::1]:8080"


def test_parse_proxy_url_parts():
    parts = parse_proxy_url("http://u:p@h:1")
    assert parts.scheme == "http"
    assert parts.host == "h"
    assert parts.port == 1
    assert parts.username == "u"
    assert parts.password == "p"


def test_mask_proxy_url_hides_password():
    masked = mask_proxy_url("http://u:secret@1.2.3.4:80")
    assert masked == "http://u:***@1.2.3.4:80"


def test_reject_missing_port():
    with pytest.raises(ValueError):
        normalize_proxy_url("http://1.2.3.4")

