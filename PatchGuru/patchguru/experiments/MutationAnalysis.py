import argparse
import ast
import difflib
import json
import os
import re
from hashlib import blake2b
from pathlib import Path

from patchguru.analysis.PRRetriever import get_repo
from patchguru.execution.DockerExecutor import DockerExecutor
from patchguru.utils.CodeMutation import beautify_code, generate_mutants
from patchguru.utils.PullRequest import PullRequest
from patchguru.utils.PythonCodeUtil import update_function_name

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROJECTS = ["marshmallow", "pandas", "scipy", "keras"]
DEFAULT_PR_FILE = ROOT / "scripts" / "pr_batch_300" / "new200.txt"
SPEC_MARKERS = ("# Formal Specification", "# Specification")
DIVE_GEN_INPUTS_MARKER = "# ===================== LLM gen_inputs() ====================="
# Bump when mutation semantics change (e.g. DIVE oracle fix); stale JSON is not resumed.
ANALYSIS_VERSION = 2


def extract_fut_code(fut_info, pre_fix=None):
    assert len(fut_info.items()) == 1, "Only single function change is supported in this analysis."
    fct_name, fct_info = list(fut_info.items())[0]
    code = fct_info["code"]
    only_name = fct_name.split(".")[-1]
    if pre_fix:
        code = update_function_name(code, only_name, f"{pre_fix}{only_name}")
    return code


def load_pr_ids_for_project(project: str, pr_file: Path | None) -> list[int]:
    if pr_file is None:
        raise FileNotFoundError("需要 --pr-file 或 --pr-id 指定 PR 范围")
    ids: list[int] = []
    with open(pr_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) == 2 and parts[0] == project:
                ids.append(int(parts[1]))
    if not ids:
        raise FileNotFoundError(f"{pr_file} 中无 {project} 的 PR")
    return sorted(ids)


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


def oracle_root(cache_dir: Path, repo_name: str) -> Path:
    return cache_dir / "oracles" / repo_name


def mutation_result_dir(cache_dir: Path, repo_name: str, pr_id: int, phase: str) -> Path:
    base = cache_dir / "mutation_testing" / repo_name / str(pr_id)
    if phase == "phase2":
        return base / "phase2"
    return base


def is_oracle_completed(results_path: Path) -> bool:
    if not results_path.is_file():
        return False
    try:
        data = json.loads(results_path.read_text())
    except json.JSONDecodeError:
        return False
    return data.get("stage") == "completed"


def mutation_score_from_results(data: dict) -> float | None:
    killed = data.get("n_mutant_fail_assert", 0) + data.get("n_mutant_fail_other", 0)
    survived = data.get("n_mutant_pass", 0)
    total = killed + survived
    if total == 0:
        return None
    return killed / total


def detect_spec_format(spec: str) -> str:
    if "__DIVE_FUT_NAME__" in spec:
        return "dive"
    if "## Before Pull Request" in spec and any(marker in spec for marker in SPEC_MARKERS):
        return "legacy"
    raise ValueError("无法识别 specification 格式（非 legacy 分段，也非 DIVE harness）")


def split_spec_marker(spec: str) -> tuple[str, str, str]:
    for marker in SPEC_MARKERS:
        if marker in spec:
            before, after = spec.split(marker, 1)
            return before, marker, after
    raise ValueError(f"spec 中缺少分段标记: {SPEC_MARKERS}")


def build_legacy_spec(spec: str, pre_fut_code: str, post_fut_code: str) -> str:
    before = spec.split("## Before Pull Request")[0]
    _, marker, after = split_spec_marker(spec)
    return (
        f"{before}## Before Pull Request\n{pre_fut_code}\n"
        f"## After Pull Request\n{post_fut_code}\n{marker}{after}"
    )


def inject_legacy_mutant(spec: str, mutant: str) -> str:
    before = spec.split("## After Pull Request")[0]
    _, marker, after = split_spec_marker(spec)
    return f"{before}## After Pull Request\n{mutant}\n{marker}{after}"


