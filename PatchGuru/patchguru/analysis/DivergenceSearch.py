"""DIVE orchestrator (host side) -- replaces baseline phase-2 generalization.

``dive_search`` mirrors the signature of ``SpecInfer.spec_generalization`` so it can
be dropped in behind the ``Config.PHASE2_STRATEGY == "dive"`` switch. Pipeline:

  (A) seed extraction      -> from phase-1 spec calls + PR test diff + docstrings
  (B) input constructor    -> type strategies + LLM gen_inputs()  (InputConstructor)
  (C)+(D) search + triage  -> single in-container harness execution (dive_harness)
  (E) intent classification-> reuse TestDriverReview.review_test_driver

Results are written to ``phase2/results.json`` in the SAME schema the baseline /
experiment scripts expect (stage / llm_queries / review_conclusion /
execution_status[-1].error_message), plus extra ``dive_*`` keys.
"""
from __future__ import annotations

import ast
import os
import re
import json
import time
from typing import Any

from patchguru import Config
from patchguru.utils.Logger import format_info_frame
from patchguru.utils.Tracker import Event, append_event
from patchguru.execution.DockerExecutor import DockerExecutor
from patchguru.analysis.TestDriverReview import review_test_driver
from patchguru.analysis.InputConstructor import (
    build_strategies_from_signature,
    generate_input_generator,
)
from patchguru.analysis import dive_harness


# ----------------------------------------------------------------------------
# Cache helpers (schema-compatible with SpecInfer.save_results_to_cache)
# ----------------------------------------------------------------------------
def _save(cache_dir: str, results: dict) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, "results.json"), "w") as f:
        json.dump(results, f, indent=4, default=str)
    with open(os.path.join(cache_dir, "specification.py"), "w") as f:
        f.write(results.get("specification", "# No specification generated."))


# ----------------------------------------------------------------------------
# (A) Seed extraction
# ----------------------------------------------------------------------------
def _extract_seeds_from_calls(source: str, fut_name: str) -> list[str]:
    """Find calls to pre_/post_/<fut_name> and turn their args into seed exprs."""
    seeds: list[str] = []
    targets = {fut_name, f"pre_{fut_name}", f"post_{fut_name}"}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return seeds

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = None
        if isinstance(node.func, ast.Name):
            fname = node.func.id
        elif isinstance(node.func, ast.Attribute):
            fname = node.func.attr
        if fname not in targets:
            continue
        try:
            pos = [ast.get_source_segment(source, a) for a in node.args
                   if not isinstance(a, ast.Starred)]
            kws = [(k.arg, ast.get_source_segment(source, k.value))
                   for k in node.keywords if k.arg is not None]
            if any(p is None for p in pos) or any(v is None for _, v in kws):
                continue
            args_inner = ", ".join(pos)
            if len(pos) == 1:
                args_inner += ","
            args_expr = "(" + args_inner + ")"
            kwargs_expr = "{" + ", ".join(f"{k!r}: {v}" for k, v in kws) + "}"
            seeds.append(f"({args_expr}, {kwargs_expr})")
        except Exception:  # noqa: BLE001 - best effort
            continue
    return seeds


def _extract_seeds_from_pr_tests(pr, fut_name: str) -> list[str]:
    """Scan added lines of test files in the PR diff for calls to the FUT."""
    seeds: list[str] = []
    try:
        patch = pr.patch
    except Exception:  # noqa: BLE001
        return seeds

    call_re = re.compile(r"\b" + re.escape(fut_name) + r"\s*\(")
    for patched_file in patch:
        path = getattr(patched_file, "path", "")
        if "test" not in path:
            continue
        for hunk in patched_file:
            for line in hunk:
                if not getattr(line, "is_added", False):
                    continue
                text = line.value if hasattr(line, "value") else str(line)
                if not call_re.search(text):
                    continue
                # capture a balanced-parens call argument list on this line
                m = call_re.search(text)
                start = m.end() - 1  # index of "("
                depth = 0
                arg_str = None
                for i in range(start, len(text)):
                    c = text[i]
                    if c == "(":
                        depth += 1
                    elif c == ")":
                        depth -= 1
                        if depth == 0:
                            arg_str = text[start + 1:i]
                            break
                if arg_str is None:
                    continue
                snippet = f"_f({arg_str})"
                try:
                    call = ast.parse(snippet, mode="eval").body
                    if not isinstance(call, ast.Call):
                        continue
                    pos = [ast.get_source_segment(snippet, a) for a in call.args
                           if not isinstance(a, ast.Starred)]
                    kws = [(k.arg, ast.get_source_segment(snippet, k.value))
                           for k in call.keywords if k.arg is not None]
                    if any(p is None for p in pos) or any(v is None for _, v in kws):
                        continue
                    inner = ", ".join(pos)
                    if len(pos) == 1:
                        inner += ","
                    args_expr = "(" + inner + ")"
                    kwargs_expr = "{" + ", ".join(f"{k!r}: {v}" for k, v in kws) + "}"
                    seeds.append(f"({args_expr}, {kwargs_expr})")
                except Exception:  # noqa: BLE001
                    continue
    return seeds


def _extract_seeds_from_docstrings(*code_blocks: str, fut_name: str) -> list[str]:
    """Extract doctest-style ``>>> fut(...)`` calls from code/docstrings."""
    seeds: list[str] = []
    dt_re = re.compile(r">>>\s*(.+)")
    for block in code_blocks:
        if not block:
            continue
        for m in dt_re.finditer(block):
            line = m.group(1)
            seeds.extend(_extract_seeds_from_calls_line(line, fut_name))
    return seeds


