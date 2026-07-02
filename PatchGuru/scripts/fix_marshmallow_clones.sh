#!/usr/bin/env bash
# 一次性清理全部 marshmallow clone 上的重复包目录。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$HERE/lib/common.sh"

NB_CLONES="${NB_CLONES:-10}"
ROOT_PG="$(cd "$HERE/.." && pwd)"

log "清理 clone1..${NB_CLONES} marshmallow 重复包目录..."
for idx in $(seq 1 "$NB_CLONES"); do
  repo="$CLONES_DIR/clone${idx}/marshmallow"
  if [ ! -d "$repo/.git" ]; then
    log "  跳过 clone${idx}（不存在）"
    continue
  fi
  removed=$(cd "$ROOT_PG" && uv run python -c "
from patchguru.utils.marshmallow_layout import fix_marshmallow_duplicate_layout
r = fix_marshmallow_duplicate_layout('$repo')
print(','.join(r) if r else '')
" 2>/dev/null)
  if [ -n "$removed" ]; then
    log "  clone${idx}: 已删除 $removed"
  else
    log "  clone${idx}: 无需清理"
  fi
done
log "marshmallow clone 布局清理完成"
