#!/usr/bin/env bash
# Cloud Agent `start`: per-boot runtime reconciliation. Brings up the Docker
# daemon (there is no systemd in the VM), the Postgres 16 service the app and
# tests read on host port 5433, and applies migrations. Idempotent: tolerates a
# daemon or container that is already running, and returns once the database is
# reachable and migrated.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

cd "$(dirname "$0")/../.."

# --- Docker daemon --------------------------------------------------------
# The nested VM cannot use the default overlayfs snapshotter (whiteout files
# are rejected), so /etc/docker/daemon.json pins the fuse-overlayfs storage
# driver. Start dockerd detached if it is not already answering.
if ! sudo docker info >/dev/null 2>&1; then
  if [ ! -f /etc/docker/daemon.json ]; then
    sudo mkdir -p /etc/docker
    printf '%s\n' '{' \
      '  "storage-driver": "fuse-overlayfs",' \
      '  "features": { "containerd-snapshotter": false }' \
      '}' | sudo tee /etc/docker/daemon.json >/dev/null
  fi
  # Redirect as root: an unprivileged shell cannot open /var/log itself.
  sudo sh -c 'setsid dockerd >/var/log/dockerd.log 2>&1 </dev/null &'
  for _ in $(seq 1 60); do
    if sudo docker info >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
fi

if ! sudo docker info >/dev/null 2>&1; then
  echo "start.sh: dockerd did not become ready" >&2
  sudo tail -n 40 /var/log/dockerd.log >&2 || true
  exit 1
fi

# Let the unprivileged `ubuntu` user (and the Makefile's plain `docker`
# invocations) reach the daemon without sudo. A world-accessible socket keeps
# this independent of whether the login session already carries the docker
# group; this is a single-user development VM.
sudo chmod 666 /var/run/docker.sock 2>/dev/null || true

# --- Postgres + migrations ------------------------------------------------
docker compose up -d --wait
CORRIDOR_LEGACY_DEV_LOGIN=1 uv run alembic upgrade head

echo "start.sh: Postgres 16 is up on host port 5433 and migrations are applied"
