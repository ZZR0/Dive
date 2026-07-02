"""容器内安装与测试执行包装（对齐 PatchGuru setup_env / expand_clones）。

背景：scipy 用 meson-python 的 **editable** 安装时，每次 ``import`` 都会调用 ``ninja``
做增量构建。本仓库的 clone 目录在 **NFS（/nvme）** 上，ninja 的 mtime/deps 跟踪在
NFS 上永远收敛不到 "no work to do"，并且每次都报 "premature end of file; recovering"
→ 每次 import 都全量重编译（scipy 实测 ~3 分钟/次）。test 的 import 被 ``timeout 300s``
包住，长编译被杀 → 永远跑不完。

按项目分策略：
- **scipy / numpy（conda kind）**：镜像里是新版 meson-python（选项名 ``build-dir``）。
  checkout 后做一次 **非 editable** 安装（``pip install .``），把编译好的 ``.so`` 装进
  conda site-packages。这样 ``import`` 不再触发 ninja，实测 0.4~0.7s。复用已有的 meson
  build 目录（``--config-settings=build-dir=<clone>/build/<cpXXX>``）做增量编译。
- **pandas（cext kind）**：镜像里的 meson-python 是 **旧版**，既不认 ``build-dir``
  （只认 ``builddir``），也做不了非 editable 打包（缺 ``prepare_metadata_for_build_editable``
  且 license_file 解析有 bug → metadata-generation-failed）。所以 pandas 保持镜像自带的
  **editable** 安装，靠 import 时 editable loader 的 ninja 增量重建（pandas 体量小，能在
  timeout 内跑完）。**绝不重装、绝不删 build 目录**（删了 loader 会找不到 build/cpXXX）。

注意：不要用 ``MESONPY_EDITABLE_SKIP`` —— 它会让 loader 的 ``find_spec`` 直接返回
None，使整个包无法 import（该变量是 loader 内部防止重建递归用的）。
"""
from __future__ import annotations

from testora.util.marshmallow_layout import shell_cleanup_script

PROJECT_KIND = {
    "pandas": "cext",
    "scipy": "conda",
    "keras": "pure",
    "marshmallow": "pure",
    "scikit-learn": "pure",
    "numpy": "conda",
    "transformers": "pure",
    "pytorch_geometric": "pure",
    "scapy": "pure",
}


def project_kind(repo_name: str) -> str:
    return PROJECT_KIND.get(repo_name, "pure")


def workdir(repo_name: str) -> str:
    return f"/home/{repo_name}"


def _conda_activate(repo_name: str) -> str:
    if repo_name == "numpy":
        return (
            "source /root/conda/etc/profile.d/conda.sh"
            " && source /root/conda/etc/profile.d/mamba.sh"
            " && mamba activate numpy-dev"
        )
    return (
        "source /root/conda/etc/profile.d/conda.sh"
        " && eval \"$(mamba shell hook --shell bash)\""
        " && mamba activate scipy-dev"
    )


def _bash(*parts: str) -> str:
    body = " && ".join(p for p in parts if p)
    return f"bash -c '{body}'"


def _container_git_prep(repo_name: str, *, submodules: bool = False) -> str:
    wd = workdir(repo_name)
    prep = f"git config --global --add safe.directory {wd}"
    if submodules and repo_name in ("scipy", "numpy"):
        prep += f" && cd {wd} && git submodule update --init --recursive"
    return prep


def _meson_noneditable_install(repo_name: str) -> str:
    """非 editable 安装（import 时不再重编译），复用 meson build 目录做增量编译。

    顺序很重要：
    1. 先 ``pip install -e .`` —— 让 build 目录与「当前 checkout 的 commit」对齐。
       直接对 golden 的旧 build 目录做非 editable 打包，会触发 meson 大规模重新生成
       pythran 目标并编译失败（scipy 实测 _max_len_seq_inner.cpp 等编译中断）。
    2. 再 ``pip install .`` 复用这个已对齐的 build 目录打包成真实安装。
    3. 若第 2 步仍失败，清空 build 目录做一次干净重建兜底。
    """
    wd = workdir(repo_name)
    find_bd = (
        f"BD=\"$(find {wd}/build -maxdepth 1 -mindepth 1 -type d 2>/dev/null | head -1)\"; "
        f"[ -n \"$BD\" ] || BD={wd}/build/testora"
    )
    noneditable = (
        "pip install -q . --no-build-isolation --config-settings=build-dir=\"$BD\""
    )
    return (
        f"pip install -q -e . --no-build-isolation; "
        f"{find_bd}; "
        f"{noneditable} || {{ echo \"[Testora] clean rebuild build-dir\"; "
        f"rm -rf \"$BD\"; {noneditable}; }}"
    )


