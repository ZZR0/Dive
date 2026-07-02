#!/usr/bin/env bash
# 首次建环境（每个项目一次）：
#   clone（种子）-> 起临时 build 容器 -> 容器内装依赖（按项目 kind）-> commit 成镜像 patchguru-<project>-dev
# 之后用 expand_clones.sh 从该镜像 + 种子 clone 铺开 clone1..NB_CLONES。
#
# 用法: bash scripts/setup_env.sh <project>
#   支持: pandas | scipy | keras | marshmallow
#   SKIP_EXPAND=1 时仅建种子仓库与镜像，不自动扩容 worker clone。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/common.sh"
source "$HERE/lib/clone.sh"

PROJECT="${1:?用法: $0 <project>}"
require_project "$PROJECT"

KIND="$(project_kind "$PROJECT")"
IMAGE="$(project_image "$PROJECT")"
CNAME="$(container_prefix "$PROJECT")build"
REPO_URL="$(project_repo_url "$PROJECT")"
REPO_DIR="$CLONES_DIR/$SEED_CLONE/$PROJECT"

log "项目=$PROJECT kind=$KIND 镜像=$IMAGE 种子=$SEED_CLONE 构建容器=$CNAME"

# ---- 1) 克隆种子 clone（含完整历史；conda 项目额外拉 submodule） ----
clone_main() {
  if [ -d "$REPO_DIR/.git" ]; then
    log "$SEED_CLONE 已存在，跳过克隆"
    return 0
  fi
  log "克隆 $REPO_URL -> $SEED_CLONE ..."
  mkdir -p "$(dirname "$REPO_DIR")"
  git clone "$REPO_URL" "$REPO_DIR"
  git -C "$REPO_DIR" config http.proxy  "$GIT_PROXY" 2>/dev/null || true
  git -C "$REPO_DIR" config https.proxy "$GIT_PROXY" 2>/dev/null || true
  git -C "$REPO_DIR" fetch --unshallow origin 2>/dev/null || git -C "$REPO_DIR" fetch origin
  if [ "$KIND" = "conda" ]; then
    log "拉取 scipy submodule ..."
    git -C "$REPO_DIR" submodule update --init --recursive
  fi
}

# ---- 2) 容器内装依赖（按 kind 分支） ----
install_deps() {
  local proxy_env=(-e http_proxy="$CONTAINER_PROXY" -e https_proxy="$CONTAINER_PROXY"
                   -e HTTP_PROXY="$CONTAINER_PROXY" -e HTTPS_PROXY="$CONTAINER_PROXY")
  case "$KIND" in
    pure)
      log "安装纯 Python 依赖（editable + coverage）..."
      docker exec -w "/home/$PROJECT" "$CNAME" pip install -q -e '.[dev]' 2>/dev/null \
        || docker exec -w "/home/$PROJECT" "$CNAME" pip install -q -e .
      docker exec -w "/home/$PROJECT" "$CNAME" pip install -q coverage
      ;;
    cext)
      log "安装 C 扩展构建工具并编译（走代理，首次较慢）..."
      docker exec "${proxy_env[@]}" -w "/home/$PROJECT" "$CNAME" \
        pip install -q meson-python ninja cython
      log "安装 requirements-dev.txt ..."
      docker exec -w "/home/$PROJECT" "$CNAME" \
        pip install -q -r requirements-dev.txt 2>/dev/null || true
      log "安装 -e . ..."
      docker exec "${proxy_env[@]}" -w "/home/$PROJECT" "$CNAME" \
        pip install -q -e . --no-build-isolation
      log "安装 coverage ..."
      docker exec -w "/home/$PROJECT" "$CNAME" pip install -q coverage
      ;;
    conda)
      log "安装 conda 环境（用 .devcontainer 安装脚本，走代理，较慢）..."
      docker cp "$ROOT/.devcontainer/setup_scipy_to_run_in_container.sh" "$CNAME:/root/setup.sh"
      docker exec "$CNAME" chmod +x /root/setup.sh
      docker exec "${proxy_env[@]}" -w "/home/$PROJECT" "$CNAME" /root/setup.sh
      ;;
  esac
}

clone_main

log "清理同名旧构建容器并启动 $CNAME ..."
docker rm -f "$CNAME" 2>/dev/null || true
docker run -t -d --name "$CNAME" \
  --add-host=host.docker.internal:host-gateway \
  -v "$REPO_DIR:/home/$PROJECT" "$PY_IMAGE" >/dev/null

install_deps
fix_clone_permissions "$PROJECT" "$SEED_CLONE" "$IMAGE"

log "从 $CNAME commit 镜像 $IMAGE ..."
docker commit "$CNAME" "$IMAGE" >/dev/null
docker rm -f "$CNAME" >/dev/null

log "完成：镜像 $IMAGE 就绪，种子 $SEED_CLONE/$PROJECT 已备好。"
if [ "${SKIP_EXPAND:-0}" = "1" ]; then
  log "SKIP_EXPAND=1，跳过扩容。下一步：NB_CLONES=$NB_CLONES bash scripts/expand_clones.sh $PROJECT"
else
  log "扩容 worker clone1..$NB_CLONES ..."
  NB_CLONES="$NB_CLONES" bash "$HERE/expand_clones.sh" "$PROJECT"
fi