def parse_dive_fut_name(spec: str) -> str:
    match = re.search(r"__DIVE_FUT_NAME__\s*=\s*['\"]([^'\"]+)['\"]", spec)
    if not match:
        raise ValueError("DIVE spec 缺少 __DIVE_FUT_NAME__")
    return match.group(1)


def dive_post_fn_name(fut: str) -> str:
    return f"post_{fut}"


def find_function_line_span(source: str, func_name: str) -> tuple[int, int]:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            if node.end_lineno is None:
                raise ValueError(f"无法定位函数 {func_name} 的结束行")
            return node.lineno, node.end_lineno
    raise ValueError(f"spec 中未找到函数 {func_name}")


def replace_function(source: str, func_name: str, new_func: str) -> str:
    start_line, end_line = find_function_line_span(source, func_name)
    lines = source.splitlines(keepends=True)
    replacement = new_func.rstrip() + "\n"
    if end_line < len(lines) and lines[end_line].strip() == "":
        end_line += 1
    return "".join(lines[: start_line - 1] + [replacement] + lines[end_line:])


def extract_dive_oracle_prefix(spec: str) -> str:
    if DIVE_GEN_INPUTS_MARKER not in spec:
        raise ValueError("DIVE spec 缺少 gen_inputs 分段，无法提取 baseline oracle")
    return spec.split(DIVE_GEN_INPUTS_MARKER, 1)[0].rstrip()


def dive_mutation_entrypoint(prefix: str) -> str:
    if re.search(r'if\s+__name__\s*==\s*[\'"]__main__[\'"]\s*:', prefix):
        return ""
    try:
        tree = ast.parse(prefix)
    except SyntaxError as exc:
        raise ValueError(f"DIVE oracle prefix 语法错误: {exc}") from exc
    names = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if "main" in names:
        return "\n\nif __name__ == '__main__':\n    main()\n"
    if "run_assertions" in names:
        return "\n\nif __name__ == '__main__':\n    run_assertions()\n"
    raise ValueError("DIVE oracle prefix 中未找到 main() 或 run_assertions()")


def build_dive_oracle_mutant_spec(spec: str, mutant: str) -> str:
    """Run baseline phase-2 assertions only; do not execute the DIVE search engine."""
    prefix = extract_dive_oracle_prefix(spec)
    fut = parse_dive_fut_name(spec)
    prefix = replace_function(prefix, dive_post_fn_name(fut), mutant)
    return prefix + dive_mutation_entrypoint(prefix)


