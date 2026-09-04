"""The Issue section of one project's ordered week, and the act it carries.

#529 prepares an immutable release candidate and #533 authorizes one, and both
merged with **no route at all**: there was no release surface in `web/app.py`
to extend, so a coordinator could neither see what had been prepared for their
customer nor approve it. #536's Issue section is where both belong, and this
module is the reading it renders — the counterpart of
``corridor.web.follow_up_view`` for the third section of the week.

**It consumes three derivations and performs none of them.**
``authorization_blockers`` (#529) is the single authority on whether this
candidate may be authorized; ``candidate_is_stale`` says which of those reasons
are staleness rather than the bound blocked state; ``candidate_set`` and
``disclosed_exceptions`` (#533) enumerate the artifacts and the honest adverse
conditions the candidate was prepared against. Nothing here re-derives
readiness, re-computes staleness, or decides a second time what blocks. Two
authorities over one readiness rule is the failure #641 exists to prevent, and
a screen is exactly where it would be reintroduced, because "grey the button
out unless everything looks fine" is such an easy line to write.

**The refusal the database owns is not pre-empted here.** Only a principal
holding the external-release designation may authorize, and #533 proves that
*inside PostgreSQL*, as the command's own owner, raising ``42501``. This module
therefore holds no designation check of its own and hides no control on the
strength of one: a second Python gate could drift from the roster the command
reads, and a control hidden by a rule the database does not enforce tells a
coordinator something untrue about who may act. The section states the rule in
words beside the control instead, and the refusal is rendered when the database
gives it.

**Nothing authorizable is offered while anything blocks.**
``_refuse_offered_with_blockers`` compares the state this view is about to
render against the blocker list it read, and raises rather than shipping a
screen that offers an act #533 would refuse. A blocked candidate and a stale
one are both un-offerable for the same reason and by the same authority: what
stands in the way is bound into the candidate's own identity, so it is cleared
by preparing a fresh candidate and never by pressing anything here.

**No predecessor is invented.** ADR-0086 requires a project's first issue to be
valid with nothing before it, and the predecessor is read from the candidate's
own ``previous_package_id`` — never the newest package by timestamp, never the
adopted baseline, never the last rendered report.
``_refuse_invented_predecessor`` refuses a view whose stated predecessor is not
the one the candidate was bound to, so "before the first issue there is none"
is a mechanical property rather than a sentence somebody remembered to write.

**Everything printed about the candidate comes out of the candidate.** The
bound coverage identity and digest, the unread source count, the unmet coverage
requirements, the policies that blocked, and the disclosed exceptions are all
read back from the immutable ``input_declaration`` the candidate's identity is
the digest of. Re-deriving any of them at reading time would let the screen
describe a candidate that was never prepared.

**No clock.** ``as_of`` is the same declared reporting cutoff the rest of the
week is read at, and the release instant is the caller's. Nothing here reads
the day.

Terminology: nothing here coins a customer word. ``Release candidate``,
``ReleasePackage`` and ``Issue Profile`` stay the internal technical names
ADR-0086 and ADR-0091 put them in and appear on no screen; the section speaks
of the issue, what it contains, and approving it for sharing, which is the
wording #536's heading already uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.issue_content import ARTIFACT_WORDS
from corridor.models import BLOCKED, ReleaseCandidate, ReleasePackage
from corridor.presentation import field_label
from corridor.release_authorization import (
    SealedArtifact,
    candidate_set,
    disclosed_exceptions,
)
from corridor.release_candidate import (
    authorization_blockers,
    candidate_is_stale,
    current_release_candidate,
)
from corridor.web.ui_primitives import StateLabel


class IssueViewRefused(ValueError):
    """The section would state or offer something the derivations do not."""


# The four states this section can be in. They are internal identifiers; the
# words each prints are in the labels and sentences below, built from #536's
# own heading rather than from a new label for anything.
NOTHING_PREPARED = "nothing_prepared"
AUTHORIZABLE = "authorizable"
NOT_AUTHORIZABLE = "not_authorizable"
ALREADY_AUTHORIZED = "already_authorized"

STATE_LABELS: dict[str, StateLabel] = {
    NOTHING_PREPARED: StateLabel("neutral", "Nothing prepared yet"),
    AUTHORIZABLE: StateLabel("neutral", "Ready for your approval"),
    NOT_AUTHORIZABLE: StateLabel("attention", "Cannot be approved as it stands"),
    ALREADY_AUTHORIZED: StateLabel("settled", "Approved and sent as this issue"),
}

# The one recovery every un-offerable candidate leads to, said once. ADR-0086
# makes the blocking coverage and decision state part of the candidate's own
# identity, so there is nothing to press that could clear it in place.
FRESH_PREPARATION = (
    "This candidate cannot be put right in place: what stands in the way is "
    "bound into the identity it was prepared under, so the way forward is a "
    "freshly prepared candidate read at the current cutoff. It stays listed "
    "here as it was prepared, so what was proposed to the customer and refused "
    "is not lost."
)

# Said beside the control, because the rule the database enforces is not the
# rule "you can see the button" would imply.
DESIGNATION_RULE = (
    "Approving an issue needs this project's external-release designation. "
    "PostgreSQL proves that when the approval is submitted, as the release "
    "command's own owner, so project membership and project coordination "
    "confer none of it and a caller that skipped a screen releases nothing. "
    "If you do not hold it, this refuses and nothing is sent."
)


@dataclass(frozen=True, slots=True)
class PackageReference:
    """One authorized issue, named the way a coordinator names one.

    ``issue_number`` is the package's position in the project's release chain,
    which is what a person means by "the third issue". The receipt's own row id
    and digest identity are internal identifiers and are deliberately absent.
    """

    issue_number: int
    accepted_revision_id: int
    authorized_at: datetime
    authorized_by_principal: str


@dataclass(frozen=True, slots=True)
class ArtifactRow:
    """One member of the set as the section's table reads it."""

    words: str
    produced_by: str
    content_sha256: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class BoundDeclaration:
    """What the candidate's immutable input declaration says it was bound to.

    Read back out of the declaration the candidate's identity is the digest of,
    so every fact the section prints about a candidate is a fact that candidate
    was actually prepared with.
    """

    coverage_identity: str
    coverage_sha256: str
    unread_source_count: int
    unmet_coverage: tuple[str, ...]
    blocked_decisions: tuple[str, ...]
    exceptions: tuple[str, ...]
    artifact_types: tuple[str, ...]

    @property
    def prepared_blockers(self) -> tuple[str, ...]:
        """Every blocker bound into this candidate, coverage first."""

        return self.unmet_coverage + self.blocked_decisions


