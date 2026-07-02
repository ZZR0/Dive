#!/bin/bash

IMAGE=pandas-dev
# Dockerfile 在 ee3ade7d0b 被删；最后一个有的 commit 是其父提交
DOCKERFILE_COMMIT=e0398c43e51c50b1213bce562cc62b382fb26681
GIT_PROXY="${GIT_PROXY:-socks5h://127.0.0.1:10808}"
PROXY="${CONTAINER_PROXY:-http://172.17.0.1:10810}"
BUILD_PROXY=(--build-arg "http_proxy=${PROXY}" --build-arg "https_proxy=${PROXY}"
             --build-arg "HTTP_PROXY=${PROXY}" --build-arg "HTTPS_PROXY=${PROXY}")
# shellcheck source=clone_common.sh
source "$(dirname "$0")/clone_common.sh"
GOLDEN="$(golden_path pandas)"

ensure_clones_dir

echo "Cleaning any existing pandas-dev containers"
docker rm -f pandas-dev1 pandas-dev2 pandas-dev3 2>/dev/null || true

echo "Creating golden clone of pandas"
sudo rm -rf "$GOLDEN"
mkdir -p "$(dirname "$GOLDEN")"
git clone https://github.com/pandas-dev/pandas.git "$GOLDEN"
cd "$GOLDEN"
echo "Checkout 到含 Dockerfile 的 commit ${DOCKERFILE_COMMIT}..."
git checkout "${DOCKERFILE_COMMIT}"
sed -i 's|^FROM python:.*|FROM docker.1ms.run/python:3.11|' Dockerfile

echo "Building dev image from golden"
DOCKER_BUILDKIT=1 docker build "${BUILD_PROXY[@]}" -t "$IMAGE" .
docker rm -f pandas-setup 2>/dev/null || true
docker run -t -d --name pandas-setup -v "${GOLDEN}:/home/pandas" "$IMAGE"
docker exec -w /home/pandas pandas-setup \
  python -m pip install -ve . --no-build-isolation --config-settings editable-verbose=true
docker exec -w /home/pandas pandas-setup python -m pip install coverage
echo "Commit image $IMAGE from pandas-setup"
docker commit pandas-setup "$IMAGE" >/dev/null
docker rm -f pandas-setup

for i in 1 2 3; do
  echo "Creating clone${i} from golden"
  copy_golden_to_clone pandas "$i"
  docker run -t -d --name "pandas-dev${i}" \
    -v "$(clone_repo_path pandas "$i"):/home/pandas" "$IMAGE"
done

cd ../../../PatchGuru
