#!/usr/bin/env bash
# Baseline phase-2 改动行覆盖率（RQ4 对比用），在 Docker 内 replay specification.py。
#
# 用法:
#   bash scripts/run_baseline_coverage.sh
#   REPO=pandas PR_ID=64183 bash scripts/run_baseline_coverage.sh
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

mkdir -p .cache_baseline_new200/coverage_analysis/logs

ARGS=(--cache-dir .cache_baseline_new200 --phase phase2 --pr-file scripts/pr_batch_300/new200.txt)
[[ -n "${PR_ID:-}" ]] && ARGS+=(--pr-id "$PR_ID")
[[ -n "${FORCE:-}" ]] && ARGS+=(--force)

if [[ -n "${REPO:-}" ]]; then
  PROJECTS=("$REPO")
else
  PROJECTS=(marshmallow pandas scipy keras)
fi

echo "baseline coverage | clones=$PATCHGURU_CLONES_DIR | tag=$PATCHGURU_CONTAINER_TAG"

for p in "${PROJECTS[@]}"; do
  echo "▶ $p → .cache_baseline_new200/coverage_analysis/logs/run_${p}.log"
  uv run python -m patchguru.experiments.BaselineCoverageAnalysis --repo "$p" "${ARGS[@]}" \
    >".cache_baseline_new200/coverage_analysis/logs/run_${p}.log" 2>&1 &
done
wait

echo "完成。汇总: uv run python scripts/summarize_rq4_coverage.py"
