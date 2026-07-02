"""Git 网络操作：代理不稳定时的重试与可选跳过 fetch。"""
from __future__ import annotations

import os
import time

from git.exc import GitCommandError

from patchguru.utils.proxy import apply_host_proxy_env


def skip_git_fetch() -> bool:
    return os.environ.get("PATCHGURU_SKIP_GIT_FETCH", "").strip().lower() in ("1", "true", "yes")


def git_fetch_optional() -> bool:
    return os.environ.get("PATCHGURU_GIT_FETCH_OPTIONAL", "").strip().lower() in ("1", "true", "yes")


def git_fetch_retries() -> int:
    return max(1, int(os.environ.get("PATCHGURU_GIT_FETCH_RETRIES", "3")))


def git_fetch_retry_sec() -> float:
    return float(os.environ.get("PATCHGURU_GIT_FETCH_RETRY_SEC", "5"))


def _origin(remote_or_repo):
    if hasattr(remote_or_repo, "remotes"):
        return remote_or_repo.remotes.origin
    return remote_or_repo


def git_fetch_with_retry(remote_or_repo, *, context: str = "") -> bool:
    """fetch origin；失败时重试。返回是否成功（optional 模式下失败返回 False 不抛异常）。"""
    apply_host_proxy_env()
    if skip_git_fetch():
        print(f"[git] skip fetch ({context})")
        return True

    origin = _origin(remote_or_repo)
    label = context or "origin"
    last_err: GitCommandError | None = None
    retries = git_fetch_retries()
    delay = git_fetch_retry_sec()

    for attempt in range(1, retries + 1):
        try:
            origin.fetch()
            return True
        except GitCommandError as exc:
            last_err = exc
            print(f"[git] fetch failed ({label}) attempt {attempt}/{retries}: {exc}")
            if attempt < retries:
                time.sleep(delay)

    if git_fetch_optional():
        print(f"[git] WARN: fetch failed ({label}), continue with local clone only")
        return False
    assert last_err is not None
    raise last_err


def git_pull_with_retry(repo, *, context: str = "") -> bool:
    """git pull；语义同 git_fetch_with_retry。"""
    apply_host_proxy_env()
    if skip_git_fetch():
        print(f"[git] skip pull ({context})")
        return True

    label = context or "pull"
    last_err: GitCommandError | None = None
    retries = git_fetch_retries()
    delay = git_fetch_retry_sec()

    for attempt in range(1, retries + 1):
        try:
            repo.git.pull()
            return True
        except GitCommandError as exc:
            last_err = exc
            print(f"[git] pull failed ({label}) attempt {attempt}/{retries}: {exc}")
            if attempt < retries:
                time.sleep(delay)

    if git_fetch_optional():
        print(f"[git] WARN: pull failed ({label}), continue with local checkout")
        return False
    assert last_err is not None
    raise last_err
