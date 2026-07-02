#!/usr/bin/env python3
"""批量运行 Testora RegressionFinder。

用法:
  python scripts/run_cases.py
  python scripts/run_cases.py --projects marshmallow pandas
  python scripts/run_cases.py --case-file scripts/dive_common_bugs.txt --force
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASE_FILE = ROOT / "scripts" / "dive_common_bugs.txt"
DEFAULT_OUTPUT_DIR = ROOT / ".results_dive_common_bugs"
SUPPORTED = {"keras", "marshmallow", "pandas", "scipy"}


def log(msg: str, log_file: Path | None = None) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    if log_file is not None:
        try:
            with log_file.open("a") as f:
                f.write(line + "\n")
        except OSError as e:
            print(f"[WARN] 无法写入日志 {log_file}: {e}", flush=True)


def load_cases(case_file: Path, projects: set[str] | None) -> list[tuple[str, int]]:
    cases: list[tuple[str, int]] = []
    for raw in case_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"无效 case 行: {raw!r}，应为 'project pr'")
        project, pr_s = parts
        if project not in SUPPORTED:
            raise ValueError(f"不支持的项目: {project}")
        if projects is not None and project not in projects:
            continue
        cases.append((project, int(pr_s)))
    return cases


def _work_log_path(output_dir: Path, project: str, pr_nb: int) -> Path:
    work_dir = output_dir / "logs" / "_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir / f"{project}_{pr_nb}.json"


def run_one(
    project: str,
    pr_nb: int,
    *,
    output_dir: Path,
    force: bool,
    python: str,
    log_file: Path | None,
) -> str:
    dest = output_dir / project / f"{pr_nb}.json"
    if dest.exists() and dest.stat().st_size > 0 and not force:
        log(f"⊘ {project} PR#{pr_nb} skipped (已有结果)", log_file)
        return "skipped"
    if dest.exists() and dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)

    work_log = _work_log_path(output_dir, project, pr_nb)
    work_log.unlink(missing_ok=True)

    cmd = [python, "-m", "testora.RegressionFinder", "--project", project, "--pr", str(pr_nb)]
    env = os.environ.copy()
    env["TESTORA_LOG_FILE"] = str(work_log.resolve())
    log(f"▶ {project} PR#{pr_nb}", log_file)
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=ROOT, env=env)
    elapsed = time.time() - t0

    if proc.returncode != 0:
        work_log.unlink(missing_ok=True)
        log(f"✗ {project} PR#{pr_nb} fail (exit={proc.returncode}, {elapsed:.0f}s)", log_file)
        return "fail"

    if not work_log.exists() or work_log.stat().st_size == 0:
        work_log.unlink(missing_ok=True)
        log(f"✗ {project} PR#{pr_nb} 未生成 log (exit={proc.returncode})", log_file)
        return "fail"

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(work_log), str(dest))
    log(f"✓ {project} PR#{pr_nb} ok ({elapsed:.0f}s) -> {dest}", log_file)
    return "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description="批量运行 Testora cases")
    parser.add_argument("--case-file", type=Path, default=DEFAULT_CASE_FILE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--projects", nargs="+", choices=sorted(SUPPORTED))
    parser.add_argument("--force", action="store_true", help="重跑已有结果的 PR")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument(
        "--run-log",
        type=Path,
        default=None,
        help="进度日志路径（默认仅 stdout）",
    )
    args = parser.parse_args()

    if not args.case_file.is_absolute():
        args.case_file = ROOT / args.case_file
    if not args.output_dir.is_absolute():
        args.output_dir = ROOT / args.output_dir

    project_filter = set(args.projects) if args.projects else None
    cases = load_cases(args.case_file, project_filter)
    if not cases:
        print("没有可运行的 case", file=sys.stderr)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    log_dir = args.output_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    run_log = args.run_log
    if run_log is not None and not run_log.is_absolute():
        run_log = ROOT / run_log

    log(
        f"计划运行 {len(cases)} 个 PR | output={args.output_dir} | force={args.force}",
        run_log,
    )

    stats = {"ok": 0, "skipped": 0, "fail": 0}
    for project, pr_nb in cases:
        status = run_one(
            project,
            pr_nb,
            output_dir=args.output_dir,
            force=args.force,
            python=args.python,
            log_file=run_log,
        )
        stats[status] = stats.get(status, 0) + 1
        done = stats["ok"] + stats["skipped"] + stats["fail"]
        log(
            f"进度 {done}/{len(cases)} | ok={stats['ok']} skip={stats['skipped']} fail={stats['fail']}",
            run_log,
        )

    log(
        f"完成: ok={stats['ok']} skip={stats['skipped']} fail={stats['fail']}",
        run_log,
    )
    return 0 if stats["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
