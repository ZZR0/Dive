from patchguru.utils.Tracker import Event, append_event


class InputConstructorPrompt:
    """
    DIVE (B) Structured Input Constructor prompt.

    Instead of writing a fixed handful of test cases, the LLM writes ONE reusable
    generator function ``gen_inputs()`` that *yields* many valid, structured
    ``(args, kwargs)`` tuples for the target function. The DIVE search engine then
    repeatedly samples / mutates these inputs at zero extra LLM cost.
    """

    def __init__(self):
        pass

    def create_prompt(
        self,
        post_fut_signatures: str,
        pull_request_details: str,
        prev_fut_code: str,
        prev_fut_names: str,
        enclosing_class: str = "",
        available_import: str = "",
    ) -> str:
        template = """
# Role
You are an expert software developer and test input designer. Your task is to write a single Python generator function named `gen_inputs()` that produces a wide variety of VALID, well-formed inputs for a target function. These inputs will later be executed and mutated by an automated divergence-directed search engine to compare the pre-PR and post-PR versions of the function. Your generator is the seed source of structured inputs, so it must focus on producing inputs that are *accepted* by the function (valid domain objects), while still covering diverse and edge-case-rich scenarios.

# Guidelines

1. **Understand the function's input domain.** Read the target function signature, the pre-PR implementation, the enclosing class (if any) and the PR details. Identify each parameter, its expected type, and any structural constraints (e.g. a custom class instance, a numpy array of a certain shape, a dict with specific keys, a DataFrame, etc.).

2. **Construct structured / domain objects when needed.** If the function expects complex or custom-typed inputs (class instances, domain objects, arrays, nested structures), construct them properly inside `gen_inputs()` using the available imports and the enclosing-class context. Do NOT just pass random bytes or trivially-wrong types — the goal is *valid* inputs that actually exercise the function body.

3. **Cover diversity and edge cases.** Across the yielded inputs, cover: typical values, boundary values (0, 1, empty, single-element, very large/small), None where allowed, varied lengths/shapes, varied dtypes, and combinations of multiple arguments that interact. Aim for roughly 20-40 diverse yields.

4. **Yield (args, kwargs).** Each yield MUST be a 2-tuple `(args, kwargs)` where `args` is a tuple/list of positional arguments and `kwargs` is a dict of keyword arguments. The arguments must match the target function's signature (use the post-PR signature). Example: `yield ((arr,), {"axis": 0})`.

5. **Be self-contained and deterministic-friendly.** The generator runs in an environment where the available imports are already imported. You may use the standard library and the available imports. Do not call the target function itself inside `gen_inputs()`. Do not read files or network. If you use randomness, seed it for reproducibility.

# Input

## Target function signature(s) after the pull request
```python
{post_fut_signatures}
```

## Source code of target function(s) before the pull request
```python
{prev_fut_code}
```

## Enclosing Class of target function(s) (If Applicable)
{context}

## Pull Request Details
{pr_details}

## Available Imports
You can assume the following are already imported in the execution environment:
```python
{available_import}
```

# Output Format Instructions

1. Provide a single generator function named exactly `gen_inputs` taking no arguments.
2. Each `yield` MUST produce a 2-tuple `(args, kwargs)` as described above.
3. Do NOT redefine the target function(s) {prev_fut_names}. Do NOT call them.
4. Do NOT include import statements that are already in the available imports; if you need an extra stdlib import, put it inside the function body or at the top of the `<generator>` block.
5. Provide your response in the following format:

<reasoning>
[Briefly analyze the parameter types and the structured objects you need to build.]
</reasoning>

<generator>
def gen_inputs():
    # yield (args, kwargs) tuples of valid, diverse inputs
    ...
</generator>
        """
        if len(enclosing_class) > 0:
            if len(enclosing_class) > 3000:
                enclosing_class = enclosing_class[:3000]
                enclosing_class += "(...truncated...)"
            context = f"""
Target function(s) are defined in the following class:

```python
{enclosing_class}
```
"""
        else:
            context = "Target function(s) are defined in the global scope. There is no enclosing class."

        # NOTE: use explicit replace (not str.format) because the template contains
        # literal braces (e.g. {"axis": 0}) that would break str.format().
        replacements = {
            "{post_fut_signatures}": post_fut_signatures or "",
            "{prev_fut_code}": prev_fut_code or "",
            "{prev_fut_names}": prev_fut_names or "",
            "{context}": context,
            "{pr_details}": pull_request_details or "",
            "{available_import}": available_import or "",
        }
        query = template
        for key, value in replacements.items():
            query = query.replace(key, value)
        return query

    def parse_answer(self, answer: str) -> dict | None:
        results = {}
        if "<reasoning>" in answer and "</reasoning>" in answer:
            reasoning_start = answer.index("<reasoning>") + len("<reasoning>")
            reasoning_end = answer.index("</reasoning>")
            results["reasoning"] = answer[reasoning_start:reasoning_end].strip()

        if "<generator>" not in answer or "</generator>" not in answer:
            append_event(Event(
                level="ERROR",
                message="LLM response is missing required tag: <generator>"
            ))
            return None

        try:
            gen_start = answer.index("<generator>") + len("<generator>")
            gen_end = answer.index("</generator>")
            generator_code = answer[gen_start:gen_end].strip()

            if "```" in generator_code:
                if "```python" in generator_code:
                    generator_code = generator_code.split("```python")[1].split("```")[0].strip()
                else:
                    generator_code = generator_code.split("```")[1].split("```")[0].strip()
            results["generator"] = generator_code
        except ValueError as e:
            append_event(Event(
                level="ERROR",
                message=f"Error while parsing LLM response for input constructor: {e}"
            ))
            return None

        return results

    def check_valid(self, parsed_response: dict) -> bool:
        generator_code = parsed_response.get("generator", "")
        if "def gen_inputs" not in generator_code:
            append_event(Event(
                level="DEBUG",
                message="Input constructor is missing required 'def gen_inputs' definition."
            ))
            return False
        if "yield" not in generator_code:
            append_event(Event(
                level="DEBUG",
                message="Input constructor 'gen_inputs' does not yield anything."
            ))
            return False

        # Must be syntactically valid Python
        import ast
        try:
            ast.parse(generator_code)
        except SyntaxError as e:
            append_event(Event(
                level="DEBUG",
                message=f"Input constructor has a syntax error: {e}"
            ))
            return False

        append_event(Event(
            level="DEBUG",
            message="Input constructor passed basic validity checks."
        ))
        return True