def _extract_seeds_from_calls_line(line: str, fut_name: str) -> list[str]:
    try:
        snippet = line.strip()
        node = ast.parse(snippet, mode="eval").body
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(node, ast.Call):
        return []
    fname = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
    if fname not in {fut_name, f"pre_{fut_name}", f"post_{fut_name}"}:
        return []
    try:
        pos = [ast.get_source_segment(snippet, a) for a in node.args
               if not isinstance(a, ast.Starred)]
        kws = [(k.arg, ast.get_source_segment(snippet, k.value))
               for k in node.keywords if k.arg is not None]
        if any(p is None for p in pos) or any(v is None for _, v in kws):
            return []
        inner = ", ".join(pos)
        if len(pos) == 1:
            inner += ","
        args_expr = "(" + inner + ")"
        kwargs_expr = "{" + ", ".join(f"{k!r}: {v}" for k, v in kws) + "}"
        return [f"({args_expr}, {kwargs_expr})"]
    except Exception:  # noqa: BLE001
        return []


def _infer_fut_name_from_spec(spec: str) -> str | None:
    """Infer the function-under-test name from a phase-1 spec.

    The spec always defines ``post_<name>`` / ``pre_<name>``. This is more robust
    than relying on the per-run PR retrieve (``prev_fut_names``), which can come
    back empty when GitHub data is re-fetched.
    """
    if not spec:
        return None
    # Prefer MODULE-LEVEL (column-0) definitions. Nested helpers like a mock
    # ``pre_loader`` defined *inside* a test driver must not be mistaken for the FUT
    # (e.g. keras specs whose real FUT is ``deserialize`` but which define local
    # ``pre_loader``/``post_loader`` mocks). Also accept assignment-style definitions
    # (``pre_<name> = types.FunctionType(...)``) used by some scipy specs.
    for pat in (r"^def\s+post_(\w+)\s*\(", r"^post_(\w+)\s*=",
                r"^def\s+pre_(\w+)\s*\(", r"^pre_(\w+)\s*="):
        m = re.findall(pat, spec, re.M)
        if m:
            return m[0]
    # Last resort: any definition, even nested.
    m = re.findall(r"def\s+post_(\w+)\s*\(", spec) or re.findall(r"def\s+pre_(\w+)\s*\(", spec)
    if m:
        return m[0]
    return None


def _strip_module_main(src: str) -> str:
    """Return the phase-1 specification with its module entry point removed.

    The phase-1 specification is a complete, container-validated program. We reuse
    it verbatim as the harness prefix (imports + pre_/post_ + helpers), but must
    drop its ``if __name__ == "__main__":`` block and any bare top-level ``main()``
    call so importing it does NOT execute phase-1's own assertions.

    NOTE: function definitions may live either under ``## Before/After Pull
    Request`` OR inside the ``# Specification`` section (when self-review rewrote
    the spec), so we must keep the WHOLE program, not just the prefix before
    ``# Specification``.
    """
    if not src:
        return ""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        idx = src.find("if __name__")
        return src[:idx] if idx != -1 else src

    new_body = []
    for node in tree.body:
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                    and test.left.id == "__name__"):
                continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Name) and func.id == "main":
                continue
        new_body.append(node)
    tree.body = new_body
    try:
        return ast.unparse(tree)
    except Exception:  # noqa: BLE001
        idx = src.find("if __name__")
        return src[:idx] if idx != -1 else src


class _AssertKiller(ast.NodeTransformer):
    """Replace every ``assert`` with ``pass`` so a driver runs through all inputs."""

    def visit_Assert(self, node):  # noqa: N802
        return ast.copy_location(ast.Pass(), node)


def _extract_baseline_driver(baseline_spec: str, fut_name: str) -> str | None:
    """Turn a baseline phase-2 spec into a re-runnable input-harvesting driver.

    The baseline driver exercises many concrete inputs but hides them inside
    helpers / loops (e.g. ``run_assertions(pre_fn, post_fn)``), so static
    extraction misses them. Instead we keep the driver verbatim EXCEPT:

      * drop its placeholder ``pre_<fut>`` / ``post_<fut>`` definitions (the
        harness injects monkeypatched real versions under those names),
      * drop the ``if __name__ == "__main__"`` block,
      * neutralize every ``assert`` -> ``pass`` so it does not stop at the first
        failing input.

    The harness then execs this, calling ``main()`` if present, with capturing
    wrappers bound to ``pre_<fut>`` / ``post_<fut>`` to record all inputs.
    """
    if not baseline_spec:
        return None
    try:
        tree = ast.parse(baseline_spec)
    except SyntaxError:
        return None

    targets = {f"pre_{fut_name}", f"post_{fut_name}"}
    new_body = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in targets:
            continue
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                    and test.left.id == "__name__"):
                continue
        new_body.append(node)
    tree.body = new_body
    tree = _AssertKiller().visit(tree)
    ast.fix_missing_locations(tree)
    try:
        code = ast.unparse(tree)
    except Exception:  # noqa: BLE001
        return None
    return code or None


def _dedupe(seq: list[str]) -> list[str]:
    seen = set()
    out = []
    for s in seq:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


