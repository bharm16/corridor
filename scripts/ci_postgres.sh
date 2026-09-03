#!/usr/bin/env bash
#
# Start the runner image's own PostgreSQL 16 on the port the suite expects.
#
# This replaces a `services:` container. A service container is pulled and
# health-checked before the job's first step runs, so none of its cost can
# overlap with anything: "Initialize containers" measured a 21s median and a
# 33s worst case on the job that decided the gate's wall clock, and every one
# of the run's nine test jobs paid it separately (#595). The ubuntu-24.04
# image already ships PostgreSQL 16.15 with the service stopped, so bringing
# it up is local work that downloads nothing and runs alongside the rest of
# the setup.
#
# The cluster is recreated rather than merely started, because the tests must
# meet the same server the container gave them. postgres:16 initdb's under
# `LANG=en_US.utf8`, so its template0, template1 and every database cloned
# from them collate as `en_US.utf8` with the libc provider; the image's
# packaged cluster is initdb'd under whatever locale the image build had.
# Collation is not a detail here — it decides ORDER BY, and #551 already
# found one test whose result depended on how two strings sorted.
#
# Durability is then turned off: a CI cluster that loses power has nothing
# worth recovering. The container could not be given these settings at all,
# because GitHub Actions passes `options` to `docker create` rather than to
# the server. Nothing else about the server changes.
set -euo pipefail

version=16
cluster=main
port=5433

if ! command -v pg_createcluster >/dev/null; then
  echo "no Debian PostgreSQL cluster tooling on this runner image" >&2
  exit 1
fi

# The harness requires PostgreSQL 16 (tests/conftest.py, tests/test_boot.py),
# so a runner image that stops shipping it must fail here and be read, not be
# worked around.
if [ ! -d "/usr/lib/postgresql/$version" ]; then
  echo "this runner image has no PostgreSQL $version" >&2
  ls /usr/lib/postgresql >&2 || true
  exit 1
fi

if ! locale -a | grep -qix en_US.utf8; then
  sudo locale-gen en_US.UTF-8 >/dev/null
fi
if pg_lsclusters -h | awk '{print $1 "/" $2}' | grep -qx "$version/$cluster"; then
  sudo pg_dropcluster --stop "$version" "$cluster"
fi
sudo pg_createcluster \
  --locale en_US.UTF-8 --encoding UTF8 --port "$port" \
  "$version" "$cluster"

sudo pg_conftool "$version" "$cluster" set fsync off
sudo pg_conftool "$version" "$cluster" set full_page_writes off
sudo pg_conftool "$version" "$cluster" set synchronous_commit off
sudo pg_ctlcluster "$version" "$cluster" start

for _ in $(seq 120); do
  if pg_isready -h localhost -p "$port" -q; then break; fi
  sleep 0.5
done
pg_isready -h localhost -p "$port"

# The login the container's POSTGRES_USER/POSTGRES_DB gave us. Superuser
# because the schema migration creates the least-privilege write roles and
# grants membership in them (#492), which the container's user could also do.
sudo -u postgres psql --port "$port" --quiet --set ON_ERROR_STOP=1 <<'SQL'
create role corridor login superuser password 'corridor';
create database corridor owner corridor;
SQL
