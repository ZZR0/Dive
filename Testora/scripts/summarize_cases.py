#!/usr/bin/env python3
"""汇总 Testora 批量运行结果（对照计划 PR 列表，风格对齐 PatchGuru summarize_results）。

用法:
  python scripts/summarize_cases.py --results-dir .results_new200
  python scripts/summarize_cases.py --results-dir .results_new200 \\
      --case-file ../PatchGuru/scripts/pr_batch_300/new200.txt
  python scripts/summarize_cases.py --results-dir .results_new200 --show-incomplete
"""
from __future__ import annotations

import argparse
from pathlib import Path

from testora.util.LogParser import parse_log_files

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS_DIR = ROOT / ".results_dive_common_bugs"
DEFAULT_PROJECTS = ["marshmallow", "pandas", "scipy", "keras"]
CASE_LIST_NAME = "run_case_file.txt"
BATCH_DIR = ROOT.parent / "PatchGuru" / "scripts" / "pr_batch_300"

STATUS_KEYS = (
    "checked",
    "ignored",
    "regression",
    "intended_change",
    "coincidental_fix",
)


def guess_case_file(results_dir: Path) -> Path | None:
    name = results_dir.name
    if "new200" in name and (BATCH_DIR / "new200.txt").is_file():
        return BATCH_DIR / "new200.txt"
    if "new100" in name and (BATCH_DIR / "new100.txt").is_file():
        return BATCH_DIR / "new100.txt"
    return None


