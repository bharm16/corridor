"""Support Assessments: whether exact Source Segments support one proposition.

Why this exists.  Corridor used to carry one boolean, ``EvidenceLink.verified``,
and showed it to customers as "verified".  It answered a mechanical question
(does the cited passage exist on that page) and was read as a semantic one
(does the passage support the value).  ADR-0077 split the two, then required
one four-link lineage for every value, which fit source-backed fields and
nothing else.  ADR-0082 replaced that with a relation: a **Support
Assessment** binds one typed proposition to one or more Source Segments and
records the evidence role, the assessment, and exactly one authority, a human
principal or a released policy with the ruleset it applied, with the time.  It
is never a column on a segment or an evidence link, because one passage can
support one proposition, contradict a second, and merely attribute a third
(ADR-0001 refused asserted field meaning on evidence links for the same
reason).

What was tried before.  Storing support on the passage with a per-field map
recreated the JSON-payload shape ADR-0067 retired and made the judgment
unattributable per proposition.  Treating a passed Source Passage Check as
support let the extractor's locator arithmetic stand in for a human's reading;
this module never reads locator validity, and nothing here may infer support
from it.  Conflating the assessment with the Human Record Decision (what the
accepted record shows) lost one of the two reversal paths; the relation keeps
its own identity, actor, and history, so one guided Save may write both in
one transaction and each remains its own act.

Propositions today are a Source Fact or an Extracted Proposal, by real foreign
key.  A Proposed Delta (#518) or an accepted-field proposition (#509) joins
``ck_support_assessments_proposition`` with its own typed column and composite
key; the relation never accepts an unchecked object-type/object-id pair.

Writes go through ``append_support_assessment`` in ``source_append.py``, the
one ``SECURITY DEFINER`` command that owns the tables (#492, #530).  Rows are
append-only: a correction supersedes its predecessor once, and the readers
here return the effective set or the set as of a moment without destroying
any prior judgment.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, aliased

from corridor.models import (
    SUPPORT_ASSESSMENT_EVIDENCE_ROLES,
    SUPPORT_ASSESSMENT_OUTCOMES,
    SourceSegment,
    SupportAssessment,
    SupportAssessmentSource,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.source_append import append_support_assessment


EVIDENCE_ROLES = frozenset(SUPPORT_ASSESSMENT_EVIDENCE_ROLES)
ASSESSMENTS = frozenset(SUPPORT_ASSESSMENT_OUTCOMES)


class SupportAssessmentRefused(ValueError):
    """The caller cannot record an attributable, typed Support Assessment."""


class StaleSupportAssessment(SupportAssessmentRefused):
    """The predecessor the caller named was superseded first."""


@dataclass(frozen=True, slots=True)
class FactProposition:
    """A Source Fact as the proposition being assessed."""

    fact_id: int


@dataclass(frozen=True, slots=True)
class ExtractedProposalProposition:
    """An Extracted Proposal as the proposition being assessed."""

    extracted_proposal_id: int


# The extension seam: a Proposed Delta or accepted-field proposition is a
# new member here, a new kind in the database check, and a new typed column.
Proposition = FactProposition | ExtractedProposalProposition


@dataclass(frozen=True, slots=True)
class ReleasedPolicy:
    """A released policy and the exact ruleset it applied (ADR-0050)."""

    policy: str
    ruleset_version: str

    def __post_init__(self) -> None:
        if not self.policy.strip() or not self.ruleset_version.strip():
            raise SupportAssessmentRefused(
                "a released policy names itself and the ruleset it applied"
            )


Authority = HumanPrincipal | ReleasedPolicy


def record_support_assessment(
    session: Session,
    *,
    project_id: int,
    proposition: Proposition,
    source_segment_ids: Sequence[int],
    evidence_role: str,
    assessment: str,
    authority: Authority,
    assessed_at: datetime | None = None,
    supersedes_id: int | None = None,
) -> SupportAssessment:
    """Append one Support Assessment; replaying the same one returns its row.

    ``supersedes_id`` names the effective assessment of the same proposition
    and role this one corrects.  Omitting it while one exists is refused, and
    naming one that was superseded first raises ``StaleSupportAssessment``;
    the caller re-reads and decides again rather than overwriting.
    """

    if evidence_role not in EVIDENCE_ROLES:
        raise SupportAssessmentRefused(
            f"evidence role must be one of {sorted(EVIDENCE_ROLES)}"
        )
    if assessment not in ASSESSMENTS:
        raise SupportAssessmentRefused(
            f"assessment must be one of {sorted(ASSESSMENTS)}"
        )
    if not source_segment_ids:
        raise SupportAssessmentRefused(
            "a Support Assessment names at least one Source Segment"
        )
    if isinstance(authority, ReleasedPolicy):
        human_principal = None
        released_policy = authority.policy
        ruleset_version = authority.ruleset_version
    else:
        human_principal = require_human_principal(authority).subject
        released_policy = None
        ruleset_version = None
    kind, fact_id, proposal_id = _proposition_columns(proposition)

    try:
        return append_support_assessment(
            session,
            project_id=project_id,
            proposition_kind=kind,
            fact_id=fact_id,
            extracted_proposal_id=proposal_id,
            source_segment_ids=tuple(int(value) for value in source_segment_ids),
            evidence_role=evidence_role,
            assessment=assessment,
            human_principal=human_principal,
            released_policy=released_policy,
            ruleset_version=ruleset_version,
            assessed_at=assessed_at,
            supersedes_id=supersedes_id,
        )
    except DBAPIError as exc:
        if "predecessor is stale" in str(getattr(exc, "orig", exc)):
            raise StaleSupportAssessment(
                "Support Assessment predecessor was superseded first"
            ) from exc
        raise


def current_support_assessments(
    session: Session, project_id: int, proposition: Proposition
) -> tuple[SupportAssessment, ...]:
    """The effective assessments of one proposition, one per evidence role."""

    return tuple(
        session.scalars(
            _proposition_query(project_id, proposition)
            .where(SupportAssessment.superseded_by.is_(None))
            .order_by(SupportAssessment.evidence_role)
        ).all()
    )


def support_assessments_as_of(
    session: Session, project_id: int, proposition: Proposition, at: datetime
) -> tuple[SupportAssessment, ...]:
    """The assessments that were effective at ``at``, without losing any since."""

    successor = aliased(SupportAssessment)
    return tuple(
        session.scalars(
            _proposition_query(project_id, proposition)
            .outerjoin(successor, successor.id == SupportAssessment.superseded_by)
            .where(
                SupportAssessment.assessed_at <= at,
                (successor.id.is_(None)) | (successor.assessed_at > at),
            )
            .order_by(SupportAssessment.evidence_role)
        ).all()
    )


def support_assessment_history(
    session: Session, project_id: int, proposition: Proposition
) -> tuple[SupportAssessment, ...]:
    """Every assessment ever recorded for one proposition, oldest first."""

    return tuple(
        session.scalars(
            _proposition_query(project_id, proposition).order_by(
                SupportAssessment.evidence_role,
                SupportAssessment.assessed_at,
                SupportAssessment.id,
            )
        ).all()
    )


def assessed_source_segments(
    session: Session, assessment: SupportAssessment
) -> tuple[SourceSegment, ...]:
    """The segments one assessment weighed, in the order it named them."""

    return tuple(
        session.scalars(
            select(SourceSegment)
            .join(
                SupportAssessmentSource,
                SupportAssessmentSource.source_segment_id == SourceSegment.id,
            )
            .where(SupportAssessmentSource.support_assessment_id == assessment.id)
            .order_by(SupportAssessmentSource.ordinal)
        ).all()
    )


def _proposition_columns(
    proposition: Proposition,
) -> tuple[str, int | None, int | None]:
    if isinstance(proposition, FactProposition):
        return "source_fact", int(proposition.fact_id), None
    if isinstance(proposition, ExtractedProposalProposition):
        return "extracted_proposal", None, int(proposition.extracted_proposal_id)
    raise SupportAssessmentRefused(
        f"{type(proposition).__name__} is not a typed proposition"
    )


def _proposition_query(project_id: int, proposition: Proposition):
    kind, fact_id, proposal_id = _proposition_columns(proposition)
    query = select(SupportAssessment).where(
        SupportAssessment.project_id == project_id,
        SupportAssessment.proposition_kind == kind,
    )
    if fact_id is not None:
        return query.where(SupportAssessment.fact_id == fact_id)
    return query.where(SupportAssessment.extracted_proposal_id == proposal_id)
