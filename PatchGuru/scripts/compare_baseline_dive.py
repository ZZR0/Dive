#!/usr/bin/env python3
"""Compare baseline vs DIVE phase-2 results."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_phase2(cache: Path, project: str, pr_nb: int) -> dict | None:
    p = cache / "oracles" / project / str(pr_nb) / "phase2" / "results.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare baseline vs DIVE phase-2 results")
    parser.add_argument(
        "--baseline-cache",
        default=os.environ.get("BASELINE_CACHE", ".cache_rerun"),
        help="Baseline cache dir (default: $BASELINE_CACHE or .cache_rerun)",
    )
    parser.add_argument(
        "--dive-cache",
        default=os.environ.get("DIVE_CACHE", ".cache_dive"),
        help="DIVE cache dir (default: $DIVE_CACHE or .cache_dive)",
    )
    args = parser.parse_args()
    baseline = ROOT / args.baseline_cache
    dive = ROOT / args.dive_cache

    rows: list[dict] = []
    for proj_dir in sorted((baseline / "oracles").iterdir()):
        if not proj_dir.is_dir():
            continue
        project = proj_dir.name
        for pr_dir in proj_dir.iterdir():
            if not pr_dir.is_dir() or not pr_dir.name.isdigit():
                continue
            pr_nb = int(pr_dir.name)
            p1 = json.loads((pr_dir / "results.json").read_text()) if (pr_dir / "results.json").exists() else {}
            if p1.get("stage") != "completed":
                continue
            if p1.get("review_conclusion") == "BUG":
                rows.append({
                    "project": project, "pr_nb": pr_nb,
                    "p1": "BUG", "base_p2": "-", "dive_p2": "-",
                })
                continue
            b2 = load_phase2(baseline, project, pr_nb)
            d2 = load_phase2(dive, project, pr_nb)
            if b2 is None:
                continue
            rows.append({
                "project": project,
                "pr_nb": pr_nb,
                "p1": p1.get("review_conclusion"),
                "base_p2": b2.get("review_conclusion"),
                "base_stage": b2.get("stage"),
                "base_llm": b2.get("llm_queries"),
                "dive_p2": d2.get("review_conclusion") if d2 else None,
                "dive_stage": d2.get("stage") if d2 else None,
                "dive_llm": d2.get("llm_queries") if d2 else None,
                "dive_stats": (d2 or {}).get("dive_stats"),
                "dive_strategy": (d2 or {}).get("phase2_strategy"),
            })

    n_base_bug = sum(1 for r in rows if r.get("base_p2") == "BUG")
    n_dive_bug = sum(1 for r in rows if r.get("dive_p2") == "BUG")
    n_dive_done = sum(1 for r in rows if r.get("dive_stage") == "completed")
    n_base_done = sum(1 for r in rows if r.get("base_stage") == "completed")

    new_in_dive = [r for r in rows if r.get("dive_p2") == "BUG" and r.get("base_p2") != "BUG"]
    lost_in_dive = [r for r in rows if r.get("base_p2") == "BUG" and r.get("dive_p2") != "BUG"]
    both_bug = [r for r in rows if r.get("base_p2") == "BUG" and r.get("dive_p2") == "BUG"]

    print("=" * 60)
    print("Baseline vs DIVE Phase-2 Comparison")
    print("=" * 60)
    print(f"Comparable PRs (phase1 completed): {len(rows)}")
    print(f"Baseline phase2 completed: {n_base_done}")
    print(f"DIVE phase2 completed:     {n_dive_done}")
    print(f"Baseline #warnings (p2 BUG): {n_base_bug}")
    print(f"DIVE #warnings (p2 BUG):     {n_dive_bug}")
    print(f"Both BUG:                  {len(both_bug)}")
    print(f"New BUG in DIVE only:      {len(new_in_dive)}")
    print(f"Lost BUG (baseline only):  {len(lost_in_dive)}")
    print()

    if new_in_dive:
        print("--- New warnings in DIVE ---")
        for r in new_in_dive[:30]:
            st = r.get("dive_stats") or {}
            print(f"  {r['project']} #{r['pr_nb']}  execs={st.get('execs')} "
                  f"clusters={st.get('reported_clusters')} hit={st.get('changed_line_hit')}/{st.get('changed_line_total')}")
        if len(new_in_dive) > 30:
            print(f"  ... and {len(new_in_dive) - 30} more")

    if lost_in_dive:
        print("--- Baseline BUG but DIVE not ---")
        for r in lost_in_dive:
            print(f"  {r['project']} #{r['pr_nb']}  dive={r.get('dive_p2')} stage={r.get('dive_stage')}")

    dive_execs = [r["dive_stats"]["execs"] for r in rows
                  if r.get("dive_stats") and "execs" in r["dive_stats"]]
    if dive_execs:
        print()
        print(f"DIVE execs per PR: min={min(dive_execs)} max={max(dive_execs)} "
              f"avg={sum(dive_execs)/len(dive_execs):.1f}")

    out = ROOT / "scripts" / "baseline_vs_dive_summary.json"
    out.write_text(json.dumps({
        "n_comparable": len(rows),
        "n_base_bug": n_base_bug,
        "n_dive_bug": n_dive_bug,
        "n_dive_done": n_dive_done,
        "new_in_dive": new_in_dive,
        "lost_in_dive": lost_in_dive,
        "both_bug": both_bug,
    }, indent=2))
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
