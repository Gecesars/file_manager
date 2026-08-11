#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$project_root"

proxy_socket="/mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.proxy.sock"
if [[ ! -S /var/run/docker.sock && -S "$proxy_socket" && -z "${DOCKER_HOST:-}" ]]; then
  export DOCKER_HOST="unix://$proxy_socket"
fi

./scripts/wsl/prepare.sh

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --disable-pip-version-check -e '.[dev]'
.venv/bin/python -m pytest

if command -v node >/dev/null 2>&1; then
  node --check src/ofc_media/static/app.js
fi

docker compose -p file-manager-wsl \
  -f compose.yaml -f compose.wsl.yaml build
docker compose -p file-manager-wsl \
  -f compose.yaml -f compose.wsl.yaml config --quiet

echo "Validacao concluida. Nenhum container foi iniciado."
