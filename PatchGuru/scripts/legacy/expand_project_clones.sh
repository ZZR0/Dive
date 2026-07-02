#!/usr/bin/env bash
# 将某项目的 clone/容器 扩容到 NB_CLONES（默认 10），不删除已有 clone1..N
# 用法: NB_CLONES=10 bash scripts/expand_project_clones.sh marshmallow
set -euo pipefail

PROJECT="${1:?用法: $0 <project> [org/repo] [python_image]}"
REPO_SLUG="${2:-}"
PY_IMAGE="${3:-python:3.11}"
NB_CLONES="${NB_CLONES:-10}"
GIT_PROXY="${GIT_PROXY:-socks5h://127.0.0.1:10808}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLONES_DIR="$(cd "$ROOT/.." && pwd)/clones"
CONTAINER_PREFIX="${PROJECT}-dev"
# shellcheck source=clone_helpers.sh
source "$ROOT/scripts/clone_helpers.sh"

case "$PROJECT" in
  marshmallow) REPO_SLUG="${REPO_SLUG:-marshmallow-code/marshmallow}" ;;
  pandas)      REPO_SLUG="${REPO_SLUG:-pandas-dev/pandas}" ;;
  scipy)       REPO_SLUG="${REPO_SLUG:-scipy/scipy}" ;;
  keras)       REPO_SLUG="${REPO_SLUG:-keras-team/keras}" ;;
  *)           REPO_SLUG="${REPO_SLUG:?请提供 GitHub 仓库 org/repo}" ;;
esac

REPO_URL="https://github.com/${REPO_SLUG}.git"
SCIPY_IMAGE="patchguru-scipy-dev"
PANDAS_IMAGE="patchguru-pandas-dev"
KERAS_IMAGE="patchguru-keras-dev"
# 容器内经宿主机 mihomo 代理联网（用于 build 时下载依赖/submodule）
CONTAINER_PROXY="${CONTAINER_PROXY:-http://host.docker.internal:10810}"

ensure_dev_image() {
  local image="$1" container="$2"
  if docker image inspect "$image" >/dev/null 2>&1; then
    return 0
  fi
  if docker ps -a --format '{{.Names}}' | grep -qx "$container"; then
    echo "[expand] 从 ${container} 创建镜像 ${image} ..."
    docker commit "$container" "$image" >/dev/null
  fi
}

echo "[expand] 项目=${PROJECT} 目标 clone 数=${NB_CLONES}"

case "$PROJECT" in
  scipy)  ensure_dev_image "$SCIPY_IMAGE" scipy-dev1 ;;
  pandas) ensure_dev_image "$PANDAS_IMAGE" pandas-dev1 ;;
  keras)  ensure_dev_image "$KERAS_IMAGE" keras-dev1 ;;
esac

if [ ! -d "$CLONES_DIR/clone1/${PROJECT}/.git" ]; then
  echo "[expand] ERROR: 缺少 $CLONES_DIR/clone1/${PROJECT}，请先运行 setup 脚本"
  exit 1
fi

HOST_UIDGID="$(id -u):$(id -g)"

# C 扩展项目（pandas/scipy）"只 build 一次"的核心：
# clone1 在 dev1 里已编译出 build 产物（在挂载的宿主机目录，docker commit 不会保存）。
# 这里用 root 容器把 clone1 完整目录（含已编译 build 产物 + submodule）复制到新 clone，
# 并 chown 回宿主机用户（宿主机 GitPython checkout 需写权限，容器内 root 仍可读写）。
# 新容器挂载后直接 import，无需重新编译。
copy_built_clone() {
  local idx="$1" image="$2"
  echo "[expand] 复制 clone1/${PROJECT}（含已编译 C 扩展产物）-> clone${idx}（免重复编译）..."
  docker run --rm -v "$CLONES_DIR:/clones" "$image" bash -c \
    "rm -rf /clones/clone${idx}/${PROJECT} && mkdir -p /clones/clone${idx} && cp -a /clones/clone1/${PROJECT} /clones/clone${idx}/${PROJECT} && chown -R ${HOST_UIDGID} /clones/clone${idx}/${PROJECT}"
}

