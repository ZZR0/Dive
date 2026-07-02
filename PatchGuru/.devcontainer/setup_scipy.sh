#!/bin/bash
set -euo pipefail

IMAGE=scipy-dev
PY=docker.1ms.run/python:3.10
GIT_PROXY="${GIT_PROXY:-socks5h://127.0.0.1:10808}"
PROXY="${CONTAINER_PROXY:-http://172.17.0.1:10810}"
PROXY_ENV=(-e "http_proxy=${PROXY}" -e "https_proxy=${PROXY}"
           -e "HTTP_PROXY=${PROXY}" -e "HTTPS_PROXY=${PROXY}")
DEVCONTAINER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=clone_common.sh
source "$(dirname "$0")/clone_common.sh"
GOLDEN="$(golden_path scipy)"

ensure_clones_dir

echo "Cleaning any existing scipy-dev containers"
docker rm -f scipy-dev1 scipy-dev2 scipy-dev3 2>/dev/null || true

echo "Creating golden clone of scipy"
sudo rm -rf "$GOLDEN"
mkdir -p "$(dirname "$GOLDEN")"
git clone https://github.com/scipy/scipy.git "$GOLDEN"
cd "$GOLDEN"
git -c http.proxy="${GIT_PROXY}" -c https.proxy="${GIT_PROXY}" submodule update --init

echo "Building dev image from golden"
docker rm -f scipy-setup 2>/dev/null || true
docker run -t -d "${PROXY_ENV[@]}" --name scipy-setup -v "${GOLDEN}:/home/scipy" "$PY"
docker cp "${DEVCONTAINER}/setup_scipy_to_run_in_container.sh" scipy-setup:/root/setup.sh
docker exec "${PROXY_ENV[@]}" scipy-setup chmod +x /root/setup.sh
docker exec "${PROXY_ENV[@]}" -w /home/scipy scipy-setup /root/setup.sh
docker exec scipy-setup test -d /root/conda/envs/scipy-dev
echo "Commit image $IMAGE from scipy-setup"
docker commit scipy-setup "$IMAGE" >/dev/null
docker rm -f scipy-setup

for i in 1 2 3; do
  echo "Creating clone${i} from golden"
  copy_golden_to_clone scipy "$i"
  docker run -t -d --name "scipy-dev${i}" \
    -v "$(clone_repo_path scipy "$i"):/home/scipy" "$IMAGE"
done

cd ../../../PatchGuru
