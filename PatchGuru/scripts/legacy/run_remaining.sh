#!/usr/bin/env bash
# 等待 pandas / scipy 容器就绪后，自动以实际容器数启动对应批量 SpecInfer。
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

log() { echo "[$(date '+%F %T')] $*"; }
count() { docker ps --filter "name=$1-dev" --format '{{.Names}}' | grep -c "^$1-dev" || true; }

start_batch() {
  local project="$1" target="$2" timeout_s="$3"
  local deadline=$(( $(date +%s) + timeout_s ))
  log "等待 ${project} 容器（目标 ${target}，超时 $((timeout_s/60)) 分钟）..."
  while [ "$(count "$project")" -lt "$target" ] && [ "$(date +%s)" -lt "$deadline" ]; do
    sleep 30
  done
  local w; w="$(count "$project")"
  if [ "$w" -lt 1 ]; then
    log "ERROR: ${project} 无可用容器，跳过"
    return 1
  fi
  log "${project} 就绪 ${w} 个容器，启动批量（workers=${w}）"
  nohup uv run python scripts/run_all_specinfer.py \
    --projects "$project" --workers "$w" --nb-clones 10 \
    > "scripts/run_${project}.log" 2>&1 &
  log "${project} 批量 pid=$!"
}

start_batch pandas 10 1800
start_batch scipy 10 5400
log "pandas / scipy 批量均已调度"
