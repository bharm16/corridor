"""The one comparison that turns what a source states into Proposed Deltas.

ADR-0076 puts exactly one step between what a source says and what the record
says: a typed difference from the accepted record, open until a human resolves
it.  Five producers appended those differences — the scheduled structured-cell
pass (`delta_generation`), a later UCM revision (`later_revision`), a Key Date
table (`key_date_table`), an email thread (`email_spine`) and minutes
(`minutes_spine`) — and every one of them had written the same branch out
again::

    if key in accepted:
        if accepted[key] == value:
            agreed += 1
            continue
        change_type = "modify"
    else:
        change_type = "add"

with its own copy of the proposed-subject delta beside it and, in the two
enumerative readers, a verbatim copy of the apparent-removal dictionary
comprehension as well.  Five copies of one rule is five chances for the rule to
drift: a value that agreed on one path could propose a change on another, and
nothing in the codebase said which reading was the contract.  So the rule lives
here, once, and every producer keeps only its own contribution — how it reads
its bytes, how it pairs a row with an accepted subject, which fields it
compares, and the rule version it compares under.

**It takes values, not callbacks.**  A producer hands over what its source
states as plain field/value pairs per subject, says whether that subject is
paired with an accepted one, and gets back the deltas, the count of values the
record already held, and the rows that turned out to say nothing new.  A
callback interface was the alternative and was rejected: it would have let each
producer keep deciding *when* a difference is a modify, which is precisely the
decision that had drifted into five copies.

**What each producer still owns.**  Its reader and its row accounting, its
identity rule (which is what `paired` and `unresolved_accepted_subjects` are the
answers of), its field set, its rule-version string, and its own removal
receipt.  The two enumerative readers genuinely disagree about *which*
withholding reason a person is shown when a source is both unsealed and
otherwise blocked, so that difference is one named parameter with a test
(`removal_disposition`) rather than two code paths.

Reading the accepted record lives here too, because the accepted side of the
comparison must be read one way: `accepted_values` is the projection's own
scalar reading, and `revision_label` names the baseline every delta was
compared against.  `delta_generation.COMPARABLE_FACT_TYPES` deliberately stays
where the pass that selects Facts by it lives.

Binding the delivery a revision arrived on is here for the same reason both
readers give in their own words: a capture that cannot name the customer's own
identity for the source and the external version it arrived at "has no revision
identity to compare under".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from sqlalchemy import text
from sqlalchemy.orm import Session

from corridor.fact_values import scalar_column_value
from corridor.models import Project, SourceDelivery
from corridor.proposed_deltas import (
    ExistingSubjectTarget,
    ProposedDeltaValues,
    ProposedSubjectTarget,
)
from corridor.source_delivery import stored_delivery

# The `target_field` an apparent removal names.  A removal is not about one
# column, and one whole-subject spelling is used by every producer so the word
# never means two things across the seam.
ENTIRE_SUBJECT = "entire_subject"

# The disposition of a source row that was paired with an accepted subject and
# compared field by field.  Every producer's own disposition set includes it,
# and it means the same thing in all of them.
COMPARED = "compared"

# Which withholding reason a removal candidate reports when a source is both
# unsealed and otherwise blocked.  The producers differ, deliberately; the test
# module says why in the words of each.
AMBIGUITY_BEFORE_SEAL = "blocking_reason_before_the_seal"
SEAL_BEFORE_ROW_FAILURE = "the_seal_before_the_blocking_reason"
_PRECEDENCES = (AMBIGUITY_BEFORE_SEAL, SEAL_BEFORE_ROW_FAILURE)


class ComparisonRefused(ValueError):
    """A comparison cannot state a difference safely."""


@dataclass(frozen=True)
class StatedSubject:
    """What one source states about one subject, and how it was paired.

    `values` are field/value pairs in the order the source states them, so a
    producer that wants its differences proposed in field order sorts them
    before handing them over.  Duplicated fields are preserved rather than
    collapsed: two renditions of one field in one source are two statements and
    the comparison says so twice.

    `paired` is the producer's identity rule already applied: True when these
    values belong to a subject the accepted record holds, False when the record
    holds no such subject and the whole set is proposed as one new subject.

    `unresolved_accepted_subjects` are the accepted subjects an unpaired row
    might be but cannot be told apart from.  A row reproducing one of them
    exactly says nothing new; `row_key` is how that row is named back to the
    producer's own accounting when it does.
    """

    subject_identity: str
    values: tuple[tuple[str, Any], ...]
    paired: bool = True
    row_key: str | None = None
    unresolved_accepted_subjects: tuple[str, ...] = ()


@dataclass(frozen=True)
class Comparison:
    """The differences, the values the record already held, and the no-ops."""

    deltas: tuple[ProposedDeltaValues, ...]
    values_agreed: int
    restated_rows: frozenset[str]


RowT = TypeVar("RowT")
AccountingT = TypeVar("AccountingT")


@dataclass(frozen=True)
class RowPlan(Generic[RowT]):
    """One source row, the subject it resolved to, and what to do with it."""

    row: RowT
    subject_identity: str
    disposition: str


@dataclass(frozen=True)
class SourcePlan(Generic[RowT, AccountingT]):
    """Every row's resolution, the removal candidates, and the accounting."""

    rows: tuple[RowPlan[RowT], ...]
    removals: tuple[Any, ...] = ()
    accounting: AccountingT | None = None
    sealed: bool = False


