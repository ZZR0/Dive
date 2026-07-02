"""DIVE (B) Structured Input Constructor.

Two complementary sources of structured inputs for the divergence search:

1. ``build_strategies_from_signature``: derive lightweight, type-driven Hypothesis
   strategy code from the post-PR function signature (best-effort, falls back to a
   generic strategy when type hints are missing).
2. ``generate_input_generator``: ask the LLM (once) to write a reusable
   ``gen_inputs()`` generator that yields valid, structured ``(args, kwargs)``.

The search engine (harness) consumes both: it samples Hypothesis strategies and
``gen_inputs()`` seeds, then mutates them. This keeps LLM cost at a single call
while the cheap concrete execution does the heavy lifting.
"""
from __future__ import annotations

from typing import Any

from patchguru import Config
from patchguru.llms.OpenAI import query_llm
from patchguru.utils.Logger import format_info_frame
from patchguru.utils.PythonCodeUtil import extract_function_info, get_parameter_types
from patchguru.utils.Tracker import Event, append_event


# Mapping from (normalized) type annotation -> Hypothesis strategy expression.
# Kept intentionally small + robust; unknown types fall back to a generic strategy.
def _strategy_for_type(type_str: str | None) -> str:
    if not type_str:
        return "st.one_of(st.integers(), st.floats(allow_nan=True, allow_infinity=True), st.text(), st.booleans(), st.none())"

    t = type_str.strip().lower().replace(" ", "")
    # strip Optional[...] / typing prefixes
    t = t.replace("typing.", "")
    optional = False
    if t.startswith("optional[") and t.endswith("]"):
        optional = True
        t = t[len("optional["):-1]

    base_map = {
        "int": "st.integers()",
        "float": "st.floats(allow_nan=True, allow_infinity=True)",
        "complex": "st.complex_numbers(allow_nan=True, allow_infinity=True)",
        "str": "st.text()",
        "bytes": "st.binary()",
        "bool": "st.booleans()",
        "none": "st.none()",
        "list": "st.lists(st.integers())",
        "list[int]": "st.lists(st.integers())",
        "list[str]": "st.lists(st.text())",
        "list[float]": "st.lists(st.floats(allow_nan=True, allow_infinity=True))",
        "dict": "st.dictionaries(st.text(), st.integers())",
        "dict[str,int]": "st.dictionaries(st.text(), st.integers())",
        "dict[str,str]": "st.dictionaries(st.text(), st.text())",
        "dict[str,float]": "st.dictionaries(st.text(), st.floats(allow_nan=True))",
        "tuple": "st.tuples(st.integers(), st.integers())",
        "set": "st.sets(st.integers())",
    }

    strat = base_map.get(t)
    if strat is None:
        if t.startswith("list["):
            strat = "st.lists(st.one_of(st.integers(), st.floats(allow_nan=True), st.text()))"
        elif t.startswith("dict["):
            strat = "st.dictionaries(st.text(), st.one_of(st.integers(), st.text()))"
        elif "ndarray" in t or t.startswith("np.") or "array" in t:
            # numpy arrays: build from lists of floats (harness converts via np.asarray)
            strat = "st.lists(st.floats(allow_nan=True, allow_infinity=True), min_size=0, max_size=8).map(lambda xs: __import__('numpy').asarray(xs))"
        else:
            strat = "st.one_of(st.integers(), st.floats(allow_nan=True), st.text(), st.none())"

    if optional:
        strat = f"st.one_of(st.none(), {strat})"
    return strat


