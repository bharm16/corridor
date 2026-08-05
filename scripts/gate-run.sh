#!/usr/bin/env bash
# The M7 gate's cold run: fetch -> ingest -> extract, with no human eyes
# between (#88, ADR-0008).
#
# This script exists so the protocol's middle is mechanical. #81 requires
# that "no human reading any page" happens between fetch and extract —
# with one person doing every job, sequence is the only available
# blindfold, and a script is how a sequence becomes something you cannot
# absent-mindedly deviate from.
#
# What this script deliberately does NOT do:
#
# - It does not lift the seal. Editing `sealed:` out of the manifest is
#   the one deliberate, irreversible human act (ADR-0008), and burying it
#   in automation would make the spend possible by accident — the exact
#   failure the seal exists to prevent.
# - It does not run the eval. The gate's denominator is a human-authored
#   gold set written AFTER this script finishes (#81), so the eval cannot
#   run in the same breath. The script ends by printing what to do next.
# - It does not show any extracted row. The extract step prints tallies —
#   counts, fallback rate, header agreement — and those were agreed
#   printable; the rows themselves stay in the database unread until the
#   labels exist.
#
# Usage:            scripts/gate-run.sh wsdot-9540
# Rehearsal (safe): scripts/gate-run.sh wsdot-9424

set -euo pipefail

SLUG="${1:?usage: scripts/gate-run.sh <project-slug>}"
MANIFEST="corpus/${SLUG}.yaml"

[ -f "$MANIFEST" ] || { echo "no manifest ${MANIFEST}" >&2; exit 1; }

# Refuse while sealed, loudly. The human lifts the seal by editing the
# manifest — this script only ever verifies that the deliberate act
# already happened.
if grep -qE '^\s*sealed:\s*true' "$MANIFEST"; then
  cat >&2 <<SEALED
${MANIFEST} is SEALED.

Lifting it is the one-shot human decision (ADR-0008, #88). If that
decision has been made:

  1. Confirm every #81 precondition is checked off on #88.
  2. Edit ${MANIFEST}: set 'sealed: false'.
  3. Re-run this script. Do not open the document.

Nothing has been fetched.
SEALED
  exit 2
fi

# The examined object is pinned on #88; refuse to run anything else. A
# gate run at an unpinned version measures something nobody named.
PINNED="matrix_tiered_v3"
CURRENT=$(uv run python -c "from corridor.extract_matrix import PROMPT_VERSION; print(PROMPT_VERSION)")
if [ "$CURRENT" != "$PINNED" ]; then
  echo "PROMPT_VERSION is ${CURRENT}, but the gate is pinned to ${PINNED} (#88)." >&2
  echo "Either run from the pinned code, or re-pin on the ticket before spending anything." >&2
  exit 2
fi

echo "== fetch (${SLUG}) =="
uv run python -m corridor.corpus "$MANIFEST"

echo
echo "== ingest (${SLUG}) =="
uv run python -m corridor.docs ingest "${SLUG}"

echo
echo "== extract (${SLUG}) — output goes to the database, not to eyes =="
uv run python -m corridor.extract_project "${SLUG}"

cat <<DONE

== cold run complete ==

The extractor's answers are committed. Human eyes may now touch the
document — the run is already over, so reading it contaminates nothing.

Next, in order (#81 as amended 2026-08-04 — machine-authored gold, a
semi-independent ceiling; the hand path below remains available and
stricter):

  1. Author the gold set from the independent grid reading:
         uv run python -m corridor.gold ${SLUG} --author
     It writes gold/${SLUG}.machine.csv and a stamped sidecar carrying
     the ceiling caveat and the page-image checklist. It refuses any
     layout that does not print the WSDOT anchor.

     (Hand alternative, unamended strength: gold/${SLUG}.csv with columns
     source_ref,page,critical — 'critical' by ADR-0009's rule; blank when
     the document has not settled. Blank reads as not-critical, out of
     the >=95% denominator.)

  2. Score:  uv run python -m corridor.eval ${SLUG} gold/${SLUG}.machine.csv --prompt-version=${PINNED}

  3. Record the result on #88 and against #22, whatever it says.
     Per #81: on a fail the score stands, and there is no retake.
DONE
