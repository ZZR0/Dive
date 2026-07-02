#!/usr/bin/env bash
# clone / 复制相关函数。依赖 common.sh（CLONES_DIR / GIT_PROXY / log）。

# 纯 Python 项目：用种子 clone 作 git reference 本地克隆 worker clone（快且省空间）。
# 避免 cp -a 碰到容器以 root 创建的文件导致权限问题。
clone_from_reference() {
  local project="$1" dst="$2"
  local repo_url; repo_url="$(project_repo_url "$project")"
  local ref="$CLONES_DIR/${SEED_CLONE:-clone}/${project}"

  if [ ! -d "$ref/.git" ]; then
    log "ERROR: 缺少种子仓库 $ref（请先 setup_env.sh）"
    return 1
  fi

  log "[clone] 本地 reference clone $ref -> $dst"
  if [ -d "$dst" ]; then
    if ! rm -rf "$dst" 2>/dev/null; then
      local clone_id; clone_id="$(basename "$(dirname "$dst")")"
      log "[clone] rm 遇权限问题，用容器强制删除 ${clone_id}/$(basename "$dst") ..."
      docker run --rm -v "$CLONES_DIR:/clones" "${PY_IMAGE:-python:3.11}" \
        rm -rf "/clones/${clone_id}/$(basename "$dst")"
    fi
  fi
  mkdir -p "$(dirname "$dst")"
  if ! git clone --reference "$ref" --dissociate "file://${ref}" "$dst" 2>/dev/null; then
    log "[clone] 本地 clone 失败，回退 reference + $repo_url"
    git clone --reference "$ref" "$repo_url" "$dst"
  fi
  git -C "$dst" config http.proxy  "$GIT_PROXY" 2>/dev/null || true
  git -C "$dst" config https.proxy "$GIT_PROXY" 2>/dev/null || true
}

# 确保种子 clone 上 submodule 已完整（全项目只需联网拉一次）。
ensure_seed_submodules() {
  local project="$1"
  local ref="$CLONES_DIR/${SEED_CLONE:-clone}/${project}"
  if [ ! -f "$ref/.gitmodules" ]; then
    return 0
  fi
  if git -C "$ref" submodule status --recursive 2>/dev/null | grep -qE '^[-+]'; then
    log "[submodule] ${SEED_CLONE:-clone} 补全 submodule（仅此一次联网）..."
    git -C "$ref" submodule sync --recursive
    git -C "$ref" submodule update --init --recursive --jobs 4
    log "[submodule] ${SEED_CLONE:-clone} 已就绪"
  fi
}

