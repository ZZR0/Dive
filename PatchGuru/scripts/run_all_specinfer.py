#!/usr/bin/env python3
"""批量运行 SpecInfer：支持多路并行，每个 worker 绑定独立 clone + Docker 容器。"""
from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PR_IDS_DIR = ROOT / ".cache" / "pr_ids"
TOKENS_FILE = ROOT / ".github_tokens"
PROGRESS_FILE = ROOT / "scripts" / "run_all_progress.jsonl"

PROJECTS = ["marshmallow", "pandas", "scipy", "keras"]


def container_prefix(project: str) -> str:
    tag = os.environ.get("PATCHGURU_CONTAINER_TAG", "").strip()
    if tag:
        return f"{tag}-{project}-dev"
    return f"{project}-dev"

_progress_lock = threading.Lock()
_token_lock = threading.Lock()
_token_counter = 0

_HOST_PROXY = (
    os.environ.get("PATCHGURU_HOST_PROXY")
    or os.environ.get("GIT_PROXY")
    or "socks5h://127.0.0.1:10808"
)


def _inject_host_proxy(env: dict[str, str]) -> None:
    if not _HOST_PROXY:
        return
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "GIT_PROXY"):
        env.setdefault(key, _HOST_PROXY)
    env.setdefault("PATCHGURU_HOST_PROXY", _HOST_PROXY)


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_pr_ids(project: str) -> list[int]:
    path = PR_IDS_DIR / f"{project}.txt"
    ids = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                ids.append(int(line))
    return ids


def load_pr_file(path: Path) -> list[tuple[str, int]]:
    prs: list[tuple[str, int]] = []
    with open(path) as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 2:
                raise ValueError(f"{path}:{lineno}: expected 'project pr_nb', got {raw!r}")
            project, pr_nb = parts[0], int(parts[1])
            if project not in PROJECTS:
                raise ValueError(f"{path}:{lineno}: unknown project {project!r}")
            prs.append((project, pr_nb))
    return prs


def docker_count(project: str) -> int:
    prefix = container_prefix(project)
    try:
        out = subprocess.check_output(
            ["docker", "ps", "--filter", f"name={prefix}", "--format", "{{.Names}}"],
            text=True,
        )
        return sum(1 for name in out.strip().splitlines() if name.startswith(prefix))
    except subprocess.CalledProcessError:
        return 0


def docker_ready(project: str, required: int) -> bool:
    return docker_count(project) >= required


def next_token_index() -> int:
    global _token_counter
    with _token_lock:
        _token_counter += 1
        return _token_counter


def pick_github_token(index: int) -> str:
    if TOKENS_FILE.exists():
        tokens = [t.strip() for t in TOKENS_FILE.read_text().splitlines() if t.strip()]
        if tokens:
            return tokens[index % len(tokens)]
    return (ROOT / ".github_token").read_text().strip()


def is_completed(cache_dir: Path) -> bool:
    p1 = cache_dir / "results.json"
    if not p1.exists():
        return False
    try:
        phase1 = json.loads(p1.read_text())
    except json.JSONDecodeError:
        return False
    if phase1.get("stage") != "completed":
        return False
    if phase1.get("review_conclusion") == "BUG":
        return True
    p2 = cache_dir / "phase2" / "results.json"
    if not p2.exists():
        return False
    try:
        phase2 = json.loads(p2.read_text())
    except json.JSONDecodeError:
        return False
    return phase2.get("stage") == "completed"


