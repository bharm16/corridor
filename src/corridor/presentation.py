"""Current product words at presentation boundaries, without changing records.

The accepted terminology differs from retained model, rule, and provenance
identifiers (ADR-0048). Replacing words in finished text would alter source
quotations and names. These pure adapters accept only known system labels or
enumerated kinds; source-authored text never passes through a replacement loop.

It also owns the coordination authority rule at the end of this file: which
residual decision a Coordination Subject still needs, the Attention Reason
codes for it, and the words for a gap.  The decision and its words are one
fact, and a module with no sibling imports is the one place every surface that
shows them can ask without closing an import cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Mapping, Protocol


_LABELS = {
    "constraint": "Constraint",
    "constraints": "Constraints",
    "constraint_plural": "Constraints",
    "dependency": "Constraint",
    "dependencies": "Constraints",
    "constraint_log": "Constraint log",
    "ledger": "Constraint log",
    "organization": "Organization",
    "external_party": "Organization",
    "key_date": "Key date",
    "key_dates": "Key dates",
    "milestone": "Key date",
    "key_date_version": "Key date version",
    "milestone_readiness": "Constraints by key date",
    "effect_on_key_dates": "Effect on key dates",
    "milestone_impact": "Effect on key dates",
    "required_by": "Required by",
    "need_date": "Required by",
    "promised_for": "Promised for",
    "committed_date": "Promised for",
    "assigned_to": "Assigned to",
    "internal_owner": "Assigned to",
    "next_action": "Next action",
    "action_due_date": "Action due date",
    "follow_up_plan": "Follow-up plan",
    "work_plan": "Follow-up plan",
    "coordination_decision": "Coordination decision",
    "source_discrepancy": "Source discrepancy",
    "discrepancy_resolution": "Discrepancy resolution",
    "record_conclusion": "Record conclusion",
    "supporting_documents": "Supporting documents",
    "evidence": "Supporting documents",
    "cited_passage": "Cited passage",
    "evidence_quote": "Cited passage",
    "required_documents": "Documents required for this condition",
    "readiness_requirement": "Documents required for this condition",
    "documentation_review": "Documentation review",
    "applies_to": "Applies to",
    "statement_history": "Statement history",
    "statement_type": "Statement type",
    "source_passage_check": "Source passage check",
    "do_not_add": "Do not add",
    "remove_incorrect_entry": "Remove incorrect entry",
    "approved_to_share": "Approved to share",
    "work_item": "Coordination item",
    "attention_reason": "Why this needs attention",
    "constraint_alerts": "Constraint alerts",
    "exceptions": "Constraint alerts",
    "constraint_check": "Constraint check",
    "report": "Coordination report",
    "coordination_summary": "Coordination summary — AI draft",
    "assertion": "Source field value",
    "derivation": "Calculated result",
    "verbal": "Recorded verbal statement",
    "provenance": "Source traceability",
    "critical_items": "Relocation / removal / abandonment",
    "resolution_strategy": "Resolution method",
    "organization_commitments": "Organization commitments",
}


def label(key: str) -> str:
    """Return a known system label; reject accidental source-text inputs."""
    return _LABELS[key]


def statement_type_label(event_type: str) -> str:
    """Explain an event kind without renaming the retained event identity."""
    return {
        "commitment": "Commitment",
        "committed_date_change": "Change to promised timing",
        "closure": "Completion reported",
    }.get(event_type, event_type)


def source_passage_check_label(status: str) -> str:
    """Name a Source Passage Check state without claiming what it supports.

    The stored identifiers stay the machine words ADR-0082 fixed (``valid``,
    ``invalid``, ``not_checked``) plus ``not_re_readable`` from #741; these are
    the customer words for those states.  Each says only where the cited
    passage was looked for and whether it was there, never that the source
    supports the value beside it — that is a Support Assessment, and it is
    displayed separately.

    Passed, Failed, and Not run were the first wording and were rejected
    (#600): each reads as a verdict on the value, or on work that passed an
    inspection, which the check never is.  "Not run" also misdescribed the
    mechanism, because the status is computed by replaying the locator when it
    is read; ``not_checked`` means there was no cited location to replay.

    "Cited location cannot be re-read" is built the same way, on this
    vocabulary's own words.  It is the state of a citation whose locator was
    written by the reader ADR-0094 retired: the location is recorded, and no
    reader in the product can return to it.  It deliberately does not say the
    passage was looked for, because it was not, and it does not say the
    passage is missing, because nobody looked.  The passage's own words are
    still provable from their digest, which is a different question and a
    different receipt (``retained_history``).
    """
    return {
        "valid": "Found at cited location",
        "invalid": "Not found at cited location",
        "not_checked": "No cited location recorded",
        "not_re_readable": "Cited location cannot be re-read",
    }[status]


def documentation_review_label(sufficient: bool) -> str:
    """Describe the legacy marker; never infer a specific construction outcome."""
    return "Documents marked sufficient" if sufficient else "Not confirmed"


def resolution_strategy_label(strategy: str) -> str:
    """Explain a known method enum without rewriting a source field value."""
    return {
        "relocate": "Relocate",
        "remove": "Remove",
        "abandon_in_place": "Abandon in place",
        "adjust_vertical": "Vertical adjustment",
        "protect_in_place": "Protect in place",
        "change_design": "Change design",
        "policy_exception": "Exception to policy",
    }.get(strategy, strategy)


def provenance_label(kind: str) -> str:
    """Name a known provenance kind while keeping its stored identifier intact."""
    return {
        "assertion": label("assertion"),
        "derivation": label("derivation"),
        "decision": label("coordination_decision"),
        "work_decision": label("coordination_decision"),
        "work decision": label("coordination_decision"),
        "workdecision": label("coordination_decision"),
        "verbal": label("verbal"),
        "cited": "Cited statement",
        "evidence": label("supporting_documents"),
        "exception": "Constraint alert",
        "exception_bucket": "Constraint alert group",
    }.get(kind.casefold(), kind)


def input_reference_label(ref: str) -> str:
    """Present an exact generated reference, preserving arbitrary source names."""
    milestone = re.fullmatch(r"Milestone Registration (MR[0-9]+)", ref)
    if milestone is not None:
        return f"{label('key_date_version')} {milestone.group(1)}"
    return ref


def field_label(field_name: str) -> str:
    """Label a known system field key, never a field's source-authored value."""
    return {
        "external_org": label("organization"),
        "external_org_id": label("organization"),
        "internal_owner": label("assigned_to"),
        "evidence_required": label("required_documents"),
        "need_date": label("required_by"),
        "resolution_strategy": label("resolution_strategy"),
        "committed_date": label("promised_for"),
        "station_from": "From station",
        "station_to": "To station",
        "utility_id": "Source conflict ID",
        "utility_type": "Utility type",
        "external_contact": "Organization contact",
        "next_action": label("next_action"),
        "action_due_date": label("action_due_date"),
        "milestone": label("key_date"),
        "milestone_id": label("key_date"),
        "milestone_impact": label("effect_on_key_dates"),
        "title": "Title",
        "notes": "Notes",
        "location_desc": "Location",
    }.get(field_name, field_name)


def exception_name(rule: str) -> str:
    """Name a known check without changing its retained rule code or meaning."""
    return {
        # The rule counts supporting documents whose cited passage was found
        # in its source, so the alert names that check rather than calling the
        # documents themselves verified (ADR-0082).
        "MISSING_EVIDENCE": "No supporting document passed the source passage check",
        "MISSING_DATE": "No exact promised date for this check",
        "MISSING_OWNER": "No person assigned",
        "OVERDUE": "Promised timing passed",
        "DUE_SOON": "Required by date is near",
        "STALE": "No recent supporting documents",
        "CONTRADICTION": "Sources disagree",
        "ORPHAN": "No key date linked",
        "SUPERSEDED_CITATION": "Supporting document replaced",
        "MISSING_ACTION": "No next action",
        "ACTION_DUE_SOON": "Next action is due soon",
        "ACTION_OVERDUE": "Next action is overdue",
    }.get(rule, rule)


# The accepted record runs its own check set (``accepted_record_checks_v2``,
# ADR-0090), and two of its rules do not mean what the released legacy
# ruleset's rules of the same name mean.  Only a rule whose predicate actually
# changed appears here; every other rule keeps the label above, verbatim, so
# one finding reads the same way wherever it is shown.
#
# ``MISSING_EVIDENCE`` is the one that changed.  The legacy rule fires when no
# supporting document on a Constraint had its cited passage found in its
# source, and its label says exactly that.  ADR-0082 decided that a locatable
# passage is not support, so the accepted record's rule instead fires when no
# Supporting Documentation is in use for the accepted proposition — read from
# the Support Assessment relation and never from the Source Passage Check.
# Carrying the old label onto the new predicate would tell a customer that a
# quotation could not be located, which the check no longer looks at.  The
# replacement wording is researched in
# docs/research/missing-evidence-alert-label-2026-09-03.md; it deliberately
# avoids the Source Passage Check state labels (#600) so the two can never be
# read as the same finding.
#
# The research found no industry counterpart to adopt, so terminology
# procedure step 6 required the maintainer's own agreement.  They gave it on
# 2026-09-03 (#613): the label "describes only the absence of effective
# Supporting Documentation for the accepted value.  It does not imply that the
# value, the underlying work, or the source itself failed, and it is distinct
# from Source Passage Check outcomes."  The string below is that approved
# wording, and the Project Record glossary entry for Supporting Documentation
# in Use now carries it; changing it is a terminology decision, not an edit.
_ACCEPTED_RECORD_LABELS = {
    "MISSING_EVIDENCE": "No supporting document in use for this value",
}


def accepted_record_exception_name(rule: str) -> str:
    """Name a check of the accepted record's own set (ADR-0090).

    The legacy ruleset keeps computing all twelve rules over a legacy
    project until ADR-0081 stage 6 retires those tables, and its labels stay
    exactly as they are: a label that stopped describing the predicate under
    it would make the legacy report lie.  The two sets are allowed to differ
    and the report declares the set it ran, so this is the one place the
    difference is expressed.
    """
    return _ACCEPTED_RECORD_LABELS.get(rule) or exception_name(rule)


class _ExceptionFact(Protocol):
    rule: str
    quantity_days: int | None


def exception_label(exception: _ExceptionFact) -> str:
    """Keep each alert's own day quantity beside its current customer label."""
    quantity = (
        f" {exception.quantity_days}d" if exception.quantity_days is not None else ""
    )
    return f"{exception_name(exception.rule)}{quantity}"


# --- The coordination authority rule, read once (ADR-0025, ADR-0042) --------
#
# "Is an owner or a Next Action missing, and does it matter?" was six separate
# expressions: `exceptions._apply`'s live-gated MISSING_OWNER/MISSING_ACTION,
# `work_list._dependency_items`' critical-only pair, `work_list._statement_items`'
# closed-with-a-live-action pair, `statement_coordination`'s owner-then-action
# ordering (restated again as a guard in five entry points),
# `report._coordination`'s nothing-recorded skip, and `notifications`'
# statement branch.  Each said the rule slightly differently, so the guided
# Save screen, the Work List and the report could disagree about the same
# subject.  The rule is these readings now, and every surface asks one.
#
# They live here, beside the words, for two reasons.  The words for a gap and
# the decision that produces it are one fact — a surface that computes the
# decision and then looks up its sentence somewhere else is how the two drift
# (`_authority_gap_label` said "organization that reported completion" for the
# gap whose own title said "External Party").  And this module imports no
# sibling: `exceptions`, `work_list`, `statement_coordination`, `report`,
# `notifications` and the web application can all ask it without closing a
# cycle, which the same reading in `work_decisions` could not — `exceptions ->
# work_decisions -> notifications -> exceptions` would have added two declared
# cycle edges to carry it.


class UnknownAttentionReason(KeyError):
    """A reason code with no customer sentence.

    Raised rather than skipped: ADR-0085 keeps every internal Attention Reason
    visible on the item, so a code the words do not know is a defect to report,
    not a row to quietly leave blank.
    """


class UnknownAuthorityGap(KeyError):
    """An authority-gap code with no customer words."""


# Group 0 is immediate, 4 is ordinary follow-up; the numbers order the reasons
# on one item and rank items against each other.  A new code belongs in both
# this table and the sentences below, and the pair is asserted equal in tests.
ATTENTION_REASON_GROUPS: dict[str, int] = {
    "past_due": 0,
    "critical_missing_internal_owner": 1,
    "critical_missing_next_action": 1,
    "committed_date_change": 2,
    "milestone_impact_unknown": 2,
    "disputed_date": 2,
    "contractual_amendment": 2,
    # The routed consequence of ineligible replacement support (ADR-0037):
    # a specific project question, never the retired generic reconfirmation.
    "support_changed_value": 2,
    "support_documentation_review": 2,
    "support_failed_citation": 2,
    "support_uncertain_match": 2,
    "support_dropped_row": 2,
    # A schedule revision moved a Required By basis (ADR-0057): surfaced as
    # attention showing old and new dates, never as an approval question.
    "required_by_advanced": 2,
    "key_date_decision_affected": 2,
    "unknown_scope": 3,
    "unplaced_statement": 3,
    "missing_internal_owner": 4,
    "missing_next_action": 4,
    "action_due": 4,
    "action_due_date_unknown": 4,
    "external_closure_follow_up": 4,
}

# One sentence per code, in the coordinator's own words.  Policy-recorded
# unknown scope reads as current state ("Applies to: not yet known.") and never
# as a question, because there is nothing here for a human to confirm
# (ADR-0035, ADR-0036, ADR-0039, ADR-0042).
_ATTENTION_REASON_SENTENCES: dict[str, str] = {
    "past_due": "The organization's commitment passed its stated date.",
    "critical_missing_internal_owner": (
        "No project person is assigned to this relocation, removal, or abandonment constraint."
    ),
    "critical_missing_next_action": (
        "This relocation, removal, or abandonment constraint has no Next Action."
    ),
    "committed_date_change": "The organization changed its promised timing.",
    "milestone_impact_unknown": "The effect on key dates is not yet known.",
    "disputed_date": "Sources disagree about a current date.",
    "contractual_amendment": (
        "Field data changed under an executed agreement — flag the agreement for amendment."
    ),
    "required_by_advanced": "A schedule revision moved this constraint's Required By date.",
    "key_date_decision_affected": "A schedule revision moved a key date a recorded decision referenced.",
    "unknown_scope": "Applies to: not yet known.",
    "unplaced_statement": "Clarify the organization's statement and which constraints it applies to.",
    "missing_internal_owner": "Assign a project person for this Commitment.",
    "missing_next_action": "Set the Next Action for this Commitment.",
    "action_due": "The project Next Action is due now.",
    "action_due_date_unknown": "The Next Action needs a return date.",
    "external_closure_follow_up": "Confirm the project Next Action after the organization reported completion.",
    "support_changed_value": (
        "A newer document states a different value than the recorded conclusion — "
        "resolve which one the record concludes."
    ),
    "support_documentation_review": (
        "A newer document changed the supporting documentation — review it against "
        "the stated requirement."
    ),
    "support_failed_citation": (
        "A newer document's supporting passage was not found at its cited location "
        "— check the citation."
    ),
    "support_uncertain_match": (
        "A newer document has more than one row that could replace this "
        "supporting document — coordinate the correct one."
    ),
    "support_dropped_row": (
        "A newer document no longer contains the row this entry relied on — remove "
        "or correct the entry."
    ),
}


def sentence_for(code: str, sentences: Mapping[str, str]) -> str:
    """One reason's sentence from its own family's table, or a loud failure.

    The Work List's codes and the Review Packet's consequence bands are two
    vocabularies with two owners — the bands carry #494's own audited sentence,
    read back rather than restated here — but a code missing from either one is
    the same defect, so both paths fail the same way.
    """
    try:
        return sentences[code]
    except KeyError:
        raise UnknownAttentionReason(code) from None


def attention_reason_sentence(code: str) -> str:
    """The customer sentence for one Work List Attention Reason code."""
    return sentence_for(code, _ATTENTION_REASON_SENTENCES)


def attention_reason_sort_key(code: str) -> int:
    """Order reasons by group; a stable sort keeps each builder's own order."""
    try:
        return ATTENTION_REASON_GROUPS[code]
    except KeyError:
        raise UnknownAttentionReason(code) from None


@dataclass(frozen=True)
class AuthorityGapWords:
    """One authority gap's title, explanation, and one-line history label."""

    title: str
    detail: str
    label: str


CLOSURE_TARGET_GAP = "closure_target_commitment_not_established"
CLOSURE_TARGET_RELATIONSHIP_GAP = "closure_target_relationship_not_established"
CLOSURE_TARGET_AMBIGUOUS_GAP = "closure_target_commitment_ambiguous"
CLOSURE_PARTY_GAP = "closure_affected_party_not_established"

_CLOSURE_GAP_WORDS: dict[str, AuthorityGapWords] = {
    CLOSURE_PARTY_GAP: AuthorityGapWords(
        title="Affected External Party not established",
        detail=(
            "The Evidence establishes a closure statement, but it does not "
            "establish the registered External Party whose Commitment could be "
            "closed."
        ),
        label="The organization that reported completion is not established",
    ),
    CLOSURE_TARGET_GAP: AuthorityGapWords(
        title="Exact target Commitment not established",
        detail=(
            "The Evidence establishes a closure statement, but it does not "
            "identify an open Commitment in the Project Record that it closes."
        ),
        label="Commitment covered by the completion report is not established",
    ),
    CLOSURE_TARGET_RELATIONSHIP_GAP: AuthorityGapWords(
        title="Closure-to-Commitment relationship not established",
        detail=(
            "The Project Record has one open Commitment for this External "
            "Party, but the closure Evidence does not establish that it is the "
            "Commitment being closed."
        ),
        label=(
            "One open commitment is recorded, but the completion report does not "
            "establish that it covers that commitment"
        ),
    ),
    CLOSURE_TARGET_AMBIGUOUS_GAP: AuthorityGapWords(
        title="Several open Commitments could be the closure target",
        detail=(
            "The Project Record has several open Commitments for this External "
            "Party, and the closure Evidence does not identify which exact "
            "Commitment it closes."
        ),
        label=(
            "Several open commitments match; the completion report does not "
            "identify which one"
        ),
    ),
}

# Dependency Admission's own abstention reasons.  A reason absent from this
# table is not an allowed unresolved gap at all, which is why the reading below
# returns ``None`` for it rather than raising: the Candidate simply has no gap
# to keep.  The screen label is the gap's own title, so the history line and
# the card cannot describe the same receipt differently.
_DEPENDENCY_ADMISSION_GAP_WORDS: dict[str, AuthorityGapWords] = {
    reason: AuthorityGapWords(title=title, detail=detail, label=title)
    for reason, (title, detail) in {
        "citations_unverified": (
            "Source citation not found at cited location",
            "Dependency Admission could not find this proposal's cited passage in its source.",
        ),
        "no_utility_id": (
            "Dependency identifier not established",
            "The current source row does not establish an identifier for this Dependency.",
        ),
        "no_row_identity": (
            "External Party identity not established",
            "This source numbers rows within each External Party, but the current Evidence does not establish the party needed to name this Dependency.",
        ),
        "missing_from_agreement_document": (
            "Current revisions do not establish one row",
            "The current source revisions do not agree that this Dependency row is present.",
        ),
        "multiple_rows_in_agreement_document": (
            "Source row identity is not unique",
            "The current source lists more than one row with this Dependency identity.",
        ),
        "revisions_disagree": (
            "Current revisions disagree",
            "The current source revisions do not establish one supported Dependency proposal.",
        ),
        "revisions_disagree_on_party": (
            "External Party differs across current Evidence",
            "The current source revisions name different External Parties for this Dependency.",
        ),
        "already_admitted": (
            "Dependency identity already exists",
            "The Project Record already carries this proposed Dependency identity.",
        ),
        "same_document_replay_unproven": (
            "Same-source Dependency replay not established",
            "This same source row was previously handled, but its current Dependency "
            "association or extracted facts no longer prove safe replay. Keep it "
            "pending for Evidence review.",
        ),
        "asserts_nothing": (
            "No Dependency facts established",
            "The current source row does not establish any Dependency facts to record.",
        ),
        "write_refused": (
            "Dependency write authority not established",
            "The current proposal did not satisfy the protected Dependency write contract.",
        ),
    }.items()
}

AUTHORITY_GAP_WORDS: Mapping[str, AuthorityGapWords] = {
    **_CLOSURE_GAP_WORDS,
    **{
        f"dependency_admission_{reason}": words
        for reason, words in _DEPENDENCY_ADMISSION_GAP_WORDS.items()
    },
}


def authority_gap_words(code: str) -> AuthorityGapWords:
    """The customer words for one recorded authority-gap code."""
    try:
        return AUTHORITY_GAP_WORDS[code]
    except KeyError:
        raise UnknownAuthorityGap(code) from None


def authority_gap_label(code: str) -> str:
    """One history line for a recorded unresolved authority gap."""
    return authority_gap_words(code).label


def dependency_admission_gap_words(reason: str) -> AuthorityGapWords | None:
    """Words for an abstention reason, or ``None`` where it is not a kept gap."""
    return _DEPENDENCY_ADMISSION_GAP_WORDS.get(reason or "")


# The guided Save offer, in one value.  It was three: the screen conjoined
# Evidence and timing, the template silently added the roster, and the write
# path refused on Evidence alone with its own sentence.  A control the template
# disables for a reason the reading does not know is a control no test can
# describe.
GUIDED_SAVE_EVIDENCE_UNAVAILABLE = (
    "Save unavailable until every registered source page for this extracted "
    "statement has its complete context: a rendered image for PDF or OCR "
    "pages, or registered cell text for a worksheet."
)
GUIDED_SAVE_TIMING_UNAVAILABLE = (
    "No structured timing is available. This proposed statement stays pending "
    "until the source supports one."
)
GUIDED_SAVE_ROSTER_UNAVAILABLE = (
    "No project-team member is available to assign. This work stays pending."
)


@dataclass(frozen=True)
class GuidedSaveOffer:
    """Whether the guided Save may be offered, and the words if it may not."""

    available: bool
    refusal: str | None = None


def read_guided_save_offer(
    *,
    evidence_available: bool,
    timing_available: bool,
    roster_available: bool,
) -> GuidedSaveOffer:
    """One value for the offer, so screen, template and write path agree."""
    if not evidence_available:
        return GuidedSaveOffer(False, GUIDED_SAVE_EVIDENCE_UNAVAILABLE)
    if not timing_available:
        return GuidedSaveOffer(False, GUIDED_SAVE_TIMING_UNAVAILABLE)
    if not roster_available:
        return GuidedSaveOffer(False, GUIDED_SAVE_ROSTER_UNAVAILABLE)
    return GuidedSaveOffer(True)


def read_supporting_evidence_offer(*, evidence_available: bool) -> GuidedSaveOffer:
    """The write path's half of the same offer, named rather than assumed.

    A submitted form carries its own timing and its own owner choice, so the
    only half of the offer still worth refusing on arrival is the Evidence one;
    the screen decided the other two from the same reading before offering the
    control at all.
    """
    return read_guided_save_offer(
        evidence_available=evidence_available,
        timing_available=True,
        roster_available=True,
    )


RESIDUAL_OWNER = "owner"
RESIDUAL_NEXT_ACTION = "next_action"
RESIDUAL_BLOCKED = "blocked"

ADMITTED_EVIDENCE_UNAVAILABLE_GAP = (
    "Statement evidence is unavailable; residual decisions remain pending."
)
ADMITTED_ROSTER_UNAVAILABLE_GAP = (
    "No active project roster choices are available; Internal Owner remains pending."
)

_CRITICAL_REASON_CODES = {
    "missing_internal_owner": "critical_missing_internal_owner",
    "missing_next_action": "critical_missing_next_action",
}


@dataclass(frozen=True)
class CoordinationPlan:
    """The two Follow-up Plan values a Coordination Decision can establish.

    Read from the projections the Work Decision seam keeps equal to its chain
    tails (ADR-0025); an absence is the absence of a current decision, never a
    stored flag.
    """

    internal_owner: str | None = None
    next_action: str | None = None


@dataclass(frozen=True)
class CoordinationResidue:
    """What is still undecided about one Coordination Subject's plan."""

    # ``None`` means nothing is residual; ``blocked`` means the subject cannot
    # be decided at all yet, which is not the same as being complete.
    decision: str | None
    missing_owner: bool
    missing_next_action: bool
    reason_codes: tuple[str, ...]
    authority_gap: str | None = None

    @property
    def nothing_recorded(self) -> bool:
        """No current Coordination Decision establishes either half."""
        return self.missing_owner and self.missing_next_action


def _residue(
    *,
    missing_owner: bool,
    missing_next_action: bool,
    codes: tuple[str, ...],
    authority_gap: str | None = None,
) -> CoordinationResidue:
    return CoordinationResidue(
        decision=(
            RESIDUAL_OWNER
            if missing_owner
            else RESIDUAL_NEXT_ACTION
            if missing_next_action
            else None
        ),
        missing_owner=missing_owner,
        missing_next_action=missing_next_action,
        reason_codes=tuple(sorted(codes, key=attention_reason_sort_key)),
        authority_gap=authority_gap,
    )


def read_coordination_residue(
    plan: CoordinationPlan, *, live: bool
) -> CoordinationResidue:
    """The Constraint rule: owner, then Next Action, and only while live.

    "Live" means neither proven Ready, nor lapsed out of currency, nor closed
    by an attributable External Party fact — registration alone must not
    manufacture a coordination gap that the same proof suppressed (ADR-0016).
    """
    missing_owner = live and not plan.internal_owner
    missing_next_action = live and not plan.next_action
    codes = tuple(
        code
        for code, missing in (
            ("missing_internal_owner", missing_owner),
            ("missing_next_action", missing_next_action),
        )
        if missing
    )
    return _residue(
        missing_owner=missing_owner,
        missing_next_action=missing_next_action,
        codes=codes,
    )


def read_critical_coordination_residue(
    plan: CoordinationPlan, *, critical: bool
) -> CoordinationResidue:
    """The legacy Constraint work item: gated on the critical work type alone.

    The frozen Ledger item surfaces a coordination gap only for relocation,
    removal, or abandonment (ADR-0009), and it does not consult readiness — a
    deliberately different predicate from the alert engine's, named rather than
    restated.
    """
    residue = read_coordination_residue(plan, live=critical)
    return CoordinationResidue(
        decision=residue.decision,
        missing_owner=residue.missing_owner,
        missing_next_action=residue.missing_next_action,
        reason_codes=tuple(
            _CRITICAL_REASON_CODES[code] for code in residue.reason_codes
        ),
    )


def read_statement_coordination_residue(
    plan: CoordinationPlan,
    *,
    closed: bool,
    action_due_date: "date | None",
    today: "date",
) -> CoordinationResidue:
    """The accepted Commitment rule, including closed-with-a-live-action.

    A closed Commitment is not finished work while the project's own Next
    Action is still open: the organization reported completion, so the
    follow-up is confirmed rather than dropped, and it still needs an owner.
    With no live action, a closed Commitment is residue-free.
    """
    owner_matters = not closed or plan.next_action is not None
    missing_owner = not plan.internal_owner and owner_matters
    missing_next_action = not plan.next_action and not closed
    codes = []
    if plan.next_action is not None and closed:
        codes.append("external_closure_follow_up")
    if missing_owner:
        codes.append("missing_internal_owner")
    if plan.next_action:
        if action_due_date is None:
            codes.append("action_due_date_unknown")
        elif action_due_date <= today:
            codes.append("action_due")
    elif missing_next_action:
        codes.append("missing_next_action")
    return _residue(
        missing_owner=missing_owner,
        missing_next_action=missing_next_action,
        codes=tuple(codes),
    )


def read_admitted_statement_residue(
    plan: CoordinationPlan,
    *,
    evidence_available: bool,
    roster_available: bool,
) -> CoordinationResidue:
    """The guided residue on a mechanically admitted Commitment (ADR-0042).

    Its accepted facts are already fixed, so the only human residue is the
    Follow-up Plan.  Without verified Evidence there is nothing to decide from,
    and with no active roster the Internal Owner cannot be recorded; each is
    reported as the gap it is rather than as an empty picker.
    """
    residue = read_coordination_residue(plan, live=True)
    if not evidence_available:
        return CoordinationResidue(
            decision=RESIDUAL_BLOCKED,
            missing_owner=residue.missing_owner,
            missing_next_action=residue.missing_next_action,
            reason_codes=residue.reason_codes,
            authority_gap=ADMITTED_EVIDENCE_UNAVAILABLE_GAP,
        )
    if residue.decision == RESIDUAL_OWNER and not roster_available:
        return CoordinationResidue(
            decision=residue.decision,
            missing_owner=residue.missing_owner,
            missing_next_action=residue.missing_next_action,
            reason_codes=residue.reason_codes,
            authority_gap=ADMITTED_ROSTER_UNAVAILABLE_GAP,
        )
    return residue


@dataclass(frozen=True)
class ActionTiming:
    """Where one Action Due Date stands against a reading's own cutoff."""

    overdue: bool
    days: int
    due_soon: bool


def read_action_timing(
    action_due_date: "date",
    today: "date",
    *,
    due_soon_days: int | None,
) -> ActionTiming:
    """Split one Action Due Date into overdue or due-soon, once.

    ``due_soon_days`` is the project's own configured threshold; ``None`` means
    the caller is classifying an already-raised condition rather than selecting
    one, so every not-yet-overdue date is still its soon band.
    """
    delta = (action_due_date - today).days
    if delta < 0:
        return ActionTiming(overdue=True, days=-delta, due_soon=False)
    return ActionTiming(
        overdue=False,
        days=delta,
        due_soon=due_soon_days is None or delta <= due_soon_days,
    )
