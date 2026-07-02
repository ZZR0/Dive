#!/usr/bin/env python3
"""RQ4: phase1 ∪ phase2 changed-line coverage (union of hit counts).

Phase1 coverage is measured by replaying ``oracles/<pr>/specification.py`` (same
tracing as baseline). Phase2 uses existing ``coverage_analysis/.../phase2/`` for
baseline, and ``phase2/results.json`` ``dive_stats`` for DIVE.

Union rule (same ``changed_line_total`` per PR):
  hit_union = min(total, hit_phase1 + hit_phase2)   # upper bound without per-line sets
  hit_union_lb = max(hit_phase1, hit_phase2)        # lower bound

When phase1 and phase2 hit disjoint line sets, hit_union == hit_p1 + hit_p2.
When nested, hit_union == max(hit_p1, hit_p2). True union is in [lb, min(total, p1+p2)].

Usage:
  bash scripts/run_baseline_coverage.sh   # add phase1: --phase phase1 (see below)
  uv run python scripts/summarize_rq4_p1p2_union.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from scipy.stats import wilcoxon

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIVE = ROOT / ".cache_dive_new200"
DEFAULT_BASELINE = ROOT / ".cache_baseline_new200"


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def phase1_cov_path(cache: Path, project: str, pr_id: str) -> Path:
    return cache / "coverage_analysis" / project / pr_id / "coverage_results.json"


def phase2_cov_path(cache: Path, project: str, pr_id: str) -> Path:
    return cache / "coverage_analysis" / project / pr_id / "phase2" / "coverage_results.json"


def dive_phase2_stats(cache: Path, project: str, pr_id: str) -> dict | None:
    p = cache / "oracles" / project / pr_id / "phase2" / "results.json"
    d = load_json(p)
    if not d or d.get("stage") != "completed":
        return None
    return d.get("dive_stats") or {}


def union_bounds(h1: int, h2: int, total: int) -> tuple[int, int, int]:
    ub = min(total, h1 + h2)
    lb = max(h1, h2)
    # point estimate: assume partial overlap — use ub when sums fit, else lb
    est = ub if h1 + h2 <= total else lb
    return lb, ub, est


def collect_rows(
    dive_cache: Path,
    baseline_cache: Path,
    *,
    v2_only: bool = True,
) -> list[dict]:
    rows: list[dict] = []
    for p2_path in sorted(baseline_cache.glob("coverage_analysis/*/*/phase2/coverage_results.json")):
        project, pr_id = p2_path.parts[-4], p2_path.parts[-3]
        bl2 = load_json(p2_path)
        if not bl2 or not bl2.get("changed_line_total"):
            continue
        if v2_only and bl2.get("analysis_version") != 2:
            continue
        total = int(bl2["changed_line_total"])
        bl2_hit = int(bl2["changed_line_hit"])

        bl1 = load_json(phase1_cov_path(baseline_cache, project, pr_id))
        bl1_hit = int(bl1["changed_line_hit"]) if bl1 and bl1.get("changed_line_total") else None
        bl1_total = int(bl1["changed_line_total"]) if bl1 and bl1.get("changed_line_total") else None

        ds = dive_phase2_stats(dive_cache, project, pr_id)
        if not ds or not ds.get("changed_line_total"):
            continue
        if v2_only and ds.get("coverage_analysis_version") != 2:
            continue
        if int(ds["changed_line_total"]) != total:
            continue
        d2_hit = int(ds.get("changed_line_hit", 0))

        dv1 = load_json(phase1_cov_path(dive_cache, project, pr_id))
        d1_hit = int(dv1["changed_line_hit"]) if dv1 and dv1.get("changed_line_total") else None
        d1_total = int(dv1["changed_line_total"]) if dv1 and dv1.get("changed_line_total") else None

        row = {
            "project": project,
            "pr_id": pr_id,
            "total": total,
            "bl_p2": bl2_hit / total,
            "d_p2": d2_hit / total,
            "bl_p1": bl1_hit / total if bl1_hit is not None else None,
            "d_p1": d1_hit / total if d1_hit is not None else None,
        }
        if bl1_hit is not None and bl1_total == total:
            lb, ub, est = union_bounds(bl1_hit, bl2_hit, total)
            row["bl_union_est"] = est / total
            row["bl_union_lb"] = lb / total
            row["bl_union_ub"] = ub / total
        if d1_hit is not None and d1_total == total:
            lb, ub, est = union_bounds(d1_hit, d2_hit, total)
            row["d_union_est"] = est / total
            row["d_union_lb"] = lb / total
            row["d_union_ub"] = ub / total
        rows.append(row)
    return rows


def summarize(name: str, rates: list[float]) -> str:
    if not rates:
        return f"{name}: n=0"
    a = np.array(rates)
    return f"{name}: n={len(a)} μ={100*a.mean():.1f}% med={100*np.median(a):.1f}% 0-hit={100*(a==0).mean():.1f}%"


def main() -> None:
    rows = collect_rows(DEFAULT_DIVE, DEFAULT_BASELINE, v2_only=True)
    if not rows:
        print("无 paired 数据。", file=sys.stderr)
        sys.exit(1)

    has_bl_p1 = [r for r in rows if r.get("bl_union_est") is not None]
    has_d_p1 = [r for r in rows if r.get("d_union_est") is not None]
    has_p1 = [r for r in rows if r.get("bl_union_est") is not None and r.get("d_union_est") is not None]
    print(
        f"=== RQ4 phase1∪phase2（双边 v2 paired，n={len(rows)}，"
        f"baseline p1={len(has_bl_p1)} dive p1={len(has_d_p1)} 双边 p1={len(has_p1)}）===\n"
    )
    print("Phase2 only（当前主表口径）:")
    print(" ", summarize("DIVE p2", [r["d_p2"] for r in rows]))
    print(" ", summarize("Baseline p2", [r["bl_p2"] for r in rows]))
    d2 = np.array([r["d_p2"] for r in rows])
    b2 = np.array([r["bl_p2"] for r in rows])
    print(f"  Δμ(DIVE−Base) p2 = {100*(d2.mean()-b2.mean()):+.1f} pp")

    if has_p1:
        print("\nPhase1 only:")
        print(" ", summarize("DIVE p1", [r["d_p1"] for r in has_p1 if r["d_p1"] is not None]))
        print(" ", summarize("Baseline p1", [r["bl_p1"] for r in has_p1 if r["bl_p1"] is not None]))

        print("\nUnion 估计（hit_union ≈ min(total, p1+p2)，无 per-line 集合时的上界估计）:")
        print(" ", summarize("DIVE union est", [r["d_union_est"] for r in has_p1]))
        print(" ", summarize("Baseline union est", [r["bl_union_est"] for r in has_p1]))
        du = np.array([r["d_union_est"] for r in has_p1])
        bu = np.array([r["bl_union_est"] for r in has_p1])
        print(f"  Δμ(DIVE−Base) union est = {100*(du.mean()-bu.mean()):+.1f} pp")
        try:
            w = wilcoxon(du, bu, alternative="greater")
            print(f"  Wilcoxon p(DIVE union > Baseline union) = {w.pvalue:.4g}")
        except Exception:
            pass

        improved_d = sum(1 for r in has_p1 if r["d_union_est"] > r["d_p2"] + 1e-9)
        improved_b = sum(1 for r in has_p1 if r["bl_union_est"] > r["bl_p2"] + 1e-9)
        print(f"\n  Union > p2 only: DIVE {improved_d}/{len(has_p1)}, Baseline {improved_b}/{len(has_p1)}")

        both_hit = [r for r in has_p1 if r["d_union_est"] > 0 and r["bl_union_est"] > 0]
        if both_hit:
            du_b = np.array([r["d_union_est"] for r in both_hit])
            bu_b = np.array([r["bl_union_est"] for r in both_hit])
            print("\nUnion 子集（双边 union>0）:")
            print(" ", summarize("DIVE union est", list(du_b)))
            print(" ", summarize("Baseline union est", list(bu_b)))
            print(f"  Δμ union = {100*(du_b.mean()-bu_b.mean()):+.1f} pp (n={len(both_hit)})")
    else:
        print("\n尚无 phase1 coverage 数据。请先跑:")
        print("  uv run python -m patchguru.experiments.BaselineCoverageAnalysis \\")
        print("    --cache-dir .cache_baseline_new200 --phase phase1 --pr-file scripts/pr_batch_300/new200.txt")
        print("  uv run python -m patchguru.experiments.BaselineCoverageAnalysis \\")
        print("    --cache-dir .cache_dive_new200 --phase phase1 --pr-file scripts/pr_batch_300/new200.txt")


if __name__ == "__main__":
    main()
