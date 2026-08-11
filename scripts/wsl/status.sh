#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$project_root"
docker compose -p file-manager-wsl \
  -f compose.yaml -f compose.wsl.yaml ps
