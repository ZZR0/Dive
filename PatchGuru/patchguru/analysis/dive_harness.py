"""DIVE in-container search engine (harness builder).

Because every ``DockerExecutor.execute_python_code`` call is expensive (re-imports,
``pip install -e .`` for keras, conda activation for scipy, ...), the *entire*
divergence-directed search loop must run inside ONE container execution. This module
assembles a single self-contained Python program (the "harness") that:

  - defines ``pre_<fn>`` / ``post_<fn>`` (the real function bodies),
  - implements ``call_impl`` (run both versions, capture ``(res, exc)``),
  - measures changed-line coverage of ``post_<fn>`` via ``sys.settrace``,
  - runs a greybox, fitness-guided search (seeds + LLM ``gen_inputs()`` +
    Hypothesis strategies + type-aware mutation),
  - triages divergences (cluster by category, delta-debug minimize, de-flake),
  - prints a JSON result block between ``===DIVE_RESULT_BEGIN===`` markers.

The host-side orchestrator (``DivergenceSearch.py``) parses that JSON and runs the
LLM intent classifier (E) on each minimized divergence.
"""
from __future__ import annotations

import json


# ---------------------------------------------------------------------------
# Static engine code. NOTE: must not contain triple-quoted strings, because it is
# embedded inside this module's triple-quoted ENGINE constant.
# ---------------------------------------------------------------------------
ENGINE = r'''
# ===================== DIVE SEARCH ENGINE (static) =====================
import sys as _sys
import time as _time
import math as _math
import copy as _copy
import json as _json
import heapq as _heapq
import random as _random
import signal as _signal
import traceback as _traceback

_random.seed(0)

try:
    import numpy as _np
    HAVE_NUMPY = True
except Exception:
    _np = None
    HAVE_NUMPY = False

try:
    from hypothesis import strategies as st
    from hypothesis import HealthCheck, settings  # noqa: F401
    HAVE_HYPOTHESIS = True
except Exception:
    st = None
    HAVE_HYPOTHESIS = False


def _dive_log(msg):
    print("[DIVE] " + str(msg), flush=True)


FUT = __DIVE_FUT_NAME__
CFG = __DIVE_CFG__
SEED_EXPRS = __DIVE_SEED_EXPRS__
STRATEGY_EXPRS = __DIVE_STRATEGY_EXPRS__
BASELINE_DRIVER = __DIVE_BASELINE_DRIVER__

BUDGET = int(CFG.get("exec_budget", 800))
TIME_BUDGET = float(CFG.get("time_budget", 300))
DEFLAKE_K = int(CFG.get("deflake_k", 3))
MAX_CLUSTERS = int(CFG.get("max_clusters", 8))
HARVEST_TIME = float(CFG.get("harvest_time", 45))
USE_CONSTRUCTOR = bool(CFG.get("use_constructor", True))
USE_GUIDED = bool(CFG.get("use_guided_search", True))
USE_TRIAGE = bool(CFG.get("use_triage", True))
DIVE_ABLATION = CFG.get("ablation")

HARNESS_FILE = __file__


class _DiveNotReconstructable(Exception):
    pass


def _resolve(name_prefix):
    fn = globals().get(name_prefix + FUT)
    return fn


pre_fn = _resolve("pre_")
post_fn = _resolve("post_")


def _post_code_object(fn):
    if fn is None:
        return None
    if isinstance(fn, property):
        return getattr(fn.fget, "__code__", None) if fn.fget else None
    if isinstance(fn, classmethod):
        return getattr(fn.__func__, "__code__", None) if fn.__func__ else None
    if isinstance(fn, staticmethod):
        return getattr(fn.__func__, "__code__", None) if fn.__func__ else None
    return getattr(fn, "__code__", None)


def _target_lines():
    code = _post_code_object(post_fn)
    if code is None:
        return set()
    first = code.co_firstlineno
    return set(first + off for off in CFG.get("changed_offsets", []))


TARGET_LINES = _target_lines()


def _safe_repr(obj, limit=300):
    try:
        r = repr(obj)
    except Exception:
        r = "<unrepr-able " + str(type(obj)) + ">"
    if len(r) > limit:
        r = r[:limit] + "...(truncated)"
    return r


def _safe_deepcopy(obj):
    try:
        return _copy.deepcopy(obj)
    except Exception:
        return obj


def call_impl(args, kwargs):
    # Run pre version
    pre_res = None
    pre_exc = None
    try:
        pre_res = pre_fn(*_safe_deepcopy(args), **_safe_deepcopy(kwargs))
    except Exception as e:  # noqa: BLE001
        pre_exc = e

    # Run post version with line tracing for changed-line coverage
    executed = set()

    def _tracer(frame, event, arg):
        if event == "line" and frame.f_code.co_filename == HARNESS_FILE:
            executed.add(frame.f_lineno)
        return _tracer

    post_res = None
    post_exc = None
    _sys.settrace(_tracer)
    try:
        post_res = post_fn(*_safe_deepcopy(args), **_safe_deepcopy(kwargs))
    except Exception as e:  # noqa: BLE001
        post_exc = e
    finally:
        _sys.settrace(None)

    hit = executed & TARGET_LINES
    return pre_res, pre_exc, post_res, post_exc, hit


def _safe_eq(a, b):
    if HAVE_NUMPY and (isinstance(a, _np.ndarray) or isinstance(b, _np.ndarray) or
                       isinstance(a, _np.generic) or isinstance(b, _np.generic)):
        try:
            return bool(_np.array_equal(a, b, equal_nan=True))
        except Exception:
            try:
                return bool(_np.array_equal(a, b))
            except Exception:
                return _safe_repr(a) == _safe_repr(b)
    if isinstance(a, float) and isinstance(b, float):
        if _math.isnan(a) and _math.isnan(b):
            return True
        return a == b
    try:
        res = (a == b)
        if isinstance(res, bool):
            return res
        # numpy-like truthiness
        try:
            return bool(res)
        except Exception:
            return _safe_repr(a) == _safe_repr(b)
    except Exception:
        return _safe_repr(a) == _safe_repr(b)


def diverges(pre_res, pre_exc, post_res, post_exc):
    if (pre_exc is None) != (post_exc is None):
        return True
    if pre_exc is not None and post_exc is not None:
        return type(pre_exc) is not type(post_exc)
    return not _safe_eq(pre_res, post_res)


def category(pre_res, pre_exc, post_res, post_exc):
    if pre_exc is not None or post_exc is not None:
        return ("exc",
                type(pre_exc).__name__ if pre_exc is not None else None,
                type(post_exc).__name__ if post_exc is not None else None)
    return ("val", type(pre_res).__name__, type(post_res).__name__)


def _summarize(res, exc):
    if exc is not None:
        return {"kind": "exception", "type": type(exc).__name__, "msg": str(exc)[:300]}
    return {"kind": "value", "type": type(res).__name__, "repr": _safe_repr(res)}


def to_expr(obj):
    # Produce a re-evaluable Python expression (assuming numpy as np + available imports).
    if obj is None or isinstance(obj, bool):
        return repr(obj)
    if isinstance(obj, float):
        if _math.isnan(obj):
            return "float('nan')"
        if _math.isinf(obj):
            return "float('inf')" if obj > 0 else "float('-inf')"
        return repr(obj)
    if isinstance(obj, (int, str, bytes)):
        return repr(obj)
    if HAVE_NUMPY and isinstance(obj, _np.ndarray):
        return "np.array(" + to_expr(obj.tolist()) + ", dtype='" + str(obj.dtype) + "')"
    if HAVE_NUMPY and isinstance(obj, _np.generic):
        return to_expr(obj.item())
    if isinstance(obj, list):
        return "[" + ", ".join(to_expr(x) for x in obj) + "]"
    if isinstance(obj, tuple):
        inner = ", ".join(to_expr(x) for x in obj)
        if len(obj) == 1:
            inner += ","
        return "(" + inner + ")"
    if isinstance(obj, set):
        if not obj:
            return "set()"
        return "{" + ", ".join(to_expr(x) for x in obj) + "}"
    if isinstance(obj, dict):
        return "{" + ", ".join(to_expr(k) + ": " + to_expr(v) for k, v in obj.items()) + "}"
    raise _DiveNotReconstructable(str(type(obj)))


def _normalize(val):
    # Accept (args, kwargs) or a bare args tuple/list.
    if isinstance(val, tuple) and len(val) == 2 and isinstance(val[1], dict) and \
            isinstance(val[0], (tuple, list)):
        return tuple(val[0]), dict(val[1])
    if isinstance(val, (tuple, list)):
        return tuple(val), {}
    return (val,), {}


def _key(args, kwargs):
    return _safe_repr((args, kwargs), limit=400)


# ----------------------------- mutation -----------------------------
_SCALAR_POOL = [0, 1, -1, 2, -2, 10, -10, 255, 256, 1023, 1024,
                2 ** 31, -(2 ** 31), 2 ** 63, -(2 ** 63), 10 ** 18,
                0.0, -0.0, 1.0, -1.0, 0.5, 1e-12, 1e12, 1e308,
                float("nan"), float("inf"), float("-inf"),
                "", "a", "0", " ", "\n", "\u00e9\u4e2d", "x" * 64,
                True, False, None, [], {}, (), [0], [1, 2, 3], {"k": 1}]


def _mutate_value(v, depth=0):
    if depth > 3:
        return v
    if isinstance(v, bool):
        return not v
    if isinstance(v, int):
        return _random.choice([v + 1, v - 1, v * 2, -v, 0, 2 ** 31, -(2 ** 31), v + 1000])
    if isinstance(v, float):
        return _random.choice([v + 1, v - 1, v * 2.0, -v, 0.0,
                               float("nan"), float("inf"), float("-inf"), 1e308])
    if isinstance(v, str):
        ops = [v + "x", v[:-1] if v else "z", "", v * 2,
               v + "\u4e2d", v.upper(), " " + v, v + "\x00"]
        return _random.choice(ops)
    if isinstance(v, bytes):
        return _random.choice([v + b"\x00", b"", v * 2])
    if isinstance(v, list):
        if not v:
            return _random.choice([[0], [1, 2], [None], [_random.choice(_SCALAR_POOL)]])
        w = list(v)
        op = _random.randint(0, 4)
        if op == 0:
            w.pop(_random.randrange(len(w)))
        elif op == 1:
            idx = _random.randrange(len(w))
            w[idx] = _mutate_value(w[idx], depth + 1)
        elif op == 2:
            w.append(_random.choice(_SCALAR_POOL))
        elif op == 3:
            w = w + w
        else:
            w = []
        return w
    if isinstance(v, tuple):
        mutated = _mutate_value(list(v), depth)
        if isinstance(mutated, (list, tuple)):
            return tuple(mutated)
        return (mutated,)
    if isinstance(v, dict):
        if not v:
            return {"k": _random.choice(_SCALAR_POOL)}
        w = dict(v)
        keys = list(w.keys())
        op = _random.randint(0, 2)
        if op == 0:
            del w[_random.choice(keys)]
        elif op == 1:
            k = _random.choice(keys)
            w[k] = _mutate_value(w[k], depth + 1)
        else:
            w["__dive__"] = _random.choice(_SCALAR_POOL)
        return w
    if v is None:
        return _random.choice([0, "", [], {}, False])
    if HAVE_NUMPY and isinstance(v, _np.ndarray):
        op = _random.randint(0, 4)
        try:
            if op == 0:
                return _np.array([], dtype=v.dtype)
            if op == 1 and v.size:
                w = v.astype(float).copy()
                w.flat[_random.randrange(w.size)] = float("nan")
                return w
            if op == 2:
                return v * 2
            if op == 3:
                return v.astype("float32") if v.dtype != _np.float32 else v.astype("float64")
            return _np.concatenate([v, v]) if v.ndim == 1 else v
        except Exception:
            return v
    return v


def _mutate_input(args, kwargs):
    args = list(args)
    kwargs = dict(kwargs)
    targets = len(args) + len(kwargs)
    if targets == 0:
        return (tuple(args), kwargs)
    pick = _random.randrange(targets)
    if pick < len(args):
        args[pick] = _mutate_value(args[pick])
    else:
        k = list(kwargs.keys())[pick - len(args)]
        kwargs[k] = _mutate_value(kwargs[k])
    return (tuple(args), kwargs)


def _random_input():
    n = 1
    code = _post_code_object(post_fn)
    if code is not None:
        n = max(0, code.co_argcount)
        # drop a likely 'self'
        names = code.co_varnames[:n]
        if names and names[0] in ("self", "cls"):
            n -= 1
    n = min(n, 4)
    args = tuple(_random.choice(_SCALAR_POOL) for _ in range(n))
    return (args, {})


# ----------------------------- seeding -----------------------------
def _build_population():
    pop = []

    for expr in SEED_EXPRS:
        try:
            val = eval(expr, globals())  # noqa: S307 - trusted, generated by us
            pop.append(_normalize(val))
        except Exception as e:  # noqa: BLE001
            _dive_log("seed eval failed: " + repr(e)[:160])

    if USE_CONSTRUCTOR and "gen_inputs" in globals():
        try:
            cnt = 0
            for val in gen_inputs():  # noqa: F821 - injected dynamically
                pop.append(_normalize(val))
                cnt += 1
                if cnt >= 200:
                    break
            _dive_log("gen_inputs() produced " + str(cnt) + " seeds")
        except Exception as e:  # noqa: BLE001
            _dive_log("gen_inputs() failed: " + repr(e)[:160])

    if USE_CONSTRUCTOR and HAVE_HYPOTHESIS and STRATEGY_EXPRS:
        try:
            strat = st.tuples(*[eval(s, globals()) for s in STRATEGY_EXPRS])  # noqa: S307
            got = 0
            import warnings as _warnings
            with _warnings.catch_warnings():
                _warnings.simplefilter("ignore")
                for _ in range(50):
                    try:
                        sample = strat.example()
                        pop.append((tuple(sample), {}))
                        got += 1
                    except Exception:
                        break
            _dive_log("hypothesis produced " + str(got) + " seeds")
        except Exception as e:  # noqa: BLE001
            _dive_log("hypothesis sampling failed: " + repr(e)[:160])

    # always add a few random inputs as a floor
    for _ in range(10):
        pop.append(_random_input())

    # dedupe
    seen = set()
    uniq = []
    for (a, k) in pop:
        key = _key(a, k)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((a, k))
    return uniq[:400]


# ----------------------------- baseline seed harvest -----------------------------
def _harvest_baseline_seeds():
    # Re-run the baseline phase-2 driver (asserts neutralized) with the REAL pre/post
    # monkeypatched under their module names, recording every concrete input it calls.
    # Guarantees DIVE searches at least every input the baseline exercised.
    captured = []
    if not BASELINE_DRIVER or pre_fn is None or post_fn is None:
        return []

    def _wrap(fn):
        def _w(*a, **k):
            try:
                captured.append((_safe_deepcopy(a), _safe_deepcopy(k)))
            except Exception:
                pass
            return fn(*a, **k)
        return _w

    ns = dict(globals())
    ns["pre_" + FUT] = _wrap(pre_fn)
    ns["post_" + FUT] = _wrap(post_fn)

    # Hard wall-clock cap on harvesting: a baseline driver (with asserts neutralized)
    # can loop for a very long time. SIGALRM interrupts it; whatever was captured so
    # far is still used. Only available on the main thread on Unix.
    class _HarvestTimeout(BaseException):
        pass

    def _on_alarm(signum, frame):
        raise _HarvestTimeout()

    _has_alarm = hasattr(_signal, "SIGALRM")
    _old_handler = None
    if _has_alarm:
        try:
            _old_handler = _signal.signal(_signal.SIGALRM, _on_alarm)
            _signal.setitimer(_signal.ITIMER_REAL, HARVEST_TIME)
        except Exception:  # noqa: BLE001 - not main thread / unsupported
            _has_alarm = False

    # NOTE: a baseline driver is arbitrary code; it may raise SystemExit or other
    # BaseExceptions. Catch BaseException so harvesting can never abort the harness.
    try:
        _code = compile(BASELINE_DRIVER, "<dive_baseline_driver>", "exec")
        exec(_code, ns)  # noqa: S102 - trusted, generated by us
        main_fn = ns.get("main")
        if callable(main_fn):
            try:
                main_fn()
            except BaseException as e:  # noqa: BLE001 - assertions/raises/exit/timeout expected
                _dive_log("baseline driver main() stopped: " + repr(e)[:120])
    except BaseException as e:  # noqa: BLE001
        _dive_log("baseline driver exec stopped: " + repr(e)[:160])
    finally:
        if _has_alarm:
            try:
                _signal.setitimer(_signal.ITIMER_REAL, 0)
                if _old_handler is not None:
                    _signal.signal(_signal.SIGALRM, _old_handler)
            except Exception:  # noqa: BLE001
                pass

    out = []
    seen_local = set()
    for (a, k) in captured:
        try:
            ta, tk = tuple(a), dict(k)
            key = _key(ta, tk)
        except Exception:
            continue
        if key in seen_local:
            continue
        seen_local.add(key)
        out.append((ta, tk))
        if len(out) >= 300:
            break
    _dive_log("baseline seed harvest captured " + str(len(out)) + " unique inputs")
    return out


# ----------------------------- triage -----------------------------
def _minimize(args, kwargs, target_cat):
    # Greedy delta-debugging: shrink while preserving same divergence category.
    def still_diverges(a, k):
        pr, pe, qr, qe, _ = call_impl(a, k)
        return diverges(pr, pe, qr, qe) and category(pr, pe, qr, qe) == target_cat

    best = (tuple(args), dict(kwargs))
    changed = True
    rounds = 0
    while changed and rounds < 40:
        changed = False
        rounds += 1
        a, k = best
        # try dropping kwargs
        for kk in list(k.keys()):
            nk = dict(k)
            del nk[kk]
            if still_diverges(a, nk):
                best = (a, nk)
                changed = True
                a, k = best
        # try shrinking each positional arg
        for i in range(len(a)):
            v = a[i]
            for cand in _shrink_candidates(v):
                na = list(a)
                na[i] = cand
                na = tuple(na)
                if still_diverges(na, k):
                    best = (na, k)
                    changed = True
                    a, k = best
                    break
    return best


def _shrink_candidates(v):
    out = []
    if isinstance(v, bool):
        return out
    if isinstance(v, int) and v not in (0, 1, -1):
        out.extend([0, v // 2, 1 if v > 0 else -1])
    elif isinstance(v, float) and v not in (0.0,):
        if _math.isfinite(v):
            out.extend([0.0, v / 2.0])
    elif isinstance(v, str) and v:
        out.extend(["", v[:len(v) // 2], v[:1]])
    elif isinstance(v, (list, tuple)) and len(v) > 0:
        seq = list(v)
        half = seq[:len(seq) // 2]
        out.append(type(v)(half) if not isinstance(v, tuple) else tuple(half))
        out.append(type(v)([]) if not isinstance(v, tuple) else tuple())
    elif isinstance(v, dict) and v:
        keys = list(v.keys())
        out.append({keys[0]: v[keys[0]]})
        out.append({})
    elif HAVE_NUMPY and isinstance(v, _np.ndarray) and v.size > 0:
        try:
            out.append(v[: max(1, v.size // 2)])
            out.append(v[:1])
        except Exception:
            pass
    return out


def _deflake(args, kwargs, target_cat):
    # Require the divergence + category + summaries to be stable across K runs.
    sigs = set()
    for _ in range(max(1, DEFLAKE_K)):
        pr, pe, qr, qe, _ = call_impl(args, kwargs)
        if not diverges(pr, pe, qr, qe):
            return False, None, None
        if category(pr, pe, qr, qe) != target_cat:
            return False, None, None
        pre_s = _summarize(pr, pe)
        post_s = _summarize(qr, qe)
        sigs.add(_json.dumps([pre_s, post_s], sort_keys=True))
    if len(sigs) != 1:
        return False, None, None
    return True, pre_s, post_s


# ----------------------------- main search -----------------------------
def __dive_main__():
    t0 = _time.time()
    if pre_fn is None or post_fn is None:
        _dive_log("ERROR: could not resolve pre_/post_ functions for '" + str(FUT) + "'")
        print("===DIVE_RESULT_BEGIN===", flush=True)
        print(_json.dumps({"error": "functions_unresolved", "clusters": [], "stats": {}}))
        print("===DIVE_RESULT_END===", flush=True)
        return

    try:
        harvested = _harvest_baseline_seeds()
    except BaseException as e:  # noqa: BLE001 - harvest must never abort the search
        _dive_log("baseline harvest aborted: " + repr(e)[:160])
        harvested = []
    population = _build_population()
    # Reset the search clock AFTER seeding so harvesting/population-build never eats
    # into the search TIME_BUDGET (critical for slow projects like keras).
    t0 = _time.time()
    _dive_log("seeds/population size = " + str(len(population)) +
              " | baseline-harvested = " + str(len(harvested)) +
              " | changed target lines = " + str(len(TARGET_LINES)) +
              " | ablation = " + str(DIVE_ABLATION) +
              " | budget = " + str(BUDGET) + " execs / " + str(TIME_BUDGET) + "s")

    covered = set()
    divergences = {}  # category -> (args, kwargs)
    frontier = []
    seen = set()
    counter = [0]

    def push(a, k, prio):
        key = _key(a, k)
        if key in seen:
            return
        seen.add(key)
        counter[0] += 1
        _heapq.heappush(frontier, (-prio, counter[0], a, k))

    # Baseline-harvested inputs first; under guided search they get highest priority.
    harvest_prio = 10 if USE_GUIDED else 1
    for (a, k) in harvested:
        push(a, k, harvest_prio)
    for (a, k) in population:
        push(a, k, 1)

    exec_count = 0
    last_log = 0
    while frontier and exec_count < BUDGET and (_time.time() - t0) < TIME_BUDGET:
        _, _, args, kwargs = _heapq.heappop(frontier)
        try:
            pre_res, pre_exc, post_res, post_exc, hit = call_impl(args, kwargs)
        except Exception as e:  # noqa: BLE001 - defensive
            _dive_log("call_impl crashed: " + repr(e)[:160])
            exec_count += 1
            continue
        exec_count += 1

        new_lines = hit - covered
        covered |= hit

        is_div = diverges(pre_res, pre_exc, post_res, post_exc)
        if is_div:
            cat = category(pre_res, pre_exc, post_res, post_exc)
            if cat not in divergences:
                divergences[cat] = (args, kwargs)
                _dive_log("NEW divergence category #" + str(len(divergences)) +
                          " " + str(cat) + " at exec " + str(exec_count))

        if USE_GUIDED:
            interesting = bool(new_lines) or is_div
            n_children = 6 if interesting else 2
            child_prio = 3 if interesting else 1
        else:
            interesting = False
            n_children = 2
            child_prio = 1
        for _ in range(n_children):
            try:
                child = _mutate_input(args, kwargs)
            except Exception as e:  # noqa: BLE001 - a bad mutation must not abort search
                _dive_log("mutation failed: " + repr(e)[:120])
                continue
            push(child[0], child[1], child_prio)

        if exec_count - last_log >= 100:
            last_log = exec_count
            _dive_log("progress: execs=" + str(exec_count) +
                      " covered_changed_lines=" + str(len(covered)) + "/" + str(len(TARGET_LINES)) +
                      " divergence_categories=" + str(len(divergences)) +
                      " frontier=" + str(len(frontier)) +
                      " elapsed=" + str(round(_time.time() - t0, 1)) + "s")

    _dive_log("search done: execs=" + str(exec_count) +
              " covered=" + str(len(covered)) + "/" + str(len(TARGET_LINES)) +
              " raw_categories=" + str(len(divergences)) +
              " elapsed=" + str(round(_time.time() - t0, 1)) + "s")

    # ---- triage: minimize + de-flake each category representative ----
    clusters = []
    cats = list(divergences.items())[:MAX_CLUSTERS]
    for idx, (cat, (args, kwargs)) in enumerate(cats):
        _dive_log("triaging cluster " + str(idx + 1) + "/" + str(len(cats)) + " " + str(cat))
        if USE_TRIAGE:
            try:
                margs, mkwargs = _minimize(args, kwargs, cat)
            except Exception as e:  # noqa: BLE001
                _dive_log("minimize failed: " + repr(e)[:120])
                margs, mkwargs = args, kwargs

            stable, pre_s, post_s = _deflake(margs, mkwargs, cat)
            if not stable:
                _dive_log("cluster dropped (flaky/unstable): " + str(cat))
                continue
        else:
            margs, mkwargs = args, kwargs
            pr, pe, qr, qe, _ = call_impl(margs, mkwargs)
            pre_s = _summarize(pr, pe)
            post_s = _summarize(qr, qe)

        # final coverage for this representative input
        _, _, _, _, hit = call_impl(margs, mkwargs)

        reconstructable = True
        try:
            args_expr = to_expr(tuple(margs))
            kwargs_expr = to_expr(dict(mkwargs))
        except Exception:  # noqa: BLE001
            reconstructable = False
            args_expr = _safe_repr(tuple(margs), 400)
            kwargs_expr = _safe_repr(dict(mkwargs), 400)

        clusters.append({
            "category": list(cat),
            "args_expr": args_expr,
            "kwargs_expr": kwargs_expr,
            "reconstructable": reconstructable,
            "pre": pre_s,
            "post": post_s,
            "hit_changed_lines": sorted(int(x) for x in hit),
        })

    stats = {
        "seeds": len(population),
        "baseline_seeds": len(harvested),
        "execs": exec_count,
        "changed_line_total": len(TARGET_LINES),
        "changed_line_hit": len(covered),
        "raw_divergence_categories": len(divergences),
        "reported_clusters": len(clusters),
        "elapsed_sec": round(_time.time() - t0, 2),
        "ablation": DIVE_ABLATION,
        "use_constructor": USE_CONSTRUCTOR,
        "use_guided_search": USE_GUIDED,
        "use_triage": USE_TRIAGE,
    }
    _dive_log("RESULT stats: " + _json.dumps(stats))

    print("===DIVE_RESULT_BEGIN===", flush=True)
    print(_json.dumps({"clusters": clusters, "stats": stats}))
    print("===DIVE_RESULT_END===", flush=True)


try:
    __dive_main__()
except BaseException as _dive_top_exc:  # noqa: BLE001 - always emit parseable markers
    _dive_log("engine crashed: " + repr(_dive_top_exc)[:200])
    print("===DIVE_RESULT_BEGIN===", flush=True)
    print(_json.dumps({"error": "engine_crash", "detail": repr(_dive_top_exc)[:300],
                       "clusters": [], "stats": {"engine_crash": True}}))
    print("===DIVE_RESULT_END===", flush=True)
'''


