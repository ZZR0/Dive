#!/usr/bin/env bash
# 等待 keras/pandas/scipy Docker 就绪后，自动续跑剩余项目（marshmallow 可先行）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

docker_count() {
  local prefix="$1"
  docker ps --filter "name=${prefix}" --format '{{.Names}}' 2>/dev/null | grep -c "^${prefix}" || true
}

NB_CLONES="${NB_CLONES:-10}"
WORKERS="${WORKERS:-10}"

wait_project() {
  local name="$1" prefix="$2"
  while [ "$(docker_count "$prefix")" -lt "$NB_CLONES" ]; do
    log "等待 ${name} Docker (${prefix}1..${NB_CLONES})，当前 $(docker_count "$prefix") 个 ..."
    sleep 120
  done
  log "${name} Docker 已就绪 (${NB_CLONES} 个)"
}

log "续跑调度器启动 — 等待 keras / pandas / scipy Docker (目标 ${NB_CLONES} 容器)"
wait_project keras keras-dev
wait_project pandas pandas-dev
wait_project scipy scipy-dev

log "全部 Docker 就绪，启动剩余项目 SpecInfer (workers=${WORKERS}) ..."
exec uv run python scripts/run_all_specinfer.py --projects pandas scipy keras --workers "$WORKERS" --nb-clones "$NB_CLONES"