def run_one(project: str, pr_nb: int, args, clone_id: str) -> dict:
    cache_base = ROOT / args.cache_dir / "oracles" / project / str(pr_nb)
    if not args.force and is_completed(cache_base):
        log(f"⊘ {project} PR#{pr_nb} [{clone_id}] skipped (already completed)")
        return {
            "status": "skipped",
            "project": project,
            "pr_nb": pr_nb,
            "clone_id": clone_id,
            "reason": "already_completed",
        }

    token_index = next_token_index()
    token = pick_github_token(token_index)
    with _token_lock:
        (ROOT / ".github_token").write_text(token + "\n")

    env = os.environ.copy()
    env["PATCHGURU_CACHE_DIR"] = args.cache_dir
    if args.phase2_strategy:
        env["PATCHGURU_PHASE2_STRATEGY"] = args.phase2_strategy
    if args.dive_seed_baseline_dir:
        env["PATCHGURU_DIVE_SEED_BASELINE_DIR"] = args.dive_seed_baseline_dir
    if args.dive_ablation:
        env["PATCHGURU_DIVE_ABLATION"] = args.dive_ablation
    clones_dir = os.environ.get("PATCHGURU_CLONES_DIR")
    if clones_dir:
        env["PATCHGURU_CLONES_DIR"] = clones_dir
    container_tag = os.environ.get("PATCHGURU_CONTAINER_TAG")
    if container_tag:
        env["PATCHGURU_CONTAINER_TAG"] = container_tag
    env.pop("PATCHGURU_LOG_DIR", None)
    env.pop("PATCHGURU_LOG_SESSION", None)
    env.pop("PATCHGURU_LOG_FLAT", None)
    env["PATCHGURU_CLONE_ID"] = clone_id
    env["PATCHGURU_NB_CLONES"] = str(args.nb_clones)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    _inject_host_proxy(env)

    cmd = [
        "uv", "run", "python", "-m", "patchguru.SpecInfer",
        "--project", project,
        "--pr_nb", str(pr_nb),
    ]
    if args.force:
        cmd.append("--force")

    log(f"▶ {project} PR#{pr_nb} [{clone_id}] (token #{token_index % 16 + 1})")
    t0 = time.time()
    # 独立进程组启动，超时后整组 SIGKILL（含 uv -> python -> docker exec 等孙进程），
    # 避免个别 PR 卡死（如 LLM/网络挂起）永久占用 worker。
    proc = subprocess.Popen(
        cmd, cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=args.timeout)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        returncode = -1
    elapsed = time.time() - t0

    record = {
        "time": datetime.now().isoformat(),
        "project": project,
        "pr_nb": pr_nb,
        "clone_id": clone_id,
        "status": "ok" if (not timed_out and returncode == 0) else "fail",
        "exit_code": returncode,
        "elapsed_sec": round(elapsed, 1),
    }
    if timed_out:
        record["error_tail"] = f"TIMEOUT after {args.timeout}s"
        log(f"⏱ {project} PR#{pr_nb} [{clone_id}] TIMEOUT killed ({elapsed:.0f}s)")
    elif returncode != 0:
        tail = (stderr or stdout or "")[-2000:]
        record["error_tail"] = tail
        log(f"✗ {project} PR#{pr_nb} [{clone_id}] failed ({elapsed:.0f}s)")
    else:
        log(f"✓ {project} PR#{pr_nb} [{clone_id}] done ({elapsed:.0f}s)")
    return record


