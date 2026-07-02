"""Git 网络操作：代理不稳定时的重试与可选跳过 fetch（对齐 PatchGuru）。"""
from __future__ import annotations

import os
import time

from git.exc import GitCommandError


def _env_flag(*names: str) -> bool:
    for name in names:
        value = os.environ.get(name, "").strip().lower()
        if value in ("1", "true", "yes"):
            return True
    return False


def _env_int(*names: str, default: int) -> int:
    for name in names:
        raw = os.environ.get(name, "").strip()
        if raw:
            return max(1, int(raw))
    return default


def _env_float(*names: str, default: float) -> float:
    for name in names:
        raw = os.environ.get(name, "").strip()
        if raw:
            return float(raw)
    return default


def skip_git_fetch() -> bool:
    return _env_flag("TESTORA_SKIP_GIT_FETCH", "PATCHGURU_SKIP_GIT_FETCH")


def git_fetch_optional() -> bool:
    return _env_flag("TESTORA_GIT_FETCH_OPTIONAL", "PATCHGURU_GIT_FETCH_OPTIONAL")


def git_fetch_retries() -> int:
    return _env_int("TESTORA_GIT_FETCH_RETRIES", "PATCHGURU_GIT_FETCH_RETRIES", default=3)


def git_fetch_retry_sec() -> float:
    return _env_float("TESTORA_GIT_FETCH_RETRY_SEC", "PATCHGURU_GIT_FETCH_RETRY_SEC", default=5.0)


def _origin(remote_or_repo):
    if hasattr(remote_or_repo, "remotes"):
        return remote_or_repo.remotes.origin
    return remote_or_repo


def git_fetch_with_retry(remote_or_repo, *, context: str = "") -> bool:
    """fetch origin；失败时重试。optional 模式下失败返回 False 不抛异常。"""
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