def compare_stated_subjects(
    *,
    accepted: Mapping[tuple[str, str], Any],
    stated: Sequence[StatedSubject],
    comparison_rule_version: str,
    accepted_baseline_revision: str | None,
    removals: Sequence[str] = (),
    sealed: bool = False,
) -> Comparison:
    """The typed differences between what a source states and the accepted record.

    A stated value equal to the accepted one produces nothing, which is the
    whole point of comparing at all: a revision of five hundred rows that
    changed three of them proposes three changes.

    `removals` are the accepted subjects the producer has already decided this
    source's absence speaks for (see `removal_disposition`).  `sealed` is the
    caller's declaration that the source is a complete enumeration whose row
    accounting is sealed; without it an apparent removal is refused here rather
    than filtered silently, which is the same rule
    `proposed_deltas.create_proposed_delta_group` enforces at the write.
    """

    proposals: list[ProposedDeltaValues] = []
    agreed = 0
    restated: set[str] = set()

    def delta(**values: Any) -> ProposedDeltaValues:
        return ProposedDeltaValues(
            comparison_rule_version=comparison_rule_version,
            accepted_baseline_revision=accepted_baseline_revision,
            **values,
        )

    for subject in stated:
        if subject.paired:
            for field_name, value in subject.values:
                key = (subject.subject_identity, field_name)
                if key in accepted:
                    if accepted[key] == value:
                        agreed += 1
                        continue
                    change_type = "modify"
                else:
                    change_type = "add"
                proposals.append(
                    delta(
                        change_type=change_type,
                        target=ExistingSubjectTarget(
                            subject_identity=subject.subject_identity,
                            field=field_name,
                        ),
                        accepted_value=accepted.get(key),
                        proposed_value=value,
                    )
                )
            continue
        if _stands_accepted(subject, accepted):
            agreed += len(subject.values)
            if subject.row_key is not None:
                restated.add(subject.row_key)
            continue
        proposals.append(
            delta(
                change_type="add",
                target=ProposedSubjectTarget(
                    subject_identity=subject.subject_identity,
                    proposed_fields=tuple(
                        dict.fromkeys(name for name, _value in subject.values)
                    ),
                ),
                proposed_value=dict(subject.values),
            )
        )

    for subject_identity in removals:
        if not sealed:
            raise ComparisonRefused(
                "an apparent removal is stated only by a complete enumerative "
                "source whose row accounting is sealed"
            )
        proposals.append(
            delta(
                change_type="apparent_removal",
                target=ExistingSubjectTarget(
                    subject_identity=subject_identity, field=ENTIRE_SUBJECT
                ),
                accepted_value={
                    field_name: value
                    for (subject, field_name), value in sorted(accepted.items())
                    if subject == subject_identity
                },
                proposed_value=None,
            )
        )
    return Comparison(
        deltas=tuple(proposals),
        values_agreed=agreed,
        restated_rows=frozenset(restated),
    )


def _stands_accepted(
    subject: StatedSubject, accepted: Mapping[tuple[str, str], Any]
) -> bool:
    """Whether this row's complete set of stated values already stands accepted.

    Asked only where an identity is unresolved, and asked of the *whole* row:
    the accepted subjects it might be cannot be told apart, so the only safe
    reading is that a row reproducing one of them exactly says nothing new, and
    a row that does not is a difference somebody has to place.
    """

    stated = dict(subject.values)
    for candidate in subject.unresolved_accepted_subjects:
        standing = {
            field_name: value
            for (owner, field_name), value in accepted.items()
            if owner == candidate
        }
        if standing == stated:
            return True
    return False


