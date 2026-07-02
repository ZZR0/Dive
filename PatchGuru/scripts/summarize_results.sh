#!/usr/bin/env bash
# 统计批量实验结果。
#   bash scripts/summarize_results.sh .cache_baseline_new200
#   PR_FILE=scripts/pr_batch_300/new200.txt bash scripts/summarize_results.sh .cache_baseline_new200
#   bash scripts/summarize_results.sh .cache_baseline_new200 --show-incomplete
set -euo pipefail
cd "$(dirname "$0")/.."

CACHE="${CACHE_DIR:-.cache_rerun_0612}"
PR_FILE="${PR_FILE:-}"
[ $# -ge 1 ] && [[ "$1" != --* ]] && CACHE="$1" && shift

EXTRA=()
[ -n "$PR_FILE" ] && EXTRA+=(--pr-file "$PR_FILE")

uv run python scripts/summarize_results.py --cache-dir "$CACHE" "${EXTRA[@]}" "$@"
