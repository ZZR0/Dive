#!/usr/bin/env bash
# 本地环境适配版 marshmallow 目标项目 Docker 设置（无需 sudo / vscode 用户）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLONES_DIR="$(cd "$ROOT/.." && pwd)/clones"

echo "[setup] PatchGuru 根目录: $ROOT"
echo "[setup] clones 目录: $CLONES_DIR"
mkdir -p "$CLONES_DIR"
cd "$CLONES_DIR"

for i in 1 2 3; do
  echo "[setup] 清理旧容器 marshmallow-dev${i} ..."
  docker rm -f "marshmallow-dev${i}" 2>/dev/null || true
done

setup_clone() {
  local idx="$1"
  local cname="marshmallow-dev${idx}"
  local dir="clone${idx}"

  echo "[setup] === 配置 clone${idx} (${cname}) ==="
  rm -rf "$dir"
  mkdir -p "$dir"
  cd "$dir"

  if [ ! -d marshmallow/.git ]; then
    echo "[setup] 克隆 marshmallow 仓库..."
    git clone --depth 1 https://github.com/marshmallow-code/marshmallow.git
  fi
  cd marshmallow

  echo "[setup] 启动容器 ${cname} ..."
  docker run -t -d --name "$cname" -v "${PWD}:/home/marshmallow" python:3.11
  echo "[setup] 在容器内安装 marshmallow[dev] + coverage ..."
  docker exec -w /home/marshmallow "$cname" pip install -q -e '.[dev]'
  docker exec -w /home/marshmallow "$cname" pip install -q coverage
  echo "[setup] ${cname} 就绪"

  cd "$CLONES_DIR"
}

setup_clone 1
echo "[setup] 复制 clone1 -> clone2/3 ..."
rm -rf clone2 clone3
cp -a clone1 clone2
cp -a clone1 clone3
setup_clone 2
setup_clone 3

echo "[setup] 全部完成。当前 marshmallow 容器:"
docker ps --filter name=marshmallow-dev --format 'table {{.Names}}\t{{.Status}}'
