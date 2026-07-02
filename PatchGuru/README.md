# PatchGuru: Patch Oracle Inference from Natural Language Artifacts with Large Language Models

PatchGuru is an LLM-powered tool that automatically infer patch oracles from natural language artifacts in pull requests and utilizes them to detect the inconsistencies between code changes and their corresponding descriptions.

Paper: [TO BE RELEASE]()

## Installation

### Setup Enviroments of PatchGuru

PatchGuru uses two kinds of Docker containers:

- A Visual Studio Code Dev Container for running PatchGuru itself. See [devcontainer.json](.devcontainer/devcontainer.json).
- Docker-in-docker containers for target projects to analyze with PatchGuru. These containers are created when creating the dev container. See [postCreateCommands.sh](.devcontainer/postCreateCommand.sh).

To install and run PatchGuru, follow these steps:

1) Install [Visual Studio Code](https://code.visualstudio.com/download) and its ["Dev Containers" extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers).

2) Open PatchGuru in Visual Studio Code:

   ```bash
   code <main_dir_of_this_project>
   ```

3) In Visual Studio Code, build the Dev Container and reopen the project in the container:

    `Ctrl + Shift + P` → `Dev Containers: Rebuild and Reopen in Container`

4) In the main directory, create a file `.openai_token` with an OpenAI API key.

5) In the main directory, create a file `.github_token` with a GitHub API key (or `.github_tokens` with one token per line for batch runs).

### Setup Enviroments of Target Projects (Dev Container 方式)

Currently, PatchGuru supports four Python projects: **pandas**, **scipy**, **keras**, **marshmallow**.

1. Uncomment selected projects in [postCreateCommands.sh](.devcontainer/postCreateCommand.sh), or
2. Run `bash .devcontainer/setup_{project}` inside the PatchGuru dev container.

### Setup Enviroments of Target Projects (宿主机批量脚本，推荐）

本仓库在 `scripts/` 下提供了一套宿主机批量运行脚本，clone 目录默认在 PatchGuru **同级**的 `../clones/`。详细说明见 [scripts/README.md](scripts/README.md)。

**首次建环境（每个项目一次）**：

```bash
cd PatchGuru
bash scripts/setup_env.sh pandas    # 或 scipy / keras / marshmallow
```

**扩容到 N 个并行 worker（默认 10）**：

```bash
NB_CLONES=10 bash scripts/expand_clones.sh pandas
```

scipy 若需重建全部 clone（reference clone + submodule 复制）：

```bash
FORCE_REFRESH=1 NB_CLONES=10 bash scripts/expand_clones.sh scipy
```

**一次性修复权限**（容器 root 写入挂载目录后 git clean 失败时）：

```bash
bash scripts/fix_clone_permissions.sh
```

**keras clone 缺少旧 commit**（`reference is not a tree`）时：

```bash
bash scripts/fix_keras_clones.sh
```

**marshmallow 双包路径冲突**（`Multiple files or folders could be module marshmallow`）时：

```bash
bash scripts/fix_marshmallow_clones.sh
```

## Usage

> [!NOTE]
> PatchGuru currently supports only pull requests that modify a single source function (excluding test functions).

### 单个 PR

`patchguru/SpecInfer.py` is the main entry point:

```bash
uv run python -m patchguru.SpecInfer --project marshmallow --pr_nb 707
```

强制重新分析（忽略缓存）：

```bash
uv run python -m patchguru.SpecInfer --project marshmallow --pr_nb 707 --force
```

### 批量并行（SpecInfer benchmark）

PR 列表在 `.cache/pr_ids/<project>.txt`（每行一个 PR 号）。**等容器就绪后**启动：

```bash
# 方式 A：等待容器 + 启动（推荐）
NB_CLONES=10 WORKERS=10 bash scripts/watch_and_run.sh pandas scipy keras marshmallow

# 方式 B：直接调用调度器
PATCHGURU_HOST_PROXY=socks5h://127.0.0.1:10808 \
http_proxy=socks5h://127.0.0.1:10808 https_proxy=socks5h://127.0.0.1:10808 \
PYTHONUNBUFFERED=1 \
uv run python scripts/run_all_specinfer.py \
  --projects pandas scipy keras marshmallow \
  --workers 10 --nb-clones 10 --timeout 2400 \
  --cache-dir .cache_rerun \
  --logs-subdir rerun
```

**只跑指定 PR 列表**（每行 `项目 PR号`）：

```bash
uv run python scripts/run_all_specinfer.py \
  --pr-file scripts/rerun_remaining_failed.txt \
  --force --workers 10 --nb-clones 10
```

**断点续跑**：不加 `--force` 时，已完成（`is_completed`）的 PR 自动跳过。

### 代理与环境变量

