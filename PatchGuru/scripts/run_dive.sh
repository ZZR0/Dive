#!/usr/bin/env bash
# baseline 上跑 DIVE phase2。
# 用法: bash scripts/run_dive.sh [baseline_cache] [dive_cache]
#       PR_FILE=scripts/pr_batch_300/new100.txt BASELINE_CACHE=.cache_baseline_new100 DIVE_CACHE=.cache_dive_new100 bash scripts/run_dive.sh
# 续跑: SKIP_PREPARE=1 bash scripts/run_dive.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export PATCHGURU_HOST_PROXY=http://127.0.0.1:10810
export GIT_PROXY=http://127.0.0.1:10810
export http_proxy=http://127.0.0.1:10810 https_proxy=http://127.0.0.1:10810

BASELINE_CACHE="${1:-${BASELINE_CACHE:-.cache_rerun_0612_v2}}"
DIVE_CACHE="${2:-${DIVE_CACHE:-.cache_dive_0612_v2}}"
INPUT_PR_FILE="${PR_FILE:-}"
PR_FILE="$DIVE_CACHE/dive_prs.txt"

mkdir -p "$DIVE_CACHE/logs"

if [[ "${SKIP_PREPARE:-0}" != "1" ]]; then
  PREP_ARGS=(--baseline-cache "$BASELINE_CACHE" --dive-cache "$DIVE_CACHE")
  [ -n "$INPUT_PR_FILE" ] && PREP_ARGS+=(--pr-file "$INPUT_PR_FILE")
  uv run python scripts/prepare_dive_cache.py "${PREP_ARGS[@]}"
elif [[ ! -f "$PR_FILE" ]]; then
  PR_FILE="${INPUT_PR_FILE:-}"
  if [[ ! -f "$PR_FILE" ]]; then
    echo "ERROR: dive_prs.txt 不存在，请先 prepare 或设置 PR_FILE" >&2
    exit 1
  fi
fi

RUN_ARGS=(
  --phase2-strategy dive
  --dive-seed-baseline-dir "$BASELINE_CACHE"
  --cache-dir "$DIVE_CACHE"
  --pr-file "$PR_FILE"
  --workers 3 --nb-clones 3
  --timeout "${PR_TIMEOUT:-3600}"
)

for p in marshmallow pandas scipy keras; do
  echo "▶ $p → $DIVE_CACHE/logs/run_${p}.log"
  uv run python scripts/run_all_specinfer.py \
    --projects "$p" "${RUN_ARGS[@]}" \
    >"$DIVE_CACHE/logs/run_${p}.log" 2>&1 &
done
wait

uv run python scripts/summarize_results.py --cache-dir "$DIVE_CACHE"
uv run python scripts/compare_baseline_dive.py \
  --baseline-cache "$BASELINE_CACHE" --dive-cache "$DIVE_CACHE"
echo "完成"
