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

**The Review Packet family is declared here too.**  #526's packet, Follow-up
Plan and reversal commands raise their refusals as ``review_packet:<code>``
tokens in exactly the same shape, and ``review_packets`` used to scrape them
with a private expression and a two-entry status table beside it, so two dozen
tokens had no declaration and the table could silently name a token the
plpgsql never raised.  The family keeps its own token prefix, because each
family declares exactly what its own commands raise, and a code both happen to
spell the same way is still two declarations; ``database_refusal_code`` reads
whichever family a message carries.
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

# Each command raises every refusal with its own leading token so the two halves
# of the rule agree on what was refused.  The runtime scrapes it out of a DBAPI
# message with this exact expression, and the agreement test parses the plpgsql
# source with it too, so neither can read a token the other cannot.
REFUSAL_TOKEN = re.compile(r"resolve_delta:([a-z_]+)")
REVIEW_PACKET_TOKEN = re.compile(r"review_packet:([a-z_]+)")


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

# The Review Packet family.  The packet's readable half has no single
# constructor, so a code declared BOTH is one ``review_packets`` also writes
# into a ``Refusal`` itself; the rest are the commands' own.  Only
# ``missing_support`` carries a status other than REFUSED: no other packet
# refusal promises the structured refresh a STALE, UNSUPPORTED,
# CONSTRAINED_EDIT or COORDINATION_NEEDED outcome does.  No sentence is
# declared, because every packet message names the delta, receipt or
# assessment the coordinator needs, and the runtime keeps that line as the
# detail.
#
# #835's closure codes join this family rather than opening a third: closing a
# Follow-up Plan acts on the same relation ``record_delta_follow_up_plan``
# writes, so its command raises the same ``review_packet:`` token, and the
# agreement test parses both blocks of plpgsql for it.
REVIEW_PACKET_VOCABULARY: tuple[RefusalCode, ...] = (
    RefusalCode("already_closed", REFUSED, DATABASE_ONLY),
    RefusalCode("already_resolved", REFUSED, BOTH),
    RefusalCode("already_reversed", REFUSED, DATABASE_ONLY),
    RefusalCode("child_identity_mismatch", REFUSED, DATABASE_ONLY),
    RefusalCode("cross_project_delta", REFUSED, BOTH),
    RefusalCode("cross_project_plan", REFUSED, DATABASE_ONLY),
    RefusalCode("cross_project_receipt", REFUSED, DATABASE_ONLY),
    RefusalCode("cross_project_revision", REFUSED, DATABASE_ONLY),
    RefusalCode("duplicate_child", REFUSED, DATABASE_ONLY),
    RefusalCode("empty_packet", REFUSED, DATABASE_ONLY),
    RefusalCode("invalid_closure_kind", REFUSED, DATABASE_ONLY),
    RefusalCode("invalid_grouping_key", REFUSED, DATABASE_ONLY),
    RefusalCode("invalid_outcome", REFUSED, BOTH),
    RefusalCode("key_bound_to_other_content", REFUSED, DATABASE_ONLY),
    RefusalCode("later_act_depends", REFUSED, DATABASE_ONLY),
    RefusalCode("missing_cancellation_reason", REFUSED, DATABASE_ONLY),
    RefusalCode("missing_decided_at", REFUSED, DATABASE_ONLY),
    RefusalCode("missing_grouping_rule", REFUSED, DATABASE_ONLY),
    RefusalCode("missing_idempotency_key", REFUSED, DATABASE_ONLY),
    RefusalCode("missing_principal", REFUSED, DATABASE_ONLY),
    RefusalCode("missing_question", REFUSED, BOTH),
    RefusalCode("missing_responsible_party", REFUSED, BOTH),
    RefusalCode("missing_revision", REFUSED, DATABASE_ONLY),
    RefusalCode("missing_source_revision", REFUSED, DATABASE_ONLY),
    RefusalCode("missing_successor_plan", REFUSED, DATABASE_ONLY),
    RefusalCode("missing_support", UNSUPPORTED, BOTH),
    RefusalCode("successor_on_other_delta", REFUSED, DATABASE_ONLY),
    RefusalCode("superseded_delta", REFUSED, BOTH),
    RefusalCode("unexpected_revision", REFUSED, DATABASE_ONLY),
    RefusalCode("unordered_children", REFUSED, DATABASE_ONLY),
)

REVIEW_PACKET_CODES: dict[str, RefusalCode] = {
    declared.code: declared for declared in REVIEW_PACKET_VOCABULARY
}

# Each family's token expression beside the codes it declares, in the order a
# message is read for them.
_FAMILIES: tuple[tuple[re.Pattern[str], dict[str, RefusalCode]], ...] = (
    (REFUSAL_TOKEN, REFUSAL_CODES),
    (REVIEW_PACKET_TOKEN, REVIEW_PACKET_CODES),
)

# Deliberately not a member of the vocabulary: no half of the boundary raises
# it.  It is what a refusal whose message carried no readable token becomes, so
# a caller still gets a structured result instead of a lost transaction.  If it
# is ever observed, the command raised something the declaration does not know
# about, and the agreement test is what should have failed first.
UNCLASSIFIED_REFUSAL = RefusalCode("refused", REFUSED, DATABASE_ONLY)


def database_refusal_code(message: str) -> RefusalCode:
    """The declared code one DBAPI refusal message named, or the fallback.

    The message's own token says which family declared it, so one reader
    serves both commands and neither half needs a table of its own.
    """

    for token, codes in _FAMILIES:
        match = token.search(message)
        if match is not None:
            return codes.get(match.group(1), UNCLASSIFIED_REFUSAL)
    return UNCLASSIFIED_REFUSAL
