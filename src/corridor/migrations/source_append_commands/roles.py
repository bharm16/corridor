"""The roles, the relations they may touch, and the command signatures (#492).

Every family in this transition grants and revokes against these names, so
they are stated once here rather than copied into each module.  The revision
module re-exports them, so an importer that already reads them from the
revision keeps working.
"""

from __future__ import annotations

SOURCE_APPEND_ROLE = "corridor_source_append"
# The one role that may make something effective. Three families each
# bound it to their own constant name, which read as three roles.
RECORD_DECISION_ROLE = "corridor_fact_decision_writer"
RUNTIME_LOGINS = "corridor_web, corridor_worker"

# Tables the application may now append to only through a command.
SOURCE_TABLES = (
    "source_segments",
    "facts",
    "fact_sources",
    "fact_applies_to",
    "fact_closure_results",
    "fact_closure_sources",
    "fact_statement_timings",
    "extracted_proposals",
    "extracted_proposal_facts",
    "source_fact_append_receipts",
)

# The Support Assessment relation (#530): created here, appended only through
# its command, readable by the runtime capabilities.
SUPPORT_TABLES = (
    "support_assessments",
    "support_assessment_sources",
)

# The Proposed Delta relation (#518): created here, appended only through
# its command, readable by the runtime capabilities.
DELTA_TABLES = (
    "delta_groups",
    "proposed_deltas",
    "delta_dispositions",
    "delta_supersessions",
    "delta_deferrals",
)

# Tables the commands read to prove a typed reference lies in scope.
REFERENCED_TABLES = (
    "projects",
    "documents",
    "dependency_events",
    "dependencies",
    "candidates",
    "extraction_runs",
)

COMMANDS = {
    "append_source_segments": "(bigint, bigint, bigint, jsonb)",
    "append_fact": (
        "(bigint, bigint, bigint, character varying, character varying, text, "
        "text, date, bigint, bigint, character varying, character varying, "
        "character varying, jsonb, jsonb)"
    ),
    "append_extracted_proposal": (
        "(bigint, bigint, bigint, bigint, character varying, text, jsonb, bigint[])"
    ),
    "append_source_fact_receipt": (
        "(bigint, bigint, bigint, character varying, character varying)"
    ),
    "append_support_assessment": (
        "(bigint, character varying, bigint, bigint, bigint[], character varying, "
        "character varying, character varying, character varying, "
        "character varying, timestamp with time zone, bigint)"
    ),
    "append_proposed_deltas": (
        "(bigint, character varying, character varying, bigint, bigint, jsonb)"
    ),
}

EVIDENCE_ROLES_SQL = "'value_support', 'attribution', 'timing', 'scope', 'context'"
ASSESSMENTS_SQL = (
    "'supported', 'partially_supported', 'contradicted', 'unclear', 'not_assessed'"
)
