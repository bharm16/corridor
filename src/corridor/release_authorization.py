"""One authorized package, and the immutable receipt that binds it (#533).

ADR-0086 makes one attributable human authorization of one prepared **set** the
external issue unit. #529 bound and prepared that set into an immutable
candidate and left ``release_packages`` empty and read-only to every runtime
login, so preparation could name a predecessor without being able to invent
one. This module is the act that populates it.

**Authorization revalidates; it never regenerates.** Every artifact was
rendered and retained during preparation. Here the bytes are read back out of
#487's content-addressed store and verified against the digests the candidate
recorded, the accepted revision, issue-profile version, template and mapping
registrations and predecessor are re-derived and compared, and the readiness
#529 already computed is read through ``authorization_blockers`` rather than
worked out a second time. Two authorities over one readiness rule is the
failure #641 exists to prevent, and it is not reintroduced here. Nothing in
this module renders anything.

**The receipt/candidate binding is composite.** ``release_packages`` carries
``(candidate_id, project_id, accepted_revision_id)`` as a foreign key into
``release_candidates (id, project_id, accepted_revision_id)``. The receipt
therefore states the revision it released *explicitly* — #635's whole
complaint about ``ExternalReportRelease``, which binds none — and in the same
key proves that it is the revision the candidate was prepared from. A receipt
whose revision disagrees with its candidate's is not a row the schema can hold,
so no application check stands between the two facts.

**Two questions, two answers.** ``revision_has_been_issued`` asks whether any
authorization references a revision at all. ``current_issue_state_differences``
asks the different question #537's portfolio needs: whether the package that
was authorized still matches the current accepted revision, the issue-profile
version in force, and the template and mapping registrations in force. The
first is a fact about history; the second is a fact about now, and a project
can answer yes to the first and no to the second on the same afternoon.

**The predecessor is a chain, never a clock.** ``latest_authorized_package``
(#529, corrected here) finds the package that no other package names as its
predecessor. Ordering by ``authorized_at`` would put the wall clock back in
charge of ADR-0086's comparison baseline, which is exactly what #634's
principle forbids; a release recorded at an earlier declared instant than its
predecessor is still its successor, because the chain says so and the clock
does not get a vote. The first package has no predecessor, invents no earlier
one, and a later package has exactly one — enforced by a composite key on the
predecessor's own sequence number, a unique successor, and one root per
project.

**Authority is proved inside PostgreSQL.** #531 delivered the external-release
designation and enforced it at the web route, which is an application check a
second caller can forget. ``authorize_release_package`` is a ``SECURITY
DEFINER`` command that re-proves the active roster entry *and* its
``can_release_externally`` flag as its own owner; no runtime login holds insert
on either package relation, so the command is the only door and a forged
principal string buys nothing.

**Legacy releases are left alone.** ``external_report_releases`` rows record a
PDF nobody can bind to an accepted revision. None is migrated, none is
adopted as a predecessor, and no revision link is inferred from a timestamp:
unknown history stays unknown by name (#635).

**Release is not delivery.** Nothing here sends an email, creates a
transmittal, records an acknowledgment, or tracks a delivery status. #563 is
where external delivery lives.

**No clock.** The release instant is supplied by the caller, exactly as the
source cutoff and the preparation instant are.

Terminology: ``ReleasePackage`` stays an internal technical name, in the class
ADR-0086 put it in. Release history is presented in the plain words a
coordinator already uses, so `docs/agents/domain.md`'s terminology-research
procedure is not triggered.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from corridor.analytics import (
    AnalyticsBinding,
    AnalyticsEvent,
    EventFamily,
    default_binding,
    emit_event,
)
from corridor.baseline_adoption import effective_baseline_formats
from corridor.issue_profile import (
    UPDATED_UCM,
    IssueInventory,
    effective_issue_inventory,
)
from corridor.models import (
    BLOCKED,
    ProjectRecordRevision,
    ReleaseCandidate,
    ReleasePackage,
    ReleasePackageArtifact,
)
from corridor.object_storage import (
    ObjectMissing,
    ObjectStore,
    StorageError,
    content_store,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.release_candidate import (
    authorization_blockers,
    candidate_artifacts,
    latest_authorized_package,
)


# The shape of the digested receipt. A receipt written under a later schema
# binds different things, so the version travels inside the bytes.
RECEIPT_SCHEMA_VERSION = "release-package-receipt-v1"

# Every reason authorization may refuse for, as a closed vocabulary. A refusal
# a person has to read a paragraph to classify is a refusal nobody counts, and
# each of these leads back to the same recovery: prepare a fresh candidate.
NOT_DESIGNATED = "not_designated"
NO_SUCH_CANDIDATE = "no_such_candidate"
CANDIDATE_BLOCKED = "candidate_blocked"
CANDIDATE_STALE = "candidate_stale"
TEMPLATE_OR_MAPPING_REPLACED = "template_or_mapping_replaced"
ARTIFACT_MISSING = "artifact_missing"
DIGEST_MISMATCH = "digest_mismatch"
REVALIDATION_FAILED = "revalidation_failed"

AUTHORIZATION_REFUSAL_REASONS: tuple[str, ...] = (
    NOT_DESIGNATED,
    NO_SUCH_CANDIDATE,
    CANDIDATE_BLOCKED,
    CANDIDATE_STALE,
    TEMPLATE_OR_MAPPING_REPLACED,
    ARTIFACT_MISSING,
    DIGEST_MISMATCH,
    REVALIDATION_FAILED,
)

# PostgreSQL's `insufficient_privilege`, which is what the command raises when
# the named principal holds no external-release designation.
_INSUFFICIENT_PRIVILEGE = "42501"


class AuthorizationRefused(Exception):
    """Authorization cannot proceed, and says which bounded reason.

    Carried rather than raised as a bare ``ValueError`` because the reason code
    is what a caller counts and what a coordinator's screen routes on; a
    message alone would make every refusal a string somebody classifies by
    reading. Every refusal leaves the candidate exactly as it was and writes no
    part of a release.
    """

    def __init__(self, code: str, sentence: str) -> None:
        if code not in AUTHORIZATION_REFUSAL_REASONS:
            raise ValueError(f"{code!r} is not a recorded refusal reason")
        super().__init__(sentence)
        self.code = code
        self.sentence = sentence


# --- what one package holds -------------------------------------------------


@dataclass(frozen=True, slots=True)
class SealedArtifact:
    """One member of the set, as the receipt enumerates it.

    ``position`` is the candidate's own ordering, so the mandatory UCM is
    always position one and the configured remainder follows in the order the
    content digest bound them.
    """

    position: int
    artifact_type: str
    renderer_identity: str
    renderer_version: str
    content_sha256: str
    storage_key: str
    byte_count: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "artifact_type": self.artifact_type,
            "renderer_identity": self.renderer_identity,
            "renderer_version": self.renderer_version,
            "content_sha256": self.content_sha256,
            "byte_count": self.byte_count,
        }


def candidate_set(
    session: Session, candidate: ReleaseCandidate
) -> tuple[SealedArtifact, ...]:
    """Every artifact one candidate holds, UCM first, then the configured rest.

    The UCM is read from the candidate's own columns rather than from an
    artifact row, carrying #529's structural decision forward: a candidate with
    no UCM and a candidate with two are both unrepresentable, so the set can
    never be assembled without one.
    """

    members = [
        SealedArtifact(
            position=1,
            artifact_type=UPDATED_UCM,
            renderer_identity=candidate.ucm_renderer_identity,
            renderer_version=candidate.ucm_renderer_version,
            content_sha256=candidate.ucm_content_sha256,
            storage_key=candidate.ucm_storage_key,
            byte_count=int(candidate.ucm_byte_count),
        )
    ]
    members.extend(
        SealedArtifact(
            position=int(row.position),
            artifact_type=row.artifact_type,
            renderer_identity=row.renderer_identity,
            renderer_version=row.renderer_version,
            content_sha256=row.content_sha256,
            storage_key=row.storage_key,
            byte_count=int(row.byte_count),
        )
        for row in candidate_artifacts(session, candidate)
    )
    return tuple(sorted(members, key=lambda one: one.position))


def package_set(
    session: Session, package: ReleasePackage
) -> tuple[SealedArtifact, ...]:
    """Every artifact one authorized package sealed, in the receipt's order."""

    members = [
        SealedArtifact(
            position=1,
            artifact_type=UPDATED_UCM,
            renderer_identity=package.ucm_renderer_identity,
            renderer_version=package.ucm_renderer_version,
            content_sha256=package.ucm_content_sha256,
            storage_key=package.ucm_storage_key,
            byte_count=int(package.ucm_byte_count),
        )
    ]
    members.extend(
        SealedArtifact(
            position=int(row.position),
            artifact_type=row.artifact_type,
            renderer_identity=row.renderer_identity,
            renderer_version=row.renderer_version,
            content_sha256=row.content_sha256,
            storage_key=row.storage_key,
            byte_count=int(row.byte_count),
        )
        for row in session.scalars(
            select(ReleasePackageArtifact)
            .where(ReleasePackageArtifact.package_id == package.id)
            .order_by(ReleasePackageArtifact.position)
        ).all()
    )
    return tuple(sorted(members, key=lambda one: one.position))