# ----------------------------------------------------------------------------
# changed-line offsets
# ----------------------------------------------------------------------------
def _find_post_fn_def_node(spec: str, fut_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    post_name = f"post_{fut_name}"
    try:
        tree = ast.parse(spec)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == post_name:
            return node
    return None


def _resolve_post_fn_changed_lines(
    pr, fut_name: str, cloned_repo_manager=None,
) -> tuple[str | None, list[int], int | None]:
    """Return (repo_file_path, changed_line_numbers, post_fn_def_start_line)."""
    info = None
    for full_name, data in pr.post_fut_info.items():
        if full_name.split(".")[-1] == fut_name:
            info = data
            break
    if info is None and pr.post_fut_info:
        info = next(iter(pr.post_fut_info.values()))

    if info is not None:
        start_line = info.get("start_line")
        end_line = info.get("end_line")
        file_path = info.get("file_path")
        if start_line is not None and end_line is not None and file_path:
            modified = pr.new_file_path_to_modified_lines.get(file_path, set())
            changed_in_fn = sorted(ln for ln in modified if start_line <= ln <= end_line)
            if changed_in_fn:
                return file_path, changed_in_fn, int(start_line)

    if cloned_repo_manager is None or not fut_name:
        return None, [], None
    try:
        cloned_repo = cloned_repo_manager.get_cloned_repo(pr.post_commit)
        working_dir = cloned_repo.repo.working_dir
    except Exception:  # noqa: BLE001
        return None, [], None

    for path, lines in pr.new_file_path_to_modified_lines.items():
        if not lines or "test" in path or not path.endswith(".py"):
            continue
        try:
            with open(os.path.join(working_dir, path)) as f:
                tree = ast.parse(f.read())
        except Exception:  # noqa: BLE001
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != fut_name:
                continue
            start = node.lineno
            end = getattr(node, "end_lineno", None) or start
            changed_in_fn = sorted(ln for ln in lines if start <= ln <= end)
            if changed_in_fn:
                return path, changed_in_fn, start
    return None, [], None


def _get_changed_line_texts(pr, fut_name: str, cloned_repo_manager=None) -> list[tuple[int, str]]:
    file_path, changed, _ = _resolve_post_fn_changed_lines(pr, fut_name, cloned_repo_manager)
    if not file_path or not changed or cloned_repo_manager is None:
        return []
    try:
        cloned_repo = cloned_repo_manager.get_cloned_repo(pr.post_commit)
        with open(os.path.join(cloned_repo.repo.working_dir, file_path)) as f:
            file_lines = f.readlines()
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[int, str]] = []
    for ln in changed:
        if 1 <= ln <= len(file_lines):
            out.append((ln, file_lines[ln - 1].rstrip("\n")))
    return out


def _map_changed_lines_to_embedded_offsets(
    spec_prefix: str,
    def_lineno: int,
    def_end_lineno: int,
    changed_texts: list[tuple[int, str]],
) -> list[int] | None:
    if not changed_texts:
        return None
    spec_lines = spec_prefix.splitlines()
    matched: list[int] = []
    search_from = def_lineno
    for _repo_ln, raw in sorted(changed_texts, key=lambda item: item[0]):
        norm = raw.strip()
        if not norm:
            return None
        found = None
        for ln in range(search_from, def_end_lineno + 1):
            if ln <= len(spec_lines) and spec_lines[ln - 1].strip() == norm:
                found = ln
                break
        if found is None:
            return None
        matched.append(found - def_lineno)
        search_from = found + 1
    return sorted(set(matched))


def compute_embedded_post_offsets(
    spec_prefix: str,
    fut_name: str,
    pr,
    cloned_repo_manager=None,
) -> tuple[list[int], int]:
    """Map repo diff lines to offsets inside the embedded ``post_<fn>`` in *spec_prefix*."""
    repo_offsets, total = _compute_changed_offsets(pr, fut_name, cloned_repo_manager)
    if not repo_offsets or not spec_prefix.strip():
        return repo_offsets, total

    post_node = _find_post_fn_def_node(spec_prefix, fut_name)
    if post_node is None:
        return repo_offsets, total

    changed_texts = _get_changed_line_texts(pr, fut_name, cloned_repo_manager)
    if not changed_texts:
        return repo_offsets, total

    embedded_end = post_node.end_lineno or post_node.lineno
    mapped = _map_changed_lines_to_embedded_offsets(
        spec_prefix, post_node.lineno, embedded_end, changed_texts
    )
    if mapped is not None and len(mapped) == total:
        return mapped, total
    if mapped:
        return mapped, len(mapped)
    return repo_offsets, total


def _compute_changed_offsets(pr, fut_name: str, cloned_repo_manager=None) -> tuple[list[int], int]:
    """Return (changed_offsets, total_changed_in_fn) relative to the post fn def line."""
    _path, changed, start = _resolve_post_fn_changed_lines(pr, fut_name, cloned_repo_manager)
    if changed and start is not None:
        return [ln - start for ln in changed], len(changed)
    return [], 0


