#!/usr/bin/env bash
# 创建与 Testora 隔离的 clone 池 + Docker 容器（dev_containers.sh 的薄封装）。
#
# 逻辑与 dev_containers.sh up 相同：从主池 golden/ 复制到 clones_<TAG>/clone{1..N}，
# 再启动 <TAG>-{project}-dev{1..N} 容器。不会 reset 主池 clone1..3。
#
# 用法:
#   bash scripts/setup_isolated_pool.sh              # 默认 TAG=pgabl, N=3
#   bash scripts/setup_isolated_pool.sh pgabl 3
#
# 等价于:
#   CLONES=../clones_pgabl CONTAINER_TAG=pgabl \
#     bash .devcontainer/dev_containers.sh up all 3
# （clone_common.sh 会把 CLONES 规范化为绝对路径，供 docker -v 挂载）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$ROOT/.." && pwd)"

TAG="${1:-${TAG:-pgabl}}"
NB_CLONES="${2:-${NB_CLONES:-3}}"

export CLONES="${REPO_ROOT}/clones_${TAG}"
export GOLDEN_ROOT="${REPO_ROOT}/clones/golden"
export CONTAINER_TAG="$TAG"

echo "隔离池: CLONES=$CLONES"
echo "golden 源（只读）: $GOLDEN_ROOT"
echo "容器前缀: ${CONTAINER_TAG}-<project>-dev<N>"
echo

bash "$ROOT/.devcontainer/dev_containers.sh" up all "$NB_CLONES"
bash "$ROOT/.devcontainer/dev_containers.sh" eval all "$NB_CLONES"

echo
echo "跑消融时 export:"
echo "  PATCHGURU_CLONES_DIR=$CLONES"
echo "  PATCHGURU_CONTAINER_TAG=$TAG"
