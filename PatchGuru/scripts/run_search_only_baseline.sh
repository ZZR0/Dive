#!/usr/bin/env bash
# Search-only baseline (E5): full DIVE search (A–D) but skip TestDriverReview (E).
# Any stable pre/post divergence => p2 BUG (Mokav-style raw detection on comparison program).
#
# 用法:
#   bash scripts/run_search_only_baseline.sh
#   REPO=marshmallow PR_ID=750 FORCE=1 bash scripts/run_search_only_baseline.sh   # 单 PR 调试
#   SKIP_PREPARE=1 WORKERS=3 bash scripts/run_search_only_baseline.sh             # 续跑
#   POOL_TAG=pgabl bash scripts/run_search_only_baseline.sh                       # 默认即 pgabl 隔离池
#
# 环境变量:
#   BASELINE_CACHE   baseline phase1 来源（默认 .cache_baseline_new200）
#   SEARCH_CACHE     输出 cache（默认 .cache_dive_search_only_new200）
#   PR_FILE          PR 列表（默认 scripts/pr_batch_300/new200.txt）
#   WORKERS          并行 worker（默认 3）
#   POOL_TAG         隔离池标签（默认 pgabl → clones_pgabl + pgabl-*-dev*）
set -euo pipefail
cd "$(dirname "$0")/.."

export PATCHGURU_HOST_PROXY="${PATCHGURU_HOST_PROXY:-http://127.0.0.1:10810}"
export GIT_PROXY="${GIT_PROXY:-http://127.0.0.1:10810}"
export http_proxy="${http_proxy:-http://127.0.0.1:10810}"
export https_proxy="${https_proxy:-http://127.0.0.1:10810}"

BASELINE_CACHE="${BASELINE_CACHE:-.cache_baseline_new200}"
SEARCH_CACHE="${SEARCH_CACHE:-.cache_dive_search_only_new200}"
PR_FILE="${PR_FILE:-scripts/pr_batch_300/new200.txt}"
WORKERS="${WORKERS:-3}"
NB_CLONES="${NB_CLONES:-$WORKERS}"
PR_TIMEOUT="${PR_TIMEOUT:-3600}"
POOL_TAG="${POOL_TAG:-pgabl}"
REPO_ROOT="$(cd .. && pwd)"
export PATCHGURU_CLONES_DIR="${PATCHGURU_CLONES_DIR:-$REPO_ROOT/clones_${POOL_TAG}}"
export PATCHGURU_CONTAINER_TAG="${PATCHGURU_CONTAINER_TAG:-$POOL_TAG}"

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
  mkdir -p "$SEARCH_CACHE/logs"
  if [[ -n "${PR_ID:-}" && -n "${REPO:-}" ]]; then
    local pr_list="$SEARCH_CACHE/dive_prs.txt"
    mkdir -p "$(dirname "$pr_list")"
    echo "${REPO} ${PR_ID}" >"$pr_list"
    local dst="$SEARCH_CACHE/oracles/${REPO}/${PR_ID}"
    local src="$BASELINE_CACHE/oracles/${REPO}/${PR_ID}"
    if [[ ! -f "$src/results.json" ]]; then
      echo "ERROR: baseline phase1 不存在: $src/results.json" >&2
      exit 1
    fi
    mkdir -p "$dst"
    for name in results.json specification.py config.json; do
      [[ -f "$src/$name" ]] && cp -f "$src/$name" "$dst/$name"
    done
    rm -rf "$dst/phase2"
    echo "单 PR prepare: ${REPO}#${PR_ID} -> $dst"
    return
  fi
  if [[ "${SKIP_PREPARE:-0}" == "1" ]]; then
    if [[ ! -f "$SEARCH_CACHE/dive_prs.txt" ]]; then
      echo "ERROR: $SEARCH_CACHE/dive_prs.txt 不存在，请先 prepare 或取消 SKIP_PREPARE" >&2
      exit 1
    fi
    return
  fi
  uv run python scripts/prepare_dive_cache.py \
    --baseline-cache "$BASELINE_CACHE" \
    --dive-cache "$SEARCH_CACHE" \
    --pr-file "$PR_FILE"
}

ensure_isolated_pool
prepare_cache

pr_list="$SEARCH_CACHE/dive_prs.txt"
RUN_ARGS=(
  --phase2-strategy dive
  --dive-ablation search_only
  --dive-seed-baseline-dir "$BASELINE_CACHE"
  --cache-dir "$SEARCH_CACHE"
  --pr-file "$pr_list"
  --workers "$WORKERS"
  --nb-clones "$NB_CLONES"
  --timeout "$PR_TIMEOUT"
)
[[ "${FORCE:-}" == "1" ]] && RUN_ARGS+=(--force)

if [[ -n "${PR_ID:-}" && -n "${REPO:-}" ]]; then
  PROJECTS=("$REPO")
else
  PROJECTS=(marshmallow pandas scipy keras)
fi

echo "======== Search-only baseline (DIVE w/o review) ========"
echo "  cache=$SEARCH_CACHE"
echo "  PATCHGURU_CLONES_DIR=$PATCHGURU_CLONES_DIR"
echo "  PATCHGURU_CONTAINER_TAG=$PATCHGURU_CONTAINER_TAG"
echo "  PR list=$pr_list ($(wc -l <"$pr_list") lines)"
echo

for p in "${PROJECTS[@]}"; do
  echo "▶ search_only / $p → $SEARCH_CACHE/logs/run_${p}.log"
  uv run python scripts/run_all_specinfer.py \
    --projects "$p" "${RUN_ARGS[@]}" \
    >"$SEARCH_CACHE/logs/run_${p}.log" 2>&1 &
done
wait

uv run python scripts/summarize_results.py --cache-dir "$SEARCH_CACHE" --pr-file "$pr_list"
echo
echo "完成。汇总: uv run python scripts/summarize_results.py --cache-dir $SEARCH_CACHE"
echo "与 full DIVE 对比（同 PR 列表）可后处理 phase2/results.json 的 review_conclusion / llm_queries。"
