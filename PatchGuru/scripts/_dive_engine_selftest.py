#!/usr/bin/env python3
"""Standalone self-test for the DIVE harness engine (no Docker, no LLM)."""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from patchguru.analysis import dive_harness

spec_prefix = """
def pre_f(x):
    return x * 2

def post_f(x):
    if x < 100:
        return x * 2
    return x * 3
"""

harness = dive_harness.build_harness(
    fut_name="f",
    spec_prefix=spec_prefix,
    changed_offsets=[2, 3],
    seed_exprs=["((1,), {})", "((200,), {})", "((50,), {})"],
    strategy_exprs=["st.integers()"],
    gen_inputs_code="def gen_inputs():\n    for v in [0, 5, 150, 1000, -3]:\n        yield ((v,), {})\n",
    exec_budget=300,
    time_budget=30,
    deflake_k=3,
    max_clusters=8,
    install_deps=False,
)

with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
    f.write(harness)
    path = f.name

print(f"[selftest] running harness at {path}")
proc = subprocess.run([sys.executable, path], capture_output=True, text=True, timeout=120)
print("----- STDOUT -----")
print(proc.stdout)
print("----- STDERR -----")
print(proc.stderr[-2000:])
print("----- EXIT -----", proc.returncode)

assert "===DIVE_RESULT_BEGIN===" in proc.stdout, "missing result marker"
payload = proc.stdout.split("===DIVE_RESULT_BEGIN===")[1].split("===DIVE_RESULT_END===")[0].strip()
data = json.loads(payload)
print("[selftest] clusters:", len(data["clusters"]))
print("[selftest] stats:", data["stats"])
assert len(data["clusters"]) >= 1, "expected at least one divergence cluster"
assert data["stats"]["changed_line_total"] == 2
assert data["stats"]["changed_line_hit"] >= 1, "expected changed-line coverage > 0"
print("[selftest] OK")
os.unlink(path)