# --- the receipt ------------------------------------------------------------


def disclosed_exceptions(candidate: ReleaseCandidate) -> tuple[str, ...]:
    """The honest adverse conditions this candidate was prepared against.

    Read out of the candidate's own bound ``input_declaration`` rather than
    re-derived: ADR-0086 requires the receipt to record the declared coverage
    state, and re-deriving it at release time would let a receipt claim a
    condition the sealed artifacts never stated.
    """

    declaration = json.loads(candidate.input_declaration)
    derived = declaration.get("derived_state") or {}
    return tuple(derived.get("disclosed_exceptions") or ())


def receipt_payload(
    candidate: ReleaseCandidate,
    artifacts: tuple[SealedArtifact, ...],
    *,
    previous: ReleasePackage | None,
    sequence_number: int,
    authorized_by_principal: str,
    authorized_at: datetime,
) -> dict[str, Any]:
    """Everything ADR-0086 requires one package receipt to enumerate."""

    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "project_id": int(candidate.project_id),
        "candidate_id": int(candidate.id),
        "candidate_identity": candidate.candidate_identity,
        "content_sha256": candidate.content_sha256,
        "accepted_revision_id": int(candidate.accepted_revision_id),
        # ADR-0086's explicit none, spelled rather than omitted: a missing key
        # and a null predecessor must not digest the same.
        "previous_authorized_package": (
            None
            if previous is None
            else {
                "package_id": int(previous.id),
                "package_identity": previous.package_identity,
                "sequence_number": int(previous.sequence_number),
                "accepted_revision_id": int(previous.accepted_revision_id),
            }
        ),
        "sequence_number": sequence_number,
        "source_cutoff": candidate.source_cutoff.isoformat(),
        "coverage": {
            "identity": candidate.coverage_identity,
            "content_sha256": candidate.coverage_sha256,
            "exceptions": list(disclosed_exceptions(candidate)),
        },
        "issue_profile": {
            "profile_id": int(candidate.issue_profile_id),
            "identity": candidate.issue_profile_identity,
            "version": int(candidate.issue_profile_version),
            "content_sha256": candidate.issue_profile_sha256,
        },
        "output_template": {
            "format_id": int(candidate.output_template_format_id),
        },
        "field_mapping": {
            "format_id": int(candidate.field_mapping_format_id),
        },
        "artifacts": [one.as_payload() for one in artifacts],
        "authorized_by_principal": authorized_by_principal,
        "authorized_at": authorized_at.isoformat(),
    }


