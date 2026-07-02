"""宿主机访问 GitHub HTTP（PyGithub / requests）的代理配置。"""
from __future__ import annotations

import os

_DEFAULT_PROXY = "socks5h://127.0.0.1:10808"


def host_proxy_url() -> str | None:
    """返回应使用的代理 URL；显式设为空字符串表示不走代理。"""
    for key in (
        "PATCHGURU_HOST_PROXY",
        "GITHUB_PROXY",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "GIT_PROXY",
    ):
        val = os.environ.get(key)
        if val is not None:
            return val or None
    return _DEFAULT_PROXY


def apply_host_proxy_env() -> str | None:
    """把代理写入当前进程环境变量，供 requests / PyGithub 使用。"""
    url = host_proxy_url()
    if not url:
        return None
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        os.environ.setdefault(key, url)
    return url


def requests_proxies() -> dict[str, str] | None:
    url = host_proxy_url()
    if not url:
        return None
    return {"http": url, "https": url}
