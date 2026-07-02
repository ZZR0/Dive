#!/usr/bin/env python3
"""对比 dive / baseline 告警，基于统一人工金标准计算 TP/FP/Precision/Recall。"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GT = ROOT / ".cache/manual_annotation/dive_new200/ground_truth.txt"
DEFAULT_DIVE = ROOT / ".cache_dive_new200"
DEFAULT_BASE = ROOT / ".cache_baseline_new200"


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def load_ground_truth(path: Path) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        body, _, note = line.partition("#")
        parts = body.split()
        if len(parts) < 4:
            continue
        proj, pr, phase, label = parts[0], parts[1], parts[2], parts[3]
        out[(proj, pr)] = {"phase": phase, "label": label, "note": note.strip()}
    return out


def collect_bugs(cache: Path) -> dict[tuple[str, str], str]:
    bugs: dict[tuple[str, str], str] = {}
    oracle = cache / "oracles"
    if not oracle.is_dir():
        return bugs
    for proj_dir in oracle.iterdir():
        if not proj_dir.is_dir():
            continue
        for pr_dir in proj_dir.iterdir():
            if not pr_dir.name.isdigit():
                continue
            p1 = load_json(pr_dir / "results.json")
            if not p1 or p1.get("stage") != "completed":
                continue
            if p1.get("review_conclusion") == "BUG":
                bugs[(proj_dir.name, pr_dir.name)] = "p1"
            elif p1.get("review_conclusion") == "NORMAL":
                p2 = load_json(pr_dir / "phase2" / "results.json")
                if p2 and p2.get("review_conclusion") == "BUG":
                    bugs[(proj_dir.name, pr_dir.name)] = "p2"
    return bugs


def metrics(reported: set[tuple[str, str]], gt: dict[tuple[str, str], dict]) -> dict:
    tp = fp = fn = tn = 0
    for key, meta in gt.items():
        is_pos = meta["label"] == "TP"
        pred = key in reported
        if pred and is_pos:
            tp += 1
        elif pred and not is_pos:
            fp += 1
        elif not pred and is_pos:
            fn += 1
        else:
            tn += 1
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": prec, "recall": rec, "f1": f1,
            "reported": len(reported)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ground-truth", type=Path, default=DEFAULT_GT)
    parser.add_argument("--dive-cache", type=Path, default=DEFAULT_DIVE)
    parser.add_argument("--baseline-cache", type=Path, default=DEFAULT_BASE)
    args = parser.parse_args()

    gt = load_ground_truth(args.ground_truth)
    dive_bugs = collect_bugs(args.dive_cache)
    base_bugs = collect_bugs(args.baseline_cache)

    dive_set = set(dive_bugs)
    base_set = set(base_bugs)
    gt_tp = sum(1 for v in gt.values() if v["label"] == "TP")
    gt_fp = sum(1 for v in gt.values() if v["label"] == "FP")

    dm = metrics(dive_set, gt)
    bm = metrics(base_set, gt)

    both = dive_set & base_set
    dive_only = dive_set - base_set
    base_only = base_set - dive_set

    def labeled(keys: set[tuple[str, str]]) -> tuple[int, int]:
        t = sum(1 for k in keys if gt.get(k, {}).get("label") == "TP")
        f = sum(1 for k in keys if gt.get(k, {}).get("label") == "FP")
        return t, f

    print("=" * 72)
    print("统一金标准（dive ∪ baseline 全部告警 PR）")
    print("=" * 72)
    print(f"标注 PR 数: {len(gt)}  (TP={gt_tp}, FP={gt_fp})")
    print()

    print("=" * 72)
    print("总体 TP/FP 对比")
    print("=" * 72)
    print(f"{'':16} {'dive':>10} {'baseline':>10}")
    print(f"{'报 BUG 数':16} {dm['reported']:>10} {bm['reported']:>10}")
    print(f"{'TP (真 bug)':16} {dm['tp']:>10} {bm['tp']:>10}")
    print(f"{'FP (误报)':16} {dm['fp']:>10} {bm['fp']:>10}")
    print(f"{'FN (漏报)':16} {dm['fn']:>10} {bm['fn']:>10}")
    print(f"{'Precision':16} {dm['precision']:>9.1%} {bm['precision']:>9.1%}")
    print(f"{'Recall':16} {dm['recall']:>9.1%} {bm['recall']:>9.1%}")
    print(f"{'F1':16} {dm['f1']:>9.1%} {bm['f1']:>9.1%}")

    print()
    print("=" * 72)
    print("告警重叠分析")
    print("=" * 72)
    bt, bf = labeled(both)
    dt, df = labeled(dive_only)
    bot, bof = labeled(base_only)
    print(f"两者都报: {len(both):2}  (TP={bt}, FP={bf}, prec={bt/(bt+bf):.0%})" if bt + bf else f"两者都报: {len(both)}")
    print(f"仅 dive 报: {len(dive_only):2}  (TP={dt}, FP={df}, prec={dt/(dt+df):.0%})" if dt + df else f"仅 dive 报: {len(dive_only)}")
    print(f"仅 baseline 报: {len(base_only):2}  (TP={bot}, FP={bof}, prec={bot/(bot+bof):.0%})" if bot + bof else f"仅 baseline 报: {len(base_only)}")

    print()
    print("仅 baseline 报（dive 漏掉的真 bug）:")
    for proj, pr in sorted(base_only, key=lambda x: (x[0], int(x[1]))):
        m = gt[(proj, pr)]
        if m["label"] == "TP":
            print(f"  {proj}/{pr}  # {m['note'][:65]}")

    print()
    print("仅 dive 报（baseline 漏掉的真 bug）:")
    for proj, pr in sorted(dive_only, key=lambda x: (x[0], int(x[1]))):
        m = gt[(proj, pr)]
        if m["label"] == "TP":
            print(f"  {proj}/{pr}  # {m['note'][:65]}")

    print()
    print("误报对比:")
    print("  dive FP:", ", ".join(f"{p}/{pr}" for p, pr in sorted(dive_set) if gt.get((p, pr), {}).get("label") == "FP"))
    print("  baseline FP:", ", ".join(f"{p}/{pr}" for p, pr in sorted(base_set) if gt.get((p, pr), {}).get("label") == "FP"))

    out = ROOT / ".cache/manual_annotation/dive_new200/full_comparison.json"
    out.write_text(json.dumps({
        "ground_truth": {"n": len(gt), "tp": gt_tp, "fp": gt_fp},
        "dive": dm, "baseline": bm,
        "overlap": {"both": len(both), "dive_only": len(dive_only), "base_only": len(base_only)},
    }, indent=2))
    print(f"\n详细 JSON: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