def receipt_declaration(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def receipt_identity(declaration: str) -> str:
    return sha256(declaration.encode("utf-8")).hexdigest()


# --- the readings a release history and a portfolio need --------------------


def current_authorized_package(
    session: Session, project_id: int
) -> ReleasePackage | None:
    """The package this project's next issue compares against, or ``None``.

    The chain head, found by the absence of a successor. This is #529's
    ``latest_authorized_package``; it is re-exported here because a caller
    reading release history should not have to import the preparation module
    to ask what the current issue is.
    """

    return latest_authorized_package(session, project_id)


def revision_has_been_issued(
    session: Session, *, project_id: int, revision_id: int
) -> bool:
    """Has *any* authorization ever referenced this accepted revision?

    #635's first question. It is answerable at all only because the receipt
    binds the revision by a real key; ``external_report_releases`` binds none,
    and a timestamp is not an answer.
    """

    return (
        session.scalar(
            select(ReleasePackage.id).where(
                ReleasePackage.project_id == project_id,
                ReleasePackage.accepted_revision_id == revision_id,
            )
        )
        is not None
    )


def current_issue_state_differences(
    session: Session, project_id: int, *, as_of: datetime
) -> tuple[str, ...]:
    """How the project's current issue state differs from what was issued.

    #635's second question, and the one #537's portfolio needs: an empty tuple
    means the authorized package still describes the project, and every entry
    names one thing that moved since. It is a different question from "has this
    revision been issued", and a project can answer yes to that and still have
    entries here.

    The code and product revision are deliberately *not* compared. A deploy is
    not a change to what the customer was issued, and treating one as a
    difference would report every project as out of date every Tuesday, which
    is a portfolio nobody reads.
    """

    return issue_state_differences(
        current_authorized_package(session, project_id),
        newest_revision_id=int(
            session.scalar(
                select(func.max(ProjectRecordRevision.id)).where(
                    ProjectRecordRevision.project_id == project_id
                )
            )
            or 0
        ),
        inventory=effective_issue_inventory(session, project_id, as_of),
        formats=effective_baseline_formats(session, project_id),
    )


def issue_state_differences(
    package: ReleasePackage | None,
    *,
    newest_revision_id: int,
    inventory: IssueInventory | None,
    formats: Mapping[str, Any],
) -> tuple[str, ...]:
    """The difference rule itself, over inputs the caller has already read.

    ``current_issue_state_differences`` above is this function plus the four
    reads it needs, and that is the only reason it is separate: a cross-project
    reading (#537, #636) already holds all four for every project it shows and
    must not go back to the database once per project to re-ask them. The rule
    lives here once; nobody derives it a second time.
    """

    if package is None:
        return ("this project has not authorized an issue yet",)

    differences: list[str] = []
    if newest_revision_id != int(package.accepted_revision_id):
        differences.append(
            f"the accepted record moved to revision {newest_revision_id} after "
            f"the last issue, which was made from revision "
            f"{package.accepted_revision_id}"
        )
    if inventory is None or (
        inventory.profile_id != int(package.issue_profile_id)
        or inventory.profile_version != int(package.issue_profile_version)
    ):
        differences.append(
            "what this project is configured to externally issue changed "
            "after the last issue"
        )
    template = formats.get("output_template")
    mapping = formats.get("field_mapping")
    if template is None or int(template.id) != int(
        package.output_template_format_id
    ):
        differences.append(
            "the output template this project renders through changed after "
            "the last issue"
        )
    if mapping is None or int(mapping.id) != int(package.field_mapping_format_id):
        differences.append(
            "the field mapping this project renders through changed after the "
            "last issue"
        )
    return tuple(differences)


def current_issue_state_is_issued(
    session: Session, project_id: int, *, as_of: datetime
) -> bool:
    """Whether the authorized package still describes the project right now."""

    return not current_issue_state_differences(session, project_id, as_of=as_of)


@dataclass(frozen=True, slots=True)
class ReleasedArtifact:
    """One released artifact as release history presents it.

    No storage key: where the bytes are kept is an internal detail, and
    ``retrieve_released_artifact`` resolves it from the receipt itself so a
    caller never has to hold one.
    """

    artifact_type: str
    renderer_identity: str
    renderer_version: str
    content_sha256: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class ReleaseHistoryEntry:
    """One authorized issue, in the words a coordinator already uses.

    ``issue_number`` is the package's position in the project's release chain,
    which is what a person means by "the third issue". The receipt's own row
    id, its digest identity and the candidate it sealed are internal receipt
    identifiers and are deliberately absent; ``with_identifiers`` on
    ``release_history`` adds them for support and reconciliation.
    """

    issue_number: int
    authorized_by_principal: str
    authorized_at: datetime
    accepted_revision_id: int
    previous_issue_number: int | None
    source_cutoff: datetime
    coverage_identity: str
    exceptions: tuple[str, ...]
    artifacts: tuple[ReleasedArtifact, ...]
    package_id: int | None = None
    package_identity: str | None = None
    candidate_id: int | None = None


def release_history(
    session: Session, project_id: int, *, with_identifiers: bool = False
) -> tuple[ReleaseHistoryEntry, ...]:
    """Every authorized issue for one project, oldest first.

    Ordered by the chain position, not by ``authorized_at``: the chain is what
    says which issue followed which, and a release recorded at an earlier
    declared instant than its predecessor is still its successor.
    """

    packages = list(
        session.scalars(
            select(ReleasePackage)
            .where(ReleasePackage.project_id == project_id)
            .order_by(ReleasePackage.sequence_number)
        ).all()
    )
    entries: list[ReleaseHistoryEntry] = []
    for package in packages:
        declaration = json.loads(package.receipt_declaration)
        coverage = declaration.get("coverage") or {}
        entries.append(
            ReleaseHistoryEntry(
                issue_number=int(package.sequence_number),
                authorized_by_principal=package.authorized_by_principal,
                authorized_at=package.authorized_at,
                accepted_revision_id=int(package.accepted_revision_id),
                previous_issue_number=(
                    None
                    if package.previous_sequence_number is None
                    else int(package.previous_sequence_number)
                ),
                source_cutoff=package.source_cutoff,
                coverage_identity=package.coverage_identity,
                exceptions=tuple(coverage.get("exceptions") or ()),
                artifacts=tuple(
                    ReleasedArtifact(
                        artifact_type=one.artifact_type,
                        renderer_identity=one.renderer_identity,
                        renderer_version=one.renderer_version,
                        content_sha256=one.content_sha256,
                        byte_count=one.byte_count,
                    )
                    for one in package_set(session, package)
                ),
                package_id=int(package.id) if with_identifiers else None,
                package_identity=(
                    package.package_identity if with_identifiers else None
                ),
                candidate_id=int(package.candidate_id) if with_identifiers else None,
            )
        )
    return tuple(entries)


def retrieve_released_artifact(
    session: Session,
    package: ReleasePackage,
    artifact_type: str,
    *,
    store: ObjectStore | None = None,
) -> bytes:
    """The exact bytes one released artifact was sealed with, verified.

    The store verifies the digest on the way out, so bytes that no longer hash
    to what the receipt recorded raise rather than being returned. Later
    Project Record changes cannot reach this: the object is content-addressed
    and the receipt is immutable.
    """

    backing = store if store is not None else content_store()
    for one in package_set(session, package):
        if one.artifact_type == artifact_type:
            return backing.get(one.storage_key, sha256=one.content_sha256)
    raise LookupError(
        f"this issue does not contain {artifact_type!r}; it contains "
        + ", ".join(one.artifact_type for one in package_set(session, package))
    )


# --- the act ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Authorization:
    """What one authorization produced, or found already there.

    Identifiers rather than an ORM row so a caller that commits and closes its
    session is not left holding a detached instance, exactly as #529's
    ``PreparationOutcome`` does.
    """

    project_id: int
    package_id: int
    package_identity: str
    issue_number: int
    candidate_id: int
    accepted_revision_id: int
    previous_package_id: int | None
    created: bool


def authorize_release_package(
    session: Session,
    *,
    project_id: int,
    candidate_id: int,
    releaser: HumanPrincipal,
    authorized_at: datetime,
    store: ObjectStore | None = None,
    binding: AnalyticsBinding | None = None,
    surface: str = "release_authorization",
) -> Authorization:
    """Authorize one prepared candidate, and write one immutable receipt.

    One transaction, and either the whole receipt or none of it: the command
    inserts the package and copies its artifact enumeration from the
    candidate's own rows in the same statement pair, so there is no state in
    which a release exists with a partial set.

    Revalidation happens before the command and again inside it. Before: the
    readiness #529 computed, the profile, revision and predecessor #529's
    ``authorization_blockers`` compares, the template and mapping
    registrations in force, and every retained artifact's bytes read back and
    verified against its recorded digest. Inside: the designation, the
    readiness, the newest revision and the chain head again, as the command's
    own owner, because an application gate that a second caller forgets is not
    the proof #531's designation asks for.

    Re-authorizing the same candidate returns the release that exists and
    writes nothing. It is not an error and it is not a second release: the
    unique key on ``candidate_id`` makes two divergent releases of one
    candidate unrepresentable.
    """

    actor = require_human_principal(releaser)
    if authorized_at.tzinfo is None:
        raise ValueError(
            "a release is recorded at a declared, time-zone-aware instant; "
            "nothing here reads a clock"
        )
    bound_binding = binding or default_binding()

    # Held outside the block so a refusal names the candidate it refused when
    # there was one, and says so honestly by omission when there was not.
    candidate: ReleaseCandidate | None = None
    try:
        candidate = session.scalars(
            select(ReleaseCandidate).where(
                ReleaseCandidate.id == candidate_id,
                ReleaseCandidate.project_id == project_id,
            )
        ).first()
        if candidate is None:
            raise AuthorizationRefused(
                NO_SUCH_CANDIDATE,
                f"candidate {candidate_id} is not a prepared candidate of "
                f"project {project_id}; authorization names a candidate that "
                "was prepared, never a set assembled at release time",
            )

        existing = session.scalars(
            select(ReleasePackage).where(
                ReleasePackage.candidate_id == candidate_id
            )
        ).first()
        if existing is not None:
            outcome = _authorization_of(existing, created=False)
            _emit(
                bound_binding,
                candidate=candidate,
                principal_subject=actor.subject,
                surface=surface,
                occurred_at=authorized_at,
                status="replayed",
                package_identity=existing.package_identity,
                issue_number=int(existing.sequence_number),
            )
            return outcome

        _revalidate(session, candidate, as_of=authorized_at)
        artifacts = candidate_set(session, candidate)
        _verify_retained_bytes(candidate, artifacts, store=store)

        previous = latest_authorized_package(session, project_id)
        sequence_number = 1 if previous is None else int(previous.sequence_number) + 1
        declaration = receipt_declaration(
            receipt_payload(
                candidate,
                artifacts,
                previous=previous,
                sequence_number=sequence_number,
                authorized_by_principal=actor.subject,
                authorized_at=authorized_at,
            )
        )
        result = _call_command(
            session,
            project_id=project_id,
            candidate_id=candidate_id,
            principal_subject=actor.subject,
            authorized_at=authorized_at,
            declaration=declaration,
        )
        session.expire_all()
        package = session.get_one(ReleasePackage, int(result["package_id"]))
    except AuthorizationRefused as refusal:
        _emit(
            bound_binding,
            candidate=candidate,
            principal_subject=actor.subject,
            surface=surface,
            occurred_at=authorized_at,
            status="refused",
            project_id=project_id,
            candidate_id=candidate_id,
            refusal_code=refusal.code,
        )
        raise

    _emit(
        bound_binding,
        candidate=candidate,
        principal_subject=actor.subject,
        surface=surface,
        occurred_at=authorized_at,
        status="authorized",
        package_identity=package.package_identity,
        issue_number=int(package.sequence_number),
    )
    return _authorization_of(package, created=bool(result["created"]))


def _authorization_of(package: ReleasePackage, *, created: bool) -> Authorization:
    return Authorization(
        project_id=int(package.project_id),
        package_id=int(package.id),
        package_identity=package.package_identity,
        issue_number=int(package.sequence_number),
        candidate_id=int(package.candidate_id),
        accepted_revision_id=int(package.accepted_revision_id),
        previous_package_id=(
            None
            if package.previous_package_id is None
            else int(package.previous_package_id)
        ),
        created=created,
    )


def _revalidate(
    session: Session, candidate: ReleaseCandidate, *, as_of: datetime
) -> None:
    """Refuse a candidate that no longer describes the project it was read from.

    Readiness is #529's, read through ``authorization_blockers`` rather than
    derived again here. What is added is the one thing preparation checks and
    that reading does not: the output-template and field-mapping registrations
    in force, which a coordinator can replace between preparation and release.
    """

    blockers = authorization_blockers(session, candidate, as_of=as_of)
    if blockers:
        code = (
            CANDIDATE_BLOCKED if candidate.readiness == BLOCKED else CANDIDATE_STALE
        )
        raise AuthorizationRefused(
            code,
            " ".join(blockers)
            + " Nothing is released; prepare a fresh candidate and review that.",
        )
    replaced = candidate_format_differences(
        candidate, effective_baseline_formats(session, int(candidate.project_id))
    )
    if replaced:
        raise AuthorizationRefused(TEMPLATE_OR_MAPPING_REPLACED, replaced[0])


def candidate_format_differences(
    candidate: ReleaseCandidate, formats: Mapping[str, Any]
) -> tuple[str, ...]:
    """Whether the template and mapping a candidate was rendered through still hold.

    The one thing ``authorization_blockers`` does not cover, stated once. A
    coordinator can replace either registration between preparation and
    release, and then the sealed artifacts are not the ones the project now
    produces. It is public because a cross-project reading (#537, #636) must
    ask the same question before it calls a candidate ready, and asking it a
    second way would let a portfolio row promise an authorization that #533
    would refuse.
    """

    replaced: list[str] = []
    for kind, format_id, what in (
        ("output_template", candidate.output_template_format_id, "output template"),
        ("field_mapping", candidate.field_mapping_format_id, "field mapping"),
    ):
        registered = formats.get(kind)
        if registered is None or int(registered.id) != int(format_id):
            replaced.append(
                f"the {what} this project renders through was replaced after "
                "this candidate was prepared, so the sealed artifacts would "
                "not be the ones this project now produces. Nothing is "
                "released; prepare a fresh candidate."
            )
    return tuple(replaced)


def _verify_retained_bytes(
    candidate: ReleaseCandidate,
    artifacts: tuple[SealedArtifact, ...],
    *,
    store: ObjectStore | None,
) -> None:
    """Read every retained artifact back and prove it is what was prepared.

    Revalidation, never regeneration: the bytes are the ones preparation
    retained, and the digest is the one the candidate recorded. A missing
    object and a changed object are different refusals because they lead a
    coordinator to different places.
    """

    declared = json.loads(candidate.content_declaration)
    stated = [
        (int(entry["position"]), entry["artifact_type"], entry["content_sha256"])
        for entry in declared.get("artifacts") or ()
    ]
    held = [
        (one.position, one.artifact_type, one.content_sha256) for one in artifacts
    ]
    if sorted(stated) != sorted(held):
        raise AuthorizationRefused(
            ARTIFACT_MISSING,
            "this candidate's stored rows do not enumerate the artifact set "
            "its content digest was taken over, so the set is incomplete. "
            "Nothing is released; prepare a fresh candidate.",
        )

    backing = store if store is not None else content_store()
    for one in artifacts:
        try:
            # `get` verifies the digest on the way out, so reading the object
            # at all is the proof. A second length comparison here would be a
            # check whose failure mode the store's own contract excludes.
            backing.get(one.storage_key, sha256=one.content_sha256)
        except StorageError as exc:
            raise AuthorizationRefused(
                _storage_refusal(exc),
                f"the exact bytes of {one.artifact_type} could not be read "
                f"back and verified: {exc}. Nothing is released.",
            ) from exc


def _storage_refusal(error: StorageError) -> str:
    return ARTIFACT_MISSING if isinstance(error, ObjectMissing) else DIGEST_MISMATCH


def _call_command(
    session: Session,
    *,
    project_id: int,
    candidate_id: int,
    principal_subject: str,
    authorized_at: datetime,
    declaration: str,
) -> dict[str, Any]:
    """Run the one writer, and translate its refusal into a bounded reason.

    The command runs inside a savepoint for #654's reason: it proves the
    designation by *raising*, which aborts the transaction the caller is in, so
    giving up only the savepoint leaves the surrounding session usable for a
    caller that means to render the refusal on the same page.
    """

    try:
        with session.begin_nested():
            return session.scalar(
                select(
                    func.authorize_release_package(
                        project_id,
                        candidate_id,
                        principal_subject,
                        authorized_at,
                        declaration,
                    )
                )
            )
    except DBAPIError as error:
        code = getattr(getattr(error, "orig", None), "sqlstate", None) or getattr(
            getattr(getattr(error, "orig", None), "diag", None), "sqlstate", None
        )
        if code == _INSUFFICIENT_PRIVILEGE:
            raise AuthorizationRefused(
                NOT_DESIGNATED,
                f"{principal_subject} holds no external-release designation "
                f"for project {project_id}. Authorizing an issue is a "
                "separate designation; project membership and project "
                "coordination confer none of it.",
            ) from error
        raise AuthorizationRefused(
            REVALIDATION_FAILED,
            "the database refused this authorization: "
            f"{_first_line(error)}. Nothing is released.",
        ) from error


def _first_line(error: DBAPIError) -> str:
    original = getattr(error, "orig", None)
    text = str(original if original is not None else error)
    return text.strip().splitlines()[0][:400]


# --- the emission (#558) ----------------------------------------------------


def _emit(
    binding: AnalyticsBinding,
    *,
    candidate: ReleaseCandidate | None,
    principal_subject: str,
    surface: str,
    occurred_at: datetime,
    status: str,
    package_identity: str | None = None,
    issue_number: int | None = None,
    refusal_code: str | None = None,
    project_id: int | None = None,
    candidate_id: int | None = None,
) -> None:
    """Record that one authorization, refusal or replay happened.

    The binding travels with it, so the code and product revision, the
    packetizer rule version, the source and connector configuration, the
    template and mapping identities and the enabled feature flags are bound to
    the measurement exactly as #558 requires.
    """

    emit_event(
        AnalyticsEvent(
            family=EventFamily.RELEASE_AUTHORIZATION,
            binding=binding,
            occurred_at=occurred_at,
            payload={
                "principal_subject": principal_subject,
                "project_id": (
                    project_id
                    if candidate is None
                    else int(candidate.project_id)
                ),
                "candidate_id": (
                    candidate_id if candidate is None else int(candidate.id)
                ),
                "candidate_identity": (
                    None if candidate is None else candidate.candidate_identity
                ),
                "accepted_revision_id": (
                    None
                    if candidate is None
                    else int(candidate.accepted_revision_id)
                ),
                "issue_profile_version": (
                    None
                    if candidate is None
                    else int(candidate.issue_profile_version)
                ),
                "source_cutoff": (
                    None if candidate is None else candidate.source_cutoff.isoformat()
                ),
                "package_identity": package_identity,
                "issue_number": issue_number,
                "refusal_code": refusal_code,
            },
            # Bounded shape only: the project and the person stay in the
            # payload, never in an infrastructure label (#491, #522).
            metric_labels={"surface": surface, "status": status},
        )
    )