# ----------------------------------------------------------------------------
# (E) build focused test driver + evidence for classification
# ----------------------------------------------------------------------------
def _build_focused_driver(cluster: dict, fut_name: str, available_import: str,
                          prev_fut_code: str, post_fut_code: str) -> str:
    args_expr = cluster["args_expr"]
    kwargs_expr = cluster["kwargs_expr"]
    driver = f"""
# Neccessary Imports
{available_import}
try:
    import numpy as np
except Exception:
    np = None
import math

# Source Code of target function(s)

## Before Pull Request
{prev_fut_code}

## After Pull Request
{post_fut_code}

# Specification
def _dive_equal(a, b):
    try:
        if np is not None and (isinstance(a, np.ndarray) or isinstance(b, np.ndarray)):
            try:
                return bool(np.array_equal(a, b, equal_nan=True))
            except Exception:
                return bool(np.array_equal(a, b))
        if isinstance(a, float) and isinstance(b, float):
            if math.isnan(a) and math.isnan(b):
                return True
        res = (a == b)
        return bool(res)
    except Exception:
        return repr(a) == repr(b)

def main():
    # Input discovered by DIVE divergence-directed search (minimized).
    args = {args_expr}
    kwargs = {kwargs_expr}
    pre_exc = None
    post_exc = None
    old = None
    new = None
    try:
        old = pre_{fut_name}(*args, **kwargs)
    except Exception as e:
        pre_exc = e
    try:
        new = post_{fut_name}(*args, **kwargs)
    except Exception as e:
        post_exc = e
    # The pre-PR and post-PR versions are expected to agree for this input.
    assert (pre_exc is None) == (post_exc is None), \\
        "[DIVE DIVERGENCE] one version raised while the other did not"
    if pre_exc is not None and post_exc is not None:
        assert type(pre_exc) is type(post_exc), \\
            "[DIVE DIVERGENCE] pre/post raised different exception types"
    else:
        assert _dive_equal(old, new), \\
            "[DIVE DIVERGENCE] pre/post returned different values"

if __name__ == "__main__":
    main()
"""
    return driver


