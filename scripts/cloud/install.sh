#!/usr/bin/env bash
# Cloud Agent `install`: idempotent repository bootstrap run after checkout.
# Durable, source-derived setup only — no daemons and no long-running
# processes (those belong in start.sh). Safe to run repeatedly.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

cd "$(dirname "$0")/../.."

# uv itself is a stable system tool captured by the base image/snapshot; only
# install it here as a fallback so a bare base can still bootstrap.
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# Project and render-worker dependencies, pinned by the committed lockfiles.
uv sync
uv sync --project workers/render --frozen

# Local defaults; copy only when absent so an override is never clobbered.
if [ ! -f .env ]; then
  cp .env.example .env
fi

echo "install.sh: dependencies synced and .env present"
