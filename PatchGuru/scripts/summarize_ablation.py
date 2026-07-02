#!/usr/bin/env python3
"""汇总 RQ2 DIVE 消融实验：流水线统计 + 可选人工金标准 precision/recall。

用法:
  uv run python scripts/summarize_ablation.py
  uv run python scripts/summarize_ablation.py --ground-truth .cache/manual_annotation/dive_new200/ground_truth.txt
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from summarize_results import resolve_pr_file, summarize_project  # noqa: E402
DEFAULT_PROJECTS = ["marshmallow", "pandas", "scipy", "keras"]
DEFAULT_ABLATIONS = ["no_constructor", "no_guided", "no_triage"]
DEFAULT_GT = ROOT / ".cache/manual_annotation/dive_new200/ground_truth.txt"
DEFAULT_BASELINE = ROOT / ".cache_baseline_new200"
DEFAULT_FULL_DIVE = ROOT / ".cache_dive_new200"
DEFAULT_PR_FILE = ROOT / "scripts/pr_batch_300/new200.txt"


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


def pipeline_stats(cache: Path, pr_file: Path | None) -> dict:
    pf = resolve_pr_file(cache, pr_file)
    per_project = {}
    totals = {
        "total": 0,
        "warnings": 0,
        "normal": 0,
        "failures": 0,
        "p1_bug": 0,
        "p2_bug": 0,
        "oracles": 0,
    }
    for project in DEFAULT_PROJECTS:
        try:
            s = summarize_project(cache, project, pf)
        except FileNotFoundError:
            continue
        per_project[project] = s
        for k in totals:
            if k in s:
                totals[k] += s[k]
    return {"totals": totals, "projects": per_project}


def avg_dive_stats(cache: Path) -> dict:
    execs = []
    changed_hit = []
    changed_total = []
    ic_queries = []
    oracle = cache / "oracles"
    if not oracle.is_dir():
        return {}
    for proj_dir in oracle.iterdir():
        if not proj_dir.is_dir():
            continue
        for pr_dir in proj_dir.iterdir():
            p2 = load_json(pr_dir / "phase2" / "results.json")
            if not p2:
                continue
            ds = p2.get("dive_stats") or {}
            if "execs" in ds:
                execs.append(ds["execs"])
            if "changed_line_hit" in ds and "changed_line_total" in ds:
                changed_hit.append(ds["changed_line_hit"])
                changed_total.append(ds["changed_line_total"])
            ic = p2.get("input_constructor_queries")
            if ic is not None:
                ic_queries.append(ic)
    out: dict = {}
    if execs:
        out["avg_execs"] = round(sum(execs) / len(execs), 1)
    if changed_total:
        out["avg_changed_line_hit"] = round(sum(changed_hit) / len(changed_hit), 2)
        out["avg_changed_line_total"] = round(sum(changed_total) / len(changed_total), 2)
    if ic_queries:
        out["avg_ic_queries"] = round(sum(ic_queries) / len(ic_queries), 2)
    return out


def metrics(reported: set[tuple[str, str]], gt: dict[tuple[str, str], dict]) -> dict:
    tp = fp = fn = 0
    for key, meta in gt.items():
        is_pos = meta["label"] == "TP"
        pred = key in reported
        if pred and is_pos:
            tp += 1
        elif pred and not is_pos:
            fp += 1
        elif not pred and is_pos:
            fn += 1
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {
        "reported": len(reported),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": prec,
        "recall": rec,
        "f1": f1,
    }


def fmt_pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总 RQ2 DIVE 消融实验")
    parser.add_argument("--baseline-cache", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--full-dive-cache", type=Path, default=DEFAULT_FULL_DIVE)
    parser.add_argument("--ground-truth", type=Path, default=None)
    parser.add_argument("--pr-file", type=Path, default=DEFAULT_PR_FILE)
    parser.add_argument("--output-json", type=Path, help="可选：写入 JSON 汇总")
    args = parser.parse_args()

    pr_file = args.pr_file if args.pr_file.is_file() else None
    gt_path = args.ground_truth
    if gt_path is None and DEFAULT_GT.is_file():
        gt_path = DEFAULT_GT
    gt = load_ground_truth(gt_path) if gt_path and gt_path.is_file() else None

    variants: list[tuple[str, Path]] = [("full_dive", ROOT / args.full_dive_cache)]
    for ab in DEFAULT_ABLATIONS:
        cache = ROOT / f".cache_dive_ablation_{ab}_new200"
        if cache.is_dir():
            variants.append((ab, cache))

    rows = []
    print("=== RQ2 DIVE 消融汇总 ===\n")
    header = f"{'variant':<18} {'warn':>5} {'p2':>5} {'fail':>5}"
    if gt:
        header += f" {'TP':>4} {'FP':>4} {'Prec':>7} {'Rec':>7} {'F1':>7}"
    header += f" {'avg_exec':>9} {'avg_cov':>10}"
    print(header)
    print("-" * len(header))

    for name, cache in variants:
        if not cache.is_dir():
            continue
        pipe = pipeline_stats(cache, pr_file)
        t = pipe["totals"]
        ds = avg_dive_stats(cache)
        row = {
            "variant": name,
            "cache": str(cache.relative_to(ROOT)),
            "pipeline": t,
            "dive_stats_avg": ds,
        }
        line = (
            f"{name:<18} {t['warnings']:>5} {t['p2_bug']:>5} {t['failures']:>5}"
        )
        if gt:
            m = metrics(set(collect_bugs(cache)), gt)
            row["annotation"] = m
            line += (
                f" {m['tp']:>4} {m['fp']:>4}"
                f" {fmt_pct(m['precision']):>7} {fmt_pct(m['recall']):>7} {fmt_pct(m['f1']):>7}"
            )
        cov = ""
        if ds.get("avg_changed_line_hit") is not None:
            cov = f"{ds['avg_changed_line_hit']}/{ds['avg_changed_line_total']}"
        line += f" {ds.get('avg_execs', '-'):>9} {cov:>10}"
        print(line)
        rows.append(row)

    baseline = ROOT / args.baseline_cache
    if baseline.is_dir():
        pipe = pipeline_stats(baseline, pr_file)
        t = pipe["totals"]
        line = f"{'baseline':<18} {t['warnings']:>5} {t['p2_bug']:>5} {t['failures']:>5}"
        if gt:
            m = metrics(set(collect_bugs(baseline)), gt)
            line += (
                f" {m['tp']:>4} {m['fp']:>4}"
                f" {fmt_pct(m['precision']):>7} {fmt_pct(m['recall']):>7} {fmt_pct(m['f1']):>7}"
            )
        line += f" {'-':>9} {'-':>10}"
        print(line)

    if gt:
        print(f"\n金标准: {gt_path} ({len(gt)} 条告警并集)")
    else:
        print("\n未找到 ground_truth，跳过 precision/recall（可用 --ground-truth 指定）")

    if args.output_json:
        out_path = args.output_json if args.output_json.is_absolute() else ROOT / args.output_json
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({"variants": rows}, indent=2))
        print(f"\n已写入 {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