def _build_evidence(cluster: dict, fut_name: str) -> str:
    def fmt(summary: dict) -> str:
        if summary.get("kind") == "exception":
            return f"raised {summary.get('type')}: {summary.get('msg')}"
        return f"returned ({summary.get('type')}) {summary.get('repr')}"

    lines = [
        "AssertionError: [DIVE DIVERGENCE] divergence-directed search found an input "
        "where pre-PR and post-PR behavior differ.",
        f"Input: args={cluster['args_expr']}, kwargs={cluster['kwargs_expr']}",
        f"Divergence category: {cluster['category']}",
        f"Pre-PR  pre_{fut_name}: {fmt(cluster['pre'])}",
        f"Post-PR post_{fut_name}: {fmt(cluster['post'])}",
        f"Changed lines hit by this input: {cluster['hit_changed_lines']}",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# main entry
# ----------------------------------------------------------------------------
def dive_search(
    pr_nb: int,
    force: bool = False,
    cache_dir: str = None,
    original_specification: str = None,
    pull_request_details: str = None,
    prev_fut_code: str = None,
    post_fut_code: str = None,
    prev_fut_names: str = None,
    post_fut_signatures: str = None,
    enclosing_class: str = None,
    pr=None,
    cloned_repo_manager=None,
    fut_name: str = None,
    code_changes: str = None,
) -> dict:
    t_start = time.time()
    states: dict[str, Any] = {
        "stage": "init",
        "llm_queries": 0,
        "phase2_strategy": "dive",
    }
    ab_cfg = Config.dive_ablation_config()
    states["dive_ablation"] = ab_cfg["ablation"]

    # cache / resume
    if cache_dir and os.path.exists(os.path.join(cache_dir, "results.json")) and not force:
        try:
            with open(os.path.join(cache_dir, "results.json")) as f:
                cached = json.load(f)
            if cached.get("stage") in ("completed", "failed"):
                append_event(Event(
                    level="INFO", pr_nb=pr_nb,
                    message="DIVE phase-2 results already cached. Loading from cache.",
                    type="CacheLoad", info={"analysis_results": cached}
                ))
                return cached
        except Exception:  # noqa: BLE001
            pass

    fut_name = fut_name or (prev_fut_names.split(",")[0].split(".")[-1] if prev_fut_names else "")
    # Robust fallback: the per-run PR retrieve can return an empty prev_fut_names,
    # which would break function resolution in the harness. The phase-1 spec always
    # defines pre_/post_<name>, so infer the real name from it when needed.
    if not fut_name or (
        f"def pre_{fut_name}" not in (original_specification or "")
        and f"def post_{fut_name}" not in (original_specification or "")
    ):
        inferred = _infer_fut_name_from_spec(original_specification or "")
        if inferred:
            if inferred != fut_name:
                append_event(Event(
                    level="INFO", pr_nb=pr_nb,
                    message=f"[DIVE] fut_name resolved from phase-1 spec: '{fut_name}' -> '{inferred}'."
                ))
            fut_name = inferred
    append_event(Event(
        level="INFO", pr_nb=pr_nb,
        message=f"[DIVE] Starting divergence-directed search for function '{fut_name}'.",
        type="GeneralInfo"
    ))

    # ---- (A) seeds ----
    seeds: list[str] = []
    seeds += _extract_seeds_from_calls(original_specification or "", fut_name)
    seeds += _extract_seeds_from_pr_tests(pr, fut_name) if pr is not None else []
    seeds += _extract_seeds_from_docstrings(prev_fut_code or "", post_fut_code or "", fut_name=fut_name)
    seeds = _dedupe(seeds)
    append_event(Event(
        level="INFO", pr_nb=pr_nb,
        message=f"[DIVE] Seed extraction collected {len(seeds)} candidate seed expressions.",
        type="GeneralInfo", info={"num_seeds": len(seeds), "seeds": seeds[:30]}
    ))

    # ---- (B) input constructor ----
    strat_info = build_strategies_from_signature(pr.post_fut_info if pr is not None else {}, fut_name)
    strategy_exprs = [s for (_, _, _, s) in strat_info.get("param_strategies", [])]
    gen_inputs_code = None
    ic_queries = 0

    if ab_cfg["use_constructor"]:
        try:
            gen_inputs_code, ic_queries = generate_input_generator(
                post_fut_signatures=post_fut_signatures or "",
                pull_request_details=pull_request_details or "",
                prev_fut_code=prev_fut_code or "",
                prev_fut_names=prev_fut_names or fut_name,
                enclosing_class=enclosing_class or "",
                available_import=pr.import_string if pr is not None else "",
            )
        except Exception as e:  # noqa: BLE001 - never let (B) crash the pipeline
            append_event(Event(
                level="WARNING", pr_nb=pr_nb,
                message=f"[DIVE] Input constructor failed ({e!r}); continuing with seeds + strategies only."
            ))
            gen_inputs_code, ic_queries = None, 0
    else:
        strategy_exprs = []
        append_event(Event(
            level="INFO", pr_nb=pr_nb,
            message="[DIVE] Ablation no_constructor: skipping Hypothesis strategies and LLM gen_inputs().",
            type="GeneralInfo",
        ))
    states["llm_queries"] += ic_queries
    states["input_constructor_queries"] = ic_queries

    # ---- seed fusion: harvest baseline phase-2 inputs (optional) ----
    baseline_driver_code = None
    bdir = getattr(Config, "DIVE_SEED_BASELINE_DIR", "")
    if bdir and cache_dir:
        try:
            norm = os.path.normpath(cache_dir).split(os.sep)
            if len(norm) >= 3 and norm[-1] == "phase2":
                project, prnum = norm[-3], norm[-2]
                bspec_path = os.path.join(bdir, "oracles", project, prnum, "phase2", "specification.py")
                if os.path.exists(bspec_path):
                    with open(bspec_path) as bf:
                        baseline_spec = bf.read()
                    baseline_driver_code = _extract_baseline_driver(baseline_spec, fut_name)
                    append_event(Event(
                        level="INFO", pr_nb=pr_nb,
                        message=f"[DIVE] Seed fusion: loaded baseline phase-2 driver from {bspec_path} "
                                f"({'ok' if baseline_driver_code else 'empty after transform'}).",
                        type="GeneralInfo"
                    ))
                else:
                    append_event(Event(
                        level="INFO", pr_nb=pr_nb,
                        message=f"[DIVE] Seed fusion enabled but no baseline spec at {bspec_path}.",
                        type="GeneralInfo"
                    ))
        except Exception as e:  # noqa: BLE001 - never let seed fusion break the run
            append_event(Event(
                level="WARNING", pr_nb=pr_nb,
                message=f"[DIVE] Seed fusion failed to load baseline driver ({e!r}); continuing without it."
            ))
            baseline_driver_code = None

    # ---- assemble harness ----
    available_import = pr.import_string if pr is not None else ""
    # Reuse the FULL validated phase-1 program (repaired imports + pre/post code +
    # helpers) so the functions import & run in the container exactly as in phase 1.
    # We only strip its __main__ entry point so phase-1 assertions don't execute.
    # (Function defs may live under "## After Pull Request" OR inside the
    # "# Specification" section, so we must NOT truncate at "# Specification".)
    spec_prefix = _strip_module_main(original_specification or "")

    # ---- changed line offsets (embedded post_<fn>, not repo file) ----
    if pr is not None:
        changed_offsets, total_changed = compute_embedded_post_offsets(
            spec_prefix, fut_name, pr, cloned_repo_manager
        )
    else:
        changed_offsets, total_changed = [], 0
    append_event(Event(
        level="INFO", pr_nb=pr_nb,
        message=f"[DIVE] Target changed lines in function: {total_changed} "
                f"(embedded offsets={changed_offsets}).",
        type="GeneralInfo"
    ))

    harness = dive_harness.build_harness(
        fut_name=fut_name,
        spec_prefix=spec_prefix,
        changed_offsets=changed_offsets,
        seed_exprs=seeds,
        strategy_exprs=strategy_exprs,
        gen_inputs_code=gen_inputs_code,
        baseline_driver_code=baseline_driver_code,
        exec_budget=Config.DIVE_EXEC_BUDGET,
        time_budget=Config.DIVE_TIME_BUDGET_SEC,
        deflake_k=Config.DIVE_DEFLAKE_K,
        max_clusters=Config.DIVE_MAX_CLUSTERS,
        harvest_time=getattr(Config, "DIVE_HARVEST_TIME_SEC", 45),
        install_deps=Config.DIVE_INSTALL_DEPS,
        use_constructor=ab_cfg["use_constructor"],
        use_guided_search=ab_cfg["use_guided_search"],
        use_triage=ab_cfg["use_triage"],
        ablation=ab_cfg["ablation"],
    )
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, "dive_harness.py"), "w") as f:
            f.write(harness)

    # ---- run harness in the container ----
    cloned_repo = cloned_repo_manager.get_cloned_repo(pr.pre_commit)
    executor = DockerExecutor(container_name=cloned_repo.container_name)
    append_event(Event(
        level="INFO", pr_nb=pr_nb,
        message="[DIVE] Executing search harness inside container...",
        type="ExecutionStart"
    ))
    exit_code, output = executor.execute_python_code(harness, timeout=Config.DIVE_TIME_BUDGET_SEC + 300)

    dive_result = _parse_harness_output(output)
    if dive_result is None:
        append_event(Event(
            level="WARNING", pr_nb=pr_nb,
            message="[DIVE] Could not parse harness result; treating as NORMAL (no divergence reported).",
            type="GeneralInfo",
            info={"exit_code": exit_code, "output_tail": output[-2000:]}
        ))
        states.update({
            "stage": "completed",
            "review_conclusion": "NORMAL",
            "specification": harness,
            "execution_status": [{"exit_code": 0, "error_message": ""}],
            "dive_stats": {"parse_failed": True},
            "elapsed_sec": round(time.time() - t_start, 2),
        })
        if cache_dir:
            _save(cache_dir, states)
        return states

    clusters = dive_result.get("clusters", [])
    dive_stats = dive_result.get("stats", {})
    # Surface engine-side errors (e.g. functions_unresolved when the phase-1 spec has
    # no module-level pre_/post_ FUT) so an otherwise-empty dive_stats is diagnosable.
    if dive_result.get("error") and isinstance(dive_stats, dict):
        dive_stats = dict(dive_stats)
        dive_stats["error"] = dive_result["error"]
    append_event(Event(
        level="INFO", pr_nb=pr_nb,
        message=[
            f"[DIVE] Harness reported {len(clusters)} divergence cluster(s).",
            format_info_frame(json.dumps(dive_stats, indent=2), "DIVE STATS")
        ],
        type="GeneralInfo",
        info={"dive_stats": dive_stats, "num_clusters": len(clusters)}
    ))

    states["dive_stats"] = dive_stats
    states["dive_clusters"] = clusters

    if not clusters:
        states.update({
            "stage": "completed",
            "review_conclusion": "NORMAL",
            "specification": harness,
            "execution_status": [{"exit_code": 0, "error_message": ""}],
            "elapsed_sec": round(time.time() - t_start, 2),
        })
        append_event(Event(
            level="INFO", pr_nb=pr_nb,
            message="[DIVE] No stable divergences found. Conclusion: NORMAL.",
            type="AnalysisComplete"
        ))
        if cache_dir:
            _save(cache_dir, states)
        return states

    # ---- (E) classify each cluster representative ----
    review_traces = []
    bug_cluster = None
    bug_evidence = None
    bug_driver = None
    bug_reasoning = None

    if not ab_cfg.get("use_review", True):
        cluster = clusters[0]
        bug_cluster = cluster
        bug_driver = _build_focused_driver(cluster, fut_name, available_import, prev_fut_code, post_fut_code)
        bug_evidence = _build_evidence(cluster, fut_name)
        bug_reasoning = (
            "search-only baseline: stable pre/post divergence reported without intent classifier "
            f"(category={cluster.get('category')}, {len(clusters)} cluster(s))."
        )
        review_traces.append({
            "cluster_index": 0,
            "category": cluster.get("category"),
            "conclusion": "DIVERGENCE",
            "search_only": True,
            "num_clusters": len(clusters),
        })
        states["review_traces"] = review_traces
        states["search_only"] = True
        states.update({
            "stage": "completed",
            "review_conclusion": "BUG",
            "review_reasoning": bug_reasoning,
            "specification": bug_driver,
            "execution_status": [{"exit_code": 1, "error_message": bug_evidence}],
            "elapsed_sec": round(time.time() - t_start, 2),
        })
        append_event(Event(
            level="WARNING", pr_nb=pr_nb,
            message=[
                "[DIVE search-only] Divergence found; skipping TestDriverReview.",
                format_info_frame(bug_driver, "DIVERGENCE-TRIGGERING SPECIFICATION"),
                format_info_frame(bug_evidence, "EXECUTION MESSAGE"),
            ],
            type="SpecReviewResult",
            info={"conclusion": "BUG", "search_only": True, "num_clusters": len(clusters)},
        ))
        if cache_dir:
            _save(cache_dir, states)
        return states

    for idx, cluster in enumerate(clusters):
        driver = _build_focused_driver(cluster, fut_name, available_import, prev_fut_code, post_fut_code)
        evidence = _build_evidence(cluster, fut_name)
        append_event(Event(
            level="INFO", pr_nb=pr_nb,
            message=[
                f"[DIVE] Classifying divergence cluster {idx + 1}/{len(clusters)} "
                f"(category={cluster['category']})...",
                format_info_frame(evidence, "DIVERGENCE EVIDENCE")
            ],
            type="GeneralInfo"
        ))

        verdicts = []
        for _vote in range(max(1, Config.DIVE_NUM_REVIEW)):
            review_results = review_test_driver(
                pull_request_details=pull_request_details,
                prev_fut_code=prev_fut_code,
                prev_fut_names=prev_fut_names,
                post_fut_signatures=post_fut_signatures,
                post_fut_code=post_fut_code,
                available_import=available_import,
                enclosing_class=enclosing_class,
                test_driver=driver,
                error_message=evidence,
                code_changes=code_changes,
            )
            if review_results is None:
                continue
            states["llm_queries"] += review_results.get("review_queries", 0)
            verdicts.append(review_results)

        if not verdicts:
            append_event(Event(
                level="WARNING", pr_nb=pr_nb,
                message=f"[DIVE] Classifier returned no valid verdict for cluster {idx + 1}. Skipping."
            ))
            continue

        bug_votes = sum(1 for v in verdicts if v["conclusion"] == "BUG")
        conclusion = "BUG" if bug_votes * 2 > len(verdicts) else "MISMATCH"
        chosen = next((v for v in verdicts if v["conclusion"] == conclusion), verdicts[0])

        review_traces.append({
            "cluster_index": idx,
            "category": cluster["category"],
            "conclusion": conclusion,
            "bug_votes": bug_votes,
            "total_votes": len(verdicts),
            "reasoning": chosen.get("reasoning", ""),
        })
        append_event(Event(
            level="INFO", pr_nb=pr_nb,
            message=f"[DIVE] Cluster {idx + 1} verdict: {conclusion} "
                    f"({bug_votes}/{len(verdicts)} BUG votes).",
            type="SpecReviewResult",
            info={"conclusion": conclusion, "category": cluster["category"]}
        ))

        if conclusion == "BUG":
            bug_cluster = cluster
            bug_evidence = evidence
            bug_driver = driver
            bug_reasoning = chosen.get("reasoning", "")
            break  # report the first confirmed bug

    states["review_traces"] = review_traces

    if bug_cluster is not None:
        states.update({
            "stage": "completed",
            "review_conclusion": "BUG",
            "review_reasoning": bug_reasoning or "",
            "specification": bug_driver,
            "execution_status": [{"exit_code": 1, "error_message": bug_evidence}],
            "elapsed_sec": round(time.time() - t_start, 2),
        })
        append_event(Event(
            level="WARNING", pr_nb=pr_nb,
            message=[
                "[DIVE] Potential bug confirmed by intent classifier.",
                format_info_frame(bug_driver, "BUG-TRIGGERING SPECIFICATION"),
                format_info_frame(bug_evidence, "EXECUTION MESSAGE"),
            ],
            type="SpecReviewResult",
            info={
                "conclusion": "BUG",
                "bug_triggering_specification": bug_driver,
                "execution_message": bug_evidence,
            }
        ))
    else:
        states.update({
            "stage": "completed",
            "review_conclusion": "NORMAL",
            "specification": bug_driver if bug_driver else harness,
            "execution_status": [{"exit_code": 0, "error_message": ""}],
            "elapsed_sec": round(time.time() - t_start, 2),
        })
        append_event(Event(
            level="INFO", pr_nb=pr_nb,
            message="[DIVE] All divergences classified as intended. Conclusion: NORMAL.",
            type="AnalysisComplete"
        ))

    if cache_dir:
        _save(cache_dir, states)
    return states


