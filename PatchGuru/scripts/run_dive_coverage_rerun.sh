#!/usr/bin/env bash
# Re-measure DIVE changed-line coverage (embedded post_<fn> offset fix).
#
# 用法:
#   bash scripts/run_dive_coverage_rerun.sh
#   REPO=marshmallow PR_ID=1354 FORCE=1 bash scripts/run_dive_coverage_rerun.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export PATCHGURU_HOST_PROXY=http://127.0.0.1:10810
export GIT_PROXY=http://127.0.0.1:10810
export http_proxy=http://127.0.0.1:10810
export https_proxy=http://127.0.0.1:10810
export PATCHGURU_GIT_FETCH_OPTIONAL=1
export PATCHGURU_GIT_FETCH_RETRIES=5
export PATCHGURU_GIT_FETCH_RETRY_SEC=10

REPO_ROOT="$(cd .. && pwd)"
export PATCHGURU_CLONES_DIR="${PATCHGURU_CLONES_DIR:-$REPO_ROOT/clones_pgabl}"
export PATCHGURU_CONTAINER_TAG="${PATCHGURU_CONTAINER_TAG:-pgabl}"
export PYTHONPATH="$PWD/mutmut${PYTHONPATH:+:$PYTHONPATH}"

DIVE_CACHE="${DIVE_CACHE:-.cache_dive_new200}"
mkdir -p "$DIVE_CACHE/coverage_analysis/logs"

ARGS=(--cache-dir "$DIVE_CACHE" --pr-file scripts/pr_batch_300/new200.txt)
[[ -n "${PR_ID:-}" ]] && ARGS+=(--pr-id "$PR_ID")
[[ -n "${FORCE:-}" ]] && ARGS+=(--force)

if [[ -n "${REPO:-}" ]]; then
  PROJECTS=("$REPO")
else
  PROJECTS=(marshmallow pandas scipy keras)
fi

echo "DIVE coverage rerun | cache=$DIVE_CACHE | clones=$PATCHGURU_CLONES_DIR"

for p in "${PROJECTS[@]}"; do
  echo "▶ $p → $DIVE_CACHE/coverage_analysis/logs/run_${p}.log"
  uv run python -m patchguru.experiments.DiveCoverageAnalysis --repo "$p" "${ARGS[@]}" \
    >"$DIVE_CACHE/coverage_analysis/logs/run_${p}.log" 2>&1 &
done
wait

echo "完成。汇总: uv run python scripts/summarize_rq4_coverage.py"