@dataclass(frozen=True, slots=True)
class IssueView:
    """One project's Issue section: the candidate, its state, and one act.

    ``may_authorize`` is the only thing the template asks before rendering the
    approval control, and it is true only where ``blockers`` is empty — which
    is #529's answer, not a second one taken here.
    """

    project_id: int
    cutoff: datetime
    state: str
    candidate: ReleaseCandidate | None = None
    declaration: BoundDeclaration | None = None
    artifacts: tuple[SealedArtifact, ...] = ()
    blockers: tuple[str, ...] = ()
    stale_reasons: tuple[str, ...] = ()
    blocked: bool = False
    predecessor: PackageReference | None = None
    authorized: PackageReference | None = None

    @property
    def prepared(self) -> bool:
        return self.candidate is not None

    @property
    def may_authorize(self) -> bool:
        """Whether this section offers the approval at all."""

        return self.state == AUTHORIZABLE

    @property
    def state_label(self) -> StateLabel:
        return STATE_LABELS[self.state]

    @property
    def exceptions(self) -> tuple[str, ...]:
        return () if self.declaration is None else self.declaration.exceptions

    @property
    def prepared_blockers(self) -> tuple[str, ...]:
        return () if self.declaration is None else self.declaration.prepared_blockers

    @property
    def summary(self) -> str:
        """What this section is saying, in one sentence."""

        if self.state == NOTHING_PREPARED:
            return (
                "No issue has been prepared for this project yet, so there is "
                "nothing here to approve. Preparing one is not something this "
                "page does."
            )
        if self.state == ALREADY_AUTHORIZED:
            assert self.authorized is not None
            return (
                f"This candidate was approved and is issue "
                f"{self.authorized.issue_number} for this project. It cannot be "
                "approved twice, and the next issue starts from a freshly "
                "prepared candidate."
            )
        if self.state == NOT_AUTHORIZABLE:
            count = len(self.blockers)
            return (
                f"{count} thing{'' if count == 1 else 's'} "
                f"{'stands' if count == 1 else 'stand'} between this prepared "
                "candidate and the customer, so it is not offered for approval."
            )
        count = len(self.exceptions)
        if count:
            return (
                f"This issue is ready to approve, and it discloses {count} "
                f"adverse condition{'' if count == 1 else 's'} honestly rather "
                "than waiting for them to clear."
            )
        return "This issue is ready to approve, with nothing outstanding on it."

    @property
    def facts(self) -> tuple[tuple[str, Any], ...]:
        """The candidate's own bound inputs, as terms a screen reader reads."""

        if self.candidate is None or self.declaration is None:
            return ()
        return (
            ("Accepted Project Record revision", int(self.candidate.accepted_revision_id)),
            ("Sources captured up to", self.candidate.source_cutoff.date().isoformat()),
            (
                "The issue before this one",
                f"issue {self.predecessor.issue_number}, made from revision "
                f"{self.predecessor.accepted_revision_id}"
                if self.predecessor is not None
                else "none — this would be this project's first issue",
            ),
            ("What the customer receives", ", ".join(self.artifact_words) or "nothing"),
            ("Declared source coverage", self.declaration.coverage_identity),
            (
                "Sources delivered but unreadable at preparation",
                self.declaration.unread_source_count,
            ),
            ("Prepared by", self.candidate.prepared_by_principal),
            ("Prepared at", self.candidate.prepared_at.isoformat()),
        )

    @property
    def artifact_words(self) -> tuple[str, ...]:
        """Each configured artifact in the plain words #641 already spells."""

        return tuple(_words(one.artifact_type) for one in self.artifacts)

    @property
    def artifact_rows(self) -> tuple[ArtifactRow, ...]:
        """The set as a table reads it, in the order the content digest binds."""

        return tuple(
            ArtifactRow(
                words=_words(one.artifact_type),
                produced_by=f"{one.renderer_identity} {one.renderer_version}",
                content_sha256=one.content_sha256,
                byte_count=one.byte_count,
            )
            for one in self.artifacts
        )

    # The two sentences the section prints beside its own controls, reached
    # through the view so the template holds no wording of its own.
    fresh_preparation = FRESH_PREPARATION
    designation_rule = DESIGNATION_RULE


