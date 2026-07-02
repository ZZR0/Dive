#!/usr/bin/env python3
"""批量运行 Mutation analysis（DIVE / baseline new200 cache），支持日志文件与并行。

默认使用与消融/Testora 相同的隔离资源池：
  PATCHGURU_CLONES_DIR=/nvme/zzr/patch_reach/clones_pgabl
  PATCHGURU_CONTAINER_TAG=pgabl  -> 容器名 pgabl-<repo>-devN

用法:
  # 按 repo 并行（四库各一个进程），详细输出进 logs/
  uv run python scripts/run_mutation.py --method dive --parallel repo --workers 4

  # 按 PR 并行（各 PR 绑定 clone1..N）
  uv run python scripts/run_mutation.py --method both --parallel pr --workers 3

  # 单 PR 日志默认写入各 cache 的 mutation_testing/ 下（与结果同目录）
  uv run python scripts/run_mutation.py --method dive --repo pandas --pr-id 63452

终端只打印任务调度摘要；PR 详情写入 {cache}/mutation_testing/{repo}/{pr}/phase2/run.log。
"""
from __future__ import annotations

import argparse
import queue
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
DEFAULT_PROJECTS = ["marshmallow", "pandas", "scipy", "keras"]
DEFAULT_POOL_TAG = "pgabl"
CACHE_BY_METHOD = {
    "dive": ROOT / ".cache_dive_new200",
    "baseline": ROOT / ".cache_baseline_new200",
}
MODULE = "patchguru.experiments.MutationAnalysis"
BATCH_DIR = ROOT / "scripts" / "pr_batch_300"

_print_lock = threading.Lock()


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _print_lock:
        print(f"[{ts}] {msg}", flush=True)


def resolve_pool_config(pool_tag: str | None, clones_dir: Path | None) -> tuple[str, Path]:
    tag = (
        pool_tag
        or os.environ.get("PATCHGURU_CONTAINER_TAG")
        or os.environ.get("POOL_TAG")
        or DEFAULT_POOL_TAG
    )
    if clones_dir is not None:
        clones = clones_dir if clones_dir.is_absolute() else ROOT / clones_dir
    else:
        env_clones = os.environ.get("PATCHGURU_CLONES_DIR")
        clones = Path(env_clones) if env_clones else REPO_ROOT / f"clones_{tag}"
    return tag, clones.resolve()


