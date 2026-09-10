"""One declared vocabulary for what resolving a Proposed Delta can come back as.

ADR-0083 puts the write boundary in two layers on purpose: PostgreSQL's
``resolve_proposed_delta_decision`` is the authority, and ``delta_resolution``
is "the readable half of the same rules", so a coordinator is told what is
wrong before anything is read for writing.  Both layers are kept.

What was **not** declared is the relationship between the two vocabularies.
The command raises every refusal with a stable ``resolve_delta:<code>`` token
and ``delta_resolution`` scrapes that token back out of the DBAPI message to
rebuild the same structured ``Refusal``; nine codes were spelled in both halves
with nothing saying so, and only a live-PostgreSQL test noticed drift, for the
cases it happened to exercise.  This module is that declaration:

  * every code either half may raise, exactly once;
  * the outcome status it carries, so a status is never written again at a
    raise site and cannot differ between two sites of the same code;
  * its **sole raiser** — ``python-precheck`` for a rule the pre-check owns
    outright, ``database-only`` for one the pre-check must not anticipate, and
    ``both`` for a rule deliberately checked twice; and
  * the one customer sentence the code always carries, where a fixed sentence
    exists at all.  A code whose detail names identifiers the reader needs (a
    revision number, a Support Assessment) carries no declared sentence and
    writes its own detail.

**Why a pre-check must not anticipate a database-only code.**  Every one of
them is decided by state the pre-check cannot hold still: a key already bound
to different content, two effective decisions for one field, a value that
became effective between the read and the write.  A Python guess at one of
those is either a lie or a race, and the honest answer is the command's own
refusal.  ``delta_resolution._refusal`` therefore refuses to build one, and
``tests/test_delta_resolution.py`` parses the plpgsql source to prove the two
vocabularies still agree.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


# Outcome statuses.  Every one of them is structured enough for the Work List
# to refresh, or for #526 to open a Follow-up Plan form, without discarding a
# coordinator's unsaved selections.
RESOLVED = "resolved"
DEFERRED = "deferred"
STALE = "stale"
UNSUPPORTED = "unsupported"
CONSTRAINED_EDIT = "constrained_edit"
COORDINATION_NEEDED = "coordination_needed"
REFUSED = "refused"

# Which half of the boundary raises a code.  ``BOTH`` is not an accident to be
# tidied away: it is ADR-0083's defense in depth, and the pre-check's copy is
# what lets a screen say "nothing was saved, and here is why" without a round
# trip that would abort the transaction it was made in.
PYTHON_PRECHECK = "python-precheck"
DATABASE_ONLY = "database-only"
BOTH = "both"

# The command raises every refusal with this leading token so the two halves of
# the rule agree on what was refused.  The runtime scrapes it out of a DBAPI
# message with this exact expression, and the agreement test parses the plpgsql
# source with it too, so neither can read a token the other cannot.
REFUSAL_TOKEN = re.compile(r"resolve_delta:([a-z_]+)")


@dataclass(frozen=True, slots=True)
class RefusalCode:
    """One declared refusal: its status, its sole raiser, and its sentence."""

    code: str
    status: str
    raiser: str
    sentence: str | None = None


REFUSAL_VOCABULARY: tuple[RefusalCode, ...] = (
    RefusalCode("already_effective", REFUSED, DATABASE_ONLY),
    RefusalCode(
        "already_resolved",
        REFUSED,
        BOTH,
        "the Proposed Delta is already resolved; correct it with a later decision",
    ),
    RefusalCode("ambiguous_effective_decision", REFUSED, DATABASE_ONLY),
    RefusalCode("append_only", REFUSED, DATABASE_ONLY),
    RefusalCode("constrained_edit", CONSTRAINED_EDIT, PYTHON_PRECHECK),
    RefusalCode("cross_project_delta", REFUSED, BOTH),
    RefusalCode(
        "cross_project_fact",
        REFUSED,
        BOTH,
        "a Resolve Delta decides only Source Facts this project captured",
    ),
    RefusalCode("cross_project_revision", REFUSED, DATABASE_ONLY),
    RefusalCode(
        "external_fact_needs_coordination",
        COORDINATION_NEEDED,
        PYTHON_PRECHECK,
        "an external fact cannot be settled by free text; the proposed value "
        "stays unaccepted and the delta stays open",
    ),
    RefusalCode(
        "field_mismatch",
        REFUSED,
        BOTH,
        "a Resolve Delta decides the delta's exact field",
    ),
    RefusalCode("invalid_action", REFUSED, BOTH),
    RefusalCode("key_bound_to_other_content", REFUSED, DATABASE_ONLY),
    RefusalCode("missing_decided_at", REFUSED, DATABASE_ONLY),
    RefusalCode("missing_idempotency_key", REFUSED, DATABASE_ONLY),
    RefusalCode("missing_principal", REFUSED, DATABASE_ONLY),
    RefusalCode(
        "missing_record_effect",
        REFUSED,
        BOTH,
        "an accepted or edited value names the Source Facts it makes effective",
    ),
    RefusalCode("missing_support", UNSUPPORTED, BOTH),
    RefusalCode("missing_wake_condition", REFUSED, DATABASE_ONLY),
    RefusalCode(
        "organization_change_kind_required",
        REFUSED,
        PYTHON_PRECHECK,
        "an organization change says whether it corrects a wrong name or "
        "records that ownership moved",
    ),
    RefusalCode("stale_accepted_revision", STALE, BOTH),
    RefusalCode(
        "subject_mismatch",
        REFUSED,
        BOTH,
        "a Resolve Delta decides the delta's exact subject",
    ),
    RefusalCode(
        "superseded_delta",
        REFUSED,
        BOTH,
        "a newer source version superseded this Proposed Delta",
    ),
    RefusalCode("unauthorized_writer", REFUSED, DATABASE_ONLY),
    RefusalCode(
        "unsupported_free_text",
        CONSTRAINED_EDIT,
        PYTHON_PRECHECK,
        "an edited value selects captured support, composes supported Source "
        "Facts under a named transformation, proves a lossless normalization, "
        "or cites a separate attributable source origin",
    ),
)

REFUSAL_CODES: dict[str, RefusalCode] = {
    declared.code: declared for declared in REFUSAL_VOCABULARY
}

# Deliberately not a member of the vocabulary: no half of the boundary raises
# it.  It is what a refusal whose message carried no readable token becomes, so
# a caller still gets a structured result instead of a lost transaction.  If it
# is ever observed, the command raised something the declaration does not know
# about, and the agreement test is what should have failed first.
UNCLASSIFIED_REFUSAL = RefusalCode("refused", REFUSED, DATABASE_ONLY)


def database_refusal_code(message: str) -> RefusalCode:
    """The declared code one DBAPI refusal message named, or the fallback."""

    match = REFUSAL_TOKEN.search(message)
    if match is None:
        return UNCLASSIFIED_REFUSAL
    return REFUSAL_CODES.get(match.group(1), UNCLASSIFIED_REFUSAL)