| 变量 | 用途 | 默认 |
|------|------|------|
| `GIT_PROXY` | 宿主机 `git clone/fetch` | `socks5h://127.0.0.1:10808` |
| `PATCHGURU_HOST_PROXY` | PyGithub / `requests` 拉 PR diff | 同 `GIT_PROXY` |
| `CONTAINER_PROXY` | 容器内 `pip install` / 编译 | `http://host.docker.internal:10810` |
| `PATCHGURU_CACHE_DIR` | 结果输出目录 | `.cache_rerun` |
| `PATCHGURU_CLONE_ID` | 绑定单个 clone（调试） | 未设置 |
| `PATCHGURU_NB_CLONES` | clone 池大小 | `3`（脚本里设为 `10`） |

批量脚本会在子进程里自动注入 `http_proxy` / `PATCHGURU_HOST_PROXY`。

### 产物与日志

| 路径 | 内容 |
|------|------|
| `.cache_rerun/oracles/<project>/<pr>/results.json` | Phase 1 分析结果（**评估请用这个目录**） |
| `.cache_rerun/oracles/<project>/<pr>/phase2/results.json` | Phase 2 规格泛化结果 |
| `.cache_rerun/oracles/<project>/<pr>/specification.py` | 生成的 oracle 测试代码 |
| `scripts/run_all_progress.jsonl` | 批量进度（每 PR 一行） |
| `scripts/logs/*.log` | 批量 stdout 总览 |
| `logs/<subdir>/<project>/<clone_id>/<session>/events.log` | 单 PR 详细事件日志 |

> **注意**：`downloads/PatchGuruData/cache/` 是论文 Figshare 下载的**别人实验结果**（时间戳较早），不要与 `.cache_rerun/` 混淆。

### 分析流水线简述

每个 PR 经过 SpecInfer 两阶段：

1. **Phase 1（Specification Inference）**：Intent 分析 → spec 验证（`error_repair`）→ **Self Review**（仅当 spec 执行出现 `AssertionError` 时调用 LLM，否则跳过并判 `NORMAL`）
2. **Phase 2（Specification Generalization）**：仅 Phase 1 结论为 `NORMAL` 时运行，流程类似

`review_conclusion`：`BUG`（发现不一致）/ `NORMAL`（一致）/ `MISMATCH`（测试本身有问题）。

## Results Reported in the Paper

Download data from [Figshare](https://figshare.com/s/02089e7f903926ad0cdf) (`.cache` + `.logs`).

### RQ1 & RQ3

官方复现脚本（读取 `.cache/oracles/` 和 `logs/<project>/`）：

```bash
python3 -m patchguru.experiments.RQ1_3
```

Table 1 指标：`#Warnings`（BUG）、`#Normal`、`#Oracles`、`#Failures`。Precision 需 `WarningAnnotation.xlsx` 人工 TP/FP 标注。

**用本次批量结果（`.cache_rerun`）统计 RQ1**，逻辑与 `RQ1_3.py` 相同，但目录不同：

```bash
# 快速统计（不依赖 logs）
cd PatchGuru
python3 -c "
import json
from pathlib import Path
for proj in ['pandas','scipy','keras','marshmallow']:
    ids = open(f'.cache/pr_ids/{proj}.txt').read().split()
    w=n=fail=0
    for i in ids:
        r = Path(f'.cache_rerun/oracles/{proj}/{i}/results.json')
        if not r.exists(): fail+=1; continue
        d=json.loads(r.read_text())
        if d.get('stage')!='completed': fail+=1; continue
        if d.get('review_conclusion')=='BUG': w+=1
        elif d.get('review_conclusion')=='NORMAL':
            p2=Path(f'.cache_rerun/oracles/{proj}/{i}/phase2/results.json')
            if p2.exists() and json.loads(p2.read_text()).get('stage')=='completed':
                p2d=json.loads(p2.read_text())
                if p2d.get('review_conclusion')=='BUG': w+=1
                elif p2d.get('review_conclusion')=='NORMAL': n+=1
                else: fail+=1
            else: fail+=1
        else: fail+=1
    print(f'{proj}: Warnings={w} Normal={n} Oracles={w+n} Failures={fail}')
"
```

与论文下载结果对比时，论文缓存路径为 `downloads/PatchGuruData/cache/oracles/`。

### RQ2

```bash
python3 -m patchguru.experiments.RQ2
```

## 常见问题

| 现象 | 处理 |
|------|------|
| `git clean` 权限错误（root 拥有文件） | `bash scripts/fix_clone_permissions.sh` |
| scipy checkout `untracked ... PROPACK` | 已在 `ClonedRepoManager` 用 `checkout -f` 修复 |
| keras `reference is not a tree` | `bash scripts/fix_keras_clones.sh` |
| marshmallow 双 `marshmallow/` 路径 | `bash scripts/fix_marshmallow_clones.sh`（checkout 后自动清理） |
| 批量日志不实时刷新 | 用 `PYTHONUNBUFFERED=1`；或看 `logs/` 下 `events.log` |
| `✓ done` 但无 `results.json` | 进程 exit 0 但分析早退（如 retrieve 失败），以 `results.json` 为准 |
