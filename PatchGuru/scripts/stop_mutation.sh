#!/usr/bin/env bash
# 停止 run_mutation.sh 启动的 mutation 实验（含后台子进程）。
#
# 用法:
#   bash scripts/stop_mutation.sh
#   bash scripts/stop_mutation.sh --dry-run   # 只列出将杀掉的进程，不实际 kill
set -euo pipefail

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

PATTERN='run_mutation\.sh|scripts/run_mutation\.py|patchguru\.experiments\.MutationAnalysis'

mapfile -t PIDS < <(pgrep -af "$PATTERN" 2>/dev/null | grep -v 'stop_mutation\.sh' | grep -v 'pgrep -af' | awk '{print $1}' || true)

if [[ ${#PIDS[@]} -eq 0 ]]; then
  echo "没有正在运行的 mutation 进程。"
  exit 0
fi

echo "将停止以下 mutation 相关进程:"
pgrep -af "$PATTERN" 2>/dev/null | grep -v 'stop_mutation\.sh' | grep -v 'pgrep -af' || true
echo

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "（dry-run，未发送信号）"
  exit 0
fi

# 先 SIGTERM，再 SIGKILL 残留
kill "${PIDS[@]}" 2>/dev/null || true
sleep 2
mapfile -t LEFT < <(pgrep -f "$PATTERN" 2>/dev/null | grep -v "$$" || true)
if [[ ${#LEFT[@]} -gt 0 ]]; then
  echo "仍有 ${#LEFT[@]} 个进程，发送 SIGKILL ..."
  kill -9 "${LEFT[@]}" 2>/dev/null || true
fi

sleep 1
if pgrep -af "$PATTERN" 2>/dev/null | grep -qv 'stop_mutation\.sh'; then
  echo "WARN: 可能仍有残留进程，请手动检查: ps aux | grep MutationAnalysis"
  exit 1
fi

echo "已全部停止。"
