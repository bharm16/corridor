"""#633 The retained report reading is evidence, not a cache (ADR-0092).

#602 bound each ``report_runs`` and ``scheduled_report_publications`` row to
the accepted Project Record revision it was taken against, and demoted
``snapshot_json`` beside it to a "rebuildable compatibility cache" awaiting
the expiry rule #603 was expected to make writable.  #603 measured it
instead, and found the demotion wrong.  A revision reproduces the two record
fields the spine carries and the Ledger identity — 212 field comparisons, no
disagreement — and cannot answer three things at all: the documentation
requirement and the Constraint Alerts of one reading under one ruleset, one
threshold configuration and one date; which subjects the report covered; and
the date projected from the external party's current statement, which shares
a name with the ``committed_date`` Fact and is a different quantity.

ADR-0092 records that those belong to the Report Reading occurrence, a third
owner beside the revision and the release package.  So the only change here
is what the database says the column is.  A comment that calls immutable
published evidence a rebuildable cache is an instruction to a future
implementer to delete it, and the honest version of that comment cannot be
written, because no rule would make it true.

Nothing else moves.  No column is added, no constraint is changed, and no
stored payload is rewritten: a version 1 payload is read exactly as it was
written, and only newly written payloads carry the schema version, the
content digest and the governed names.  A retention rule is deliberately
absent — the payload's retention is the run's, which ends with the governing
customer-environment retention and never on a cache TTL.
"""

from __future__ import annotations



REPORT_READING_PAYLOAD_COMMENT = """
comment on column public.report_runs.snapshot_json is
    'Immutable Report Reading payload (#633, ADR-0092): the population this '
    'report covered, its derived outcomes, the statement-projected '
    'published_promised_for, and the rules and thresholds used. Not a cache '
    'of revision_id and not rebuildable from it; retained as long as the run '
    'is, on no cache TTL.';
comment on column public.scheduled_report_publications.snapshot_json is
    'Immutable Report Reading payload (#633, ADR-0092): the population this '
    'reading covered, its derived outcomes, the statement-projected '
    'published_promised_for, and the rules and thresholds used. Not a cache '
    'of revision_id and not rebuildable from it; retained as long as the '
    'publication and its released package are, on no cache TTL.';
"""

# The downgrade restores #602's wording rather than clearing the comment,
# because the block below this one in ``downgrade`` is #602's, and it is what
# owns clearing it.  Restoring a description the schema at that point no
# longer justifies is worse than restoring the one it was given there.
REPORT_READING_PAYLOAD_COMMENT_DOWN = """
comment on column public.report_runs.snapshot_json is
    'Rebuildable compatibility cache (#602). The authority is revision_id; '
    'this copy is retained only until #603 proves what rebuilds it.';
comment on column public.scheduled_report_publications.snapshot_json is
    'Rebuildable compatibility cache (#602). The authority is revision_id; '
    'this copy is retained only until #603 proves what rebuilds it.';
"""


def upgrade(op) -> None:
    # Next to last, because it re-describes a column #602's block above created
    # the description for. Comments only: the column, its type and every
    # constraint on it are exactly what #602 left.
    op.execute(REPORT_READING_PAYLOAD_COMMENT)


def downgrade(op) -> None:
    # Next, because the upgrade added it last but one. Restoring #602's wording
    # is the whole of it: nothing was added, so nothing can be lost, and #602's
    # block further down is what clears the description entirely.
    op.execute(REPORT_READING_PAYLOAD_COMMENT_DOWN)
