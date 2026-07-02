#!/usr/bin/env bash
# 等待指定项目的容器池就绪（达到 NB_CLONES），然后启动批量 SpecInfer。
# 合并了原 run_remaining.sh / run_all_when_ready.sh 的功能。
#
# 用法:
#   bash scripts/watch_and_run.sh                  # 等全部项目就绪后一起跑
#   bash scripts/watch_and_run.sh pandas scipy     # 只等并跑指定项目
# 环境变量:
#   NB_CLONES     容器池目标大小（默认 10）
#   WORKERS       并行 worker 数（默认 = NB_CLONES）
#   PR_TIMEOUT    单个 PR 超时秒数（默认 1800）
#   WAIT_TIMEOUT  每个项目等待容器的超时秒数（默认 0 = 无限等）
#   FORCE         设为 1 则忽略缓存强制重跑（重新调用 LLM）
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/common.sh"

PROJECTS=("$@")
[ ${#PROJECTS[@]} -eq 0 ] && PROJECTS=("${SUPPORTED_PROJECTS[@]}")

WORKERS="${WORKERS:-$NB_CLONES}"
PR_TIMEOUT="${PR_TIMEOUT:-1800}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-0}"
FORCE="${FORCE:-0}"
mkdir -p "$LOG_DIR"

# 宿主机 GitHub HTTP（PyGithub / PR diff）走代理
if [ -n "${PATCHGURU_HOST_PROXY:-}" ]; then
  export http_proxy="$PATCHGURU_HOST_PROXY" https_proxy="$PATCHGURU_HOST_PROXY"
  export HTTP_PROXY="$PATCHGURU_HOST_PROXY" HTTPS_PROXY="$PATCHGURU_HOST_PROXY"
  export GIT_PROXY="${GIT_PROXY:-$PATCHGURU_HOST_PROXY}"
  log "宿主机 GitHub 代理: $PATCHGURU_HOST_PROXY"
fi

wait_project() {
  local project="$1"
  local deadline=0
  [ "$WAIT_TIMEOUT" -gt 0 ] && deadline=$(( $(date +%s) + WAIT_TIMEOUT ))
  log "等待 $project 容器（目标 $NB_CLONES，当前 $(docker_count "$project")）..."
  while [ "$(docker_count "$project")" -lt "$NB_CLONES" ]; do
    if [ "$deadline" -gt 0 ] && [ "$(date +%s)" -ge "$deadline" ]; then
      log "WARN: $project 等待超时，当前仅 $(docker_count "$project") 个容器，继续后续流程"
      return 0
    fi
    sleep 30
  done
  log "$project 容器已就绪（$NB_CLONES 个）"
}

for p in "${PROJECTS[@]}"; do
  require_project "$p"
  wait_project "$p"
done

EXTRA=()
[ "$FORCE" = "1" ] && EXTRA+=(--force)

log "全部目标项目就绪，启动批量 SpecInfer（workers=$WORKERS, pr_timeout=${PR_TIMEOUT}s, force=$FORCE）"
exec uv run python "$SCRIPTS_DIR/run_all_specinfer.py" \
  --projects "${PROJECTS[@]}" \
  --workers "$WORKERS" \
  --nb-clones "$NB_CLONES" \
  --timeout "$PR_TIMEOUT" \
  "${EXTRA[@]}"
