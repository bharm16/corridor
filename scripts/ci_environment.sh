#!/usr/bin/env bash
#
# Prepare one CI job: PostgreSQL and both uv projects. The test harness owns
# lazy schema provisioning and shared-source isolation.
#
# Why one step rather than five: the gate's wall clock is the *slowest of its
# nine test jobs*, so every run samples the worst setup draw taken in it, not
# the average one. Across the twelve pull-request runs after the five-way
# split (#548) the job that decided the wall clock spent a median of 60s and
# a mean of 72s outside its test command, against a fleet-wide per-job median
# of 38s. The deciding job was usually the one whose downloads were slow, not
# the one holding the most tests: the worst single draws were 105s in `uv
# sync --project workers/render`, 43s in `uv sync --locked`, 42s in apt and
# 33s pulling the PostgreSQL image. Run one after another those draws add;
# run concurrently, only the largest of them counts, and the distribution the
# maximum is drawn from is much tighter.
#
# Measured runs: 33732783365, 33781328253, 33781937852, 33782155550,
# 33782650582, 33782912441, 33783895850, 33787663707, 33788227642,
# 33789014596, 33796350276, 33798786527.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
logs="${RUNNER_TEMP:-/tmp}"

# The local OCR engine used to be installed here, concurrently with the two
# `uv sync` calls, because it was a real dependency of the suite. ADR-0094
# retired it and #741 removed it from the product, so a CI job that installed
# it would be provisioning something nothing can call.

scripts/ci_postgres.sh >"$logs/postgres.log" 2>&1 &
postgres_job=$!

uv sync --locked
# `make boot` provisions this; CI never did, so the first page render inside a
# test built the worker's environment over the network, in four racing xdist
# workers, with its output captured and invisible. opencv-python-headless is
# locked only here, so it is never in the restored uv cache either (#548).
uv sync --project workers/render --frozen

wait "$postgres_job" || { cat "$logs/postgres.log" >&2; exit 1; }
cat "$logs/postgres.log"

# Shared-corpus tests lazily clone the harness's empty migrated template. The
# configured CI source stays untouched; an existing corpus is never substituted.
# Publish the opt-in only after ci_postgres.sh created this disposable database.
if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
  printf 'CORRIDOR_CI_EMPTY_SHARED_SOURCE=1\n' >> "$GITHUB_ENV"
fi
