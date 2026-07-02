#!/usr/bin/env bash
# 用法: bash scripts/collect_pr.sh pandas
#       bash scripts/collect_pr.sh marshmallow 2500   # 从 PR#2500 往下扫
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${1:?用法: bash scripts/collect_pr.sh <pandas|scipy|keras|marshmallow> [max_pr]}"
MAX_PR="${2:-}"
mkdir -p scripts/pr_batch_300/logs

export PATCHGURU_CLONE_ID=collect
export PATCHGURU_HOST_PROXY=http://127.0.0.1:10810

EXTRA=()
[ -n "$MAX_PR" ] && EXTRA+=(--max-pr "$MAX_PR")

PYTHONUNBUFFERED=1 uv run python -m patchguru.experiments.PRCollector \
  -p "$PROJECT" \
  -n 300 \
  --out-dir scripts/pr_batch_300 \
  "${EXTRA[@]}" \
  2>&1 | tee "scripts/pr_batch_300/logs/collect_${PROJECT}.log"
