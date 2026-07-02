#!/usr/bin/env bash
# 容器生命周期管理：扩容是一次性、永久可重用的资源（clone 目录 + 镜像 + 容器对象）。
# 容器创建后【不删除】，用 up 启动、down 停止，从而反复复用，平时不占运行资源。
#
# 用法:
#   bash scripts/containers.sh up   <project|all> [N]   # 启动 dev1..devN（缺失则按镜像+clone 创建）
#   bash scripts/containers.sh down <project|all>       # 停止该项目所有容器（保留，不删除）
#   bash scripts/containers.sh status [project|all]     # 查看容器状态
#
# 说明:
#   - up 会保证 clone1..cloneN 都对应一个容器 <project>-dev{i}（含历史上缺失的 dev1）。
#   - 已存在但已停止的容器用 docker start 复用；完全缺失的才按镜像+clone 目录新建。
#   - 缺 clone 目录 -> 提示先 expand_clones.sh；缺镜像 -> 提示先 setup_env.sh。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/common.sh"

ACTION="${1:?用法: $0 up|down|status <project|all> [N]}"
TARGET="${2:-all}"

# 解析目标项目列表
projects_for() {
  if [ "$1" = "all" ]; then
    printf '%s\n' "${SUPPORTED_PROJECTS[@]}"
  else
    require_project "$1"
    echo "$1"
  fi
}

container_exists()  { docker ps -a --format '{{.Names}}' | grep -qx "$1"; }
container_running() { docker ps    --format '{{.Names}}' | grep -qx "$1"; }

# 启动单个容器：running 跳过；stopped 则 start；缺失则按镜像+clone 新建。
up_one() {
  local project="$1" idx="$2"
  local cname; cname="$(container_prefix "$project")${idx}"
  local repo_dir="$CLONES_DIR/clone${idx}/$project"
  local image; image="$(project_image "$project")"

  if container_running "$cname"; then
    return 0
  fi
  if container_exists "$cname"; then
    docker start "$cname" >/dev/null && log "▶ start $cname"
    return $?
  fi
  if [ ! -d "$repo_dir/.git" ]; then
    log "WARN: 缺少 $repo_dir（先扩容: NB_CLONES=$NB_CLONES bash scripts/expand_clones.sh $project）"
    return 1
  fi
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    log "WARN: 缺少镜像 $image（先建环境: bash scripts/setup_env.sh $project）"
    return 1
  fi
  docker run -t -d --name "$cname" \
    --add-host=host.docker.internal:host-gateway \
    -v "$repo_dir:/home/$project" "$image" >/dev/null \
    && log "✚ create+run $cname"
}

up_project() {
  local project="$1" n="$2"
  log "=== up $project (dev1..dev${n}) ==="
  local ok=0
  for idx in $(seq 1 "$n"); do
    [ -d "$CLONES_DIR/clone${idx}/$project/.git" ] || continue
    up_one "$project" "$idx" && ok=$((ok+1))
  done
  log "$project 运行中容器: $(docker_count "$project") 个"
}

down_project() {
  local project="$1"
  local prefix; prefix="$(container_prefix "$project")"
  local names; names="$(docker ps --format '{{.Names}}' | grep "^${prefix}" || true)"
  if [ -z "$names" ]; then
    log "$project 无运行中容器"
    return 0
  fi
  echo "$names" | xargs -r docker stop >/dev/null
  log "⏹ 已停止 $project: $(echo "$names" | tr '\n' ' ')"
}

status_project() {
  local project="$1"
  local prefix; prefix="$(container_prefix "$project")"
  printf '%-14s 运行中=%s  全部=' "$project" "$(docker_count "$project")"
  docker ps -a --format '{{.Names}} ({{.Status}})' | grep "^${prefix}" \
    | sort -V | tr '\n' ' '; echo
}

case "$ACTION" in
  up)
    N="${3:-$NB_CLONES}"
    while IFS= read -r p; do up_project "$p" "$N"; done < <(projects_for "$TARGET")
    ;;
  down)
    while IFS= read -r p; do down_project "$p"; done < <(projects_for "$TARGET")
    ;;
  status)
    while IFS= read -r p; do status_project "$p"; done < <(projects_for "$TARGET")
    ;;
  *)
    log "ERROR: 未知动作 '$ACTION'（up|down|status）"; exit 1 ;;
esac
