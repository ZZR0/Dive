#!/usr/bin/env bash
# 在 Testora 上跑 PatchGuru DIVE 两次实验重合的 16 个 BUG case。
#
# 用法:
#   bash scripts/run_dive_common_bugs.sh
#   bash scripts/run_dive_common_bugs.sh --force
#   PROJECTS="marshmallow pandas" bash scripts/run_dive_common_bugs.sh
#
# 续跑（跳过已有结果）:
#   bash scripts/run_dive_common_bugs.sh
#
# 前置条件:
#   1. 在 Testora 根目录有 .openai_token 和 .github_token
#   2. pip install -e . 已执行
#   3. ../clones 下四个项目 + 对应 *-dev{1,2,3} 容器已就绪
set -euo pipefail
cd "$(dirname "$0")/.."

CASE_FILE="${CASE_FILE:-scripts/dive_common_bugs.txt}"
RESULTS_DIR="${RESULTS_DIR:-.results_dive_common_bugs}"
PYTHON="${PYTHON:-${TESTORA_PYTHON:-}}"
if [[ -z "$PYTHON" && -x ".venv/bin/python" ]]; then
  PYTHON=".venv/bin/python"
fi
PYTHON="${PYTHON:-python}"
FORCE=0
EXTRA_ARGS=()

for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    *) EXTRA_ARGS+=("$arg") ;;
  esac
done

if [[ ! -f ".openai_token" ]]; then
  echo "ERROR: 缺少 .openai_token" >&2
  exit 1
fi
if [[ ! -f ".github_token" ]]; then
  echo "ERROR: 缺少 .github_token" >&2
  exit 1
fi

mkdir -p "$RESULTS_DIR/logs"

RUN_ARGS=(--case-file "$CASE_FILE" --output-dir "$RESULTS_DIR" --python "$PYTHON")
if [[ "$FORCE" == "1" ]]; then
  RUN_ARGS+=(--force)
fi

if [[ -n "${PROJECTS:-}" ]]; then
  # shellcheck disable=SC2206
  RUN_ARGS+=(--projects ${PROJECTS})
  echo "▶ 单批运行 projects: $PROJECTS"
  "$PYTHON" scripts/run_cases.py "${RUN_ARGS[@]}" "${EXTRA_ARGS[@]}" \
    >"$RESULTS_DIR/logs/run_all.log" 2>&1
else
  for p in marshmallow pandas scipy keras; do
    echo "▶ $p → $RESULTS_DIR/logs/run_${p}.log"
    "$PYTHON" scripts/run_cases.py "${RUN_ARGS[@]}" --projects "$p" "${EXTRA_ARGS[@]}" \
      >"$RESULTS_DIR/logs/run_${p}.log" 2>&1 &
  done
  wait
fi

"$PYTHON" scripts/summarize_cases.py --results-dir "$RESULTS_DIR"
echo "完成。结果在 $RESULTS_DIR/"
