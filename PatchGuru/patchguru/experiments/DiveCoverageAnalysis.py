"""Re-measure DIVE changed-line coverage with embedded post_<fn> line mapping.

Replays the in-container search harness (same exec budget / seeds as the original
phase-2 run) but updates only ``dive_stats`` in ``phase2/results.json``.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from patchguru.analysis.DivergenceSearch import (
    COVERAGE_ANALYSIS_VERSION,
    _strip_module_main,
    build_coverage_rerun_harness,
    execute_coverage_rerun_harness,
    parse_saved_phase2_harness,
)
from patchguru.analysis.PRRetriever import get_repo
from patchguru.experiments.MutationAnalysis import (
    ROOT,
    iter_phase_jobs,
    load_pr_ids_for_project,
    oracle_root,
    resolve_pr_file,
)
from patchguru.utils.PullRequest import PullRequest
from patchguru.utils.log_util import truncate_log_text


def phase1_spec_path(cache_dir: Path, repo_name: str, pr_id: int) -> Path:
    return oracle_root(cache_dir, repo_name) / str(pr_id) / "specification.py"


def phase2_spec_path(cache_dir: Path, repo_name: str, pr_id: int) -> Path:
    return oracle_root(cache_dir, repo_name) / str(pr_id) / "phase2" / "specification.py"


def coverage_result_dir(cache_dir: Path, repo_name: str, pr_id: int) -> Path:
    return cache_dir / "coverage_analysis" / repo_name / str(pr_id) / "phase2"


def measure_dive_coverage(
    cache_dir: Path,
    repo_name: str,
    pr_id: int,
    github_repo,
    cloned_repo_manager,
    *,
    force: bool = False,
) -> bool:
    phase2_dir = oracle_root(cache_dir, repo_name) / str(pr_id) / "phase2"
    results_path = phase2_dir / "results.json"
    if not results_path.is_file():
        print(f"PR {pr_id}: skip (no phase2/results.json)")
        return False

    try:
        results = json.loads(results_path.read_text())
    except json.JSONDecodeError:
        print(f"PR {pr_id}: skip (invalid results.json)")
        return False

    if results.get("stage") != "completed":
        print(f"PR {pr_id}: skip (phase2 not completed)")
        return False

    ds = results.get("dive_stats") or {}
    if (
        not force
        and ds.get("coverage_analysis_version") == COVERAGE_ANALYSIS_VERSION
        and ds.get("changed_line_total")
    ):
        print(f"PR {pr_id}: skip (coverage v{COVERAGE_ANALYSIS_VERSION} already measured)")
        return True

    p1_spec_path = phase1_spec_path(cache_dir, repo_name, pr_id)
    p2_spec_path = phase2_spec_path(cache_dir, repo_name, pr_id)
    if not p1_spec_path.is_file():
        print(f"PR {pr_id}: skip (missing phase-1 specification.py)")
        return False
    if not p2_spec_path.is_file():
        print(f"PR {pr_id}: skip (missing phase-2 specification.py)")
        return False

    phase1_spec = p1_spec_path.read_text()
    saved = parse_saved_phase2_harness(
        p2_spec_path.read_text(),
        phase1_spec=phase1_spec,
    )
    fut_name = saved.get("fut_name")
    if not fut_name:
        print(f"PR {pr_id}: skip (could not parse __DIVE_FUT_NAME__ from phase-2 harness)")
        return False
    if saved.get("legacy_fallback_used"):
        print(f"PR {pr_id}: legacy phase-2 harness fallback (fut={fut_name!r}, "
              f"seeds={len(saved.get('seed_exprs') or [])})")

    github_pr = github_repo.get_pull(pr_id)
    pr = PullRequest(github_pr, github_repo, cloned_repo_manager)
    spec_prefix = _strip_module_main(phase1_spec)

    harness, embedded_offsets, total_changed = build_coverage_rerun_harness(
        fut_name=fut_name,
        spec_prefix=spec_prefix,
        pr=pr,
        cloned_repo_manager=cloned_repo_manager,
        seed_exprs=saved["seed_exprs"],
        strategy_exprs=saved["strategy_exprs"],
        gen_inputs_code=saved["gen_inputs_code"],
        baseline_driver_code=saved["baseline_driver_code"],
        cfg=saved["cfg"],
    )
    if total_changed <= 0:
        print(f"PR {pr_id}: skip (no changed-line targets)")
        return False

    out_dir = coverage_result_dir(cache_dir, repo_name, pr_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dive_harness.py").write_text(harness)
    (out_dir / "embedded_offsets.json").write_text(
        json.dumps(
            {
                "fut_name": fut_name,
                "embedded_offsets": embedded_offsets,
                "changed_line_total": total_changed,
                "coverage_analysis_version": COVERAGE_ANALYSIS_VERSION,
            },
            indent=2,
        )
    )

    print(
        f"PR {pr_id}: rerun harness (embedded offsets={embedded_offsets}, total={total_changed})"
    )
    exit_code, dive_result, output = execute_coverage_rerun_harness(
        harness, pr=pr, cloned_repo_manager=cloned_repo_manager
    )
    log_path = out_dir / "run.log"
    log_path.write_text(truncate_log_text(output, max_bytes=512_000, label="harness output"))

    if dive_result is None:
        print(f"PR {pr_id}: failed to parse harness output")
        return False

    new_stats = dict(dive_result.get("stats") or {})
    new_stats["coverage_analysis_version"] = COVERAGE_ANALYSIS_VERSION
    new_stats["embedded_offsets"] = embedded_offsets
    old_hit = ds.get("changed_line_hit")
    results["dive_stats"] = new_stats
    results_path.write_text(json.dumps(results, indent=4, default=str))
    print(
        f"PR {pr_id}: updated dive_stats hit {old_hit} -> {new_stats.get('changed_line_hit')} "
        f"/ {new_stats.get('changed_line_total')}"
    )
    return True


def run_dive_coverage_analysis(
    cache_dir: Path,
    repo_name: str,
    *,
    pr_file: Path | None = None,
    pr_ids: list[int] | None = None,
    limit: int | None = None,
    force: bool = False,
):
    cache_dir = cache_dir.resolve()
    if not oracle_root(cache_dir, repo_name).is_dir():
        raise FileNotFoundError(f"Oracle 目录不存在: {oracle_root(cache_dir, repo_name)}")

    if pr_ids is None:
        pr_file = resolve_pr_file(cache_dir, pr_file)
        pr_ids = load_pr_ids_for_project(repo_name, pr_file)
    allowed = set(pr_ids)

    github_repo, cloned_repo_manager = get_repo(repo_name)
    processed = 0
    for pr_id, _spec_path, _mutation_dir in iter_phase_jobs(cache_dir, repo_name, "phase2"):
        if pr_id not in allowed:
            continue
        print(f"[{cache_dir.name}] {repo_name}#{pr_id} (phase2 coverage rerun)")
        measure_dive_coverage(
            cache_dir,
            repo_name,
            pr_id,
            github_repo,
            cloned_repo_manager,
            force=force,
        )
        processed += 1
        if limit is not None and processed >= limit:
            break
    print(f"Done: processed {processed} PR(s) for {repo_name} in {cache_dir.name}")


def main():
    parser = argparse.ArgumentParser(
        description="Re-measure DIVE phase-2 changed-line coverage (embedded offset fix)."
    )
    parser.add_argument("--cache-dir", type=str, default=".cache_dive_new200")
    parser.add_argument("--repo", type=str, required=True)
    parser.add_argument("--pr-file", type=str, default=None)
    parser.add_argument("--pr-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = ROOT / cache_dir
    pr_file = Path(args.pr_file) if args.pr_file else None
    pr_ids = [args.pr_id] if args.pr_id is not None else None

    run_dive_coverage_analysis(
        cache_dir,
        args.repo,
        pr_file=pr_file,
        pr_ids=pr_ids,
        limit=args.limit,
        force=args.force,
    )


if __name__ == "__main__":
    main()
