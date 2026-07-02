#!/usr/bin/env bash
# 跑 PatchGuru new200（703 PR）。用法: bash scripts/run_testora.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export http_proxy=http://127.0.0.1:10810
export https_proxy=http://127.0.0.1:10810
export HTTP_PROXY=http://127.0.0.1:10810
export HTTPS_PROXY=http://127.0.0.1:10810

CASE_FILE="../PatchGuru/scripts/pr_batch_300/new200.txt"
RESULTS_DIR=".results_new200_0626_v1"
CLONES_DIR="${CLONES_DIR:-$(cd .. && pwd)/clones}"

EXTRA=()
[ "${FORCE:-0}" = "1" ] && EXTRA+=(--force)

mkdir -p "$RESULTS_DIR/logs"
cp -f "$CASE_FILE" "$RESULTS_DIR/run_case_file.txt"

# 容器 root 写入 clone 后需 chown，否则 git clean Permission denied（同 PatchGuru）
echo "▶ fix clone permissions"
CLONES_DIR="$CLONES_DIR" bash scripts/fix_clone_permissions.sh

# 代理不稳定时可跳过失败的 fetch，使用本地 clone 继续
export PATCHGURU_GIT_FETCH_OPTIONAL="${PATCHGURU_GIT_FETCH_OPTIONAL:-1}"
export PATCHGURU_GIT_FETCH_RETRIES="${PATCHGURU_GIT_FETCH_RETRIES:-3}"
export PATCHGURU_GIT_FETCH_RETRY_SEC="${PATCHGURU_GIT_FETCH_RETRY_SEC:-5}"

for p in marshmallow pandas scipy keras; do
  echo "▶ $p"
  .venv/bin/python scripts/run_cases.py \
    --case-file "$CASE_FILE" \
    --output-dir "$RESULTS_DIR" \
    --projects "$p" \
    --run-log "$RESULTS_DIR/logs/run_${p}.log" \
    "${EXTRA[@]}" > /dev/null 2>&1 &
done
wait

.venv/bin/python scripts/summarize_cases.py --results-dir "$RESULTS_DIR" --brief
echo "完成"
