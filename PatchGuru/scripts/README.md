# SpecInfer 批量运行脚本

这套脚本用于在隔离的 Docker 环境中，对多个项目的大量 PR 并行运行 SpecInfer。
核心思路：**每个项目的环境只构建一次并固化成镜像，之后用「复制已编译代码 + 复用镜像」快速铺开多个容器**，每个容器绑定一个 worker 串行处理 PR。

## 流水线总览

```
setup_env.sh        expand_clones.sh         watch_and_run.sh
─────────────  ───►  ────────────────  ───►  ─────────────────
clone1 + dev1        clone2..N + devN         等容器就绪后
装依赖 + 编译         复制已编译产物            启动并行批量
commit 成镜像         零重编译起容器            (run_all_specinfer.py)
```

## 目录结构

```
scripts/
├── lib/
│   ├── common.sh        # 唯一配置源：项目表 / 代理 / 路径 + log、docker_count
│   └── clone.sh         # clone_from_reference、copy_built_clone
├── setup_env.sh         # 首次建环境：clone1 + 装依赖 + commit 镜像
├── expand_clones.sh     # 扩容：建 clone2..NB_CLONES 容器
├── watch_and_run.sh     # 等容器就绪后启动批量
├── run_all_specinfer.py # 批量调度器（多线程 worker，每个绑定一个 clone/容器）
├── logs/                # 运行日志
└── legacy/              # 旧脚本归档（已被上面的脚本替代）
```

## 快速开始（三步）

```bash
# 1) 首次建环境（每个项目一次；自动 commit 成 patchguru-<project>-dev 镜像）
bash scripts/setup_env.sh scipy

# 2) 扩容到 10 个 clone/容器
NB_CLONES=10 bash scripts/expand_clones.sh scipy

# 3) 等容器就绪后启动批量（可指定项目，缺省跑全部）
NB_CLONES=10 WORKERS=10 bash scripts/watch_and_run.sh pandas scipy keras marshmallow
```

也可以跳过 `watch_and_run.sh`，在容器已就绪时直接调用调度器：

```bash
uv run python scripts/run_all_specinfer.py \
  --projects pandas scipy keras marshmallow \
  --workers 10 --nb-clones 10 --timeout 1800
```

## 配置（统一在 `lib/common.sh`）

| 项 | 说明 | 默认值 |
|----|------|--------|
| `NB_CLONES` | clone/容器池目标大小 | `10` |
| `PY_IMAGE` | 首次建环境的基础镜像 | `python:3.11` |
| `GIT_PROXY` | 宿主机 git clone 代理 | `socks5h://127.0.0.1:10808` |
| `CONTAINER_PROXY` | 容器内 pip/build 代理 | `http://host.docker.internal:10810` |

项目元数据由 `project_repo` / `project_kind` / `project_image` / `container_prefix` 提供。
**新增一个项目**只需在 `lib/common.sh` 的这几个函数里各加一行：

```bash
project_repo()  # 加: foo) echo "org/foo" ;;
project_kind()  # 加: foo) echo "pure|cext|conda" ;;
```

`kind` 决定建环境/扩容方式：

| kind | 含义 | 示例 | 扩容方式 |
|------|------|------|----------|
| `pure` | 纯 Python，editable 安装 | keras、marshmallow | reference clone |
| `cext` | C 扩展，pip + meson 编译 | pandas | 复制已编译 clone |
| `conda` | 依赖 conda/mamba 环境 | scipy | reference clone + **从 clone1 复制 submodule**（clone1 只联网拉一次） |

scipy 从 `cp -a` 迁移到 reference clone 时：

```bash
FORCE_REFRESH=1 NB_CLONES=10 bash scripts/expand_clones.sh scipy
```

## `run_all_specinfer.py` 参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `--projects` | 要运行的项目列表 | 全部 |
| `--workers` | 并行 worker 数（每个绑定一个 clone） | `10` |
| `--nb-clones` | 容器池大小（传给 `PATCHGURU_NB_CLONES`） | `10` |
| `--timeout` | 单个 PR 超时秒数，超时整进程组 SIGKILL | `1800` |
| `--force` | 忽略缓存，强制重新分析（重新调用 LLM） | 关 |
| `--cache-dir` | 结果缓存目录 | `.cache_rerun` |
| `--pr-file` | 只跑文件中的 PR（每行 `项目 PR号`） | 无 |
| `--logs-subdir` | 事件日志子目录 `logs/<subdir>/...` | `rerun` |

不加 `--force` 时，已完成的 PR 会被自动跳过（断点续跑）。

只补跑失败 PR 示例：

```bash
uv run python scripts/run_all_specinfer.py \
  --pr-file scripts/rerun_remaining_failed.txt \
  --force --workers 10 --nb-clones 10
```

## 维护脚本

| 脚本 | 用途 |
|------|------|
| `fix_clone_permissions.sh` | 容器 root 写入后 chown，修复 git clean |
| `fix_keras_clones.sh` | keras clone2..N 的 origin 改回 GitHub 并 fetch |
| `fix_marshmallow_clones.sh` | 清理 `marshmallow/` 与 `src/marshmallow/` 重复包目录 |

## 产物与日志

- **分析结果**：`<cache-dir>/oracles/<project>/<pr_nb>/`（**不要用** `downloads/PatchGuruData/cache/`，那是论文下载的旧结果）
  - `specification.py`：LLM 推断出的 oracle 测试代码
  - `results.json`：完整状态（结论 BUG/NORMAL、执行记录、LLM 调用次数等）
  - `phase2/`：第二阶段（规格泛化）结果
- **进度汇总**：`scripts/run_all_progress.jsonl`（每个 PR 一行）
- **事件日志**：`logs/<subdir>/<project>/<clone_id>/<session>/events.log`
- **批量 stdout**：`scripts/logs/run_<project>.log`

## 隔离机制

每个 PR 是独立子进程；进程启动时对绑定的 clone 做 `git reset --hard` + `clean -fd` 再 `checkout` 目标 commit；容器内每次执行前清空 `/tmp/PatchGuru`。因此不同 PR 之间互不污染（`.gitignore` 内的 `build/` 编译产物会保留，仅在切换差异较大的 commit 时增量重编译）。
