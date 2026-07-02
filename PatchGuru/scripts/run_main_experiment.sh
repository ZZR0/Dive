#!/usr/bin/env bash
# PatchGuru 主试验（RQ1 & RQ3）运行脚本 — 使用 uv 环境
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "========================================"
echo " PatchGuru 主试验 (RQ1 & RQ3)"
echo " 工作目录: $ROOT"
echo "========================================"

# 1) 检查 uv 环境
if [ ! -d ".venv/bin" ]; then
  echo "[1/4] 创建 uv 虚拟环境..."
  uv venv .venv --python 3.11
  uv pip install -e . pandas tqdm
else
  echo "[1/4] uv 虚拟环境已存在: .venv"
fi

# 2) 检查复现数据（Figshare: .cache + logs）
need_cache=(
  ".cache/pr_ids/marshmallow.txt"
  ".cache/oracles/marshmallow"
  "logs/marshmallow"
)
missing=0
for p in "${need_cache[@]}"; do
  if [ ! -e "$p" ]; then
    echo "[2/4] 缺少复现数据: $p"
    missing=1
  fi
done

if [ "$missing" -eq 1 ]; then
  echo "[2/4] 未找到完整复现数据。"
  echo "      请从 Figshare 手动下载并解压到 PatchGuru 根目录:"
  echo "      https://figshare.com/s/02089e7f903926ad0cdf"
  echo "      解压后应包含 .cache/ 与 logs/ 两个顶层目录。"
  echo ""
  echo "      （本机 curl 访问 Figshare 返回 403，无法自动下载）"
  exit 1
fi
echo "[2/4] 复现数据检查通过"

# 3) 无头模式绘图
export MPLBACKEND=Agg
export PYTHONDONTWRITEBYTECODE=1
mkdir -p .cache/violin_plot

# 4) 运行 RQ1_3
echo "[3/4] 运行 patchguru.experiments.RQ1_3 ..."
uv run python -m patchguru.experiments.RQ1_3

echo "[4/4] 主试验完成。图表保存在 .cache/violin_plot/"