def _pip_install_body(repo_name: str, *, quick: bool) -> str:
    wd = workdir(repo_name)
    kind = project_kind(repo_name)
    git_prep = _container_git_prep(repo_name, submodules=not quick)
    chdir = f"cd {wd}"

    if kind == "cext":
        # pandas 走 editable（镜像旧版 meson-python 做不了非 editable 打包）：
        #   - 正常情况下切 commit 后第一次 import，editable loader 用 ninja 增量重建；
        #     pandas 体量比 scipy 小，能在 timeout 内跑完。
        #   - 但若 build/cpXXX 目录缺失（早期 bug 版本曾 rm -rf 删过），loader 无法重建，
        #     import 会 FileNotFoundError。此时升级构建工具链并重新 editable 安装，
        #     重新生成 build 目录与 loader。
        #   - 绝不删 build 目录。
        upgrade = "pip install -q \"meson>=1.2.3\" meson-python ninja cython"
        recover = "pip install -q -e . --no-build-isolation"
        import_or_recover = (
            f"(python -c \"import {repo_name}\" 2>/dev/null || ({recover}))"
        )
        if quick:
            return (
                f"{_container_git_prep(repo_name)} && {chdir} && {import_or_recover}"
            )
        return f"{git_prep} && {chdir} && {upgrade} && {import_or_recover}"

    if kind == "conda":
        module = repo_name
        install = _meson_noneditable_install(repo_name)
        submodule_retry = ""
        if repo_name in ("scipy", "numpy"):
            submodule_retry = "git submodule update --init --recursive && "
        if quick:
            return (
                f"{_container_git_prep(repo_name)} && {chdir} && "
                f"(python -c \"import {module}\" 2>/dev/null || "
                f"({submodule_retry}{install}))"
            )
        return f"{git_prep} && {chdir} && {install}"

    if repo_name == "marshmallow":
        cleanup = shell_cleanup_script()
        pip_cmd = "pip install -q -e '.[dev]' 2>/dev/null || pip install -q -e ."
        base = _container_git_prep(repo_name)
        if quick:
            return (
                f"{base} && {chdir} && "
                f"(python -c \"import marshmallow\" 2>/dev/null || ({cleanup} && {pip_cmd}))"
            )
        return f"{base} && {chdir} && {cleanup} && {pip_cmd}"

    base = _container_git_prep(repo_name)
    if quick:
        return (
            f"{base} && {chdir} && "
            f"(python -c \"import {repo_name}\" 2>/dev/null || pip install -q -e .)"
        )
    return f"{base} && {chdir} && pip install -q -e ."


def install_editable_command(repo_name: str, *, quick: bool = False) -> str:
    body = _pip_install_body(repo_name, quick=quick)
    if project_kind(repo_name) == "conda":
        return _bash(_conda_activate(repo_name), body)
    return _bash(body)


def verify_import_command(repo_name: str, module_name: str) -> str:
    # 非 editable 安装后，scipy/numpy 等会拒绝「在源码树里 import」，必须从 /tmp 运行。
    body = f"cd /tmp && python -c \"import {module_name}\""
    if project_kind(repo_name) == "conda":
        return _bash(_conda_activate(repo_name), body)
    return _bash(body)


def wrap_test_command(container_name: str, repo_name: str, command: str) -> str:
    """包装 generated test 的执行命令。

    - meson 项目（pandas/scipy/numpy）：用非 editable 安装（checkout 后完成），这里
      绝不触发重装/重建；从 /tmp 运行，避免「在源码树里 import」被拒。
    - 纯 Python 项目（keras/marshmallow 等）：保留轻量 import-or-reinstall。
    """
    escaped = command.replace("'", "'\\''")

    if container_name.startswith("scipy-dev") or (
        project_kind(repo_name) == "conda" and repo_name == "scipy"
    ):
        return _bash(_conda_activate("scipy"), "cd /tmp", escaped)

    if container_name.startswith("numpy-dev"):
        return _bash(_conda_activate("numpy"), "cd /tmp", escaped)

    if project_kind(repo_name) == "cext":
        return _bash("cd /tmp", escaped)

    prep = _pip_install_body(repo_name, quick=True)
    return _bash(prep, escaped)