def build_env(pool_tag: str, clones_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    mutmut_path = str(ROOT / "mutmut")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{mutmut_path}:{existing}" if existing else mutmut_path
    env["PATCHGURU_CLONES_DIR"] = str(clones_dir)
    env["PATCHGURU_CONTAINER_TAG"] = pool_tag
    return env


def container_prefix(project: str, pool_tag: str) -> str:
    return f"{pool_tag}-{project}-dev"


def docker_count(project: str, pool_tag: str) -> int:
    prefix = container_prefix(project, pool_tag)
    try:
        out = subprocess.check_output(
            ["docker", "ps", "--filter", f"name={prefix}", "--format", "{{.Names}}"],
            text=True,
        )
        return sum(1 for name in out.strip().splitlines() if name.startswith(prefix))
    except subprocess.CalledProcessError:
        return 0


def guess_pr_file(cache_dir: Path) -> Path | None:
    name = cache_dir.name
    if "new200" in name and (BATCH_DIR / "new200.txt").is_file():
        return BATCH_DIR / "new200.txt"
    if "new100" in name and (BATCH_DIR / "new100.txt").is_file():
        return BATCH_DIR / "new100.txt"
    return None


def resolve_pr_file(cache_dir: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        path = explicit if explicit.is_absolute() else ROOT / explicit
        if not path.is_file():
            raise FileNotFoundError(f"--pr-file 不存在: {path}")
        return path
    for candidate in (cache_dir / "run_pr_file.txt", cache_dir / "dive_prs.txt"):
        if candidate.is_file():
            return candidate
    guessed = guess_pr_file(cache_dir)
    if guessed is not None:
        return guessed
    raise FileNotFoundError(f"无法推断 PR 列表，请指定 --pr-file（cache={cache_dir}）")


def load_pr_ids_for_project(project: str, pr_file: Path) -> list[int]:
    ids: list[int] = []
    with open(pr_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) == 2 and parts[0] == project:
                ids.append(int(parts[1]))
    return sorted(ids)


def is_oracle_completed(results_path: Path) -> bool:
    if not results_path.is_file():
        return False
    try:
        data = json.loads(results_path.read_text())
    except json.JSONDecodeError:
        return False
    return data.get("stage") == "completed"


def iter_eligible_pr_ids(cache_dir: Path, repo: str, phase: str, pr_file: Path) -> list[int]:
    if phase == "phase1":
        spec_rel = Path("specification.py")
        results_rel = Path("results.json")
    else:
        spec_rel = Path("phase2/specification.py")
        results_rel = Path("phase2/results.json")

    allowed = set(load_pr_ids_for_project(repo, pr_file))
    oracle_dir = cache_dir / "oracles" / repo
    if not oracle_dir.is_dir():
        return []

    eligible: list[int] = []
    for pr_dir in sorted(oracle_dir.iterdir()):
        if not pr_dir.is_dir():
            continue
        try:
            pr_id = int(pr_dir.name)
        except ValueError:
            continue
        if pr_id not in allowed:
            continue
        if (pr_dir / spec_rel).is_file() and is_oracle_completed(pr_dir / results_rel):
            eligible.append(pr_id)
    return eligible


@dataclass(frozen=True)
class MutationTask:
    method: str
    cache_dir: Path
    repo: str
    phase: str
    pr_id: int | None
    log_path: Path
    limit: int | None
    pr_file: Path


def task_log_path(cache_dir: Path, phase: str, repo: str, pr_id: int | None) -> Path:
    """{cache}/mutation_testing/{repo}/{pr}/phase2/run.log（与 mutation_results.json 同目录）。"""
    if pr_id is None:
        return cache_dir / "mutation_testing" / "logs" / f"{repo}.log"
    base = cache_dir / "mutation_testing" / repo / str(pr_id)
    if phase == "phase2":
        base = base / "phase2"
    return base / "run.log"


def master_log_path(cache_dir: Path) -> Path:
    return cache_dir / "mutation_testing" / "run_mutation_master.log"


def build_tasks(
    methods: list[str],
    repos: list[str],
    *,
    phase: str,
    pr_file: Path | None,
    pr_id_filter: int | None,
    limit: int | None,
    parallel: str,
    legacy_log_dir: Path | None = None,
) -> list[MutationTask]:
    def log_path_for(cache_dir: Path, method: str, repo: str, pr_id: int | None) -> Path:
        if legacy_log_dir is not None:
            if pr_id is None:
                return legacy_log_dir / f"{method}_{repo}.log"
            return legacy_log_dir / method / repo / f"{pr_id}.log"
        return task_log_path(cache_dir, phase, repo, pr_id)

    tasks: list[MutationTask] = []
    for method in methods:
        cache_dir = CACHE_BY_METHOD[method]
        if not cache_dir.is_dir():
            log(f"Skip {method}: cache not found at {cache_dir}")
            continue
        resolved_pr_file = resolve_pr_file(cache_dir, pr_file)
        for repo in repos:
            pr_ids = iter_eligible_pr_ids(cache_dir, repo, phase, resolved_pr_file)
            if pr_id_filter is not None:
                pr_ids = [p for p in pr_ids if p == pr_id_filter]
            if not pr_ids:
                log(f"Skip {method}/{repo}: 无 eligible PR")
                continue

            if parallel == "repo":
                tasks.append(
                    MutationTask(
                        method=method,
                        cache_dir=cache_dir,
                        repo=repo,
                        phase=phase,
                        pr_id=None,
                        log_path=log_path_for(cache_dir, method, repo, None),
                        limit=limit,
                        pr_file=resolved_pr_file,
                    )
                )
            else:
                prs = pr_ids if limit is None else pr_ids[:limit]
                for pr_id in prs:
                    tasks.append(
                        MutationTask(
                            method=method,
                            cache_dir=cache_dir,
                            repo=repo,
                            phase=phase,
                            pr_id=pr_id,
                            log_path=log_path_for(cache_dir, method, repo, pr_id),
                            limit=None,
                            pr_file=resolved_pr_file,
                        )
                    )
    return tasks


def make_clone_pools(repos: list[str], nb_clones: int) -> dict[str, queue.Queue[str]]:
    pools: dict[str, queue.Queue[str]] = {}
    for repo in repos:
        q: queue.Queue[str] = queue.Queue()
        for i in range(1, nb_clones + 1):
            q.put(f"clone{i}")
        pools[repo] = q
    return pools


def run_task(
    task: MutationTask,
    env_base: dict[str, str],
    nb_clones: int,
    clone_pools: dict[str, queue.Queue[str]] | None,
) -> tuple[int, MutationTask, float]:
    env = env_base.copy()
    clone_id: str | None = None
    if task.pr_id is not None and clone_pools is not None:
        clone_id = clone_pools[task.repo].get()
        env["PATCHGURU_CLONE_ID"] = clone_id
        env["PATCHGURU_NB_CLONES"] = str(nb_clones)

    cmd = [
        sys.executable,
        "-m",
        MODULE,
        "--cache-dir",
        str(task.cache_dir),
        "--repo",
        task.repo,
        "--phase",
        task.phase,
        "--pr-file",
        str(task.pr_file),
    ]
    if task.pr_id is not None:
        cmd.extend(["--pr-id", str(task.pr_id)])
    if task.limit is not None:
        cmd.extend(["--limit", str(task.limit)])

    task.log_path.parent.mkdir(parents=True, exist_ok=True)
    label = f"{task.method}/{task.repo}"
    if task.pr_id is not None:
        label += f"#{task.pr_id}"
    if clone_id:
        label += f" [{clone_id}]"

    log(f"▶ {label} → {task.log_path}")
    t0 = time.time()
    try:
        with open(task.log_path, "w", encoding="utf-8") as log_f:
            log_f.write(f"# started: {datetime.now().isoformat()}\n")
            log_f.write(f"# cmd: {' '.join(cmd)}\n\n")
            log_f.flush()
            proc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=log_f, stderr=subprocess.STDOUT)
        elapsed = time.time() - t0
        status = "✓" if proc.returncode == 0 else "✗"
        log(f"{status} {label} ({elapsed:.0f}s, exit={proc.returncode})")
        return proc.returncode, task, elapsed
    finally:
        if clone_id is not None and clone_pools is not None:
            clone_pools[task.repo].put(clone_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch mutation analysis with logging and parallelism")
    parser.add_argument(
        "--method",
        choices=["dive", "baseline", "both"],
        default="both",
        help="Which cache to run (default: both)",
    )
    parser.add_argument(
        "--repo",
        action="append",
        dest="repos",
        help="Repository (repeatable). Default: all four projects",
    )
    parser.add_argument("--phase", choices=["phase1", "phase2"], default="phase2")
    parser.add_argument("--pr-file", type=Path, default=None)
    parser.add_argument("--pr-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None, help="Max PRs per repo (smoke test)")
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="可选：覆盖默认日志位置（默认写入各 cache 的 mutation_testing/）",
    )
    parser.add_argument(
        "--parallel",
        choices=["repo", "pr"],
        default="repo",
        help="repo=每库一个进程（推荐）；pr=每个 PR 独立进程并绑定 clone",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="最大并行任务数（default: 4）",
    )
    parser.add_argument(
        "--nb-clones",
        type=int,
        default=3,
        help="clone 数量，pr 并行时用于绑定 PATCHGURU_CLONE_ID（default: 3）",
    )
    parser.add_argument(
        "--pool-tag",
        type=str,
        default=None,
        help=f"Docker 容器前缀标签（默认 {DEFAULT_POOL_TAG}）",
    )
    parser.add_argument(
        "--clones-dir",
        type=Path,
        default=None,
        help="Git clone 池目录（默认 ../clones_pgabl）",
    )
    args = parser.parse_args()

    pool_tag, clones_dir = resolve_pool_config(args.pool_tag, args.clones_dir)
    env = build_env(pool_tag, clones_dir)
    legacy_log_dir: Path | None = None
    if args.log_dir is not None:
        legacy_log_dir = args.log_dir if args.log_dir.is_absolute() else ROOT / args.log_dir
        legacy_log_dir.mkdir(parents=True, exist_ok=True)

    repos = args.repos or DEFAULT_PROJECTS
    methods = ["dive", "baseline"] if args.method == "both" else [args.method]

    workers = args.workers
    if args.parallel == "pr" and workers > args.nb_clones:
        log(f"WARN: pr 并行时 workers={workers} > nb_clones={args.nb_clones}，同库可能争用 clone")

    for repo in repos:
        need = args.nb_clones if args.parallel == "pr" else 1
        count = docker_count(repo, pool_tag)
        if count < need:
            log(f"WARN: {repo} 仅 {count} 个容器（需要 ≥{need}，前缀 {container_prefix(repo, pool_tag)}*）")

    tasks = build_tasks(
        methods,
        repos,
        phase=args.phase,
        pr_file=args.pr_file,
        pr_id_filter=args.pr_id,
        limit=args.limit,
        parallel=args.parallel,
        legacy_log_dir=legacy_log_dir,
    )
    if not tasks:
        log("没有可运行的 mutation 任务")
        return 1

    clone_pools = make_clone_pools(repos, args.nb_clones) if args.parallel == "pr" else None

    cache_dirs = sorted({task.cache_dir for task in tasks})
    log_desc = str(legacy_log_dir) if legacy_log_dir else ", ".join(
        str(c / "mutation_testing") for c in cache_dirs
    )
    log(f"PATCHGURU_CLONES_DIR={clones_dir}")
    log(f"PATCHGURU_CONTAINER_TAG={pool_tag}")
    log(f"parallel={args.parallel} workers={workers} tasks={len(tasks)} logs={log_desc}")

    rc = 0
    t0 = time.time()
    for cache_dir in cache_dirs:
        master = master_log_path(cache_dir)
        master.parent.mkdir(parents=True, exist_ok=True)
        with open(master, "a", encoding="utf-8") as f:
            f.write(f"\n# session {datetime.now().isoformat()} tasks={len(tasks)}\n")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(run_task, task, env, args.nb_clones, clone_pools): task
            for task in tasks
        }
        for future in as_completed(futures):
            try:
                code, finished, elapsed = future.result()
                master = master_log_path(finished.cache_dir)
                with open(master, "a", encoding="utf-8") as f:
                    f.write(
                        f"{datetime.now().isoformat()} "
                        f"exit={code} elapsed={elapsed:.0f}s "
                        f"{finished.method}/{finished.repo}"
                        f"{'' if finished.pr_id is None else '#' + str(finished.pr_id)} "
                        f"log={finished.log_path}\n"
                    )
                if code != 0:
                    rc = code
            except Exception as exc:
                task = futures[future]
                log(f"✗ {task.method}/{task.repo} 异常: {exc}")
                rc = 1

    masters = ", ".join(str(master_log_path(c)) for c in cache_dirs)
    log(f"完成 {len(tasks)} 个任务，耗时 {time.time() - t0:.0f}s，master: {masters}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