# 从种子 clone 复制 submodule 元数据与工作区到目标 worker clone（reference clone 之后调用）。
copy_submodules_from_seed() {
  local project="$1" dst="$2" image="${3:-}"
  local ref="$CLONES_DIR/${SEED_CLONE:-clone}/${project}"
  local clone_id; clone_id="$(basename "$(dirname "$dst")")"
  image="${image:-$(project_image "$project")}"

  ensure_seed_submodules "$project"

  local paths_file
  paths_file="$(mktemp)"
  git -C "$ref" submodule foreach --recursive --quiet 'echo $sm_path' > "$paths_file"
  local npaths; npaths="$(wc -l < "$paths_file")"
  log "[submodule] 从 ${SEED_CLONE:-clone} 复制 ${npaths} 个 submodule -> ${clone_id}/${project}（零网络）..."

  docker run --rm \
    -v "$CLONES_DIR:/clones" \
    -v "$paths_file:/tmp/submodule_paths:ro" \
    "$image" bash -c "
      set -e
      ref=/clones/${SEED_CLONE:-clone}/${project}
      dst=/clones/${clone_id}/${project}
      if [ -d \"\$ref/.git/modules\" ]; then
        rm -rf \"\$dst/.git/modules\"
        cp -a \"\$ref/.git/modules\" \"\$dst/.git/modules\"
      fi
      while IFS= read -r path; do
        [ -z \"\$path\" ] && continue
        rm -rf \"\$dst/\$path\"
        mkdir -p \"\$(dirname \"\$dst/\$path\")\"
        cp -a \"\$ref/\$path\" \"\$dst/\$path\"
      done < /tmp/submodule_paths
    "
  rm -f "$paths_file"

  fix_clone_permissions "$project" "$clone_id" "$image"
  log "[submodule] sync + 本地 checkout ${clone_id}/${project} ..."
  git -C "$dst" submodule sync --recursive
  git -C "$dst" submodule update --init --recursive
  log "[submodule] ${clone_id}/${project} 完成"
}

# editable scipy 需要 meson build/ 缓存；从种子 clone（或任意已有 build 的 worker clone）复制，避免每容器重编译。
copy_build_cache_from_reference() {
  local project="$1" dst="$2" image="${3:-}"
  local clone_id; clone_id="$(basename "$(dirname "$dst")")"
  image="${image:-$(project_image "$project")}"
  local src_clone=""
  for c in "${SEED_CLONE:-clone}" clone1 clone2 clone3 clone4 clone5 clone6 clone7 clone8 clone9 clone10; do
    [ "$c" = "$clone_id" ] && continue
    if [ -d "$CLONES_DIR/${c}/${project}/build" ]; then
      src_clone="$c"
      break
    fi
  done
  [ -n "$src_clone" ] || { log "[build] WARN: 未找到 build/ 缓存，跳过"; return 0; }

  log "[build] 复制 ${src_clone}/build -> ${clone_id}/${project} ..."
  docker run --rm -v "$CLONES_DIR:/clones" "$image" bash -c "
    rm -rf /clones/${clone_id}/${project}/build
    cp -a /clones/${src_clone}/${project}/build /clones/${clone_id}/${project}/build
  "
  fix_clone_permissions "$project" "$clone_id" "$image"
}

# scipy：reference clone（干净 git）+ 从种子 clone 复制 submodule，不重复联网。
clone_with_submodules() {
  local project="$1" dst="$2" image="${3:-}"
  clone_from_reference "$project" "$dst"
  copy_submodules_from_seed "$project" "$dst" "$image"
  copy_build_cache_from_reference "$project" "$dst" "$image"
}

# 容器内 root 写入挂载目录后，用 root 容器把 clone 目录 chown 回宿主机用户。
# clone_id 形如 clone1 / clone10；image 可选，默认 patchguru-<project>-dev。
fix_clone_permissions() {
  local project="$1" clone_id="$2" image="${3:-}"
  local uidgid; uidgid="$(id -u):$(id -g)"
  image="${image:-$(project_image "$project")}"
  log "[perms] chown ${clone_id}/${project} -> ${uidgid}"
  if docker run --rm -v "$CLONES_DIR:/clones" "$image" \
      chown -R "${uidgid}" "/clones/${clone_id}/${project}" 2>/dev/null; then
    return 0
  fi
  docker run --rm -v "$CLONES_DIR:/clones" "$PY_IMAGE" \
    chown -R "${uidgid}" "/clones/${clone_id}/${project}"
}

# C 扩展项目：把种子 clone 整个目录（含已编译产物）复制到 worker clone N。
copy_built_clone() {
  local project="$1" idx="$2" image="$3"
  local clone_id="clone${idx}"
  local seed="${SEED_CLONE:-clone}"
  log "[clone] 复制 ${seed}/${project}（含编译产物）-> ${clone_id}（零重编译）"
  docker run --rm -v "$CLONES_DIR:/clones" "$image" bash -c "
    rm -rf /clones/${clone_id}/${project} &&
    mkdir -p /clones/${clone_id} &&
    cp -a /clones/${seed}/${project} /clones/${clone_id}/${project}
  "
  fix_clone_permissions "$project" "$clone_id" "$image"
}
