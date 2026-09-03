#!/usr/bin/env bash
#
# Prepare one CI job: PostgreSQL, the OCR engine, both uv projects, and the
# schema of the database the suite's shared-state tests read.
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

# The OCR engine is a real dependency, not an optional one: token_layers.py
# runs the `tesseract` executable and tests/test_token_layers.py asserts the
# version it reports, so a job without it fails rather than skipping.
# `apt-get update` measured 11-34s and the runner image normally ships usable
# package lists, so the lists are refreshed only when the install actually
# needs them (#548). A genuine failure still fails the job.
(
  export DEBIAN_FRONTEND=noninteractive
  sudo apt-get install -y tesseract-ocr ||
    { sudo apt-get update && sudo apt-get install -y tesseract-ocr; }
) >"$logs/tesseract.log" 2>&1 &
tesseract_job=$!

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

# Not redundant with the pytest harness, which migrates its own per-run
# template and clones it per worker. `shared_source_database_url` hands three
# tests the *configured* database rather than a worker clone —
# tests/test_briefing.py, tests/test_sh99_admission_acceptance.py and
# tests/test_sh99_shared_admission_seal.py — and each is written to skip when
# the shared corpus is absent, which is what CI wants. Against a database
# with no schema at all they raise UndefinedTable instead, which is what all
# three measurably do (#595). tests/test_ci_policy.py holds the guard.
uv run alembic upgrade head

wait "$tesseract_job" || { cat "$logs/tesseract.log" >&2; exit 1; }
tesseract --version
