#!/bin/bash
set -euo pipefail

# 继承 docker -e 传入的代理（setup_scipy.sh CONTAINER_PROXY）
export http_proxy="${http_proxy:-${HTTP_PROXY:-}}"
export https_proxy="${https_proxy:-${HTTPS_PROXY:-}}"
export HTTP_PROXY="${HTTP_PROXY:-$http_proxy}"
export HTTPS_PROXY="${HTTPS_PROXY:-$https_proxy}"

apt update
apt install -y gcc g++ gfortran libopenblas-dev liblapack-dev pkg-config

WGET_PROXY_OPTS=()
if [ -n "${https_proxy:-}" ]; then
  WGET_PROXY_OPTS=(-e use_proxy=yes -e "https_proxy=${https_proxy}"
                  -e "http_proxy=${http_proxy:-$https_proxy}")
fi
wget "${WGET_PROXY_OPTS[@]}" -O Miniforge3.sh \
  "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh"
bash Miniforge3.sh -b -p "${HOME}/conda"

export MAMBA_ROOT_PREFIX="${HOME}/conda"
source "${HOME}/conda/etc/profile.d/conda.sh"

# 非交互 setup 用 mamba run，无需 mamba shell init / activate
mamba env create -f environment.yml -y
mamba run -n scipy-dev pip install -e . --no-build-isolation

mamba run -n scipy-dev python -c "import scipy; print('scipy setup OK:', scipy.__file__)"
