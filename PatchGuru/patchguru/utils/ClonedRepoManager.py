# This file is developed based on the code from the [Testora](https://github.com/michaelpradel/Testora) project by Michael Pradel.
from dataclasses import dataclass
import json
import os
from os.path import exists
import subprocess
from typing import List
from git import Repo
import time
from patchguru.utils.PythonLanguageServer import PythonLanguageServer
from patchguru.utils.marshmallow_layout import fix_marshmallow_duplicate_layout
from patchguru.utils.git_network import git_fetch_with_retry


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
        self.nb_clones = int(os.environ.get("PATCHGURU_NB_CLONES", str(self.nb_clones)))
        self.pinned_clone_id = os.environ.get("PATCHGURU_CLONE_ID")

        self.clone_state_file = f"{self.pool_dir}/clone_state_{repo_name}.json"
        self._read_clone_state()

        if self.pinned_clone_id:
            if self.pinned_clone_id not in self.clone_id_to_state:
                raise ValueError(f"Unknown clone id: {self.pinned_clone_id}")
            self._active_clone_ids = [self.pinned_clone_id]
        else:
            self._active_clone_ids = [f"clone{i}" for i in range(1, self.nb_clones + 1)]

        self.usage_order: List[str] = list(self._active_clone_ids)

        for clone_id in self._active_clone_ids:
            self._reset_and_clean_clone(clone_id)

        self.clone_id_to_language_server = {}
        for clone_id in self._active_clone_ids:
            clone_path = f"{self.pool_dir}/{clone_id}/{self.repo_name}"
            if not exists(clone_path):
                raise FileNotFoundError(f"Clone directory {clone_path} does not exist.")
            self.clone_id_to_language_server[clone_id] = PythonLanguageServer(clone_path)

    def _read_clone_state(self):
        if not exists(self.clone_state_file):
            self.clone_id_to_state = {
                f"clone{i}": {"commit": "unknown", "container_name": f"{self.container_base_name}{i}"}
                for i in range(1, self.nb_clones + 1)
            }
            return

        with open(self.clone_state_file, "r") as f:
            self.clone_id_to_state = json.load(f)

        expanded = False
        for i in range(1, self.nb_clones + 1):
            clone_id = f"clone{i}"
            if clone_id not in self.clone_id_to_state:
                self.clone_id_to_state[clone_id] = {
                    "commit": "unknown",
                    "container_name": f"{self.container_base_name}{i}",
                }
                expanded = True
        pinned = os.environ.get("PATCHGURU_CLONE_ID")
        if pinned and pinned not in self.clone_id_to_state:
            if exists(f"{self.pool_dir}/{pinned}/{self.repo_name}"):
                self.clone_id_to_state[pinned] = {
                    "commit": "unknown",
                    "container_name": f"{self.container_base_name}-collect",
                }
                expanded = True
        if expanded:
            self._write_clone_state()

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
        clones_dir = os.path.abspath(os.path.join(cloned_repo_dir, os.pardir, os.pardir))
        rel_path = f"{clone_id}/{self.repo_name}"
        uidgid = f"{os.getuid()}:{os.getgid()}"
        for image in (f"patchguru-{self.repo_name}-dev", "python:3.11"):
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

    def _fix_marshmallow_layout(self, clone_id: str) -> None:
        if self.repo_name != "marshmallow":
            return
        cloned_repo_dir = f"{self.pool_dir}/{clone_id}/{self.repo_name}"
        removed = fix_marshmallow_duplicate_layout(cloned_repo_dir)
        if removed:
            print(f"[marshmallow] {clone_id} 清理重复包目录: {removed}")

    def _hard_reset_submodules(self, cloned_repo: Repo) -> None:
        """Reset submodule working trees so checkout across history does not fail."""
        for args in (
            ('foreach', '--recursive', 'git', 'reset', '--hard'),
            ('foreach', '--recursive', 'git', 'clean', '-f', '-d'),
        ):
            try:
                cloned_repo.git.submodule(*args)
            except Exception:
                pass

    def _update_submodules(self, cloned_repo: Repo) -> None:
        self._hard_reset_submodules(cloned_repo)
        cloned_repo.git.submodule('update', '--init', '--recursive', '--force')

    def _reset_and_clean_clone(self, clone_id: str):
        print(clone_id)
        cloned_repo_dir = f"{self.pool_dir}/{clone_id}/{self.repo_name}"
        self._fix_clone_permissions(clone_id)
        cloned_repo = Repo(cloned_repo_dir)
        cloned_repo.git.rm('--cached', '-rf', '.')
        cloned_repo.git.reset('--hard')
        cloned_repo.git.clean('-f', '-d')
        self._hard_reset_submodules(cloned_repo)
        self._fix_marshmallow_layout(clone_id)
        git_fetch_with_retry(cloned_repo, context=f"{clone_id}/{self.repo_name}")

    def _get_least_recently_used_clone_id(self) -> str:
        return self.usage_order[0]

    def _have_used_clone_id(self, clone_id: str):
        self.usage_order.remove(clone_id)
        self.usage_order.append(clone_id)

    def _safe_checkout(self, cloned_repo: Repo, commit: str, clone_id: str | None = None):
        try:
            # -f 强制覆盖工作区残留(含 submodule 摘除 gitlink 后变成 untracked 的文件,
            # 例如 scipy 的 PROPACK/README)。不带 -x,保护 build/ 缓存避免重新编译。
            cloned_repo.git.checkout('-f', commit)
            self._update_submodules(cloned_repo)
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
                cloned_repo.git.clean('-f', '-d')
                self._hard_reset_submodules(cloned_repo)
                git_fetch_with_retry(cloned_repo, context=f"{clone_id}/{self.repo_name}/checkout")
                cloned_repo.git.checkout('-f', commit)
                self._update_submodules(cloned_repo)
                if clone_id:
                    self._fix_marshmallow_layout(clone_id)

    def get_cloned_repo(self, commit) -> ClonedRepo:
        search_ids = self._active_clone_ids

        for clone_id in search_ids:
            state = self.clone_id_to_state[clone_id]
            if state["commit"] == commit:
                self._have_used_clone_id(clone_id)
                cloned_repo_dir = f"{self.pool_dir}/{clone_id}/{self.repo_name}"
                return ClonedRepo(
                    Repo(cloned_repo_dir),
                    state["container_name"],
                    self.clone_id_to_language_server[clone_id],
                )

        clone_id = self.pinned_clone_id or self._get_least_recently_used_clone_id()
        cloned_repo_dir = f"{self.pool_dir}/{clone_id}/{self.repo_name}"
        cloned_repo = Repo(cloned_repo_dir)
        self._safe_checkout(cloned_repo, commit, clone_id)

        # update clone state
        state = self.clone_id_to_state[clone_id]
        state["commit"] = commit
        self.clone_id_to_state[clone_id] = state
        self._write_clone_state()
        self._have_used_clone_id(clone_id)

        time.sleep(1)

        return ClonedRepo(
            cloned_repo,
            state["container_name"],
            self.clone_id_to_language_server[clone_id],
        )
