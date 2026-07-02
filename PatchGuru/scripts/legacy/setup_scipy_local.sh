#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLONES_DIR="$(cd "$ROOT/.." && pwd)/clones"
GIT_PROXY="${GIT_PROXY:-socks5h://127.0.0.1:10808}"
PY_IMAGE="python:3.11"

echo "[setup-scipy] 开始配置 scipy-dev1/2/3 ..."
for i in 1 2 3; do docker rm -f "scipy-dev${i}" 2>/dev/null || true; done

setup_one() {
  local idx="$1"
  local cname="scipy-dev${idx}"
  local dir="$CLONES_DIR/clone${idx}"
  mkdir -p "$dir"
  cd "$dir"
  if [ ! -d scipy/.git ]; then
    echo "[setup-scipy] clone${idx}: 克隆 scipy..."
    git clone https://github.com/scipy/scipy.git
    cd scipy && git submodule update --init
  else
    cd scipy
  fi
  git config http.proxy "$GIT_PROXY" 2>/dev/null || true
  git config https.proxy "$GIT_PROXY" 2>/dev/null || true
  git fetch --unshallow origin 2>/dev/null || git fetch origin

  echo "[setup-scipy] 启动 ${cname} ..."
  docker run -t -d --name "$cname" -v "${PWD}:/home/scipy" "$PY_IMAGE"
  docker cp "$ROOT/.devcontainer/setup_scipy_to_run_in_container.sh" "${cname}:/root/setup.sh"
  docker exec "$cname" chmod +x /root/setup.sh
  echo "[setup-scipy] 容器内安装 scipy 环境（较慢）..."
  docker exec -w /home/scipy "$cname" /root/setup.sh
  echo "[setup-scipy] ${cname} 完成"
  cd "$CLONES_DIR"
}

setup_one 1
for idx in 2 3; do
  rm -rf "$CLONES_DIR/clone${idx}/scipy"
  mkdir -p "$CLONES_DIR/clone${idx}"
  cp -a "$CLONES_DIR/clone1/scipy" "$CLONES_DIR/clone${idx}/scipy"
  setup_one "$idx"
done
docker ps --filter name=scipy-dev --format 'table {{.Names}}\t{{.Status}}'