def do_mutation(spec_path, result_dir, github_repo, pr_id, cloned_repo_manager, repo_name):
    os.makedirs(result_dir, exist_ok=True)
    with open(spec_path, "r") as f:
        original_spec = f.read()

    try:
        spec_format = detect_spec_format(original_spec)
    except ValueError as exc:
        print(f"Skipping PR {pr_id}: {exc}")
        return False

    github_pr = github_repo.get_pull(pr_id)
    pr = PullRequest(github_pr, github_repo, cloned_repo_manager)
    commit = pr.pre_commit
    cloned_repo = cloned_repo_manager.get_cloned_repo(commit)
    container_name = cloned_repo.container_name
    docker_executor = DockerExecutor(container_name)

    post_fut_code_without_prefix = extract_fut_code(pr.post_fut_info)
    pre_fut_code_without_prefix = extract_fut_code(pr.prev_fut_info)
    post_fut_code_without_prefix = beautify_code(post_fut_code_without_prefix)
    pre_fut_code_without_prefix = beautify_code(pre_fut_code_without_prefix)
    diff = difflib.unified_diff(
        pre_fut_code_without_prefix.splitlines(),
        post_fut_code_without_prefix.splitlines(),
        lineterm="",
    )
    added_lines = []
    for line in diff:
        if line.startswith("+") and not line.startswith("+++") and len(line.strip()) > 1:
            added_lines.append(line[1:].strip())

    post_fut_code = extract_fut_code(pr.post_fut_info, pre_fix="post_")
    pre_fut_code = extract_fut_code(pr.prev_fut_info, pre_fix="pre_")

    if spec_format == "legacy":
        spec = build_legacy_spec(original_spec, pre_fut_code, post_fut_code)
    elif spec_format == "dive":
        try:
            extract_dive_oracle_prefix(original_spec)
        except ValueError as exc:
            print(f"Skipping PR {pr_id}: {exc}")
            return False

    n_mutant_pass = 0
    n_mutant_fail_assert = 0
    n_mutant_fail_other = 0
    try:
        mutants = generate_mutants(post_fut_code)
    except Exception as e:
        print(f"Error generating mutants for PR {pr_id}: {e}")
        return False

    n_relevant = 0
    result_json = os.path.join(result_dir, "mutation_results.json")
    if os.path.exists(result_json):
        with open(result_json, "r") as f:
            existing_results = json.load(f)
        stale = (
            spec_format == "dive"
            and existing_results.get("analysis_version") != ANALYSIS_VERSION
        )
        if stale:
            print(
                f"PR {pr_id}: ignoring stale DIVE mutation results "
                f"(analysis_version={existing_results.get('analysis_version')!r})"
            )
            execution_results = {}
        else:
            execution_results = existing_results.get("execution_results", {})
            n_mutant_pass = existing_results.get("n_mutant_pass", 0)
            n_mutant_fail_assert = existing_results.get("n_mutant_fail_assert", 0)
            n_mutant_fail_other = existing_results.get("n_mutant_fail_other", 0)
    else:
        execution_results = {}

    for idx, mutant in enumerate(mutants):
        diff = difflib.unified_diff(
            post_fut_code.splitlines(),
            mutant.splitlines(),
            lineterm="",
        )
        removed_lines = []
        for line in diff:
            if line.startswith("-") and not line.startswith("---") and len(line.strip()) > 1:
                removed_lines.append(line[1:].strip())

        is_relevant = any(removed_line in added_lines for removed_line in removed_lines)
        if not is_relevant:
            continue
        n_relevant += 1
        hash_id = blake2b(mutant.encode()).hexdigest()
        if hash_id in execution_results:
            print(f"Skipping already tested mutant {idx + 1}/{len(mutants)} for PR {pr_id}")
            continue
        print(f"Testing mutant {idx + 1}/{len(mutants)} for PR {pr_id}")
        try:
            if spec_format == "legacy":
                mutated_spec = inject_legacy_mutant(spec, mutant)
            else:
                mutated_spec = build_dive_oracle_mutant_spec(original_spec, mutant)
        except ValueError as exc:
            print(f"Skipping mutant {idx + 1} for PR {pr_id}: {exc}")
            continue
        exit_code, output = docker_executor.execute_python_code(mutated_spec, timeout=300)
        print(output)
        print("-" * 40)
        mutant_file_name = f"mutant_{idx + 1}_fail.py"
        if exit_code == 0:
            mutant_file_name = f"mutant_{idx + 1}_pass.py"
            n_mutant_pass += 1
        else:
            if "AssertionError" in output:
                mutant_file_name = f"mutant_{idx + 1}_assert.py"
                n_mutant_fail_assert += 1
            else:
                n_mutant_fail_other += 1
        with open(os.path.join(result_dir, mutant_file_name), "w") as f:
            f.write(mutated_spec)
        execution_results[hash_id] = {
            "exit_code": exit_code,
            "output": output,
        }

    mutation_results = {
        "analysis_version": ANALYSIS_VERSION,
        "total_mutants": len(mutants),
        "relevant_mutants": n_relevant,
        "spec_format": spec_format,
        "mutation_oracle": "legacy_specification" if spec_format == "legacy" else "baseline_driver",
        "execution_results": execution_results,
        "n_mutant_pass": n_mutant_pass,
        "n_mutant_fail_assert": n_mutant_fail_assert,
        "n_mutant_fail_other": n_mutant_fail_other,
        "mutation_score": mutation_score_from_results(
            {
                "n_mutant_pass": n_mutant_pass,
                "n_mutant_fail_assert": n_mutant_fail_assert,
                "n_mutant_fail_other": n_mutant_fail_other,
            }
        ),
    }
    with open(result_json, "w") as f:
        json.dump(mutation_results, f, indent=4)
    return True