def issue_view(session: Session, *, project_id: int, as_of: datetime) -> IssueView:
    """Read this project's current candidate and what may be done with it.

    ``as_of`` is the reporting cutoff the rest of the week is read at, and it
    is what ``authorization_blockers`` compares the candidate against, so the
    section and a release attempted from it answer at the same instant.
    """

    candidate = current_release_candidate(session, project_id)
    if candidate is None:
        return IssueView(
            project_id=project_id, cutoff=as_of, state=NOTHING_PREPARED
        )

    package = session.scalars(
        select(ReleasePackage).where(ReleasePackage.candidate_id == candidate.id)
    ).first()
    # #529's answer, read once. Nothing below asks it a second way.
    blockers = authorization_blockers(session, candidate, as_of=as_of)
    stale_reasons = candidate_is_stale(session, candidate, as_of=as_of)
    previous = _package(session, candidate.previous_package_id)
    _refuse_invented_predecessor(candidate, previous)

    if package is not None:
        state = ALREADY_AUTHORIZED
    elif blockers:
        state = NOT_AUTHORIZABLE
    else:
        state = AUTHORIZABLE
    _refuse_offered_with_blockers(state, blockers)

    return IssueView(
        project_id=project_id,
        cutoff=as_of,
        state=state,
        candidate=candidate,
        declaration=read_declaration(candidate),
        artifacts=candidate_set(session, candidate),
        blockers=blockers,
        stale_reasons=stale_reasons,
        blocked=candidate.readiness == BLOCKED,
        predecessor=_reference(previous),
        authorized=_reference(package),
    )


