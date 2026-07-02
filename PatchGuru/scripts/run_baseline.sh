#!/usr/bin/env bash
# 4 项目 × 3 容器并行。
# 用法: bash scripts/run_baseline.sh
#       PR_FILE=scripts/pr_batch_300/new100.txt CACHE_DIR=.cache_baseline_new100 bash scripts/run_baseline.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export PATCHGURU_HOST_PROXY=http://127.0.0.1:10810
export GIT_PROXY=http://127.0.0.1:10810
export http_proxy=http://127.0.0.1:10810 https_proxy=http://127.0.0.1:10810

CACHE_DIR="${CACHE_DIR:-.cache_rerun_0612_v2}"
PR_FILE="${PR_FILE:-}"
mkdir -p "$CACHE_DIR/logs"

EXTRA=()
[ "${FORCE:-0}" = "1" ] && EXTRA+=(--force)
if [ -n "$PR_FILE" ]; then
  EXTRA+=(--pr-file "$PR_FILE")
  cp -f "$PR_FILE" "$CACHE_DIR/run_pr_file.txt"
fi

for p in marshmallow pandas scipy keras; do
  echo "▶ $p → $CACHE_DIR/logs/run_${p}.log"
  uv run python scripts/run_all_specinfer.py \
    --projects "$p" --workers 3 --nb-clones 3 \
    --timeout "${PR_TIMEOUT:-2400}" --cache-dir "$CACHE_DIR" \
    "${EXTRA[@]}" >"$CACHE_DIR/logs/run_${p}.log" 2>&1 &
done
wait
echo "完成"
