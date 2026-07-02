#!/usr/bin/env bash
# 本地 smoke test：在 pgabl 池上跑 1 个已知有 DIVE 分歧的 PR，验证 search-only 路径。
set -euo pipefail
cd "$(dirname "$0")/.."

REPO="${REPO:-marshmallow}"
PR_ID="${PR_ID:-750}"
export REPO PR_ID
FORCE=1 WORKERS=1 NB_CLONES=3 SKIP_POOL_SETUP=1 \
  bash scripts/run_search_only_baseline.sh

out=".cache_dive_search_only_new200/oracles/${REPO}/${PR_ID}/phase2/results.json"
echo "=== smoke result: $out ==="
python3 - <<PY
import json, sys
from pathlib import Path
p = Path("$out")
if not p.exists():
    sys.exit("missing results.json")
d = json.loads(p.read_text())
assert d.get("stage") == "completed", d.get("stage")
assert d.get("search_only") is True, "search_only flag missing"
assert d.get("phase2_strategy") == "dive"
clusters = d.get("dive_clusters") or []
concl = d.get("review_conclusion")
print("review_conclusion:", concl)
print("clusters:", len(clusters))
print("llm_queries:", d.get("llm_queries"))
print("review_traces:", d.get("review_traces"))
if clusters and concl != "BUG":
    sys.exit("expected BUG when clusters present")
if not clusters and concl != "NORMAL":
    sys.exit("expected NORMAL when no clusters")
print("smoke OK")
PY
