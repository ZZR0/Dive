#!/usr/bin/env bash
# 扩容：从已 commit 的镜像 patchguru-<project>-dev + 种子 clone，建 clone1..NB_CLONES 容器。
#   pure  : reference clone
#   cext  : cp -a 种子 clone（含 meson 编译产物，pandas）
#   conda : reference clone + submodule init（scipy；conda 环境在镜像内）
# 已存在的容器会跳过，可重复执行（幂等）。
# FORCE_REFRESH=1 时删除 clone2..N 的目录与容器后重建（用于 scipy 从 cp 方案迁移）。
#
# 用法: NB_CLONES=10 bash scripts/expand_clones.sh <project>
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/common.sh"
source "$HERE/lib/clone.sh"

PROJECT="${1:?用法: $0 <project>}"
require_project "$PROJECT"

FORCE_REFRESH="${FORCE_REFRESH:-0}"
KIND="$(project_kind "$PROJECT")"
IMAGE="$(project_image "$PROJECT")"
PREFIX="$(container_prefix "$PROJECT")"

docker image inspect "$IMAGE" >/dev/null 2>&1 \
  || { log "ERROR: 缺少镜像 $IMAGE，请先运行 setup_env.sh $PROJECT"; exit 1; }
[ -d "$CLONES_DIR/$SEED_CLONE/$PROJECT/.git" ] \
  || { log "ERROR: 缺少 $SEED_CLONE/$PROJECT，请先运行 setup_env.sh $PROJECT"; exit 1; }

PROXY_ENV=(-e http_proxy="$CONTAINER_PROXY" -e https_proxy="$CONTAINER_PROXY"
           -e HTTP_PROXY="$CONTAINER_PROXY" -e HTTPS_PROXY="$CONTAINER_PROXY")

# 准备 clone N 的代码目录
prepare_clone() {
  local idx="$1" repo_dir="$CLONES_DIR/clone$idx/$PROJECT"
  case "$KIND" in
    pure)
      [ -d "$repo_dir/.git" ] || clone_from_reference "$PROJECT" "$repo_dir"
      ;;
    cext)
      copy_built_clone "$PROJECT" "$idx" "$IMAGE"
      ;;
    conda)
      [ -d "$repo_dir/.git" ] || clone_with_submodules "$PROJECT" "$repo_dir" "$IMAGE"
      ;;
  esac
}

# 验证容器内可直接 import（复制方案应零编译）；失败则回退到一次性编译。
verify_container() {
  local cname="$1"
  case "$KIND" in
    pure)
      docker exec -w "/home/$PROJECT" "$cname" python3 -c "import $PROJECT" >/dev/null 2>&1 \
        && return 0
      docker exec -w "/home/$PROJECT" "$cname" pip install -q -e . --no-deps
      ;;
    cext)
      docker exec -w "/home/$PROJECT" "$cname" python3 -c "import $PROJECT" >/dev/null 2>&1 \
        && { log "$cname import $PROJECT OK（复用产物，零编译）"; return 0; }
      log "$cname 复用失败，回退编译（走代理）..."
      docker exec "${PROXY_ENV[@]}" -w "/home/$PROJECT" "$cname" \
        pip install -q -e . --no-build-isolation
      ;;
    conda)
      docker exec -w "/home/$PROJECT" "$cname" bash -c \
        'source /root/conda/etc/profile.d/conda.sh && conda activate scipy-dev && python -c "import scipy"' \
        >/dev/null 2>&1 \
        && { log "$cname import scipy OK（复用产物，零编译）"; return 0; }
      log "$cname 复用失败，回退编译（conda，走代理）..."
      docker exec "${PROXY_ENV[@]}" -w "/home/$PROJECT" "$cname" bash -c \
        'source /root/conda/etc/profile.d/conda.sh && conda activate scipy-dev && pip install -e . --no-build-isolation' \
        && docker exec -w "/home/$PROJECT" "$cname" bash -c \
        'source /root/conda/etc/profile.d/conda.sh && conda activate scipy-dev && python -c "import scipy"'
      ;;
  esac
}

log "扩容 $PROJECT 到 $NB_CLONES 个（镜像=$IMAGE kind=$KIND force_refresh=$FORCE_REFRESH）"

if [ "$FORCE_REFRESH" = "1" ]; then
  state_file="$CLONES_DIR/clone_state_${PROJECT}.json"
  if [ -f "$state_file" ]; then
    rm -f "$state_file"
    log "已清除 $state_file（避免旧 commit 缓存）"
  fi
  # scipy 种子 clone 不删除，仅重置 git/submodule 状态
  if [ "$PROJECT" = "scipy" ] && [ -d "$CLONES_DIR/$SEED_CLONE/scipy/.git" ]; then
    log "修复 $SEED_CLONE/scipy git+submodule 状态（不重建目录）..."
    repo="$CLONES_DIR/$SEED_CLONE/scipy"
    git -C "$repo" submodule deinit -f --all 2>/dev/null || true
    git -C "$repo" reset --hard
    git -C "$repo" clean -fdx
    git -C "$repo" checkout main
    git -C "$repo" submodule sync --recursive
    git -C "$repo" submodule update --init --recursive --jobs 4
    fix_clone_permissions "$PROJECT" "$SEED_CLONE" "$IMAGE"
    log "$SEED_CLONE/scipy 已修复"
  fi
fi

for idx in $(seq 1 "$NB_CLONES"); do
  cname="${PREFIX}${idx}"
  repo_dir="$CLONES_DIR/clone${idx}/$PROJECT"
  if docker ps -a --format '{{.Names}}' | grep -qx "$cname"; then
    if [ "$FORCE_REFRESH" = "1" ]; then
      log "FORCE_REFRESH: 删除 $cname 与 $repo_dir"
      docker rm -f "$cname" 2>/dev/null || true
      rm -rf "$repo_dir" 2>/dev/null || \
        docker run --rm -v "$CLONES_DIR:/clones" "$PY_IMAGE" \
          rm -rf "/clones/clone${idx}/${PROJECT}"
    else
      log "跳过 $cname（已存在）"
      continue
    fi
  elif [ "$FORCE_REFRESH" = "1" ] && [ -d "$repo_dir" ]; then
    log "FORCE_REFRESH: 删除残留 $repo_dir"
    rm -rf "$repo_dir" 2>/dev/null || \
      docker run --rm -v "$CLONES_DIR:/clones" "$PY_IMAGE" \
        rm -rf "/clones/clone${idx}/${PROJECT}"
  fi

  log "=== clone$idx + $cname ==="
  prepare_clone "$idx"
  docker run -t -d --name "$cname" \
    --add-host=host.docker.internal:host-gateway \
    -v "$CLONES_DIR/clone$idx/$PROJECT:/home/$PROJECT" "$IMAGE" >/dev/null

  if verify_container "$cname"; then
    fix_clone_permissions "$PROJECT" "clone${idx}" "$IMAGE"
    log "$cname 就绪"
  else
    log "WARN: $cname 环境准备失败，删除以便重试"
    docker rm -f "$cname" 2>/dev/null || true
  fi
done

log "当前 $PROJECT 容器: $(docker_count "$PROJECT") 个"
docker ps --filter "name=${PREFIX}" --format '{{.Names}}' | sort -V | tr '\n' ' '; echo
