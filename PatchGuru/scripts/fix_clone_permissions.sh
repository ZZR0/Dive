#!/usr/bin/env bash
# 一次性把现有 clones 里容器 root 写入的文件 chown 回宿主机用户。
# 用法: bash scripts/fix_clone_permissions.sh [project ...]
#   缺省处理全部支持项目、clone1..NB_CLONES。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/common.sh"
source "$HERE/lib/clone.sh"

PROJECTS=("$@")
[ ${#PROJECTS[@]} -eq 0 ] && PROJECTS=("${SUPPORTED_PROJECTS[@]}")

log "开始修复 clone 目录权限（NB_CLONES=${NB_CLONES}）..."
for project in "${PROJECTS[@]}"; do
  require_project "$project"
  image="$(project_image "$project")"
  for idx in $(seq 1 "$NB_CLONES"); do
    clone_id="clone${idx}"
    repo_dir="$CLONES_DIR/${clone_id}/${project}"
    if [ ! -d "$repo_dir" ]; then
      continue
    fi
    fix_clone_permissions "$project" "$clone_id" "$image"
  done
  log "${project} 全部 clone 权限已修复"
done
log "完成"