DIVE_GEN_INPUTS_MARKER = "# ===================== LLM gen_inputs() ====================="
DIVE_CONFIG_MARKER = "# ===================== DIVE injected config ====================="
COVERAGE_ANALYSIS_VERSION = 2

_LEGACY_HEADER_RE = re.compile(r"###\s+([\w\.]+)#(\d+)-(\d+)")
_POST_DEF_RE = re.compile(r"^def (post_\w+)\(", re.M)


def _default_dive_cfg() -> dict[str, Any]:
    return {
        "exec_budget": Config.DIVE_EXEC_BUDGET,
        "time_budget": Config.DIVE_TIME_BUDGET_SEC,
        "deflake_k": Config.DIVE_DEFLAKE_K,
        "max_clusters": Config.DIVE_MAX_CLUSTERS,
        "harvest_time": getattr(Config, "DIVE_HARVEST_TIME_SEC", 45),
        "use_constructor": True,
        "use_guided_search": True,
        "use_triage": True,
    }


def _parse_legacy_fut_name(phase2_spec: str, phase1_spec: str | None = None) -> str | None:
    """Infer target function short name from legacy minimized phase-2 specs."""
    idx = phase2_spec.rfind("## After Pull Request")
    section = phase2_spec[idx:] if idx >= 0 else phase2_spec

    post_def = _POST_DEF_RE.search(section)
    if post_def:
        fn = post_def.group(1)
        if fn.startswith("post_"):
            return fn[len("post_") :]

    header = _LEGACY_HEADER_RE.search(section)
    if header:
        return header.group(1).rsplit(".", 1)[-1]

    if phase1_spec:
        m = re.search(r"def run_assertions\(\s*(pre_\w+)", phase1_spec)
        if m and m.group(1).startswith("pre_"):
            return m.group(1)[len("pre_") :]
    return None


