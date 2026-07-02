#!/usr/bin/env python3
"""RQ4：DIVE vs Baseline 改动行覆盖率（changed_line_hit / changed_line_total）汇总。

DIVE 在搜索 harness 内对 post_<fn> 做 sys.settrace；Baseline 原始流水线不记录该指标，
需先跑 ``run_baseline_coverage.sh`` 在 Docker 内 replay baseline specification.py。

用法:
  bash scripts/run_baseline_coverage.sh          # 后验测 baseline 覆盖率
  uv run python scripts/summarize_rq4_coverage.py
  uv run python scripts/summarize_rq4_coverage.py --json-out .cache/manual_annotation/dive_new200/rq4_coverage_summary.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from scipy.stats import mannwhitneyu, wilcoxon

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECTS = ["marshmallow", "pandas", "scipy", "keras"]
DEFAULT_DIVE = ROOT / ".cache_dive_new200"
DEFAULT_BASELINE = ROOT / ".cache_baseline_new200"
DEFAULT_PR_FILE = ROOT / "scripts" / "pr_batch_300" / "new200.txt"
DEFAULT_GT = ROOT / ".cache/manual_annotation/dive_new200/ground_truth.txt"

PROJECT_LABELS = {
    "marshmallow": "Marshmallow",
    "pandas": "Pandas",
    "scipy": "SciPy",
    "keras": "Keras",
}

ABLATION_CACHES = {
    "full DIVE": DEFAULT_DIVE,
    "w/o constructor": ROOT / ".cache_dive_ablation_no_constructor_new200",
    "w/o guided": ROOT / ".cache_dive_ablation_no_guided_new200",
    "w/o triage": ROOT / ".cache_dive_ablation_no_triage_new200",
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
    if "new200" in name and (ROOT / "scripts/pr_batch_300/new200.txt").is_file():
        return ROOT / "scripts/pr_batch_300/new200.txt"
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
    return DEFAULT_PR_FILE


def load_pr_ids_for_project(project: str, pr_file: Path) -> list[str]:
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


def load_ground_truth(path: Path) -> dict[tuple[str, str], str]:
    if not path.is_file():
        return {}
    out: dict[tuple[str, str], str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 4:
            out[(parts[0], parts[1])] = parts[3]
    return out


def phase2_path(cache: Path, project: str, pr_id: str) -> Path:
    return cache / "oracles" / project / pr_id / "phase2" / "results.json"


def baseline_coverage_path(cache: Path, project: str, pr_id: str) -> Path:
    return cache / "coverage_analysis" / project / pr_id / "phase2" / "coverage_results.json"


def collect_baseline_coverage_rows(
    cache: Path,
    project: str,
    pr_ids: list[str],
) -> list[dict]:
    rows: list[dict] = []
    for pr_id in pr_ids:
        cov = load_json(baseline_coverage_path(cache, project, pr_id))
        if cov is None:
            continue
        total = cov.get("changed_line_total")
        if total is None or total <= 0:
            continue
        hit = int(cov.get("changed_line_hit", 0))
        total = int(total)
        p2 = load_json(phase2_path(cache, project, pr_id)) or {}
        rows.append(
            {
                "project": project,
                "pr_id": pr_id,
                "hit": hit,
                "total": total,
                "rate": hit / total,
                "p2_conclusion": p2.get("review_conclusion"),
            }
        )
    return rows


def collect_coverage_rows(
    cache: Path,
    project: str,
    pr_ids: list[str],
    *,
    require_completed: bool = True,
) -> list[dict]:
    rows: list[dict] = []
    for pr_id in pr_ids:
        p2 = load_json(phase2_path(cache, project, pr_id))
        if p2 is None:
            continue
        if require_completed and p2.get("stage") != "completed":
            continue
        ds = p2.get("dive_stats") or {}
        total = ds.get("changed_line_total")
        if total is None or total <= 0:
            continue
        hit = int(ds.get("changed_line_hit", 0))
        total = int(total)
        rows.append(
            {
                "project": project,
                "pr_id": pr_id,
                "hit": hit,
                "total": total,
                "rate": hit / total,
                "execs": ds.get("execs"),
                "clusters": ds.get("reported_clusters"),
                "p2_conclusion": p2.get("review_conclusion"),
                "elapsed_sec": ds.get("elapsed_sec"),
            }
        )
    return rows


def aggregate(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    rates = [r["rate"] for r in rows]
    hits = [r["hit"] for r in rows]
    totals = [r["total"] for r in rows]
    execs = [r["execs"] for r in rows if r.get("execs") is not None]
    return {
        "n": len(rows),
        "mean_hit": float(np.mean(hits)),
        "median_hit": float(np.median(hits)),
        "mean_total": float(np.mean(totals)),
        "median_total": float(np.median(totals)),
        "mean_rate": float(np.mean(rates)),
        "median_rate": float(np.median(rates)),
        "zero_hit_frac": float(sum(1 for h in hits if h == 0) / len(hits)),
        "full_hit_frac": float(sum(1 for r in rows if r["hit"] == r["total"]) / len(rows)),
        "mean_execs": float(np.mean(execs)) if execs else None,
    }


def asymmetric_zero_hit_stats(paired_rows: list[dict]) -> dict:
    """Coverage stats after removing 0-hit PRs separately per side (paired subset)."""
    dive_nonzero = [p for p in paired_rows if p["dive_hit"] > 0]
    base_nonzero = [p for p in paired_rows if p["baseline_hit"] > 0]

    def _side_stats(subset: list[dict], primary: str, other: str) -> dict:
        if not subset:
            return {"n": 0}
        primary_rates = [p[f"{primary}_rate"] for p in subset]
        other_rates = [p[f"{other}_rate"] for p in subset]
        return {
            "n": len(subset),
            f"mean_{primary}_rate": float(np.mean(primary_rates)),
            f"median_{primary}_rate": float(np.median(primary_rates)),
            f"mean_{other}_rate": float(np.mean(other_rates)),
            f"median_{other}_rate": float(np.median(other_rates)),
        }

    return {
        "exclude_dive_zero_hit": _side_stats(dive_nonzero, "dive", "baseline"),
        "exclude_baseline_zero_hit": _side_stats(base_nonzero, "baseline", "dive"),
    }


def build_paired_rows(dive_rows: list[dict], baseline_rows: list[dict]) -> list[dict]:
    base_by_key = {(r["project"], r["pr_id"]): r for r in baseline_rows}
    paired: list[dict] = []
    for d in dive_rows:
        b = base_by_key.get((d["project"], d["pr_id"]))
        if b is None or d["total"] != b["total"]:
            continue
        paired.append(
            {
                "project": d["project"],
                "pr_id": d["pr_id"],
                "dive_hit": d["hit"],
                "baseline_hit": b["hit"],
                "total": d["total"],
                "dive_rate": d["rate"],
                "baseline_rate": b["rate"],
                "delta_rate": d["rate"] - b["rate"],
                "dive_p2": d.get("p2_conclusion"),
                "baseline_p2": b.get("p2_conclusion"),
            }
        )
    return paired


def paired_coverage_compare(
    dive_rows: list[dict],
    baseline_rows: list[dict],
) -> dict:
    paired = build_paired_rows(dive_rows, baseline_rows)
    out: dict = {"n": len(paired)}
    if not paired:
        return out

    dive_rates = [p["dive_rate"] for p in paired]
    base_rates = [p["baseline_rate"] for p in paired]
    deltas = [p["delta_rate"] for p in paired]
    out.update(
        {
            "mean_dive_rate": float(np.mean(dive_rates)),
            "mean_baseline_rate": float(np.mean(base_rates)),
            "median_dive_rate": float(np.median(dive_rates)),
            "median_baseline_rate": float(np.median(base_rates)),
            "mean_delta_rate": float(np.mean(deltas)),
            "dive_higher_frac": float(sum(1 for p in paired if p["dive_hit"] > p["baseline_hit"]) / len(paired)),
            "baseline_higher_frac": float(
                sum(1 for p in paired if p["baseline_hit"] > p["dive_hit"]) / len(paired)
            ),
            "equal_hit_frac": float(sum(1 for p in paired if p["dive_hit"] == p["baseline_hit"]) / len(paired)),
        }
    )
    if len(paired) >= 10:
        try:
            stat, p = wilcoxon(deltas, alternative="greater")
            out["wilcoxon_delta_greater_p"] = float(p)
            out["wilcoxon_stat"] = float(stat)
        except ValueError:
            pass
    if len(dive_rates) >= 2 and len(base_rates) >= 2:
        _, p = mannwhitneyu(dive_rates, base_rates, alternative="two-sided")
        out["mannwhitney_p"] = float(p)
    return out


def print_asymmetric_zero_hit_table(asym: dict) -> None:
    print("Paired comparison (each side excludes its own 0-hit PRs):")
    header = f"{'Filter':<22} {'n':>5} {'DIVE μ/med':>16} {'Base μ/med':>16}"
    print(header)
    print("-" * len(header))
    for label, key in (
        ("exclude DIVE 0-hit", "exclude_dive_zero_hit"),
        ("exclude Baseline 0-hit", "exclude_baseline_zero_hit"),
    ):
        s = asym.get(key, {})
        if not s.get("n"):
            print(f"{label:<22} {0:>5}")
            continue
        print(
            f"{label:<22} {s['n']:>5} "
            f"{s['mean_dive_rate']:>6.3f}/{s['median_dive_rate']:<6.3f} "
            f"{s['mean_baseline_rate']:>6.3f}/{s['median_baseline_rate']:<6.3f}"
        )
    print()


def print_compare_table(title: str, dive: dict, baseline: dict, paired: dict) -> None:
    print(title)
    header = f"{'Method':<12} {'n':>5} {'μ rate':>8} {'med rate':>9} {'0-hit%':>7}"
    print(header)
    print("-" * len(header))
    for label, s in (("DIVE", dive), ("Baseline", baseline)):
        if not s.get("n"):
            print(f"{label:<12} {0:>5}")
            continue
        print(
            f"{label:<12} {s['n']:>5} {s['mean_rate']:>8.3f} "
            f"{s['median_rate']:>9.3f} {100*s['zero_hit_frac']:>6.1f}%"
        )
    if paired.get("n"):
        print("-" * len(header))
        print(f"Paired n={paired['n']}  Δμ(DIVE−Base)={paired.get('mean_delta_rate', 0):+.3f}  "
              f"DIVE>{paired.get('dive_higher_frac', 0)*100:.1f}%  "
              f"Base>{paired.get('baseline_higher_frac', 0)*100:.1f}%")
        if "wilcoxon_delta_greater_p" in paired:
            print(f"  Wilcoxon p (DIVE > Baseline rate): {paired['wilcoxon_delta_greater_p']:.4g}")
        if "mannwhitney_p" in paired:
            print(f"  Mann–Whitney p (rates): {paired['mannwhitney_p']:.4g}")
    print()


def case_studies(rows: list[dict], gt: dict[tuple[str, str], str], limit: int = 5) -> dict:
    def key(r):
        return (r["project"], r["pr_id"])

    tp_rows = [r for r in rows if gt.get(key(r)) == "TP"]
    bug_rows = [r for r in rows if r.get("p2_conclusion") == "BUG"]
    normal_rows = [r for r in rows if r.get("p2_conclusion") == "NORMAL"]

    def pick(sorted_rows, n):
        return [
            {
                "project": r["project"],
                "pr_id": r["pr_id"],
                "hit": r["hit"],
                "total": r["total"],
                "rate": round(r["rate"], 3),
                "p2": r.get("p2_conclusion"),
                "gt": gt.get(key(r)),
            }
            for r in sorted_rows[:n]
        ]

    tp_by_rate = sorted(tp_rows, key=lambda r: r["rate"], reverse=True)
    tp_zero = [r for r in tp_rows if r["hit"] == 0]
    bug_zero = [r for r in bug_rows if r["hit"] == 0]

    return {
        "tp_high_coverage": pick(tp_by_rate, limit),
        "tp_zero_coverage": pick(sorted(tp_zero, key=lambda r: r["total"], reverse=True), limit),
        "bug_zero_coverage": pick(sorted(bug_zero, key=lambda r: r["total"], reverse=True), limit),
        "normal_full_coverage": pick(
            sorted([r for r in normal_rows if r["hit"] == r["total"]], key=lambda r: -r["total"]),
            min(3, limit),
        ),
    }


def compare_groups(rows: list[dict], gt: dict[tuple[str, str], str]) -> dict:
    def key(r):
        return (r["project"], r["pr_id"])

    bug_rates = [r["rate"] for r in rows if r.get("p2_conclusion") == "BUG"]
    normal_rates = [r["rate"] for r in rows if r.get("p2_conclusion") == "NORMAL"]
    tp_rates = [r["rate"] for r in rows if gt.get(key(r)) == "TP"]
    fp_rates = [r["rate"] for r in rows if gt.get(key(r)) == "FP"]

    out: dict = {}
    if bug_rates:
        out["p2_bug"] = {"n": len(bug_rates), "mean_rate": float(np.mean(bug_rates)), "median_rate": float(np.median(bug_rates))}
    if normal_rates:
        out["p2_normal"] = {
            "n": len(normal_rates),
            "mean_rate": float(np.mean(normal_rates)),
            "median_rate": float(np.median(normal_rates)),
        }
    if len(bug_rates) >= 2 and len(normal_rates) >= 2:
        _, p = mannwhitneyu(bug_rates, normal_rates, alternative="two-sided")
        out["bug_vs_normal_mannwhitney_p"] = float(p)
    if tp_rates:
        out["gold_tp"] = {"n": len(tp_rates), "mean_rate": float(np.mean(tp_rates))}
    if fp_rates:
        out["gold_fp"] = {"n": len(fp_rates), "mean_rate": float(np.mean(fp_rates))}
    return out


def print_table(title: str, by_project: dict[str, dict], overall: dict) -> None:
    print(title)
    header = (
        f"{'Project':<14} {'n':>5} {'μ hit':>7} {'μ total':>8} "
        f"{'μ rate':>7} {'med rate':>9} {'0-hit%':>7}"
    )
    print(header)
    print("-" * len(header))
    for project in DEFAULT_PROJECTS:
        s = by_project.get(project, {"n": 0})
        if not s.get("n"):
            print(f"{PROJECT_LABELS.get(project, project):<14} {0:>5}")
            continue
        print(
            f"{PROJECT_LABELS.get(project, project):<14} {s['n']:>5} "
            f"{s['mean_hit']:>7.2f} {s['mean_total']:>8.2f} "
            f"{s['mean_rate']:>7.3f} {s['median_rate']:>9.3f} {100*s['zero_hit_frac']:>6.1f}%"
        )
    print("-" * len(header))
    if overall.get("n"):
        print(
            f"{'Overall':<14} {overall['n']:>5} "
            f"{overall['mean_hit']:>7.2f} {overall['mean_total']:>8.2f} "
            f"{overall['mean_rate']:>7.3f} {overall['median_rate']:>9.3f} {100*overall['zero_hit_frac']:>6.1f}%"
        )
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize RQ4 DIVE changed-line coverage")
    parser.add_argument("--dive-cache", type=Path, default=DEFAULT_DIVE)
    parser.add_argument("--baseline-cache", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--pr-file", type=Path, default=None)
    parser.add_argument("--ground-truth", type=Path, default=DEFAULT_GT)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--include-ablations", action="store_true", default=True)
    parser.add_argument("--no-ablations", action="store_false", dest="include_ablations")
    args = parser.parse_args()

    dive_cache = args.dive_cache if args.dive_cache.is_absolute() else ROOT / args.dive_cache
    baseline_cache = (
        args.baseline_cache if args.baseline_cache.is_absolute() else ROOT / args.baseline_cache
    )
    pr_file = resolve_pr_file(dive_cache, args.pr_file)
    gt = load_ground_truth(
        args.ground_truth if args.ground_truth.is_absolute() else ROOT / args.ground_truth
    )

    all_rows: list[dict] = []
    by_project: dict[str, dict] = {}
    baseline_all: list[dict] = []
    baseline_by_project: dict[str, dict] = {}
    for project in DEFAULT_PROJECTS:
        pr_ids = load_pr_ids_for_project(project, pr_file)
        rows = collect_coverage_rows(dive_cache, project, pr_ids)
        base_rows = collect_baseline_coverage_rows(baseline_cache, project, pr_ids)
        by_project[project] = aggregate(rows)
        baseline_by_project[project] = aggregate(base_rows)
        all_rows.extend(rows)
        baseline_all.extend(base_rows)

    overall = aggregate(all_rows)
    baseline_overall = aggregate(baseline_all)
    paired_rows = build_paired_rows(all_rows, baseline_all)
    paired_cmp = paired_coverage_compare(all_rows, baseline_all)
    asym_zero_hit = asymmetric_zero_hit_stats(paired_rows)
    dive_excl = aggregate([r for r in all_rows if r["hit"] > 0])
    base_excl = aggregate([r for r in baseline_all if r["hit"] > 0])
    groups = compare_groups(all_rows, gt)
    cases = case_studies(all_rows, gt)

    print("=== RQ4 Changed-Line Coverage (DIVE vs Baseline, new200) ===")
    print(f"DIVE cache:     {dive_cache}")
    print(f"Baseline cache: {baseline_cache}")
    print(f"PR list:        {pr_file}")
    if not baseline_all:
        print("\n⚠ Baseline 覆盖率未测量。先运行: bash scripts/run_baseline_coverage.sh\n")
    print()
    print_table("DIVE changed_line_hit / changed_line_total", by_project, overall)
    if baseline_all:
        print_table("Baseline (one-shot oracle replay)", baseline_by_project, baseline_overall)
        print_compare_table("Paired comparison (same PR, same changed_line_total)", overall, baseline_overall, paired_cmp)
        print_asymmetric_zero_hit_table(asym_zero_hit)
        print("Unpaired (each side excludes its own 0-hit PRs):")
        for label, s in (("DIVE", dive_excl), ("Baseline", base_excl)):
            if s.get("n"):
                print(f"  {label:<10} n={s['n']:>4}  μ rate={s['mean_rate']:.3f}  med rate={s['median_rate']:.3f}")
        print()
    else:
        print("(Baseline coverage table pending — run run_baseline_coverage.sh)\n")

    print("Coverage vs DIVE phase2 conclusion:")
    for label in ("p2_bug", "p2_normal"):
        g = groups.get(label, {})
        if g.get("n"):
            print(f"  {label}: n={g['n']}  mean_rate={g.get('mean_rate', 0):.3f}")
    if "bug_vs_normal_mannwhitney_p" in groups:
        print(f"  Mann–Whitney p (BUG vs NORMAL rate): {groups['bug_vs_normal_mannwhitney_p']:.4f}")
    if groups.get("gold_tp"):
        print(
            f"  Gold TP (82-alert set): n={groups['gold_tp']['n']}  "
            f"mean_rate={groups['gold_tp']['mean_rate']:.3f}"
        )
    print()

    ablation_summary: dict[str, dict] = {}
    if args.include_ablations:
        print("Ablation variants (mean hit / mean total → rate):")
        for label, cache in ABLATION_CACHES.items():
            if not cache.is_dir():
                continue
            ab_rows: list[dict] = []
            for project in DEFAULT_PROJECTS:
                pr_ids = load_pr_ids_for_project(project, pr_file)
                ab_rows.extend(collect_coverage_rows(cache, project, pr_ids))
            ab = aggregate(ab_rows)
            ablation_summary[label] = ab
            if ab.get("n"):
                print(
                    f"  {label:<16} n={ab['n']:>4}  "
                    f"{ab['mean_hit']:.2f}/{ab['mean_total']:.2f}  "
                    f"rate={ab['mean_rate']:.3f}  0-hit={100*ab['zero_hit_frac']:.1f}%"
                )
        print()

    print("Case study candidates:")
    for section, items in cases.items():
        if not items:
            continue
        print(f"  [{section}]")
        for it in items:
            gt_s = f" gt={it['gt']}" if it.get("gt") else ""
            print(
                f"    {it['project']}#{it['pr_id']}  {it['hit']}/{it['total']} "
                f"({it['rate']})  p2={it.get('p2')}{gt_s}"
            )

    note = (
        "Baseline changed_line_hit measured post-hoc via BaselineCoverageAnalysis "
        "(replay phase2 specification.py with sys.settrace on post_<fn>). "
        "DIVE stats are union over search execs; baseline is union over one-shot test inputs."
    )

    payload = {
        "pr_file": str(pr_file),
        "dive_cache": str(dive_cache),
        "baseline_cache": str(baseline_cache),
        "note": note,
        "dive": {"overall": overall, "by_project": by_project},
        "baseline": {"overall": baseline_overall, "by_project": baseline_by_project},
        "paired_coverage": paired_cmp,
        "asymmetric_zero_hit": asym_zero_hit,
        "dive_excluding_zero_hit": dive_excl,
        "baseline_excluding_zero_hit": base_excl,
        "overall": overall,
        "by_project": by_project,
        "baseline_overall": baseline_overall,
        "baseline_by_project": baseline_by_project,
        "groups": groups,
        "ablations": ablation_summary,
        "case_studies": cases,
    }

    if args.json_out is not None:
        out = args.json_out if args.json_out.is_absolute() else ROOT / args.json_out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2))
        print(f"\nWrote {out}")

    if not all_rows:
        print("\n无 coverage 数据（检查 dive cache phase2/dive_stats）。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