def build_strategies_from_signature(post_fut_info: dict, fut_name: str) -> dict[str, Any]:
    """Return per-parameter Hypothesis strategy expressions for the target function.

    Returns a dict with keys:
      - ``param_strategies``: list of (param_name, kind, has_default, strategy_expr)
      - ``ok``: whether parsing succeeded
    """
    result = {"param_strategies": [], "ok": False}
    # Find the post function code for the target function name.
    target_code = None
    for fct_full_name, info in post_fut_info.items():
        if fct_full_name.split(".")[-1] == fut_name:
            target_code = info.get("code")
            break
    if target_code is None and post_fut_info:
        target_code = next(iter(post_fut_info.values())).get("code")

    if not target_code:
        append_event(Event(
            level="WARNING",
            message="InputConstructor: could not locate post function code; using generic strategies."
        ))
        return result

    try:
        func_infos = extract_function_info(target_code)
        # pick the function whose name matches fut_name (with/without post_ prefix)
        func_info = None
        for fi in func_infos:
            if fi.name in (fut_name, f"post_{fut_name}"):
                func_info = fi
                break
        if func_info is None and func_infos:
            func_info = func_infos[0]
        if func_info is None:
            return result

        param_types = get_parameter_types(func_info)
        param_strategies = []
        for param in func_info.parameters:
            if param.kind in ("*args", "**kwargs"):
                continue
            if param.name in ("self", "cls"):
                continue
            strat = _strategy_for_type(param_types.get(param.name))
            has_default = param.default_value is not None
            param_strategies.append((param.name, param.kind, has_default, strat))
        result["param_strategies"] = param_strategies
        result["ok"] = True
        append_event(Event(
            level="DEBUG",
            message=f"InputConstructor: derived {len(param_strategies)} type-driven strategies for {fut_name}.",
            type="GeneralInfo",
            info={"param_strategies": [(n, s) for n, _, _, s in param_strategies]}
        ))
    except Exception as e:  # noqa: BLE001 - best effort, never fail the pipeline
        append_event(Event(
            level="WARNING",
            message=f"InputConstructor: failed to derive strategies ({e}); using generic strategies."
        ))
    return result


def load_prompt_template() -> Any:
    from patchguru.prompts.input_constructor.InputConstructorPromptV1 import InputConstructorPrompt
    return InputConstructorPrompt()


def generate_input_generator(
    post_fut_signatures: str,
    pull_request_details: str,
    prev_fut_code: str,
    prev_fut_names: str,
    enclosing_class: str = "",
    available_import: str = "",
) -> tuple[str | None, int]:
    """Ask the LLM to write a ``gen_inputs()`` generator.

    Returns ``(generator_code_or_None, n_llm_queries)``. ``n_llm_queries`` MUST be
    counted by the caller into the phase-2 ``llm_queries`` total to stay consistent
    with the logged LLM usage (see experiments/RQ1_3.py assertion).
    """
    PromptTemplate = load_prompt_template()
    prompt = PromptTemplate.create_prompt(
        post_fut_signatures=post_fut_signatures,
        pull_request_details=pull_request_details,
        prev_fut_code=prev_fut_code,
        prev_fut_names=prev_fut_names,
        enclosing_class=enclosing_class,
        available_import=available_import,
    )
    append_event(Event(
        level="DEBUG",
        message=[
            "Input constructor prompt created successfully!",
            format_info_frame(prompt, "INPUT CONSTRUCTOR PROMPT")
        ],
        type="AnalysisPrompt",
        info={"prompt": prompt}
    ))

    is_valid = False
    llm_queries = 0
    max_retries = Config.ANALYSIS_ATTEMPTS
    parsed_response = None
    while not is_valid and llm_queries < max_retries:
        append_event(Event(
            level="DEBUG",
            message=f"Querying LLM for input constructor (Attempt {llm_queries + 1}/{max_retries})..."
        ))
        response = query_llm(prompt)
        llm_queries += 1
        parsed_response = PromptTemplate.parse_answer(response)
        if parsed_response is None:
            continue
        is_valid = PromptTemplate.check_valid(parsed_response)

    if not is_valid or parsed_response is None:
        append_event(Event(
            level="WARNING",
            message=f"Failed to get a valid gen_inputs() from LLM after {llm_queries} attempts. "
                    "Falling back to seeds + type strategies only."
        ))
        return None, llm_queries

    generator_code = parsed_response["generator"]
    append_event(Event(
        level="INFO",
        message=[
            "Input constructor generated successfully.",
            format_info_frame(generator_code, "GEN_INPUTS GENERATOR")
        ],
        type="GeneralInfo",
        info={"generator": generator_code, "input_constructor_queries": llm_queries}
    ))
    return generator_code, llm_queries
