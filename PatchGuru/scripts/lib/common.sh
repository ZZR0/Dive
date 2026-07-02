#!/usr/bin/env bash
# 公共配置与工具函数 —— 所有 setup / expand / watch 脚本的唯一配置源。
# 用法：在脚本顶部 source 本文件。
#   HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   source "$HERE/lib/common.sh"

# ---------- 路径 ----------
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # .../PatchGuru/scripts
ROOT="$(cd "$SCRIPTS_DIR/.." && pwd)"                            # .../PatchGuru
CLONES_DIR="$(cd "$ROOT/.." && pwd)/clones"                     # .../clones（与 PatchGuru 同级）
LOG_DIR="$SCRIPTS_DIR/logs"

# ---------- 网络代理 ----------
# GIT_PROXY           : 宿主机 git clone 用（mihomo SOCKS5）
# PATCHGURU_HOST_PROXY: 宿主机 PyGithub / requests 拉 PR diff 用（默认同 GIT_PROXY）
# CONTAINER_PROXY     : 容器内 pip/build 用（经 host.docker.internal 访问宿主机代理）
GIT_PROXY="${GIT_PROXY:-socks5h://127.0.0.1:10808}"
PATCHGURU_HOST_PROXY="${PATCHGURU_HOST_PROXY:-$GIT_PROXY}"
CONTAINER_PROXY="${CONTAINER_PROXY:-http://host.docker.internal:10810}"

# ---------- 通用参数 ----------
NB_CLONES="${NB_CLONES:-10}"        # worker clone/容器池目标大小（clone1..NB_CLONES）
SEED_CLONE="${SEED_CLONE:-clone}"   # 种子仓库目录名（联网克隆 + 首次编译，无对应 worker 容器）
PY_IMAGE="${PY_IMAGE:-python:3.11}" # 首次建环境用的基础镜像

# ---------- 项目元数据 ----------
# 受支持的项目；kind 决定建环境/扩容的处理方式：
#   pure  : 纯 Python，editable 安装（keras / marshmallow）；扩容 reference clone
#   cext  : C 扩展 pip+meson（pandas）；扩容 cp -a 种子 clone 带编译产物
#   conda : scipy；setup 在种子 clone 上 conda build；扩容 reference clone + submodule init
SUPPORTED_PROJECTS=(pandas scipy keras marshmallow)

project_repo() {   # GitHub org/repo
  case "$1" in
    pandas)      echo "pandas-dev/pandas" ;;
    scipy)       echo "scipy/scipy" ;;
    keras)       echo "keras-team/keras" ;;
    marshmallow) echo "marshmallow-code/marshmallow" ;;
    *) return 1 ;;
  esac
}

project_kind() {
  case "$1" in
    pandas)      echo "cext" ;;
    scipy)       echo "conda" ;;
    keras|marshmallow) echo "pure" ;;
    *) return 1 ;;
  esac
}

project_image()    { echo "patchguru-$1-dev"; }  # docker commit 后的镜像名
container_prefix() { echo "$1-dev"; }            # 容器命名前缀，配 1..N
project_repo_url() { echo "https://github.com/$(project_repo "$1").git"; }

# ---------- 工具函数 ----------
log() { echo "[$(date '+%F %T')] $*"; }

# 当前正在运行的某项目容器数量
docker_count() {
  local prefix; prefix="$(container_prefix "$1")"
  docker ps --filter "name=${prefix}" --format '{{.Names}}' 2>/dev/null \
    | grep -c "^${prefix}" || true
}

# 校验项目名合法，否则报错退出
require_project() {
  local p="$1"
  if ! project_repo "$p" >/dev/null 2>&1; then
    log "ERROR: 不支持的项目 '$p'（支持: ${SUPPORTED_PROJECTS[*]}）"
    exit 1
  fi
}