# 验证容器内可直接 import（复制 build 产物后应零编译）；万一失败再回退到一次性 build。
verify_or_build() {
  local cname="$1"
  echo "[expand] 容器 ${cname} 验证环境（复用镜像 + build 产物）..."
  case "$PROJECT" in
    keras|marshmallow)
      docker exec -w "/home/${PROJECT}" "$cname" python3 -c "import ${PROJECT}" >/dev/null 2>&1 \
        && { echo "[expand] ${cname} import ${PROJECT} OK（零重装）"; return 0; }
      docker exec -w "/home/${PROJECT}" "$cname" pip install -q -e . --no-deps
      ;;
    pandas)
      docker exec -w "/home/pandas" "$cname" python3 -c "import pandas" >/dev/null 2>&1 \
        && { echo "[expand] ${cname} import pandas OK（复用 build 产物，零编译）"; return 0; }
      echo "[expand] ${cname} 复用失败，回退编译（走代理）..."
      docker exec \
        -e http_proxy="$CONTAINER_PROXY" -e https_proxy="$CONTAINER_PROXY" \
        -e HTTP_PROXY="$CONTAINER_PROXY" -e HTTPS_PROXY="$CONTAINER_PROXY" \
        -w "/home/pandas" "$cname" pip install -q -e . --no-build-isolation
      ;;
    scipy)
      docker exec -w "/home/scipy" "$cname" bash -c \
        'source /root/conda/etc/profile.d/conda.sh && conda activate scipy-dev && python -c "import scipy"' >/dev/null 2>&1 \
        && { echo "[expand] ${cname} import scipy OK（复用 build 产物，零编译）"; return 0; }
      echo "[expand] ${cname} 复用失败，回退编译（conda，走代理）..."
      docker exec \
        -e http_proxy="$CONTAINER_PROXY" -e https_proxy="$CONTAINER_PROXY" \
        -e HTTP_PROXY="$CONTAINER_PROXY" -e HTTPS_PROXY="$CONTAINER_PROXY" \
        -w "/home/scipy" "$cname" bash -c \
        'source /root/conda/etc/profile.d/conda.sh && conda activate scipy-dev && pip install -e . --no-build-isolation'
      ;;
  esac
}

for idx in $(seq 1 "$NB_CLONES"); do
  cname="${CONTAINER_PREFIX}${idx}"
  clone_dir="$CLONES_DIR/clone${idx}"
  repo_dir="$clone_dir/${PROJECT}"

  if docker ps -a --format '{{.Names}}' | grep -qx "$cname"; then
    echo "[expand] 跳过 ${cname}（已存在）"
    continue
  fi

  echo "[expand] === 创建 clone${idx} + ${cname} ==="
  mkdir -p "$clone_dir"

  image="$PY_IMAGE"
  case "$PROJECT" in
    scipy)  docker image inspect "$SCIPY_IMAGE" >/dev/null 2>&1 && image="$SCIPY_IMAGE" ;;
    pandas) docker image inspect "$PANDAS_IMAGE" >/dev/null 2>&1 && image="$PANDAS_IMAGE" ;;
    keras)  docker image inspect "$KERAS_IMAGE" >/dev/null 2>&1 && image="$KERAS_IMAGE" ;;
  esac

  case "$PROJECT" in
    pandas|scipy)
      # clone1 是构建源（dev1 已编译）；其余直接复制 clone1（含编译产物），不再逐个编译
      if [ "$idx" -eq 1 ]; then
        [ -d "$repo_dir/.git" ] || { echo "[expand] ERROR: clone1/${PROJECT} 不存在，无法作为构建源"; exit 1; }
      else
        copy_built_clone "$idx" "$image"
      fi
      ;;
    *)
      # 纯 Python：本地 reference clone 即可（无 C 扩展产物需要复制）
      if [ ! -d "$repo_dir/.git" ]; then
        if [ -d "$repo_dir" ]; then
          echo "[expand] 用 root 容器清理残留 $repo_dir ..."
          docker run --rm -v "$clone_dir:/work" "$image" rm -rf "/work/${PROJECT}" 2>/dev/null || true
          rm -rf "$repo_dir" 2>/dev/null || true
        fi
        clone_repo_from_reference "$PROJECT" "$repo_dir" "$REPO_URL"
      fi
      ;;
  esac

  docker run -t -d --name "$cname" \
    --add-host=host.docker.internal:host-gateway \
    -v "$repo_dir:/home/${PROJECT}" "$image" >/dev/null
  if verify_or_build "$cname"; then
    echo "[expand] ${cname} 就绪"
  else
    echo "[expand] WARN: ${cname} 环境准备失败，删除该容器以便后续重试"
    docker rm -f "$cname" 2>/dev/null || true
  fi
done

echo "[expand] 当前 ${PROJECT} 容器:"
docker ps --filter "name=${CONTAINER_PREFIX}" --format '{{.Names}}' | sort