def append_progress(record: dict) -> None:
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def worker(worker_id: int, task_queue: queue.Queue, args, stats: dict) -> None:
    clone_id = f"clone{worker_id + 1}"
    while True:
        try:
            project, pr_nb = task_queue.get_nowait()
        except queue.Empty:
            return

        record = run_one(project, pr_nb, args, clone_id=clone_id)
        with _progress_lock:
            append_progress(record)
            status = record["status"]
            stats[status] = stats.get(status, 0) + 1
            done = stats.get("ok", 0) + stats.get("fail", 0) + stats.get("skipped", 0)
            log(
                f"汇总进度 {done}/{stats['total']} | ok={stats.get('ok', 0)} "
                f"skip={stats.get('skipped', 0)} fail={stats.get('fail', 0)}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch run SpecInfer on all benchmark PRs")
    parser.add_argument("--cache-dir", default=".cache_rerun", help="Output cache (default: .cache_rerun)")
    parser.add_argument("--force", action="store_true", help="Force re-analyze even if cached")
    parser.add_argument(
        "--pr-file",
        type=Path,
        help="只跑文件中的 PR，每行格式: <project> <pr_nb>",
    )
    parser.add_argument("--projects", nargs="*", default=PROJECTS)
    parser.add_argument("--workers", type=int, default=10, help="Parallel workers (default: 10)")
    parser.add_argument("--nb-clones", type=int, default=10, help="Clone/container pool size (default: 10)")
    parser.add_argument("--timeout", type=int, default=1800, help="Per-PR timeout seconds; kill on exceed (default: 1800)")
    parser.add_argument(
        "--phase2-strategy",
        choices=("baseline", "dive"),
        help="Phase 2 strategy (sets PATCHGURU_PHASE2_STRATEGY for child processes)",
    )
    parser.add_argument(
        "--dive-seed-baseline-dir",
        help="Baseline cache for DIVE seed fusion (sets PATCHGURU_DIVE_SEED_BASELINE_DIR)",
    )
    parser.add_argument(
        "--dive-ablation",
        choices=("none", "no_constructor", "no_guided", "no_triage", "search_only"),
        help="DIVE ablation / search-only baseline (sets PATCHGURU_DIVE_ABLATION)",
    )
    args = parser.parse_args()

    if args.workers > args.nb_clones:
        log(f"WARN: workers={args.workers} > nb_clones={args.nb_clones}, clamping workers")
        args.workers = args.nb_clones

    if not (ROOT / ".openai_token").exists():
        log("ERROR: missing .openai_token")
        return 1

    all_prs: list[tuple[str, int]] = []
    if args.pr_file:
        if not args.pr_file.is_file():
            log(f"ERROR: --pr-file 不存在: {args.pr_file}")
            return 1
        candidates = load_pr_file(args.pr_file)
        projects_needed = sorted({p for p, _ in candidates})
        ready = {
            p for p in projects_needed
            if docker_count(p) >= args.workers
        }
        for project in projects_needed:
            count = docker_count(project)
            if project not in ready:
                log(
                    f"WARN: skip {project} — 需要 {args.workers} 个容器，"
                    f"当前 {count} 个 ({container_prefix(project)}*)"
                )
        for project, pr_nb in candidates:
            if project not in args.projects:
                continue
            if project in ready:
                all_prs.append((project, pr_nb))
    else:
        for project in args.projects:
            count = docker_count(project)
            if count < args.workers:
                log(
                    f"WARN: skip {project} — 需要 {args.workers} 个容器，"
                    f"当前 {count} 个 ({container_prefix(project)}*)"
                )
                continue
            for pr_nb in load_pr_ids(project):
                all_prs.append((project, pr_nb))

    total = len(all_prs)
    log(
        f"计划运行 {total} 个 PR | workers={args.workers} | "
        f"nb_clones={args.nb_clones} | cache={args.cache_dir} | force={args.force} | "
        f"clones={os.environ.get('PATCHGURU_CLONES_DIR', '../clones')} | "
        f"container_tag={os.environ.get('PATCHGURU_CONTAINER_TAG') or 'default'} | "
        f"host_proxy={_HOST_PROXY or 'none'}"
    )

    if total == 0:
        log("没有可运行的 PR，请先扩容 clone/容器: NB_CLONES=10 bash scripts/expand_project_clones.sh <project>")
        return 1

    task_queue: queue.Queue = queue.Queue()
    for item in all_prs:
        task_queue.put(item)

    stats = {"total": total, "ok": 0, "skipped": 0, "fail": 0}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(worker, i, task_queue, args, stats) for i in range(args.workers)]
        for fut in futures:
            fut.result()

    log(
        f"完成: ok={stats.get('ok', 0)} skip={stats.get('skipped', 0)} "
        f"fail={stats.get('fail', 0)} | {PROGRESS_FILE}"
    )
    return 0 if stats.get("fail", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
