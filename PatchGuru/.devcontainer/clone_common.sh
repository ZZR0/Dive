#!/bin/bash
# clones/golden/<project> 为 setup 后的快照；clone1..N 从 golden 复制。
#
# 环境变量（可选，用于隔离池）:
#   CLONES        clone 根目录，默认 <repo>/clones
#   GOLDEN_ROOT   golden 快照目录，默认 ${CLONES}/golden；隔离池可指向主池 clones/golden
#   CONTAINER_TAG 容器名前缀标签，如 pgabl -> pgabl-pandas-dev1

_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
_DEFAULT_CLONES="${_REPO_ROOT}/clones"

_abs_path() {
  local p="$1"
  if [[ "$p" == /* ]]; then
    echo "$p"
  elif command -v realpath >/dev/null 2>&1; then
    realpath -m "$p"
  else
    local dir base
    dir="$(dirname "$p")"
    base="$(basename "$p")"
    if [[ "$dir" == /* ]]; then
      echo "${dir}/${base}"
    else
      echo "$(cd "$dir" 2>/dev/null && pwd || pwd)/${base}"
    fi
  fi
}

CLONES="${CLONES:-${_DEFAULT_CLONES}}"
CLONES="$(_abs_path "$CLONES")"
GOLDEN_ROOT="${GOLDEN_ROOT:-${CLONES}/golden}"
# 隔离池目录下通常没有 golden/，默认从主池读 golden（只读）
if [ ! -d "$GOLDEN_ROOT" ] && [ -d "${_DEFAULT_CLONES}/golden" ]; then
  GOLDEN_ROOT="${_DEFAULT_CLONES}/golden"
fi
GOLDEN_ROOT="$(_abs_path "$GOLDEN_ROOT")"

ensure_clones_dir() {
  sudo mkdir -p "$CLONES"
  sudo chown "$(whoami):$(whoami)" "$CLONES" 2>/dev/null || true
}

golden_path() {
  echo "${GOLDEN_ROOT}/$1"
}

clone_repo_path() {
  echo "${CLONES}/clone${2}/${1}"
}

container_name_for() {
  # $1=project $2=index
  if [ -n "${CONTAINER_TAG:-}" ]; then
    echo "${CONTAINER_TAG}-${1}-dev${2}"
  else
    echo "${1}-dev${2}"
  fi
}

# 将 golden/<project> 复制到 clone<num>/<project>
copy_golden_to_clone() {
  local project="$1" num="$2"
  local golden dst
  golden="$(golden_path "$project")"
  dst="$(clone_repo_path "$project" "$num")"
  [ -d "$golden" ] || { echo "ERROR: 缺少 golden $golden"; return 1; }
  sudo rm -rf "$dst"
  sudo mkdir -p "${CLONES}/clone${num}"
  sudo cp -a "$golden" "$dst"
}

# 从 clone1 引导 golden（兼容旧布局，无 golden 时可用）
bootstrap_golden_from_clone1() {
  local project="$1"
  local golden seed
  golden="$(golden_path "$project")"
  seed="$(clone_repo_path "$project" 1)"
  [ -d "$golden" ] && return 0
  [ -d "$seed" ] || return 1
  echo "WARN: 无 golden，从 clone1 创建 ${golden}"
  sudo mkdir -p "$(dirname "$golden")"
  sudo cp -a "$seed" "$golden"
}
