#!/usr/bin/env python3
"""汇总 Mutation score（mutmut + oracle killed rate），对比 DIVE vs baseline new200 cache。

用法:
  uv run python scripts/summarize_mutation.py
  uv run python scripts/summarize_mutation.py --phase phase2
  uv run python scripts/summarize_mutation.py --dive-cache .cache_dive_new200 \\
      --baseline-cache .cache_baseline_new200
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from scipy.stats import wilcoxon

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECTS = ["marshmallow", "pandas", "scipy", "keras"]
DEFAULT_DIVE = ROOT / ".cache_dive_new200"
DEFAULT_BASELINE = ROOT / ".cache_baseline_new200"
DEFAULT_PR_FILE = ROOT / "scripts" / "pr_batch_300" / "new200.txt"

PROJECT_LABELS = {
    "scipy": "SciPy",
    "marshmallow": "Marshmallow",
    "pandas": "Pandas",
    "keras": "Keras",
}


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def guess_pr_file(cache_dir: Path) -> Path | None:
    name = cache_dir.name
    if "new200" in name and (ROOT / "scripts" / "pr_batch_300" / "new200.txt").is_file():
        return ROOT / "scripts" / "pr_batch_300" / "new200.txt"
    if "new100" in name and (ROOT / "scripts" / "pr_batch_300" / "new100.txt").is_file():
        return ROOT / "scripts" / "pr_batch_300" / "new100.txt"
    return None


def resolve_pr_file(cache_dir: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        path = explicit if explicit.is_absolute() else ROOT / explicit
        if not path.is_file():
            raise FileNotFoundError(f"--pr-file 不存在: {path}")
        return path
    for candidate in (cache_dir / "run_pr_file.txt", cache_dir / "dive_prs.txt"):
        if candidate.is_file():
            return candidate
    return guess_pr_file(cache_dir)


def load_pr_ids_for_project(project: str, pr_file: Path | None) -> list[str]:
    if pr_file is None:
        raise FileNotFoundError("需要 --pr-file")
    ids: list[str] = []
    with open(pr_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) == 2 and parts[0] == project:
                ids.append(parts[1])
    return ids


def mutation_result_path(cache_dir: Path, project: str, pr_id: str, phase: str) -> Path:
    base = cache_dir / "mutation_testing" / project / pr_id
    if phase == "phase2":
        return base / "phase2" / "mutation_results.json"
    return base / "mutation_results.json"


def mutation_score(data: dict) -> float | None:
    if data.get("mutation_score") is not None:
        return float(data["mutation_score"])
    killed = data.get("n_mutant_fail_assert", 0) + data.get("n_mutant_fail_other", 0)
    survived = data.get("n_mutant_pass", 0)
    total = killed + survived
    if total == 0:
        return None
    return killed / total


def collect_scores(
    cache_dir: Path,
    project: str,
    pr_ids: list[str],
    phase: str,
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for pr_id in pr_ids:
        path = mutation_result_path(cache_dir, project, pr_id, phase)
        data = load_json(path)
        if data is None:
            continue
        score = mutation_score(data)
        if score is not None:
            scores[pr_id] = score
    return scores


def summarize_cache(
    cache_dir: Path,
    label: str,
    projects: list[str],
    pr_file: Path,
    phase: str,
) -> dict:
    per_project: dict[str, dict] = {}
    all_scores: list[float] = []
    for project in projects:
        pr_ids = load_pr_ids_for_project(project, pr_file)
        scores = collect_scores(cache_dir, project, pr_ids, phase)
        values = list(scores.values())
        per_project[project] = {
            "n_prs_listed": len(pr_ids),
            "n_with_score": len(values),
            "mean": float(np.mean(values)) if values else None,
            "std": float(np.std(values)) if values else None,
            "scores_by_pr": scores,
        }
        all_scores.extend(values)
    return {
        "label": label,
        "cache_dir": str(cache_dir),
        "overall_mean": float(np.mean(all_scores)) if all_scores else None,
        "overall_std": float(np.std(all_scores)) if all_scores else None,
        "n_with_score": len(all_scores),
        "projects": per_project,
    }


def paired_wilcoxon(
    dive_scores: dict[str, float],
    baseline_scores: dict[str, float],
) -> tuple[int, float | None]:
    common = sorted(set(dive_scores) & set(baseline_scores))
    if len(common) < 2:
        return len(common), None
    dive_vals = [dive_scores[p] for p in common]
    baseline_vals = [baseline_scores[p] for p in common]
    stat, p_value = wilcoxon(dive_vals, baseline_vals)
    return len(common), float(p_value)


def print_summary(dive: dict, baseline: dict, phase: str) -> None:
    print(f"=== Mutation Score ({phase}) ===")
    print(f"DIVE cache:      {dive['cache_dir']}")
    print(f"Baseline cache:  {baseline['cache_dir']}")
    print()

    header = f"{'Project':<14} {'DIVE n':>7} {'DIVE μ':>8} {'Base n':>7} {'Base μ':>8} {'Δμ':>8} {'Wilcoxon p':>12}"
    print(header)
    print("-" * len(header))

    for project in DEFAULT_PROJECTS:
        d = dive["projects"][project]
        b = baseline["projects"][project]
        d_mean = d["mean"]
        b_mean = b["mean"]
        delta = (d_mean - b_mean) if d_mean is not None and b_mean is not None else None
        n_pair, p_val = paired_wilcoxon(d["scores_by_pr"], b["scores_by_pr"])
        p_str = f"{p_val:.4f}" if p_val is not None else "n/a"
        d_mean_str = f"{d_mean:.3f}" if d_mean is not None else "n/a"
        b_mean_str = f"{b_mean:.3f}" if b_mean is not None else "n/a"
        delta_str = f"{delta:+.3f}" if delta is not None else "n/a"
        label = PROJECT_LABELS.get(project, project)
        print(
            f"{label:<14} {d['n_with_score']:>7} {d_mean_str:>8} "
            f"{b['n_with_score']:>7} {b_mean_str:>8} {delta_str:>8} {p_str:>12}"
        )

    print("-" * len(header))
    d_over = dive["overall_mean"]
    b_over = baseline["overall_mean"]
    delta_over = (d_over - b_over) if d_over is not None and b_over is not None else None
    d_str = f"{d_over:.3f}" if d_over is not None else "n/a"
    b_str = f"{b_over:.3f}" if b_over is not None else "n/a"
    delta_str = f"{delta_over:+.3f}" if delta_over is not None else "n/a"
    print(
        f"{'Overall':<14} {dive['n_with_score']:>7} {d_str:>8} "
        f"{baseline['n_with_score']:>7} {b_str:>8} {delta_str:>8}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize mutation scores for DIVE vs baseline")
    parser.add_argument("--dive-cache", type=Path, default=DEFAULT_DIVE)
    parser.add_argument("--baseline-cache", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--pr-file", type=Path, default=None)
    parser.add_argument("--phase", choices=["phase1", "phase2"], default="phase2")
    parser.add_argument(
        "--projects",
        nargs="+",
        default=DEFAULT_PROJECTS,
        help="Projects to include",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Optional path to write full summary JSON",
    )
    args = parser.parse_args()

    dive_cache = args.dive_cache if args.dive_cache.is_absolute() else ROOT / args.dive_cache
    baseline_cache = (
        args.baseline_cache if args.baseline_cache.is_absolute() else ROOT / args.baseline_cache
    )
    pr_file = resolve_pr_file(dive_cache, args.pr_file if args.pr_file else None)
    if pr_file is None:
        pr_file = DEFAULT_PR_FILE

    dive = summarize_cache(dive_cache, "dive", args.projects, pr_file, args.phase)
    baseline = summarize_cache(baseline_cache, "baseline", args.projects, pr_file, args.phase)

    print_summary(dive, baseline, args.phase)

    if args.json_out is not None:
        out = {
            "phase": args.phase,
            "pr_file": str(pr_file),
            "dive": dive,
            "baseline": baseline,
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(out, indent=2))
        print(f"\nWrote {args.json_out}")

    if dive["n_with_score"] == 0 and baseline["n_with_score"] == 0:
        print(
            "\n尚无 mutation 结果。请先运行 scripts/run_mutation.py "
            "（需 Docker 容器与 mutmut PYTHONPATH）。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
