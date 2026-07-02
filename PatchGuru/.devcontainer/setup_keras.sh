#!/bin/bash

IMAGE=keras-dev
PY=docker.1ms.run/python:3.11
GIT_PROXY="${GIT_PROXY:-socks5h://127.0.0.1:10808}"
PROXY="${CONTAINER_PROXY:-http://172.17.0.1:10810}"
PROXY_ENV=(-e "http_proxy=${PROXY}" -e "https_proxy=${PROXY}"
           -e "HTTP_PROXY=${PROXY}" -e "HTTPS_PROXY=${PROXY}")
# shellcheck source=clone_common.sh
source "$(dirname "$0")/clone_common.sh"
GOLDEN="$(golden_path keras)"

ensure_clones_dir

echo "Cleaning any existing keras-dev containers"
docker rm -f keras-dev1 keras-dev2 keras-dev3 2>/dev/null || true

echo "Creating golden clone of keras"
sudo rm -rf "$GOLDEN"
mkdir -p "$(dirname "$GOLDEN")"
git clone https://github.com/keras-team/keras.git "$GOLDEN"
cd "$GOLDEN"

echo "Building dev image from golden"
docker rm -f keras-setup 2>/dev/null || true
docker run -t -d --name keras-setup -v "${GOLDEN}:/home/keras" "$PY"
docker exec "${PROXY_ENV[@]}" -w /home/keras keras-setup pip install -r requirements.txt
docker exec "${PROXY_ENV[@]}" -w /home/keras keras-setup pip install -e ./
docker exec "${PROXY_ENV[@]}" -w /home/keras keras-setup pip install coverage
echo "Commit image $IMAGE from keras-setup"
docker commit keras-setup "$IMAGE" >/dev/null
docker rm -f keras-setup

for i in 1 2 3; do
  echo "Creating clone${i} from golden"
  copy_golden_to_clone keras "$i"
  docker run -t -d --name "keras-dev${i}" \
    -v "$(clone_repo_path keras "$i"):/home/keras" "$IMAGE"
done

cd ../../../PatchGuru
