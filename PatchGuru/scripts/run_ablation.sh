#!/usr/bin/env bash
# RQ2 消融实验：在 new200 上分别跑 w/o (B)/(C)/(D)。
#
# 默认使用与 Testora 隔离的资源池（../clones_pgabl + pgabl-*-dev* 容器）。
# 由 dev_containers.sh 从主池 golden/ 复制 clone 并启动容器，不 reset 主池 clone1-3。
#
# 用法:
#   bash scripts/run_ablation.sh
#   ABLATIONS=no_constructor bash scripts/run_ablation.sh   # 只跑单个变体
#   SKIP_PREPARE=1 bash scripts/run_ablation.sh               # 续跑（不重新 prepare）
#   POOL_TAG=pgabl bash scripts/run_ablation.sh               # 自定义隔离池 TAG
#
# 环境变量:
#   BASELINE_CACHE  baseline phase1/2 缓存（默认 .cache_baseline_new200）
#   PR_FILE         PR 列表（默认 scripts/pr_batch_300/new200.txt）
#   ABLATIONS       空格分隔的变体列表（默认全部三个）
#   WORKERS         并行 worker 数（默认 3）
#   POOL_TAG        隔离池标签（默认 pgabl）
#   SKIP_POOL_SETUP 1=不自动 setup 隔离池（需已手动 setup）
set -euo pipefail
cd "$(dirname "$0")/.."

export PATCHGURU_HOST_PROXY="${PATCHGURU_HOST_PROXY:-http://127.0.0.1:10810}"
export GIT_PROXY="${GIT_PROXY:-http://127.0.0.1:10810}"
export http_proxy="${http_proxy:-http://127.0.0.1:10810}"
export https_proxy="${https_proxy:-http://127.0.0.1:10810}"

BASELINE_CACHE="${BASELINE_CACHE:-.cache_baseline_new200}"
PR_FILE="${PR_FILE:-scripts/pr_batch_300/new200.txt}"
WORKERS="${WORKERS:-3}"
NB_CLONES="${NB_CLONES:-$WORKERS}"
PR_TIMEOUT="${PR_TIMEOUT:-3600}"
POOL_TAG="${POOL_TAG:-pgabl}"
REPO_ROOT="$(cd .. && pwd)"
export PATCHGURU_CLONES_DIR="${PATCHGURU_CLONES_DIR:-$REPO_ROOT/clones_${POOL_TAG}}"
export PATCHGURU_CONTAINER_TAG="${PATCHGURU_CONTAINER_TAG:-$POOL_TAG}"

if [[ -z "${ABLATIONS:-}" ]]; then
  ABLATIONS="no_constructor no_guided no_triage"
fi

ensure_isolated_pool() {
  if [[ "${SKIP_POOL_SETUP:-0}" == "1" ]]; then
    return
  fi
  local marker="$PATCHGURU_CLONES_DIR/clone1/marshmallow/.git"
  if [[ -d "$marker" ]]; then
    echo "隔离池已存在: $PATCHGURU_CLONES_DIR"
    return
  fi
  echo "创建隔离池 (TAG=$POOL_TAG, N=$NB_CLONES) ..."
  bash scripts/setup_isolated_pool.sh "$POOL_TAG" "$NB_CLONES"
}

prepare_cache() {
  local cache="$1"
  mkdir -p "$cache/logs"
  if [[ "${SKIP_PREPARE:-0}" == "1" ]]; then
    if [[ ! -f "$cache/dive_prs.txt" ]]; then
      echo "ERROR: $cache/dive_prs.txt 不存在，请先 prepare 或取消 SKIP_PREPARE" >&2
      exit 1
    fi
    return
  fi
  uv run python scripts/prepare_dive_cache.py \
    --baseline-cache "$BASELINE_CACHE" \
    --dive-cache "$cache" \
    --pr-file "$PR_FILE"
}

run_variant() {
  local ab="$1"
  local cache=".cache_dive_ablation_${ab}_new200"
  local pr_list="$cache/dive_prs.txt"

  echo "======== RQ2 ablation: $ab -> $cache ========"
  prepare_cache "$cache"

  RUN_ARGS=(
    --phase2-strategy dive
    --dive-ablation "$ab"
    --dive-seed-baseline-dir "$BASELINE_CACHE"
    --cache-dir "$cache"
    --pr-file "$pr_list"
    --workers "$WORKERS"
    --nb-clones "$NB_CLONES"
    --timeout "$PR_TIMEOUT"
  )

  for p in marshmallow pandas scipy keras; do
    echo "▶ $ab / $p → $cache/logs/run_${p}.log"
    uv run python scripts/run_all_specinfer.py \
      --projects "$p" "${RUN_ARGS[@]}" \
      >"$cache/logs/run_${p}.log" 2>&1 &
  done
  wait

  uv run python scripts/summarize_results.py --cache-dir "$cache"
}

ensure_isolated_pool

echo "使用隔离资源:"
echo "  PATCHGURU_CLONES_DIR=$PATCHGURU_CLONES_DIR"
echo "  PATCHGURU_CONTAINER_TAG=$PATCHGURU_CONTAINER_TAG"
echo "  Testora 主池不受影响: $REPO_ROOT/clones + *-dev{1..3}"
echo

for ab in $ABLATIONS; do
  case "$ab" in
    no_constructor|no_guided|no_triage) ;;
    *)
      echo "ERROR: unknown ablation '$ab' (expected no_constructor|no_guided|no_triage)" >&2
      exit 1
      ;;
  esac
  run_variant "$ab"
done

echo "======== 汇总消融对比 ========"
uv run python scripts/summarize_ablation.py \
  --baseline-cache "$BASELINE_CACHE" \
  --full-dive-cache .cache_dive_new200 \
  --pr-file "$PR_FILE"

echo "完成"
