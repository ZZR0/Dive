#!/usr/bin/env bash
# 修复 keras clone2..N 的 origin 指向 GitHub 并 fetch 全量历史。
# clone1 在 setup 时已指向 GitHub；clone2..N 经 reference clone 后 origin 会变成 file://clone1，
# 导致旧 commit 对象缺失（reference is not a tree）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$HERE/lib/common.sh"

PROJECT=keras
REPO_URL="$(project_repo_url "$PROJECT")"
NB_CLONES="${NB_CLONES:-10}"

log "修复 keras clone1..${NB_CLONES}：origin -> GitHub + fetch"

for idx in $(seq 1 "$NB_CLONES"); do
  repo="$CLONES_DIR/clone${idx}/$PROJECT"
  if [ ! -d "$repo/.git" ]; then
    log "  跳过 clone${idx}/$PROJECT（不存在）"
    continue
  fi
  log "  [clone${idx}] 设置 origin 并 fetch..."
  git -C "$repo" remote set-url origin "$REPO_URL"
  git -C "$repo" config http.proxy "$GIT_PROXY"
  git -C "$repo" config https.proxy "$GIT_PROXY"
  git -C "$repo" fetch origin --tags --prune 2>&1 | tail -2 || true
done

# 验证 3 个曾缺失的 commit
TEST_SHAS=(
  603affa5e783b5e1b2df7322f9ceee3338faa817
  45e5771a1816ce487287aa9a7a37e01971c3dcd6
  1204de220aaa2a90e578d4ab1e925e9349656802
)
log "验证缺失 commit 是否已在全部 clone 中可用..."
for idx in $(seq 1 "$NB_CLONES"); do
  repo="$CLONES_DIR/clone${idx}/$PROJECT"
  [ -d "$repo/.git" ] || continue
  ok=0
  for sha in "${TEST_SHAS[@]}"; do
    if git -C "$repo" cat-file -t "$sha" >/dev/null 2>&1; then
      ok=$((ok + 1))
    fi
  done
  log "  clone${idx}: ${ok}/${#TEST_SHAS[@]} commits 可用"
done

log "keras clone 修复完成"
