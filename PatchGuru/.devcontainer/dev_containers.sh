#!/bin/bash
# 用法: bash .devcontainer/dev_containers.sh up|down|eval <project|all> [N]
#   project: marshmallow | pandas | scipy | keras | all
#   N 默认 3
#
# 隔离池（与 Testora 并行跑 PatchGuru 消融）:
#   CLONES=../clones_pgabl CONTAINER_TAG=pgabl bash .devcontainer/dev_containers.sh up all 3
#   golden 默认仍从主池 ../clones/golden 只读复制（见 clone_common.sh GOLDEN_ROOT）
#   或: bash scripts/setup_isolated_pool.sh pgabl 3

ACTION="${1:?用法: $0 up|down|eval <project|all> [N]}"
TARGET="${2:?用法: $0 up|down|eval <project|all> [N]}"
N="${3:-3}"
# shellcheck source=clone_common.sh
source "$(dirname "$0")/clone_common.sh"
PROJECTS=(marshmallow pandas scipy keras)

if [ -n "${CONTAINER_TAG:-}" ] || [ "$CLONES" != "$_DEFAULT_CLONES" ]; then
  echo "池: CLONES=$CLONES | GOLDEN_ROOT=$GOLDEN_ROOT | CONTAINER_TAG=${CONTAINER_TAG:-<none>}"
fi

project_image() {
  case "$1" in
    marshmallow) echo marshmallow-dev ;;
    pandas)      echo pandas-dev ;;
    scipy)       echo scipy-dev ;;
    keras)       echo keras-dev ;;
    *) echo "ERROR: 未知项目 '$1'"; exit 1 ;;
  esac
}

projects_for() {
  if [ "$1" = all ]; then
    printf '%s\n' "${PROJECTS[@]}"
  else
    project_image "$1" >/dev/null
    echo "$1"
  fi
}

# 从 golden 快照复制到 clone1..N（与 setup_*.sh 一致）
restore_clones() {
  local project="$1" golden i
  golden="$(golden_path "$project")"
  bootstrap_golden_from_clone1 "$project" || true
  [ -d "$golden" ] || {
    echo "FAIL: 缺少 golden $golden（先运行 setup_${project}.sh）"
    return 1
  }
  for i in $(seq 1 "$N"); do
    copy_golden_to_clone "$project" "$i" || {
      echo "FAIL reset $(clone_repo_path "$project" "$i")"
      return 1
    }
    echo "↺ reset $(clone_repo_path "$project" "$i")"
  done
}

up_project() {
  local project="$1" image repo cname i
  image="$(project_image "$project")"
  ensure_clones_dir
  restore_clones "$project" || return 1
  for i in $(seq 1 "$N"); do
    cname="$(container_name_for "$project" "$i")"
    repo="${CLONES}/clone${i}/${project}"
    [ -d "$repo" ] || { echo "WARN: 缺少 $repo"; continue; }
    if docker ps --format '{{.Names}}' | grep -qx "$cname"; then
      echo "跳过 $cname（已运行）"
    elif docker ps -a --format '{{.Names}}' | grep -qx "$cname"; then
      docker start "$cname" && echo "▶ start $cname"
    else
      docker run -t -d --name "$cname" \
        --add-host=host.docker.internal:host-gateway \
        -v "${repo}:/home/${project}" "$image" \
        && echo "✚ create+run $cname"
    fi
  done
}

down_project() {
  local project="$1" names prefix
  if [ -n "${CONTAINER_TAG:-}" ]; then
    prefix="${CONTAINER_TAG}-${project}-dev"
  else
    prefix="${project}-dev"
  fi
  names="$(docker ps -a --format '{{.Names}}' | grep "^${prefix}" || true)"
  [ -z "$names" ] && echo "$project 无容器" && return 0
  echo "$names" | xargs -r docker stop
  echo "$names" | xargs -r docker rm
  echo "⏹ 已停止并删除 $project: $(echo "$names" | tr '\n' ' ')"
}

eval_one() {
  local project="$1" cname="$2"
  case "$project" in
    marshmallow)
      docker exec -w /home/marshmallow "$cname" bash -c \
        'HAS_SRC=$(git ls-tree HEAD src/marshmallow 2>/dev/null | wc -l); \
         HAS_ROOT=$(git ls-tree HEAD marshmallow 2>/dev/null | wc -l); \
         if [ "$HAS_SRC" -gt 0 ] && [ "$HAS_ROOT" -eq 0 ] && [ -d marshmallow ]; then \
           rm -rf marshmallow marshmallow.egg-info; \
         elif [ "$HAS_ROOT" -gt 0 ] && [ "$HAS_SRC" -eq 0 ] && [ -d src/marshmallow ]; then \
           rm -rf src/marshmallow marshmallow.egg-info; \
         elif [ "$HAS_SRC" -gt 0 ] && [ -d marshmallow ]; then \
           rm -rf marshmallow marshmallow.egg-info; \
         fi; \
         pip install -q -e ".[dev]" 2>/dev/null || pip install -q -e .; \
         python3 -c "import marshmallow; print(marshmallow.__file__)"'
      ;;
    keras)
      docker exec -w /home/keras "$cname" bash -c \
        'pip install -q -e . && python3 -c "import keras; print(keras.__file__)"'
      ;;
    pandas)
      docker exec -w /home/pandas "$cname" python3 -c "import pandas; print(pandas.__file__)" \
        || docker exec -w /home/pandas "$cname" \
          python3 -m pip install -q -e . --no-build-isolation \
        && docker exec -w /home/pandas "$cname" \
          python3 -c "import pandas; print(pandas.__file__)"
      ;;
    scipy)
      docker exec -w /home/scipy "$cname" bash -c \
        'export MAMBA_ROOT_PREFIX=/root/conda
         source /root/conda/etc/profile.d/conda.sh
         mamba run -n scipy-dev python -c "import scipy; print(scipy.__file__)"' \
        || docker exec -w /home/scipy "$cname" bash -c \
        'export MAMBA_ROOT_PREFIX=/root/conda
         source /root/conda/etc/profile.d/conda.sh
         mamba run -n scipy-dev pip install -q -e . --no-build-isolation
         mamba run -n scipy-dev python -c "import scipy; print(scipy.__file__)"'
      ;;
  esac
}

eval_project() {
  local project="$1" cname i
  for i in $(seq 1 "$N"); do
    cname="$(container_name_for "$project" "$i")"
    docker ps --format '{{.Names}}' | grep -qx "$cname" \
      || docker start "$cname" >/dev/null 2>&1 \
      || { echo "FAIL $cname（不存在，先 up）"; continue; }
    if eval_one "$project" "$cname"; then
      echo "OK $cname"
    else
      echo "FAIL $cname"
    fi
  done
}

case "$ACTION" in
  up|down|eval)
    while IFS= read -r p; do
      [ "$TARGET" = all ] && echo "=== $p ==="
      "$ACTION"_project "$p"
    done < <(projects_for "$TARGET")
    ;;
  *)
    echo "用法: $0 up|down|eval <project|all> [N]"; exit 1 ;;
esac
