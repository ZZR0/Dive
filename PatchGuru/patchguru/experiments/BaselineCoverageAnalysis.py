"""Post-hoc changed-line coverage for PatchGuru baseline phase-2 oracles.

Baseline specs do not record ``changed_line_hit`` during the original pipeline.
This module replays each completed baseline ``specification.py`` in Docker with
``sys.settrace`` on ``post_<fn>`` (same line-set definition as DIVE search).
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

from patchguru.analysis.DivergenceSearch import _compute_changed_offsets
from patchguru.analysis.PRRetriever import get_repo
from patchguru.execution.DockerExecutor import DockerExecutor
from patchguru.experiments.MutationAnalysis import (
    ROOT,
    detect_spec_format,
    dive_post_fn_name,
    iter_phase_jobs,
    load_pr_ids_for_project,
    oracle_root,
    parse_dive_fut_name,
    resolve_pr_file,
)
from patchguru.utils.PullRequest import PullRequest
from patchguru.utils.log_util import truncate_log_text

ANALYSIS_VERSION = 2
HARNESS_PATH = "/tmp/PatchGuru/PatchGuru_test_code.py"
RESULT_BEGIN = "===COVERAGE_RESULT_BEGIN==="
RESULT_END = "===COVERAGE_RESULT_END==="


def coverage_result_dir(cache_dir: Path, repo_name: str, pr_id: int, phase: str) -> Path:
    base = cache_dir / "coverage_analysis" / repo_name / str(pr_id)
    return base / "phase2" if phase == "phase2" else base


def legacy_after_section(spec: str) -> str:
    if "## After Pull Request" not in spec:
        return ""
    after = spec.split("## After Pull Request", 1)[1]
    for marker in ("# Formal Specification", "# Specification", "## Extended Specification"):
        if marker in after:
            after = after.split(marker, 1)[0]
            break
    return after


def parse_legacy_post_fn_name(spec: str) -> str:
    section = legacy_after_section(spec)
    if not section:
        raise ValueError("legacy spec 缺少 ## After Pull Request 段")
    match = re.search(r"def (post_\w+)\s*\(", section)
    if not match:
        raise ValueError("legacy spec 中未找到 post_<fn> 定义")
    return match.group(1)


def coverage_entry_call(spec: str) -> str:
    try:
        tree = ast.parse(spec)
    except SyntaxError as exc:
        raise ValueError(f"spec 语法错误: {exc}") from exc
    names = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if "main" in names:
        return "main()"
    if "run_assertions" in names:
        return "run_assertions()"
    raise ValueError("spec 中未找到 main() 或 run_assertions()")


def strip_main_guard(spec: str) -> str:
    pattern = (
        r"\nif __name__\s*==\s*(?:'__main__'|\"__main__\")\s*:\s*\n"
        r"\s*(?:main\(\)|run_assertions\(\))\s*\Z"
    )
    match = re.search(pattern, spec, re.DOTALL)
    if match:
        return spec[: match.start()].rstrip()
    # tolerate trailing whitespace/comments after main()
    pattern2 = (
        r"\nif __name__\s*==\s*(?:'__main__'|\"__main__\")\s*:\s*\n"
        r"\s*(?:main\(\)|run_assertions\(\))\s*"
    )
    match = re.search(pattern2 + r"(?:#.*?\n|\s)*\Z", spec, re.DOTALL)
    if match:
        return spec[: match.start()].rstrip()
    return spec.rstrip()


def build_coverage_spec(spec: str, post_fn_name: str, changed_offsets: list[int], entry_call: str) -> str:
    if not changed_offsets:
        raise ValueError("changed_offsets 为空")
    body = strip_main_guard(spec)
    suffix = f"""

# === RQ4 baseline changed-line coverage (instrumentation) ===
import sys as _rq4_sys
import json as _rq4_json

_RQ4_HARNESS = {HARNESS_PATH!r}
_RQ4_OFFSETS = {json.dumps(changed_offsets)}
_RQ4_STATE = {{"covered": set()}}
_RQ4_TARGETS = set()


def _rq4_trace_call(_callable, *args, **kwargs):
    executed = set()

    def _tracer(frame, event, arg):
        if event == "line" and frame.f_code.co_filename == _RQ4_HARNESS:
            executed.add(frame.f_lineno)
        return _tracer

    _rq4_sys.settrace(_tracer)
    try:
        return _callable(*args, **kwargs)
    finally:
        _rq4_sys.settrace(None)
        _RQ4_STATE["covered"] |= (executed & _RQ4_TARGETS)


def _rq4_wrap_post():
    global {post_fn_name}, _RQ4_TARGETS
    _orig = {post_fn_name}

    if isinstance(_orig, property):
        _fget = _orig.fget
        if _fget is None:
            raise TypeError("post_<fn> property has no getter")
        _first = _fget.__code__.co_firstlineno
        _RQ4_TARGETS = set(_first + off for off in _RQ4_OFFSETS)

        def _wrapped_fget(*args, **kwargs):
            return _rq4_trace_call(_fget, *args, **kwargs)

        {post_fn_name} = property(_wrapped_fget, _orig.fset, _orig.fdel, _orig.__doc__)
        return

    if not callable(_orig) or not hasattr(_orig, "__code__"):
        raise TypeError(
            "post_<fn> must be a function or @property, got "
            + type(_orig).__name__
        )

    _first = _orig.__code__.co_firstlineno
    _RQ4_TARGETS = set(_first + off for off in _RQ4_OFFSETS)

    def _wrapped(*args, **kwargs):
        return _rq4_trace_call(_orig, *args, **kwargs)

    {post_fn_name} = _wrapped


