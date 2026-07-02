"""marshmallow 在 checkout 切换 PR 时可能同时残留 marshmallow/ 与 src/marshmallow/，导致 pip install -e 失败。"""
from __future__ import annotations

import os
import shutil
import subprocess


def _git_has_path(repo_dir: str, path: str) -> bool:
    proc = subprocess.run(
        ["git", "ls-tree", "HEAD", path],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


def fix_marshmallow_duplicate_layout(repo_dir: str) -> list[str]:
    """按当前 HEAD 的 tracked 布局删除多余的包目录，返回已删除路径。"""
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        return []

    has_src = _git_has_path(repo_dir, "src/marshmallow")
    has_root = _git_has_path(repo_dir, "marshmallow")
    root_pkg = os.path.join(repo_dir, "marshmallow")
    src_pkg = os.path.join(repo_dir, "src", "marshmallow")
    egg_info = os.path.join(repo_dir, "marshmallow.egg-info")

    removed: list[str] = []

    def _rm(path: str) -> None:
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path)

    if has_src and not has_root:
        _rm(root_pkg)
    elif has_root and not has_src:
        _rm(src_pkg)
    elif has_src and has_root:
        # 极少数提交同时存在时，优先 src 布局
        _rm(root_pkg)
    elif os.path.isdir(root_pkg) and os.path.isdir(src_pkg):
        # 无 tracked 信息时的兜底：pyproject 声明 src 则删根目录
        pyproject = os.path.join(repo_dir, "pyproject.toml")
        if os.path.isfile(pyproject):
            text = open(pyproject, encoding="utf-8", errors="replace").read()
            if 'src = ["src"]' in text or "package-dir" in text and '"src"' in text:
                _rm(root_pkg)
            else:
                _rm(src_pkg)
        else:
            _rm(src_pkg)

    if removed:
        _rm(egg_info)

    return removed


def shell_cleanup_script() -> str:
    """容器内 bash 片段：checkout 后、pip install -e 前执行。"""
    return (
        "HAS_SRC=$(git ls-tree HEAD src/marshmallow 2>/dev/null | wc -l); "
        "HAS_ROOT=$(git ls-tree HEAD marshmallow 2>/dev/null | wc -l); "
        "if [ \"$HAS_SRC\" -gt 0 ] && [ \"$HAS_ROOT\" -eq 0 ] && [ -d marshmallow ]; then "
        "rm -rf marshmallow marshmallow.egg-info; "
        "elif [ \"$HAS_ROOT\" -gt 0 ] && [ \"$HAS_SRC\" -eq 0 ] && [ -d src/marshmallow ]; then "
        "rm -rf src/marshmallow marshmallow.egg-info; "
        "elif [ \"$HAS_SRC\" -gt 0 ] && [ -d marshmallow ]; then "
        "rm -rf marshmallow marshmallow.egg-info; "
        "fi"
    )
