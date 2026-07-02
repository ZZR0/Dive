#!/usr/bin/env bash
# 从 clone1 用 git reference 复制仓库，避免 cp 碰到 Docker 创建的 root 文件
clone_repo_from_reference() {
  local project="$1"
  local dst="$2"
  local repo_url="$3"
  local ref="$CLONES_DIR/clone1/${project}"

  if [ ! -d "$ref/.git" ]; then
    echo "[clone] ERROR: 缺少参考仓库 $ref"
    return 1
  fi

  echo "[clone] 本地 git clone $ref -> $dst"
  rm -rf "$dst"
  mkdir -p "$(dirname "$dst")"
  if ! git clone --reference "$ref" --dissociate "file://${ref}" "$dst" 2>/dev/null; then
    echo "[clone] 本地 clone 失败，尝试 reference + $repo_url"
    git clone --reference "$ref" "$repo_url" "$dst"
  fi
  git -C "$dst" config http.proxy "${GIT_PROXY:-}" 2>/dev/null || true
  git -C "$dst" config https.proxy "${GIT_PROXY:-}" 2>/dev/null || true
}

# 幂等地把 clone1 的离线 build 依赖复制到目标 clone，避免容器内联网下载。
# pandas: subprojects/packagecache/*.tar.gz；scipy: subprojects/* submodule 工作区。
# 用 root 容器执行，因为 clone1 中这些文件可能属 root。
ensure_build_deps() {
  local project="$1" clone_id="$2" image="$3"
  local dst="$CLONES_DIR/$clone_id/$project"
  case "$project" in
    pandas)
      if ls "$dst/subprojects/packagecache/"*.tar.gz >/dev/null 2>&1; then
        return 0
      fi
      echo "[deps] $clone_id: 复制 pandas packagecache（离线 build 依赖）..."
      docker run --rm -v "$CLONES_DIR:/clones" "$image" bash -c \
        "mkdir -p /clones/$clone_id/pandas/subprojects/packagecache && \
         cp -an /clones/clone1/pandas/subprojects/packagecache/. /clones/$clone_id/pandas/subprojects/packagecache/ 2>/dev/null; \
         chmod -R a+rX /clones/$clone_id/pandas/subprojects/packagecache 2>/dev/null; true"
      ;;
    scipy)
      if [ -n "$(ls -A "$dst/subprojects/array_api_compat" 2>/dev/null)" ]; then
        return 0
      fi
      echo "[deps] $clone_id: 复制 scipy submodule 工作区（离线 build 依赖）..."
      docker run --rm -v "$CLONES_DIR:/clones" "$image" bash -c \
        "cp -an /clones/clone1/scipy/subprojects/. /clones/$clone_id/scipy/subprojects/ 2>/dev/null; \
         chmod -R a+rX /clones/$clone_id/scipy/subprojects 2>/dev/null; true"
      ;;
  esac
}