def resolve_case_file(results_dir: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        path = explicit if explicit.is_absolute() else ROOT / explicit
        if not path.is_file():
            raise FileNotFoundError(f"--case-file 不存在: {path}")
        return path
    candidate = results_dir / CASE_LIST_NAME
    if candidate.is_file():
        return candidate
    return guess_case_file(results_dir)


def load_cases_for_project(project: str, case_file: Path) -> list[str]:
    ids: list[str] = []
    with open(case_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) == 2 and parts[0] == project:
                ids.append(parts[1])
    if not ids:
        raise FileNotFoundError(f"{case_file} 中无 {project} 的 PR")
    return ids


def load_result_index(results_dir: Path) -> dict[tuple[str, int], object]:
    log_files = [
        p
        for p in sorted(results_dir.glob("*/*.json"))
        if p.stat().st_size > 0
    ]
    if not log_files:
        return {}

    pr_results, _ = parse_log_files([str(p) for p in log_files])
    index: dict[tuple[str, int], object] = {}
    for pr_result in pr_results:
        project = "?"
        for lf in log_files:
            if lf.name == f"{pr_result.number}.json":
                project = lf.parent.name
                break
        index[(project, pr_result.number)] = pr_result
    return index


def summarize_project(
    results_dir: Path,
    project: str,
    case_file: Path,
    result_index: dict[tuple[str, int], object],
) -> dict:
    pr_ids = load_cases_for_project(project, case_file)
    stats = {
        "total": len(pr_ids),
        "fail": 0,
        "incomplete_prs": [],
    }
    for key in STATUS_KEYS:
        stats[key] = 0

    for pr_id in pr_ids:
        pr_nb = int(pr_id)
        pr_result = result_index.get((project, pr_nb))
        if pr_result is None:
            stats["fail"] += 1
            stats["incomplete_prs"].append(pr_id)
            continue

        status = pr_result.status()
        if status in STATUS_KEYS:
            stats[status] += 1
        else:
            stats["fail"] += 1
            stats["incomplete_prs"].append(pr_id)

    stats["done"] = stats["total"] - stats["fail"]
    return stats


def print_json_only_summary(results_dir: Path) -> int:
    log_files = sorted(results_dir.glob("*/*.json"))
    if not log_files:
        print(f"未找到结果: {results_dir}/*/*.json")
        return 1

    result_index = load_result_index(results_dir)
    by_status: dict[str, list[str]] = {}
    for (project, pr_nb), pr_result in sorted(result_index.items(), key=lambda x: x[0][1]):
        status = pr_result.status()
        by_status.setdefault(status, []).append(f"{project}/{pr_nb}")

    print(f"结果目录: {results_dir}")
    print(f"日志文件: {len(log_files)}（未指定 case 列表，仅统计已有 json）")
    print()
    print("按 status 统计:")
    for status in sorted(by_status):
        print(f"  {status}: {len(by_status[status])}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总 Testora case 结果")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument(
        "--case-file",
        type=Path,
        help="case 列表（每行 'project pr_nb'）；默认读 results/run_case_file.txt 或按目录名推断 new100/new200",
    )
    parser.add_argument("--projects", nargs="*", default=DEFAULT_PROJECTS)
    parser.add_argument(
        "--show-incomplete",
        action="store_true",
        help="列出未完成（无结果 json）的 PR",
    )
    parser.add_argument(
        "--brief",
        action="store_true",
        help="只打印汇总表，不打印每个 PR 明细",
    )
    args = parser.parse_args()

    if not args.results_dir.is_absolute():
        args.results_dir = ROOT / args.results_dir

    if not args.results_dir.is_dir():
        print(f"ERROR: 目录不存在: {args.results_dir}")
        return 1

    try:
        case_file = resolve_case_file(args.results_dir, args.case_file)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        return 1

    if case_file is None:
        return print_json_only_summary(args.results_dir)

    result_index = load_result_index(args.results_dir)

    print(f"结果目录: {args.results_dir}")
    try:
        rel = case_file.relative_to(ROOT)
        print(f"Case 列表: {rel}")
    except ValueError:
        print(f"Case 列表: {case_file}")
    print(
        f"{'project':<14} {'total':>5} {'done':>5} {'chk':>5} {'ign':>5} "
        f"{'regr':>5} {'intd':>5} {'coin':>5} {'fail':>5}"
    )
    print("-" * 72)

    tot = {k: 0 for k in ("total", "done", "fail", *STATUS_KEYS)}
    all_rows: list[tuple[str, int, str, str]] = []

    for project in args.projects:
        try:
            s = summarize_project(args.results_dir, project, case_file, result_index)
        except FileNotFoundError as e:
            print(f"{project:<14} SKIP  ({e})")
            continue

        print(
            f"{project:<14} {s['total']:>5} {s['done']:>5} {s['checked']:>5} {s['ignored']:>5} "
            f"{s['regression']:>5} {s['intended_change']:>5} {s['coincidental_fix']:>5} {s['fail']:>5}"
        )
        for k in tot:
            tot[k] += s[k]
        if args.show_incomplete and s["incomplete_prs"]:
            shown = ", ".join(s["incomplete_prs"][:20])
            extra = f" ... +{len(s['incomplete_prs']) - 20}" if len(s["incomplete_prs"]) > 20 else ""
            print(f"  incomplete: {shown}{extra}")

        for pr_id in load_cases_for_project(project, case_file):
            pr_nb = int(pr_id)
            pr_result = result_index.get((project, pr_nb))
            if pr_result is not None:
                all_rows.append((project, pr_nb, pr_result.status(), pr_result.summary()))

    print("-" * 72)
    print(
        f"{'ALL':<14} {tot['total']:>5} {tot['done']:>5} {tot['checked']:>5} {tot['ignored']:>5} "
        f"{tot['regression']:>5} {tot['intended_change']:>5} {tot['coincidental_fix']:>5} {tot['fail']:>5}"
    )

    if not args.brief and all_rows:
        print()
        print(f"{'project':<12} {'pr':<8} {'status':<18} summary")
        print("-" * 100)
        for project, pr_nb, status, summary in sorted(all_rows, key=lambda r: (r[0], r[1])):
            short = summary if len(summary) <= 60 else summary[:57] + "..."
            print(f"{project:<12} {pr_nb:<8} {status:<18} {short}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