def removal_disposition(
    *,
    sealed: bool,
    unsealed_reason: str,
    blocking_reason: str | None,
    precedence: str,
) -> tuple[bool, str | None]:
    """Whether one absent accepted subject is proposed, and why it is not.

    Two reasons can withhold a removal at once: the source is not a complete
    sealed enumeration, and something about this candidate blocks it anyway.
    Which one a person is shown is the one difference between the enumerative
    readers, so it is stated here as `precedence` rather than written twice.
    """

    if precedence not in _PRECEDENCES:
        raise ComparisonRefused(
            f"{precedence!r} is not a declared withholding precedence"
        )
    if precedence == AMBIGUITY_BEFORE_SEAL:
        withheld = blocking_reason or (None if sealed else unsealed_reason)
    else:
        withheld = unsealed_reason if not sealed else blocking_reason
    return withheld is None, withheld


def accepted_values(session: Session, project_id: int) -> dict[tuple[str, str], Any]:
    """The accepted record as one comparable scalar per subject and field.

    The projection is read through ``current_project_record``, the view
    ``current_record`` proves, and the value is read by ``fact_values`` so the
    accepted side and the source side of every comparison share one shape.

    Only the scalar columns are selected, deliberately: the comparison covers
    ``delta_generation.COMPARABLE_FACT_TYPES`` and a satellite field's key stays
    in the mapping with a ``None`` value, so an already-appended delta's
    ``accepted_value`` keeps meaning what it meant.  Reading the satellites here
    would change every open delta on a satellite field, which is a comparison
    change and belongs with widening ``COMPARABLE_FACT_TYPES``, not with a
    reader split.
    """

    rows = session.execute(
        text(
            "select subject_key, fact_type, text_value, date_value, "
            "external_org_value_id, document_value_id "
            "from current_project_record where project_id = :project_id"
        ),
        {"project_id": project_id},
    ).all()
    return {
        (subject_key, fact_type): scalar_column_value(
            fact_type, text_value, date_value, external_org_value_id, document_value_id
        )
        for (
            subject_key,
            fact_type,
            text_value,
            date_value,
            external_org_value_id,
            document_value_id,
        ) in rows
    }


def revision_label(revision_id: int | None) -> str | None:
    """How a delta names the accepted baseline it was compared against."""

    return f"revision:{revision_id}" if revision_id is not None else None


def require_bound_delivery(
    session: Session,
    project: Project,
    staged,
    envelope,
    *,
    refusal: type[ValueError],
    source_noun: str,
    identity_noun: str,
    act_noun: str,
) -> SourceDelivery:
    """The exact bytes, digest, source identity, and external version are held.

    Checked rather than trusted: the ledger row is what retains the customer's
    own identity for the source and the external version it arrived at, and a
    capture that cannot name one has no revision identity to compare under.

    The proven row is returned rather than discarded, because it is also the
    delivery this source's Document came in on (#675, #687).  Every check a
    caller would otherwise repeat has already happened here: the disposition is
    ``stored``, the digest is the staged source's own, and the project is this
    one.  Re-deriving that link downstream would be a second rule that could
    disagree with this one.

    `refusal` and the three nouns are the caller's own words, kept exactly as
    each reader already refused, because a refusal a project person reads names
    the act they performed — taking delivery of a *revision* or of an *export* —
    and each capture raises its own refusal type.
    """

    if envelope.content_digest != staged.sha256:
        raise refusal(
            f"the delivered digest is not the staged {source_noun}'s digest"
        )
    if not envelope.external_identity.strip() or not envelope.external_version.strip():
        raise refusal(
            f"{act_noun} names the customer's own identity for the "
            f"{identity_noun} and the external version it arrived at"
        )
    delivery = stored_delivery(session, idempotency_key=envelope.idempotency_key)
    if delivery is None or delivery.content_sha256 != staged.sha256:
        raise refusal(
            "no stored delivery holds these exact bytes; take delivery of the "
            f"{source_noun} before capturing it"
        )
    if delivery.project_id != project.id:
        raise refusal("this delivery was taken for another project")
    return delivery
