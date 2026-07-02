#!/usr/bin/env bash
# 通用目标项目 Docker 设置: setup_project_docker.sh <project> <github_org/repo> <python_image>
set -euo pipefail

PROJECT="${1:?用法: $0 <project> <org/repo> <python_image>}"
REPO_SLUG="${2:?}"
PY_IMAGE="${3:-python:3.11}"
GIT_PROXY="${GIT_PROXY:-socks5h://127.0.0.1:10808}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLONES_DIR="$(cd "$ROOT/.." && pwd)/clones"
CONTAINER_PREFIX="${PROJECT}-dev"
REPO_URL="https://github.com/${REPO_SLUG}.git"
# shellcheck source=clone_helpers.sh
source "$ROOT/scripts/clone_helpers.sh"

echo "[setup] 项目: $PROJECT | 仓库: $REPO_SLUG | 镜像: $PY_IMAGE"
mkdir -p "$CLONES_DIR"

for i in 1 2 3; do
  docker rm -f "${CONTAINER_PREFIX}${i}" 2>/dev/null || true
done

setup_clone() {
  local idx="$1"
  local cname="${CONTAINER_PREFIX}${idx}"
  local dir="$CLONES_DIR/clone${idx}"

  echo "[setup] === clone${idx} (${cname}) ==="
  mkdir -p "$dir"
  cd "$dir"

  if [ ! -d "$PROJECT/.git" ]; then
    echo "[setup] 克隆 $REPO_SLUG ..."
    git clone "https://github.com/${REPO_SLUG}.git" "$PROJECT"
  fi
  cd "$PROJECT"
  git config http.proxy "$GIT_PROXY" 2>/dev/null || true
  git config https.proxy "$GIT_PROXY" 2>/dev/null || true
  echo "[setup] 拉取完整历史..."
  git fetch --unshallow origin 2>/dev/null || git fetch origin

  echo "[setup] 启动容器 ${cname} ..."
  docker run -t -d --name "$cname" -v "${PWD}:/home/${PROJECT}" "$PY_IMAGE"

  echo "[setup] 安装项目依赖..."
  if [ "$PROJECT" = "pandas" ]; then
    docker exec -w "/home/${PROJECT}" "$cname" pip install -q meson-python mesonpy ninja cython
    docker exec -w "/home/${PROJECT}" "$cname" pip install -q -r requirements-dev.txt 2>/dev/null || true
    docker exec -w "/home/${PROJECT}" "$cname" pip install -q -e . --no-build-isolation
  elif [ "$PROJECT" = "keras" ]; then
    docker exec -w "/home/${PROJECT}" "$cname" pip install -q -r requirements.txt
    docker exec -w "/home/${PROJECT}" "$cname" pip install -q -e .
  else
    docker exec -w "/home/${PROJECT}" "$cname" pip install -q -e '.[dev]' 2>/dev/null || \
      docker exec -w "/home/${PROJECT}" "$cname" pip install -q -e .
  fi
  docker exec -w "/home/${PROJECT}" "$cname" pip install -q coverage
  echo "[setup] ${cname} 就绪"
  cd "$CLONES_DIR"
}

setup_clone 1
for idx in 2 3; do
  echo "[setup] 从 clone1 复制 git 仓库到 clone${idx} ..."
  clone_repo_from_reference "$PROJECT" "$CLONES_DIR/clone${idx}/${PROJECT}" "$REPO_URL"
  setup_clone "$idx"
done

echo "[setup] $PROJECT 全部容器:"
docker ps --filter "name=${CONTAINER_PREFIX}" --format '{{.Names}}'
