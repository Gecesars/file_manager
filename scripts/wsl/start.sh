#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$project_root"
./scripts/wsl/prepare.sh

docker compose -p file-manager-wsl \
  -f compose.yaml -f compose.wsl.yaml up -d
docker compose -p file-manager-wsl \
  -f compose.yaml -f compose.wsl.yaml ps

public_port="$(awk -F= '$1 == "OFC_PUBLIC_PORT" {print $2}' .env | tail -n 1)"
echo "File Manager WSL disponivel em http://127.0.0.1:${public_port:-5091}"
