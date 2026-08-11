#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$project_root"

case "$project_root" in
  /mnt/*)
    echo "ERRO: o codigo deve ficar no filesystem Linux (por exemplo, ~/src/file_manager)." >&2
    exit 1
    ;;
esac

# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "26.04" ]]; then
  echo "ERRO: esperado Ubuntu 26.04; encontrado ${PRETTY_NAME:-desconhecido}." >&2
  exit 1
fi

declare -a sources=(
  "${OFC_FILECR_DATA_DIR:-/mnt/d/dev/Torrents/FileCR/data}/inventory.sqlite3"
  "${OFC_1337X_DATA_DIR:-/mnt/d/dev/Torrents/1337xVault/data}/inventory.sqlite3"
  "${OFC_WEB_DATA_DIR:-/mnt/d/dev/Torrents/FileCRWeb/data}/catalog_metadata.sqlite3"
  "${OFC_SUBTITLE_DATA_DIR:-/mnt/d/dev/Torrents/SubtitleVault/data}/subtitles.sqlite3"
)

for source_db in "${sources[@]}"; do
  if [[ ! -f "$source_db" || ! -r "$source_db" ]]; then
    echo "ERRO: inventario somente leitura indisponivel: $source_db" >&2
    exit 1
  fi
done

if ! command -v docker >/dev/null 2>&1; then
  echo "ERRO: habilite a integracao WSL do Docker Desktop para Ubuntu-26.04." >&2
  exit 1
fi
proxy_socket="/mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.proxy.sock"
if [[ ! -S /var/run/docker.sock && -S "$proxy_socket" && -z "${DOCKER_HOST:-}" ]]; then
  export DOCKER_HOST="unix://$proxy_socket"
fi
docker_ready=0
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if docker version >/dev/null 2>&1; then
    docker_ready=1
    break
  fi
  sleep 0.25
done
if [[ "$docker_ready" -ne 1 ]]; then
  echo "ERRO: proxy local do Docker Desktop nao ficou pronto." >&2
  exit 1
fi
docker compose version >/dev/null

mkdir -p storage/{hls,media,resume,snapshots,uploads,logs} logs
if [[ ! -f .env ]]; then
  umask 077
  password="$(openssl rand -hex 32)"
  token="$(openssl rand -hex 32)"
  pepper="$(openssl rand -hex 32)"
  awk -v password="$password" -v token="$token" -v pepper="$pepper" '
    /^POSTGRES_PASSWORD=/ {$0="POSTGRES_PASSWORD=" password}
    /^OFC_INTERNAL_TOKEN=/ {$0="OFC_INTERNAL_TOKEN=" token}
    /^OFC_SESSION_PEPPER=/ {$0="OFC_SESSION_PEPPER=" pepper}
    {print}
  ' .env.wsl.example > .env
  chmod 600 .env
fi

drive_root="$(awk -F= '$1 == "OFC_GDRIVE_ROOT_ID" {print $2}' .env | tail -n 1)"
if [[ -z "$drive_root" || "$drive_root" == "YOUR_GOOGLE_DRIVE_FOLDER_ID" ]]; then
  echo "ERRO: configure OFC_GDRIVE_ROOT_ID em .env." >&2
  exit 1
fi

docker compose -p file-manager-wsl \
  -f compose.yaml -f compose.wsl.yaml config --quiet

echo "File Manager WSL pronto em $project_root"
echo "Inventarios de D: validados; nenhum banco foi copiado ou alterado."