_rq4_wrap_post()

_rq4_exc = None
try:
    {entry_call}
except BaseException as _e:
    _rq4_exc = repr(_e)

print("{RESULT_BEGIN}", flush=True)
print(_rq4_json.dumps({{
    "changed_line_total": len(_RQ4_OFFSETS),
    "changed_line_hit": len(_RQ4_STATE["covered"]),
    "driver_error": _rq4_exc,
}}))
print("{RESULT_END}", flush=True)
"""
    return body + suffix


def parse_coverage_output(output: str) -> dict:
    if RESULT_BEGIN not in output or RESULT_END not in output:
        raise ValueError("容器输出缺少 coverage 结果标记")
    block = output.split(RESULT_BEGIN, 1)[1].split(RESULT_END, 1)[0].strip()
    return json.loads(block)


def resolve_post_fn_and_fut(spec: str, spec_format: str) -> tuple[str, str]:
    if spec_format == "legacy":
        post_fn = parse_legacy_post_fn_name(spec)
        fut = post_fn[len("post_") :]
        return post_fn, fut
    fut = parse_dive_fut_name(spec)
    return dive_post_fn_name(fut), fut


def measure_baseline_coverage(
    spec_path: Path,
    result_dir: Path,
    github_repo,
    pr_id: int,
    cloned_repo_manager,
    *,
    force: bool = False,
) -> bool:
    result_json = result_dir / "coverage_results.json"
    if result_json.is_file() and not force:
        try:
            existing = json.loads(result_json.read_text())
        except json.JSONDecodeError:
            existing = {}
        if existing.get("analysis_version") == ANALYSIS_VERSION and existing.get("changed_line_total"):
            print(f"PR {pr_id}: skip (existing coverage_results.json)")
            return True

    spec = spec_path.read_text()
    try:
        spec_format = detect_spec_format(spec)
    except ValueError as exc:
        print(f"Skipping PR {pr_id}: {exc}")
        return False

    github_pr = github_repo.get_pull(pr_id)
    pr = PullRequest(github_pr, github_repo, cloned_repo_manager)
    try:
        post_fn_name, fut_name = resolve_post_fn_and_fut(spec, spec_format)
    except ValueError as exc:
        print(f"Skipping PR {pr_id}: {exc}")
        return False
    changed_offsets, _ = _compute_changed_offsets(pr, fut_name, cloned_repo_manager)
    if not changed_offsets:
        print(f"Skipping PR {pr_id}: no changed-line offsets for {fut_name}")
        return False

    try:
        entry_call = coverage_entry_call(spec)
        instrumented = build_coverage_spec(spec, post_fn_name, changed_offsets, entry_call)
    except ValueError as exc:
        print(f"Skipping PR {pr_id}: {exc}")
        return False

    cloned_repo = cloned_repo_manager.get_cloned_repo(pr.post_commit)
    docker_executor = DockerExecutor(cloned_repo.container_name)
    exit_code, output = docker_executor.execute_python_code(instrumented, timeout=300)
    print(f"PR {pr_id}: container exit={exit_code}, output_bytes={len(output.encode('utf-8', errors='replace'))}")
    print("-" * 40)

    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "run.log").write_text(truncate_log_text(output, max_bytes=512_000, label="harness output"))

    try:
        parsed = parse_coverage_output(output)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"PR {pr_id}: failed to parse coverage output: {exc}")
        return False

    payload = {
        "analysis_version": ANALYSIS_VERSION,
        "post_fn": post_fn_name,
        "fut_name": fut_name,
        "changed_line_total": int(parsed["changed_line_total"]),
        "changed_line_hit": int(parsed["changed_line_hit"]),
        "execution_exit_code": exit_code,
        "driver_error": parsed.get("driver_error"),
    }
    result_dir.mkdir(parents=True, exist_ok=True)
    result_json.write_text(json.dumps(payload, indent=2))
    return True


def run_baseline_coverage_analysis(
    cache_dir: Path,
    repo_name: str,
    *,
    phase: str = "phase2",
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
    for pr_id, spec_path, _mutation_dir in iter_phase_jobs(cache_dir, repo_name, phase):
        if pr_id not in allowed:
            continue
        out_dir = coverage_result_dir(cache_dir, repo_name, pr_id, phase)
        print(f"[{cache_dir.name}] {repo_name}#{pr_id} ({phase}) -> {out_dir}")
        measure_baseline_coverage(
            spec_path,
            out_dir,
            github_repo,
            pr_id,
            cloned_repo_manager,
            force=force,
        )
        processed += 1
        if limit is not None and processed >= limit:
            break
    print(f"Done: processed {processed} PR(s) for {repo_name} in {cache_dir.name}")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Measure baseline phase-2 changed-line coverage (RQ4). "
            "Requires PATCHGURU_CLONES_DIR and PATCHGURU_CONTAINER_TAG."
        )
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=".cache_baseline_new200",
        help="Baseline cache root (default: .cache_baseline_new200)",
    )
    parser.add_argument("--repo", type=str, required=True)
    parser.add_argument("--phase", type=str, default="phase2", choices=["phase1", "phase2"])
    parser.add_argument("--pr-file", type=str, default=None)
    parser.add_argument("--pr-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true", help="Recompute even if result exists")
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = ROOT / cache_dir
    pr_file = Path(args.pr_file) if args.pr_file else None
    pr_ids = [args.pr_id] if args.pr_id is not None else None

    run_baseline_coverage_analysis(
        cache_dir,
        args.repo,
        phase=args.phase,
        pr_file=pr_file,
        pr_ids=pr_ids,
        limit=args.limit,
        force=args.force,
    )


if __name__ == "__main__":
    main()