def read_declaration(candidate: ReleaseCandidate) -> BoundDeclaration:
    """The bound inputs, out of the declaration the identity is a digest of."""

    declared = json.loads(candidate.input_declaration)
    coverage = declared.get("coverage") or {}
    derived = declared.get("derived_state") or {}
    return BoundDeclaration(
        coverage_identity=coverage.get("identity") or "",
        coverage_sha256=coverage.get("content_sha256") or "",
        unread_source_count=int(derived.get("unread_source_count") or 0),
        unmet_coverage=tuple(derived.get("unmet_coverage") or ()),
        blocked_decisions=tuple(
            _blocked_sentence(entry)
            for entry in derived.get("blocked_decisions") or ()
        ),
        exceptions=tuple(disclosed_exceptions(candidate)),
        artifact_types=tuple(declared.get("configured_artifact_types") or ()),
    )


def _blocked_sentence(entry: dict[str, Any]) -> str:
    """One customer policy that waited on a decision nobody had made.

    The declaration binds the policy, the selector it executed and the proposed
    change it selected, which is what a coordinator has to go and settle. It
    deliberately does not restate the policy's prose: that lives in the issue
    profile, and a sentence composed here could disagree with it.
    """

    selector = str(entry.get("selector") or "")
    field = selector.split(":", 1)[1] if selector.startswith("field:") else ""
    named = field_label(field) if field else selector
    return (
        f"proposed change {entry.get('delta_id')} is undecided, and this "
        f"project's policy {entry.get('policy')} waits on {named}"
    )


def _package(session: Session, package_id: Any) -> ReleasePackage | None:
    if package_id is None:
        return None
    return session.get(ReleasePackage, int(package_id))


def _reference(package: ReleasePackage | None) -> PackageReference | None:
    if package is None:
        return None
    return PackageReference(
        issue_number=int(package.sequence_number),
        accepted_revision_id=int(package.accepted_revision_id),
        authorized_at=package.authorized_at,
        authorized_by_principal=package.authorized_by_principal,
    )


def _words(artifact_type: str) -> str:
    """The words one configured artifact goes by, in #641's own spelling.

    ``ARTIFACT_WORDS`` is the vocabulary the issue content already publishes,
    including for the mandatory workbook. Respelling it here would be a second
    name for one thing on one more screen, which ADR-0048 exists to stop.
    """

    return ARTIFACT_WORDS.get(artifact_type, artifact_type)


# --- the guards -------------------------------------------------------------


def _refuse_offered_with_blockers(state: str, blockers: tuple[str, ...]) -> None:
    """Refuse to offer an approval #533 would refuse.

    ``authorization_blockers`` is the authority and this is that authority made
    mechanical at the surface: a screen that offered the act anyway would put a
    coordinator in front of a control whose only outcome is a refusal, and a
    screen that derived its own second opinion about readiness is the drift
    #641 exists to prevent.
    """

    if state == AUTHORIZABLE and blockers:
        raise IssueViewRefused(
            "this candidate cannot be authorized and the section would offer "
            "it anyway; whether an issue may be approved is #529's answer, "
            f"and it named {len(blockers)} reason(s) not to"
        )


def _refuse_invented_predecessor(
    candidate: ReleaseCandidate, previous: ReleasePackage | None
) -> None:
    """Refuse a predecessor the candidate was not prepared against.

    ADR-0086 requires a project's first issue to be valid with nothing before
    it, and every substitute an eye could reach for — the newest package by
    timestamp, the adopted baseline, the last rendered report — is a different
    fact wearing the same heading. The comparison baseline is the one bound
    into the candidate, and before the first authorized package it is an
    explicit none.
    """

    stated = None if previous is None else int(previous.id)
    bound = (
        None
        if candidate.previous_package_id is None
        else int(candidate.previous_package_id)
    )
    if stated != bound:
        raise IssueViewRefused(
            "the predecessor shown is not the one this candidate was prepared "
            f"against ({stated} against {bound}); the comparison baseline is "
            "bound into the candidate and is never derived at reading time"
        )
