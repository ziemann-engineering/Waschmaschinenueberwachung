#!/usr/bin/env bash

set -Eeuo pipefail

SERVICE_NAME="${SERVICE_NAME:-app}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/api/health}"
HEALTH_ATTEMPTS="${HEALTH_ATTEMPTS:-30}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$script_dir/docker-compose.yml" ]]; then
    deploy_dir="$script_dir"
elif [[ -f "$script_dir/../docker-compose.yml" ]]; then
    deploy_dir="$(cd -- "$script_dir/.." && pwd)"
else
    echo "Error: docker-compose.yml not found beside this script or one directory above." >&2
    exit 1
fi

cd "$deploy_dir"

if ! command -v docker >/dev/null 2>&1; then
    echo "Error: Docker is not installed or not on PATH." >&2
    exit 1
fi

if docker compose version >/dev/null 2>&1; then
    compose=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    compose=(docker-compose)
else
    echo "Error: neither 'docker compose' nor 'docker-compose' is available." >&2
    exit 1
fi

if ! command -v git >/dev/null 2>&1; then
    echo "Error: Git is not installed or not on PATH." >&2
    exit 1
fi

if ! command -v rsync >/dev/null 2>&1; then
    echo "Error: rsync is not installed or not on PATH." >&2
    exit 1
fi

repository_url="https://github.com/ziemann-engineering/Waschmaschinenueberwachung.git"

echo "Pulling server updates from GitHub..."
source_dir="$(mktemp -d)"
cleanup_source() {
    rm -rf -- "$source_dir"
}
trap cleanup_source EXIT

echo "Fetching server updates from GitHub..."
git clone --depth 1 --filter=blob:none --sparse \
    "$repository_url" "$source_dir"
git -C "$source_dir" sparse-checkout set server

echo "Synchronizing server source..."
rsync -a --delete \
    --exclude='data/' \
    --exclude='backups/' \
    --exclude='.update.lock' \
    "$source_dir/server/" "$deploy_dir/"
required_files=(Dockerfile requirements.txt data/config.json)
for required_file in "${required_files[@]}"; do
    if [[ ! -f "$required_file" ]]; then
        echo "Error: required deployment file is missing: $deploy_dir/$required_file" >&2
        exit 1
    fi
done

lock_file="$deploy_dir/.update.lock"
exec 9>"$lock_file"
if command -v flock >/dev/null 2>&1 && ! flock -n 9; then
    echo "Error: another update is already running." >&2
    exit 1
fi

app_stopped=false
on_error() {
    exit_code=$?
    echo "Update failed (exit $exit_code). Current service status:" >&2
    "${compose[@]}" ps >&2 || true
    "${compose[@]}" logs --tail=80 "$SERVICE_NAME" >&2 || true
    if [[ "$app_stopped" == true ]]; then
        echo "Attempting to start the service again..." >&2
        "${compose[@]}" up -d --no-build "$SERVICE_NAME" >&2 || true
    fi
    exit "$exit_code"
}
trap on_error ERR

echo "Validating Docker Compose configuration..."
"${compose[@]}" config --quiet

echo "Building the updated image while the current service remains online..."
"${compose[@]}" build --pull "$SERVICE_NAME"

echo "Stopping $SERVICE_NAME for a consistent backup..."
"${compose[@]}" stop "$SERVICE_NAME"
app_stopped=true

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="$deploy_dir/backups/$timestamp"
mkdir -p "$backup_dir"
cp --preserve=mode,timestamps data/config.json "$backup_dir/config.json"

if [[ -f data/washing_machines.db ]]; then
    cp --preserve=mode,timestamps data/washing_machines.db "$backup_dir/washing_machines.db"
fi

echo "Starting the updated service..."
"${compose[@]}" up -d --no-build --remove-orphans "$SERVICE_NAME"
app_stopped=false

echo "Waiting for the application health check..."
healthy=false
for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
    if "${compose[@]}" exec -T "$SERVICE_NAME" python -c \
        "import urllib.request; urllib.request.urlopen('${HEALTH_URL}', timeout=3).read()" \
        >/dev/null 2>&1; then
        healthy=true
        break
    fi
    sleep 2
done

if [[ "$healthy" != true ]]; then
    echo "Error: application did not become healthy at $HEALTH_URL." >&2
    false
fi

if [[ "$BACKUP_RETENTION_DAYS" =~ ^[0-9]+$ ]]; then
    find "$deploy_dir/backups" -mindepth 1 -maxdepth 1 -type d \
        -mtime "+$BACKUP_RETENTION_DAYS" -exec rm -rf -- {} +
fi

trap - ERR
echo "Update complete. Backup: $backup_dir"
"${compose[@]}" ps