#!/bin/bash

IMAGE=marshmallow-dev
PY=docker.1ms.run/python:3.10
# shellcheck source=clone_common.sh
source "$(dirname "$0")/clone_common.sh"
GOLDEN="$(golden_path marshmallow)"

ensure_clones_dir

echo "Cleaning any existing marshmallow-dev containers"
docker rm -f marshmallow-dev1 marshmallow-dev2 marshmallow-dev3 2>/dev/null || true

echo "Creating golden clone of marshmallow"
sudo rm -rf "$GOLDEN"
mkdir -p "$(dirname "$GOLDEN")"
git clone https://github.com/marshmallow-code/marshmallow.git "$GOLDEN"
cd "$GOLDEN"

echo "Building dev image from golden"
docker rm -f marshmallow-setup 2>/dev/null || true
docker run -t -d --name marshmallow-setup -v "${GOLDEN}:/home/marshmallow" "$PY"
docker exec -w /home/marshmallow marshmallow-setup pip install -e '.[dev]'
docker exec -w /home/marshmallow marshmallow-setup pip install coverage
echo "Commit image $IMAGE from marshmallow-setup"
docker commit marshmallow-setup "$IMAGE" >/dev/null
docker rm -f marshmallow-setup

for i in 1 2 3; do
  echo "Creating clone${i} from golden"
  copy_golden_to_clone marshmallow "$i"
  docker run -t -d --name "marshmallow-dev${i}" \
    -v "$(clone_repo_path marshmallow "$i"):/home/marshmallow" "$IMAGE"
done

cd ../../../PatchGuru
