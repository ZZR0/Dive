from dataclasses import dataclass
import json
import os
from os.path import exists
from pathlib import Path
import shutil
import subprocess
from typing import Dict, List
import docker
from git import Repo, GitCommandError

from testora.util.PythonLanguageServer import PythonLanguageServer
from testora.util.container_deps import install_editable_command, verify_import_command
from testora.util.git_network import git_fetch_with_retry
from testora.util.marshmallow_layout import fix_marshmallow_duplicate_layout


@dataclass
class ClonedRepo:
    repo: Repo
    container_name: str
    language_server: PythonLanguageServer


class ClonedRepoManager:
    nb_clones = 3

    def __init__(self, pool_dir, repo_name, repo_id, container_base_name, module_name):
        self.pool_dir = pool_dir
        self.repo_name = repo_name
        self.repo_id = repo_id
        self.container_base_name = container_base_name
        self.module_name = module_name
        self._deps_ready: Dict[str, str] = {}

        self.clone_state_file = f"{self.pool_dir}/clone_state_{self.repo_name}.json"
        self._read_clone_state()

        self.usage_order: List[str] = [f"clone{i}" for i in range(
            1, self.nb_clones + 1)]  # last = last used

        self._reset_and_clean_all_clones()

        # start one language server for each clone
        self.clone_id_to_language_server = {}
        for i in range(1, self.nb_clones + 1):
            server = PythonLanguageServer(
                f"{self.pool_dir}/clone{i}/{self.repo_name}")
            self.clone_id_to_language_server[f"clone{i}"] = server

    def _default_clone_state(self):
        return {
            f"clone{i}": {
                "commit": "unknown",
                "container_name": f"{self.container_base_name}{i}",
            }
            for i in range(1, self.nb_clones + 1)
        }

    def _read_clone_state(self):
        self.clone_id_to_state = self._default_clone_state()
        if not exists(self.clone_state_file):
            return

        with open(self.clone_state_file, "r") as f:
            raw = json.load(f)

        for clone_id in self.clone_id_to_state:
            if clone_id not in raw:
                continue
            entry = raw[clone_id]
            idx = clone_id.removeprefix("clone")
            expected_container = f"{self.container_base_name}{idx}"
            container_name = entry.get("container_name", expected_container)
            if not container_name.startswith(f"{self.container_base_name}"):
                container_name = expected_container
            self.clone_id_to_state[clone_id] = {
                "commit": entry.get("commit", "unknown"),
                "container_name": container_name,
            }

    def _write_clone_state(self):
        with open(self.clone_state_file, "w") as f:
            json.dump(self.clone_id_to_state, f)

    def _fix_clone_permissions(self, clone_id: str) -> None:
        """容器 root 写入挂载 clone 后，chown 回宿主机用户以便 git clean/checkout。"""
        cloned_repo_dir = os.path.abspath(
            os.path.join(self.pool_dir, clone_id, self.repo_name)
        )
        if not os.path.isdir(cloned_repo_dir):
            return

        uidgid = f"{os.getuid()}:{os.getgid()}"
        container_path = f"/home/{self.repo_name}"
        container_name = self.clone_id_to_state[clone_id].get("container_name")

        if container_name:
            try:
                client = docker.from_env()
                container = client.containers.get(container_name)
                container.start()
                result = container.exec_run(["chown", "-R", uidgid, container_path])
                if result.exit_code == 0:
                    return
            except Exception as exc:
                print(
                    f"[Testora] WARN: chown via container {container_name} failed: "
                    f"{exc!s}"[:200]
                )

        clones_dir = os.path.abspath(os.path.join(cloned_repo_dir, os.pardir, os.pardir))
        rel_path = f"{clone_id}/{self.repo_name}"
        for image in (
            f"{self.repo_name}-dev",
            f"patchguru-{self.repo_name}-dev",
            "python:3.11",
        ):
            proc = subprocess.run(
                [
                    "docker", "run", "--rm",
                    "-v", f"{clones_dir}:/clones",
                    image, "chown", "-R", uidgid, f"/clones/{rel_path}",
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if proc.returncode == 0:
                return

        print(f"[Testora] WARN: could not chown {rel_path}")

    def _reset_and_clean_all_clones(self):
        for clone_id in self.clone_id_to_state:
            self._reset_and_clean_clone(clone_id)

    def _reset_and_clean_clone(self, clone_id: str):
        cloned_repo_dir = f"{self.pool_dir}/{clone_id}/{self.repo_name}"
        self._fix_clone_permissions(clone_id)
        cloned_repo = Repo(cloned_repo_dir)
        cloned_repo.git.rm('--cached', '-rf', '.')
        cloned_repo.git.reset('--hard')
        self._git_clean_tolerant(cloned_repo, clone_id)
        git_fetch_with_retry(cloned_repo, context=f"{clone_id}/{self.repo_name}")

    def _git_clean_tolerant(self, cloned_repo: Repo, clone_id: str | None = None):
        # `git clean -fd` may fail on NFS .nfs* busy files or root-owned artifacts
        # from container exec. chown first (PatchGuru); retry once on Permission denied.
        try:
            cloned_repo.git.clean('-f', '-d')
        except GitCommandError as e:
            stderr = e.stderr or ""
            if "Device or resource busy" in stderr or ".nfs" in stderr:
                return
            if "Permission denied" in stderr and clone_id:
                print(f"[Testora] git clean permission denied on {clone_id}, retrying chown")
                self._fix_clone_permissions(clone_id)
                try:
                    cloned_repo.git.clean('-f', '-d')
                    return
                except GitCommandError as retry_exc:
                    stderr = retry_exc.stderr or ""
                    if (
                        "Device or resource busy" in stderr
                        or ".nfs" in stderr
                        or "Permission denied" in stderr
                    ):
                        print(
                            f"[Testora] WARN: git clean still incomplete for {clone_id} "
                            f"after chown; continuing"
                        )
                        return
                    raise retry_exc
            raise

    def _get_least_recently_used_clone_id(self) -> str:
        return self.usage_order[0]

    def _have_used_clone_id(self, clone_id: str):
        self.usage_order.remove(clone_id)
        self.usage_order.append(clone_id)

    def _fix_marshmallow_layout(self, clone_id: str) -> None:
        if self.repo_name != "marshmallow":
            return
        cloned_repo_dir = f"{self.pool_dir}/{clone_id}/{self.repo_name}"
        removed = fix_marshmallow_duplicate_layout(cloned_repo_dir)
        if removed:
            print(f"[Testora] marshmallow {clone_id} 清理重复包目录: {removed}")

    def _ensure_container_deps(self, clone_id: str, container_name: str, commit: str) -> None:
        if self._deps_ready.get(clone_id) == commit:
            return

        client = docker.from_env()
        container = client.containers.get(container_name)
        container.start()

        install_cmd = install_editable_command(self.repo_name, quick=False)
        install_result = container.exec_run(install_cmd, demux=True)
        install_err = (install_result.output[1] or b"").decode("utf-8", errors="replace")
        if install_result.exit_code != 0:
            print(
                f"[Testora] WARN: install failed in {container_name} "
                f"@ {commit}: {install_err[-4000:]}"
            )

        # 打印 import 来源路径，确认 meson 项目确实是「非 editable」（site-packages），
        # 而不是失败后残留的 editable（源码树）—— 后者会让每个 test 重编译陷入死循环。
        verify_cmd = verify_import_command(self.repo_name, self.module_name)
        verify_cmd = verify_cmd[:-1] + (
            f"; python -c \"import {self.module_name} as _m; print(_m.__file__)\"'"
        )
        verify_result = container.exec_run(verify_cmd, demux=True)
        verify_out = (verify_result.output[0] or b"").decode("utf-8", errors="replace")
        verify_err = (verify_result.output[1] or b"").decode("utf-8", errors="replace")
        if verify_result.exit_code != 0:
            print(
                f"[Testora] WARN: import {self.module_name} failed in {container_name} "
                f"@ {commit}: {verify_err[-2000:]}"
            )
            return

        module_file = verify_out.strip().splitlines()[-1] if verify_out.strip() else ""
        # 仅 scipy/numpy 要求装进 site-packages（非 editable）；pandas 走 editable
        # （镜像旧版 meson-python 无法非 editable 打包），从 /home/pandas import 是正常的。
        editable_leftover = (
            self._requires_noneditable()
            and module_file.startswith(f"/home/{self.repo_name}/")
        )
        if editable_leftover:
            print(
                f"[Testora] WARN: {container_name} 仍是 editable 安装（import 自 "
                f"{module_file}），非 editable 安装未成功，test 将重编译。"
                f" 安装错误尾部: {install_err[-2000:]}"
            )
            return

        self._deps_ready[clone_id] = commit
        print(f"[Testora] {container_name} ready @ {commit[:12]} ({module_file})")

    def _is_meson_project(self) -> bool:
        return self.repo_name in ("pandas", "scipy", "numpy")

    def _requires_noneditable(self) -> bool:
        # 只有 scipy/numpy 在容器里用新版 meson-python 做了非 editable 安装，
        # 必须从 site-packages import；pandas 仍是 editable，不在此列。
        return self.repo_name in ("scipy", "numpy")

    def _finalize_cloned_repo(self, clone_id: str, commit: str) -> ClonedRepo:
        state = self.clone_id_to_state[clone_id]
        self._ensure_container_deps(clone_id, state["container_name"], commit)
        cloned_repo_dir = f"{self.pool_dir}/{clone_id}/{self.repo_name}"
        return ClonedRepo(
            Repo(cloned_repo_dir),
            state["container_name"],
            self.clone_id_to_language_server[clone_id],
        )

    def _safe_checkout(self, cloned_repo: Repo, commit: str, clone_id: str | None = None):
        try:
            # -f 覆盖工作区残留；不带 -x，保留 build/ 缓存避免重编译。
            cloned_repo.git.checkout("-f", commit)
            cloned_repo.git.submodule('update', '--init', '--recursive')
            if clone_id:
                self._fix_marshmallow_layout(clone_id)
        except Exception:
            if commit == "main":
                self._safe_checkout(cloned_repo, "master", clone_id)
            elif commit == "master":
                self._safe_checkout(cloned_repo, "dev", clone_id)
            else:
                if clone_id:
                    self._fix_clone_permissions(clone_id)
                cloned_repo.git.rm('--cached', '-rf', '.')
                cloned_repo.git.reset('--hard')
                self._git_clean_tolerant(cloned_repo, clone_id)
                git_fetch_with_retry(
                    cloned_repo, context=f"{clone_id}/{self.repo_name}/checkout"
                )
                try:
                    cloned_repo.git.checkout("-f", commit)
                    cloned_repo.git.submodule('update', '--init', '--recursive')
                    if clone_id:
                        self._fix_marshmallow_layout(clone_id)
                except Exception:
                    # we get here when submodules are in a strange state
                    self._remove_and_reinit_submodules(cloned_repo, commit, clone_id)

    def _remove_and_reinit_submodules(
            self, cloned_repo: Repo, commit: str, clone_id: str | None = None):
        # 1) de-initialize all submodules
        cloned_repo.git.submodule('deinit', '-f', '--all')

        # 2) remove all submodule working trees
        root = Path(cloned_repo.working_dir)
        ls_output = subprocess.run(
            ["git", "ls-files", "-s"], capture_output=True, text=True, check=True
        ).stdout.splitlines()
        for line in ls_output:
            parts = line.split()
            if len(parts) >= 4 and parts[0] == "160000":
                path = " ".join(parts[3:])
                shutil.rmtree(root / path, ignore_errors=True)

        # 3) remove all submodule git metadata under .git/modules
        modules_dir = root / ".git" / "modules"
        if modules_dir.exists():
            for child in modules_dir.iterdir():
                shutil.rmtree(child, ignore_errors=True)

        # 4) checkout the desired commit
        cloned_repo.git.checkout("-f", commit)

        # 5) re-initialize submodules recursively
        cloned_repo.git.submodule('update', '--init', '--recursive')
        if clone_id:
            self._fix_marshmallow_layout(clone_id)

    def get_cloned_repo(self, commit) -> ClonedRepo:
        # reuse existing clone if possible
        for clone_id, state in self.clone_id_to_state.items():
            if state["commit"] == commit:
                self._have_used_clone_id(clone_id)
                cloned_repo_dir = f"{self.pool_dir}/{clone_id}/{self.repo_name}"
                cloned_repo = Repo(cloned_repo_dir)
                if commit in ("main", "master") and cloned_repo.head.is_detached:
                    self._safe_checkout(cloned_repo, commit, clone_id)

                return self._finalize_cloned_repo(clone_id, commit)

        # checkout desired commit
        clone_id = self._get_least_recently_used_clone_id()
        cloned_repo_dir = f"{self.pool_dir}/{clone_id}/{self.repo_name}"
        self._fix_clone_permissions(clone_id)
        cloned_repo = Repo(cloned_repo_dir)
        self._safe_checkout(cloned_repo, commit, clone_id)

        # update clone state
        state = self.clone_id_to_state[clone_id]
        state["commit"] = commit
        self.clone_id_to_state[clone_id] = state
        self._write_clone_state()
        self._have_used_clone_id(clone_id)

        return self._finalize_cloned_repo(clone_id, commit)