def _extract_run_assertions_driver(phase1_spec: str) -> str | None:
    """Build a baseline-driver snippet from phase-1 ``run_assertions``."""
    try:
        tree = ast.parse(phase1_spec)
    except SyntaxError:
        return None

    lines = phase1_spec.splitlines()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "run_assertions":
            continue
        if len(node.args.args) < 2:
            continue
        pre_name = node.args.args[0].arg
        post_name = node.args.args[1].arg
        end = node.end_lineno or node.lineno
        block = "\n".join(lines[node.lineno - 1 : end])
        return block + f"\n\ndef main():\n    run_assertions({pre_name}, {post_name})\n"
    return None


def _extract_legacy_main_seed_exprs(phase2_spec: str) -> list[str]:
    """Use minimized ``main()`` args/kwargs as a single search seed when present."""
    try:
        tree = ast.parse(phase2_spec)
    except SyntaxError:
        return []

    args_expr: str | None = None
    kwargs_expr: str | None = None
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "main":
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
                continue
            target = stmt.targets[0]
            if not isinstance(target, ast.Name):
                continue
            try:
                rendered = ast.unparse(stmt.value)
            except Exception:  # noqa: BLE001
                continue
            if target.id == "args":
                args_expr = rendered
            elif target.id == "kwargs":
                kwargs_expr = rendered

    if args_expr is None:
        return []
    kw = kwargs_expr or "{}"
    return [f"({args_expr}, {kw})"]


