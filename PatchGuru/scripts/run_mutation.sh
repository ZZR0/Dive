#!/usr/bin/env bash
# Mutation score：4 项目 × 3 clone 并行（结构同 run_baseline.sh）。
#
# 用法:
#   bash scripts/run_mutation.sh
#   METHOD=baseline bash scripts/run_mutation.sh
#   REPO=marshmallow PR_ID=1008 bash scripts/run_mutation.sh
#
# 日志（默认写入各 cache 的 mutation_testing/）:
#   .cache_dive_new200/mutation_testing/logs/run_<project>.log     调度摘要
#   .cache_dive_new200/mutation_testing/<repo>/<pr>/phase2/run.log 单 PR 详情
#   .cache_baseline_new200/mutation_testing/...                     baseline 同理
set -euo pipefail
cd "$(dirname "$0")/.."

export PATCHGURU_HOST_PROXY=http://127.0.0.1:10810
export GIT_PROXY=http://127.0.0.1:10810
export http_proxy=http://127.0.0.1:10810
export https_proxy=http://127.0.0.1:10810
export PATCHGURU_GIT_FETCH_OPTIONAL=1
export PATCHGURU_GIT_FETCH_RETRIES=5
export PATCHGURU_GIT_FETCH_RETRY_SEC=10

REPO_ROOT="$(cd .. && pwd)"
export PATCHGURU_CLONES_DIR="$REPO_ROOT/clones_pgabl"
export PATCHGURU_CONTAINER_TAG=pgabl

METHOD="${METHOD:-both}"
mkdir -p .cache_dive_new200/mutation_testing/logs .cache_baseline_new200/mutation_testing/logs

ARGS=(
  --method "$METHOD"
  --phase phase2
  --parallel pr
  --workers 3
  --nb-clones 3
  --pool-tag pgabl
  --pr-file scripts/pr_batch_300/new200.txt
)
[[ -n "${PR_ID:-}" ]] && ARGS+=(--pr-id "$PR_ID")

if [[ -n "${REPO:-}" ]]; then
  PROJECTS=("$REPO")
else
  PROJECTS=(marshmallow pandas scipy keras)
fi

# 调度进程 stdout：METHOD=baseline 时写 baseline cache，否则写 dive cache
if [[ "$METHOD" == "baseline" ]]; then
  SCHED_ROOT=.cache_baseline_new200/mutation_testing/logs
else
  SCHED_ROOT=.cache_dive_new200/mutation_testing/logs
fi

echo "METHOD=$METHOD | clones=$PATCHGURU_CLONES_DIR | sched_logs=$SCHED_ROOT"

for p in "${PROJECTS[@]}"; do
  echo "▶ $p → $SCHED_ROOT/run_${p}.log"
  uv run python scripts/run_mutation.py --repo "$p" "${ARGS[@]}" \
    >"$SCHED_ROOT/run_${p}.log" 2>&1 &
done
wait

echo "完成。汇总: uv run python scripts/summarize_mutation.py"
