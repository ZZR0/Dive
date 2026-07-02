#!/usr/bin/env bash
# 把 Testora clones 里容器 root 写入的文件 chown 回宿主机用户（对齐 PatchGuru fix_clone_permissions.sh）。
#
# 用法:
#   bash scripts/fix_clone_permissions.sh                  # ../clones, 四库 clone1..3
#   CLONES_DIR=/nvme/zzr/patch_reach/clones bash scripts/fix_clone_permissions.sh pandas
set -euo pipefail
cd "$(dirname "$0")/.."

CLONES_DIR="${CLONES_DIR:-$(cd .. && pwd)/clones}"
NB_CLONES="${NB_CLONES:-3}"
PROJECTS=("$@")
[ ${#PROJECTS[@]} -eq 0 ] && PROJECTS=(marshmallow pandas scipy keras)
PY_IMAGE="${PY_IMAGE:-python:3.11}"
UIDGID="$(id -u):$(id -g)"

project_image() {
  case "$1" in
    marshmallow|pandas|scipy|keras|numpy) echo "${1}-dev" ;;
    *) echo "$PY_IMAGE" ;;
  esac
}

fix_one() {
  local project="$1" clone_id="$2" image
  image="$(project_image "$project")"
  local repo_dir="${CLONES_DIR}/${clone_id}/${project}"
  [ -d "$repo_dir" ] || return 0
  echo "[perms] chown ${clone_id}/${project} -> ${UIDGID} (image=${image})"
  if docker run --rm -v "${CLONES_DIR}:/clones" "$image" \
      chown -R "$UIDGID" "/clones/${clone_id}/${project}" 2>/dev/null; then
    return 0
  fi
  docker run --rm -v "${CLONES_DIR}:/clones" "$PY_IMAGE" \
    chown -R "$UIDGID" "/clones/${clone_id}/${project}"
}

for project in "${PROJECTS[@]}"; do
  for idx in $(seq 1 "$NB_CLONES"); do
    fix_one "$project" "clone${idx}"
  done
  echo "[perms] ${project} done"
done
echo "完成"
