#!/usr/bin/env python3
"""Seed .cache_dive with phase-1 artifacts from baseline so DIVE only re-runs phase 2."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def eligible_prs(baseline_dir: Path) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for proj_dir in sorted(baseline_dir.iterdir()):
        if not proj_dir.is_dir():
            continue
        project = proj_dir.name
        for pr_dir in proj_dir.iterdir():
            if not pr_dir.is_dir() or not pr_dir.name.isdigit():
                continue
            p1 = pr_dir / "results.json"
            p2 = pr_dir / "phase2" / "results.json"
            if not p1.exists() or not p2.exists():
                continue
            d1 = json.loads(p1.read_text())
            d2 = json.loads(p2.read_text())
            if d1.get("stage") != "completed" or d1.get("review_conclusion") != "NORMAL":
                continue
            if d2.get("stage") != "completed":
                continue
            out.append((project, int(pr_dir.name)))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-cache", default=".cache_rerun")
    parser.add_argument("--dive-cache", default=".cache_dive")
    parser.add_argument("--pr-file", type=Path, help="Optional subset: lines 'project pr_nb'")
    args = parser.parse_args()

    baseline = ROOT / args.baseline_cache / "oracles"
    dive = ROOT / args.dive_cache / "oracles"
    dive.mkdir(parents=True, exist_ok=True)

    if args.pr_file:
        prs: list[tuple[str, int]] = []
        with open(args.pr_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                proj, nb = line.split()
                prs.append((proj, int(nb)))
    else:
        prs = eligible_prs(baseline)

    prepared: list[tuple[str, int]] = []
    for project, pr_nb in prs:
        src = baseline / project / str(pr_nb)
        dst = dive / project / str(pr_nb)
        if not (src / "results.json").exists():
            print(f"skip {project} {pr_nb}: no baseline phase1")
            continue
        dst.mkdir(parents=True, exist_ok=True)
        for name in ("results.json", "specification.py", "config.json"):
            sp = src / name
            if sp.exists():
                shutil.copy2(sp, dst / name)
        # ensure no stale phase2
        phase2 = dst / "phase2"
        if phase2.exists():
            shutil.rmtree(phase2)
        prepared.append((project, pr_nb))

    pr_list_path = ROOT / args.dive_cache / "dive_prs.txt"
    pr_list_path.parent.mkdir(parents=True, exist_ok=True)
    pr_list_path.write_text("\n".join(f"{p} {n}" for p, n in prepared) + "\n")
    print(f"Prepared phase1 for {len(prepared)} PRs -> {dive}")
    print(f"Wrote PR list -> {pr_list_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