_BOOTSTRAP_TEMPLATE = r'''
# ===================== DIVE dependency bootstrap =====================
import sys as _dive_sys
import subprocess as _dive_subprocess


def _dive_ensure(pkg):
    try:
        __import__(pkg)
        return True
    except Exception:
        try:
            _dive_subprocess.run(
                [_dive_sys.executable, "-m", "pip", "install", "-q", pkg],
                timeout=240,
            )
            __import__(pkg)
            return True
        except Exception:
            return False


if {install_deps}:
    _dive_ensure("hypothesis")
    _dive_ensure("coverage")
'''


def _extract_future_imports(text: str) -> tuple[list[str], str]:
    """Pull ``from __future__ import ...`` lines out of a code block.

    ``__future__`` imports MUST be the first statement of a module, so they cannot
    appear after the harness bootstrap. We hoist them to the very top of the file.
    """
    if not text:
        return [], text or ""
    futures: list[str] = []
    rest: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("from __future__ import"):
            futures.append(line.strip())
        else:
            rest.append(line)
    return futures, "\n".join(rest)


def build_harness(
    fut_name: str,
    spec_prefix: str,
    changed_offsets: list[int],
    seed_exprs: list[str],
    strategy_exprs: list[str],
    gen_inputs_code: str | None = None,
    baseline_driver_code: str | None = None,
    exec_budget: int = 800,
    time_budget: int = 300,
    deflake_k: int = 3,
    max_clusters: int = 8,
    harvest_time: int = 45,
    install_deps: bool = True,
    use_constructor: bool = True,
    use_guided_search: bool = True,
    use_triage: bool = True,
    ablation: str | None = None,
) -> str:
    """Assemble the full DIVE harness program (string) to run in the container.

    ``spec_prefix`` is the validated phase-1 specification *up to* the
    ``# Specification`` marker, i.e. the (already-repaired, container-runnable)
    imports + ``pre_<fn>`` / ``post_<fn>`` source code. Reusing it guarantees the
    functions import and run exactly as they did in phase 1.
    """
    cfg = {
        "exec_budget": exec_budget,
        "time_budget": time_budget,
        "deflake_k": deflake_k,
        "max_clusters": max_clusters,
        "harvest_time": harvest_time,
        "changed_offsets": list(changed_offsets),
        "use_constructor": use_constructor,
        "use_guided_search": use_guided_search,
        "use_triage": use_triage,
    }
    if ablation:
        cfg["ablation"] = ablation

    # Hoist any __future__ imports to the very top of the harness.
    all_futures: list[str] = []
    f1, spec_prefix = _extract_future_imports(spec_prefix or "")
    f2, gen_inputs_code = _extract_future_imports(gen_inputs_code or "") if gen_inputs_code else ([], gen_inputs_code)
    for fs in (f1, f2):
        for line in fs:
            if line not in all_futures:
                all_futures.append(line)

    parts = []
    if all_futures:
        parts.append("\n".join(all_futures))
    parts.append(_BOOTSTRAP_TEMPLATE.format(install_deps=str(bool(install_deps))))
    parts.append("try:\n    import numpy as np\nexcept Exception:\n    np = None")
    parts.append("import math")
    parts.append("# ===================== Phase-1 imports + function under test (pre/post) =====================")
    parts.append(spec_prefix or "")
    if gen_inputs_code:
        parts.append("# ===================== LLM gen_inputs() =====================")
        parts.append(gen_inputs_code)
    parts.append("# ===================== DIVE injected config =====================")
    parts.append("__DIVE_FUT_NAME__ = " + repr(fut_name))
    parts.append("__DIVE_CFG__ = " + repr(cfg))
    parts.append("__DIVE_SEED_EXPRS__ = [" + ", ".join(repr(s) for s in seed_exprs) + "]")
    parts.append("__DIVE_STRATEGY_EXPRS__ = [" + ", ".join(repr(s) for s in strategy_exprs) + "]")
    parts.append("__DIVE_BASELINE_DRIVER__ = " + repr(baseline_driver_code or ""))
    parts.append(ENGINE)
    return "\n\n".join(parts)