def iter_phase_jobs(cache_dir: Path, repo_name: str, phase: str):
    if phase == "phase1":
        spec_name = ("specification.py", "results.json", "")
    elif phase == "phase2":
        spec_name = ("phase2/specification.py", "phase2/results.json", "phase2")
    else:
        raise ValueError(f"Unsupported phase: {phase}")

    spec_rel, results_rel, phase_tag = spec_name
    for pr_dir in sorted(oracle_root(cache_dir, repo_name).iterdir()):
        if not pr_dir.is_dir():
            continue
        spec_path = pr_dir / spec_rel
        results_path = pr_dir / results_rel
        if not spec_path.is_file() or not is_oracle_completed(results_path):
            continue
        yield int(pr_dir.name), spec_path, mutation_result_dir(cache_dir, repo_name, int(pr_dir.name), phase_tag or "phase1")


def run_mutation_analysis(
    cache_dir: Path,
    repo_name: str,
    *,
    phase: str = "phase2",
    pr_file: Path | None = None,
    pr_ids: list[int] | None = None,
    limit: int | None = None,
):
    cache_dir = cache_dir.resolve()
    oracle_dir = oracle_root(cache_dir, repo_name)
    if not oracle_dir.is_dir():
        raise FileNotFoundError(f"Oracle 目录不存在: {oracle_dir}")

    if pr_ids is None:
        pr_file = resolve_pr_file(cache_dir, pr_file)
        pr_ids = load_pr_ids_for_project(repo_name, pr_file)
    allowed = set(pr_ids)

    github_repo, cloned_repo_manager = get_repo(repo_name)
    processed = 0
    for pr_id, spec_path, result_dir in iter_phase_jobs(cache_dir, repo_name, phase):
        if pr_id not in allowed:
            continue
        print(f"[{cache_dir.name}] {repo_name}#{pr_id} ({phase}) -> {result_dir}")
        do_mutation(spec_path, result_dir, github_repo, pr_id, cloned_repo_manager, repo_name)
        processed += 1
        if limit is not None and processed >= limit:
            break
    print(f"Done: processed {processed} PR(s) for {repo_name} in {cache_dir.name}")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Mutation analysis for inferred patch oracles (new200 cache layout). "
            "Requires PATCHGURU_CLONES_DIR and PATCHGURU_CONTAINER_TAG "
            "(use scripts/run_mutation.py to set pgabl defaults)."
        )
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        required=True,
        help="SpecInfer cache root, e.g. .cache_dive_new200 or .cache_baseline_new200",
    )
    parser.add_argument("--repo", type=str, required=True, help="Repository name (e.g., pandas)")
    parser.add_argument(
        "--phase",
        type=str,
        default="phase2",
        choices=["phase1", "phase2"],
        help="Which oracle phase to mutate (default: phase2)",
    )
    parser.add_argument(
        "--pr-file",
        type=str,
        default=None,
        help="PR list file (repo pr_id per line). Defaults to new200.txt for *new200* caches.",
    )
    parser.add_argument(
        "--pr-id",
        type=int,
        default=None,
        help="Run a single PR instead of the full list",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N PRs (for smoke tests)",
    )
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = ROOT / cache_dir

    pr_file = Path(args.pr_file) if args.pr_file else None
    pr_ids = [args.pr_id] if args.pr_id is not None else None

    run_mutation_analysis(
        cache_dir,
        args.repo,
        phase=args.phase,
        pr_file=pr_file,
        pr_ids=pr_ids,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