def _apply_legacy_harness_fallback(result: dict[str, Any], phase2_spec: str, phase1_spec: str | None) -> dict[str, Any]:
    if result.get("fut_name"):
        return result

    fut_name = _parse_legacy_fut_name(phase2_spec, phase1_spec)
    if not fut_name:
        return result

    baseline_driver = None
    if phase1_spec:
        baseline_driver = _extract_run_assertions_driver(phase1_spec)

    seed_exprs = _extract_legacy_main_seed_exprs(phase2_spec)
    result.update(
        {
            "fut_name": fut_name,
            "cfg": result.get("cfg") or _default_dive_cfg(),
            "seed_exprs": seed_exprs,
            "strategy_exprs": [],
            "baseline_driver_code": baseline_driver,
            "legacy_fallback_used": True,
        }
    )
    return result


def _literal_config_line(prefix: str, text: str):
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return ast.literal_eval(stripped.split("=", 1)[1].strip())
    return None


def parse_saved_phase2_harness(phase2_spec: str, phase1_spec: str | None = None) -> dict:
    """Extract reproducibility knobs from a saved phase-2 DIVE harness.

    Modern specs embed ``__DIVE_*`` constants. Legacy minimized specs only retain
    ``### module.fn#start-end`` headers plus ``pre_/post_`` bodies and a ``main()``
    repro; when ``phase1_spec`` is supplied we reconstruct a best-effort harness config.
    """
    fut_name = _literal_config_line("__DIVE_FUT_NAME__", phase2_spec)
    cfg = _literal_config_line("__DIVE_CFG__", phase2_spec) or {}
    seed_exprs = _literal_config_line("__DIVE_SEED_EXPRS__", phase2_spec) or []
    strategy_exprs = _literal_config_line("__DIVE_STRATEGY_EXPRS__", phase2_spec) or []
    baseline_driver = _literal_config_line("__DIVE_BASELINE_DRIVER__", phase2_spec)

    gen_inputs_code = None
    if DIVE_GEN_INPUTS_MARKER in phase2_spec:
        tail = phase2_spec.split(DIVE_GEN_INPUTS_MARKER, 1)[1]
        if DIVE_CONFIG_MARKER in tail:
            gen_inputs_code = tail.split(DIVE_CONFIG_MARKER, 1)[0].strip() or None

    result = {
        "fut_name": fut_name,
        "cfg": cfg,
        "seed_exprs": seed_exprs,
        "strategy_exprs": strategy_exprs,
        "baseline_driver_code": baseline_driver if baseline_driver else None,
        "gen_inputs_code": gen_inputs_code,
        "legacy_fallback_used": False,
    }
    return _apply_legacy_harness_fallback(result, phase2_spec, phase1_spec)


def build_coverage_rerun_harness(
    *,
    fut_name: str,
    spec_prefix: str,
    pr,
    cloned_repo_manager,
    seed_exprs: list[str],
    strategy_exprs: list[str],
    gen_inputs_code: str | None,
    baseline_driver_code: str | None,
    cfg: dict | None = None,
) -> tuple[str, list[int], int]:
    cfg = dict(cfg or {})
    changed_offsets, total_changed = compute_embedded_post_offsets(
        spec_prefix, fut_name, pr, cloned_repo_manager
    )
    harness = dive_harness.build_harness(
        fut_name=fut_name,
        spec_prefix=spec_prefix,
        changed_offsets=changed_offsets,
        seed_exprs=seed_exprs,
        strategy_exprs=strategy_exprs,
        gen_inputs_code=gen_inputs_code,
        baseline_driver_code=baseline_driver_code,
        exec_budget=int(cfg.get("exec_budget", Config.DIVE_EXEC_BUDGET)),
        time_budget=float(cfg.get("time_budget", Config.DIVE_TIME_BUDGET_SEC)),
        deflake_k=int(cfg.get("deflake_k", Config.DIVE_DEFLAKE_K)),
        max_clusters=int(cfg.get("max_clusters", Config.DIVE_MAX_CLUSTERS)),
        harvest_time=float(cfg.get("harvest_time", getattr(Config, "DIVE_HARVEST_TIME_SEC", 45))),
        install_deps=Config.DIVE_INSTALL_DEPS,
        use_constructor=bool(cfg.get("use_constructor", True)),
        use_guided_search=bool(cfg.get("use_guided_search", True)),
        use_triage=bool(cfg.get("use_triage", True)),
        ablation=cfg.get("ablation"),
    )
    return harness, changed_offsets, total_changed


def execute_coverage_rerun_harness(
    harness: str,
    *,
    pr,
    cloned_repo_manager,
    timeout: int | None = None,
) -> tuple[int, dict | None, str]:
    cloned_repo = cloned_repo_manager.get_cloned_repo(pr.pre_commit)
    executor = DockerExecutor(container_name=cloned_repo.container_name)
    if timeout is None:
        timeout = Config.DIVE_TIME_BUDGET_SEC + 300
    exit_code, output = executor.execute_python_code(harness, timeout=timeout)
    return exit_code, _parse_harness_output(output), output


def _parse_harness_output(output: str) -> dict | None:
    begin = "===DIVE_RESULT_BEGIN==="
    end = "===DIVE_RESULT_END==="
    if begin not in output or end not in output:
        return None
    try:
        payload = output.split(begin, 1)[1].split(end, 1)[0].strip()
        return json.loads(payload)
    except Exception:  # noqa: BLE001
        return None
