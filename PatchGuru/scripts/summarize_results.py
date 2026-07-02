#!/usr/bin/env python3
"""统计 SpecInfer 批量结果（RQ1 Table 1 风格）。

用法:
  uv run python scripts/summarize_results.py --cache-dir .cache_baseline_new200
  uv run python scripts/summarize_results.py --cache-dir .cache_baseline_new100 \\
      --pr-file scripts/pr_batch_300/new100.txt
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECTS = ["marshmallow", "pandas", "scipy", "keras"]
PR_LIST_NAME = "run_pr_file.txt"
DEFAULT_PR_IDS_DIR = ROOT / ".cache" / "pr_ids"
BATCH_DIR = ROOT / "scripts" / "pr_batch_300"


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def guess_pr_file(cache_dir: Path) -> Path | None:
    name = cache_dir.name
    if "new200" in name and (BATCH_DIR / "new200.txt").is_file():
        return BATCH_DIR / "new200.txt"
    if "new100" in name and (BATCH_DIR / "new100.txt").is_file():
        return BATCH_DIR / "new100.txt"
    return None


def resolve_pr_file(cache_dir: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        path = explicit if explicit.is_absolute() else ROOT / explicit
        if not path.is_file():
            raise FileNotFoundError(f"--pr-file 不存在: {path}")
        return path
    for candidate in (cache_dir / PR_LIST_NAME, cache_dir / "dive_prs.txt"):
        if candidate.is_file():
            return candidate
    return guess_pr_file(cache_dir)


def load_pr_ids_for_project(project: str, pr_file: Path | None) -> list[str]:
    if pr_file is not None:
        ids: list[str] = []
        with open(pr_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) == 2 and parts[0] == project:
                    ids.append(parts[1])
        if not ids:
            raise FileNotFoundError(f"{pr_file} 中无 {project} 的 PR")
        return ids

    legacy = DEFAULT_PR_IDS_DIR / f"{project}.txt"
    if not legacy.is_file():
        raise FileNotFoundError(f"缺少 PR 列表: {legacy}（请用 --pr-file）")
    return [line.strip() for line in legacy.read_text().splitlines() if line.strip()]


def summarize_project(cache_dir: Path, project: str, pr_file: Path | None) -> dict:
    pr_ids = load_pr_ids_for_project(project, pr_file)
    oracle_root = cache_dir / "oracles" / project

    stats = {
        "total": len(pr_ids),
        "warnings": 0,
        "normal": 0,
        "failures": 0,
        "incomplete": 0,
        "p1_bug": 0,
        "p2_bug": 0,
    }
    incomplete_prs: list[str] = []

    for pr_id in pr_ids:
        p1_path = oracle_root / pr_id / "results.json"
        p1 = load_json(p1_path)
        if p1 is None or p1.get("stage") != "completed":
            stats["failures"] += 1
            stats["incomplete"] += 1
            incomplete_prs.append(pr_id)
            continue

        if p1.get("review_conclusion") == "BUG":
            stats["warnings"] += 1
            stats["p1_bug"] += 1
            continue

        if p1.get("review_conclusion") != "NORMAL":
            stats["failures"] += 1
            incomplete_prs.append(pr_id)
            continue

        p2 = load_json(oracle_root / pr_id / "phase2" / "results.json")
        if p2 is None or p2.get("stage") != "completed":
            stats["failures"] += 1
            incomplete_prs.append(pr_id)
            continue

        p2c = p2.get("review_conclusion")
        if p2c == "BUG":
            stats["warnings"] += 1
            stats["p2_bug"] += 1
        elif p2c == "NORMAL":
            stats["normal"] += 1
        else:
            stats["failures"] += 1
            incomplete_prs.append(pr_id)

    stats["oracles"] = stats["warnings"] + stats["normal"]
    stats["incomplete_prs"] = incomplete_prs
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="统计 SpecInfer 批量结果")
    parser.add_argument("--cache-dir", default=".cache_rerun_0612", help="缓存根目录（含 oracles/）")
    parser.add_argument(
        "--pr-file",
        type=Path,
        help="PR 列表（每行 'project pr_nb'）；默认读 cache/run_pr_file.txt 或按 cache 名推断 new100/new200",
    )
    parser.add_argument("--projects", nargs="*", default=DEFAULT_PROJECTS)
    parser.add_argument("--show-incomplete", action="store_true", help="列出未完成 PR")
    args = parser.parse_args()

    cache_dir = ROOT / args.cache_dir
    if not cache_dir.is_dir():
        print(f"ERROR: 目录不存在: {cache_dir}")
        return 1

    try:
        pr_file = resolve_pr_file(cache_dir, args.pr_file)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        return 1

    print(f"缓存: {cache_dir}")
    if pr_file is not None:
        try:
            rel = pr_file.relative_to(ROOT)
            print(f"PR 列表: {rel}")
        except ValueError:
            print(f"PR 列表: {pr_file}")
    else:
        print(f"PR 列表: {DEFAULT_PR_IDS_DIR}/{{project}}.txt")
    print(f"{'project':<14} {'total':>5} {'warn':>5} {'normal':>6} {'oracle':>6} {'fail':>5}  (p1_bug / p2_bug)")
    print("-" * 62)

    tot = {k: 0 for k in ("total", "warnings", "normal", "oracles", "failures", "p1_bug", "p2_bug")}
    for project in args.projects:
        try:
            s = summarize_project(cache_dir, project, pr_file)
        except FileNotFoundError as e:
            print(f"{project:<14} SKIP  ({e})")
            continue
        print(
            f"{project:<14} {s['total']:>5} {s['warnings']:>5} {s['normal']:>6} "
            f"{s['oracles']:>6} {s['failures']:>5}  ({s['p1_bug']} / {s['p2_bug']})"
        )
        for k in tot:
            tot[k] += s[k]
        if args.show_incomplete and s["incomplete_prs"]:
            print(f"  incomplete: {', '.join(s['incomplete_prs'][:20])}"
                  + (f" ... +{len(s['incomplete_prs']) - 20}" if len(s["incomplete_prs"]) > 20 else ""))

    print("-" * 62)
    print(
        f"{'ALL':<14} {tot['total']:>5} {tot['warnings']:>5} {tot['normal']:>6} "
        f"{tot['oracles']:>6} {tot['failures']:>5}  ({tot['p1_bug']} / {tot['p2_bug']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
