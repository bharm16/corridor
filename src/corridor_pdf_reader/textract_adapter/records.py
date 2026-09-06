"""The records the outbound boundary matches, and the matching rule (#732, ADR-0094).

Three record kinds, kept apart on purpose. The provider posture says what the
provider is approved to do at all, once, and is bound to its document by
digest. A customer authorization is #522's signed, project-specific instance
accepting that posture for named projects, source classes, purposes, stages
and a region. An experiment scope is the record for public or synthetic data:
it names the dataset, the purpose and the scope, and authorizes the
experiment stage and nothing else, so no fictional customer agreement is ever
written to cover reference material.

`mismatches` compares a request against a record on every field and returns
every field that fails, not the first one, so a refusal names the whole gap.
An empty result is the only thing that lets the boundary construct a client.
Earlier drafts checked the first failing field and returned; a refusal that
said "project" when region and stage were also wrong would have sent the
maintainer back three times.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

PROVIDER = "aws-textract"
OPERATION = "AnalyzeDocument"
FEATURE_TYPES: tuple[str, ...] = ("TABLES",)

CUSTOMER_STAGES: tuple[str, ...] = ("compatibility", "shadow", "authoritative")
EXPERIMENT_STAGE = "experiment"


@dataclass(frozen=True)
class ProviderPosture:
    """The reusable provider posture, as `docs/operations/textract-provider-posture.md` records it.

    `digest` is the SHA-256 of that document's bytes; the test that checks it
    is what makes "the adapter binds to the posture" a true sentence. The
    three `unverified` fields are the maintainer's to confirm, and confirming
    them changes the document, the digest, and therefore this record.
    """

    identity: str
    document: str
    digest: str
    provider: str
    operation: str
    feature_types: tuple[str, ...]
    region: str
    permitted_purposes: tuple[str, ...]
    retention: str
    ai_services_opt_out: str
    permissions: str
    status: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


PROVIDER_POSTURE = ProviderPosture(
    identity="aws-textract-analyze-document-tables-posture-1",
    document="docs/operations/textract-provider-posture.md",
    digest="1a222e3c92be1777921f1201bc2c993389cc237c924e9f8e9b557a1537322b3f",
    provider=PROVIDER,
    operation=OPERATION,
    feature_types=FEATURE_TYPES,
    region="us-east-2",
    permitted_purposes=("scanned-page-reading", "image-region-reading", "extraction-measurement"),
    retention="unverified",
    ai_services_opt_out="unverified",
    permissions="unverified",
    status="proposed",
)


@dataclass(frozen=True)
class CustomerAuthorization:
    """#522's signed instance, reduced to the fields the boundary matches.

    The full instance names much more (credential custody, incident contact,
    audit access, retention of artifacts, termination); those govern people
    and operations, not this check. What the adapter needs is who signed for
    which projects, source classes, purposes and stages, in which region,
    accepting which posture.
    """

    record_id: str
    customer: str
    projects: frozenset[str]
    source_classes: frozenset[str]
    purposes: frozenset[str]
    stages: frozenset[str]
    region: str
    posture_identity: str
    posture_digest: str
    signed_by: str
    signed_on: str

    kind = "customer-authorization"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "record_id": self.record_id,
            "customer": self.customer,
            "projects": sorted(self.projects),
            "source_classes": sorted(self.source_classes),
            "purposes": sorted(self.purposes),
            "stages": sorted(self.stages),
            "region": self.region,
            "posture_identity": self.posture_identity,
            "posture_digest": self.posture_digest,
            "signed_by": self.signed_by,
            "signed_on": self.signed_on,
        }


@dataclass(frozen=True)
class ExperimentScope:
    """The recorded scope for public or synthetic experiment data.

    A request under this record names the dataset as its project and
    `experiment` as its stage. It can never authorize a customer stage, and
    a customer authorization can never be read as an experiment scope.
    """

    record_id: str
    dataset: str
    dataset_digest: str
    purpose: str
    scope: str
    source_classes: frozenset[str]
    region: str
    posture_identity: str
    posture_digest: str
    recorded_by: str
    recorded_on: str

    kind = "experiment-scope"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "record_id": self.record_id,
            "dataset": self.dataset,
            "dataset_digest": self.dataset_digest,
            "purpose": self.purpose,
            "scope": self.scope,
            "source_classes": sorted(self.source_classes),
            "region": self.region,
            "posture_identity": self.posture_identity,
            "posture_digest": self.posture_digest,
            "recorded_by": self.recorded_by,
            "recorded_on": self.recorded_on,
        }


AuthorizationRecord = CustomerAuthorization | ExperimentScope


@dataclass(frozen=True)
class RequestBoundary:
    """What one request claims about itself, matched against the record.

    `project` is the customer project identity, or the dataset identity when
    the record is an experiment scope. `stage` is one of the three customer
    stages or `experiment`.
    """

    project: str
    source_class: str
    purpose: str
    region: str
    posture_identity: str
    stage: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def mismatches(
    record: object | None,
    request: RequestBoundary,
    posture: ProviderPosture = PROVIDER_POSTURE,
) -> tuple[str, ...]:
    """Every field on which the request is not covered; empty means covered.

    Each entry is `<field>: <what was asked>, <what the record or posture
    allows>`. The posture is checked first because a record naming another
    posture, or an old digest of this one, has accepted terms the adapter
    does not implement, whatever else it says.
    """

    found: list[str] = []
    if request.posture_identity != posture.identity:
        found.append(
            f"posture-identity: request names {request.posture_identity!r}, "
            f"the adapter implements {posture.identity!r}"
        )
    if request.region != posture.region:
        found.append(f"region: request names {request.region!r}, the posture is for {posture.region!r}")
    if request.purpose not in posture.permitted_purposes:
        found.append(
            f"purpose: {request.purpose!r} is not a purpose the posture permits "
            f"({', '.join(posture.permitted_purposes)})"
        )
    if record is None:
        found.append("authorization-absent: no customer authorization or experiment scope was given")
        return tuple(found)
    if not isinstance(record, (CustomerAuthorization, ExperimentScope)):
        found.append(f"record-kind: {type(record).__name__} is not an authorization record")
        return tuple(found)
    if record.posture_identity != posture.identity:
        found.append(
            f"posture-identity: record {record.record_id!r} accepts {record.posture_identity!r}, "
            f"the adapter implements {posture.identity!r}"
        )
    if record.posture_digest != posture.digest:
        found.append(
            f"posture-digest: record {record.record_id!r} accepts digest {record.posture_digest[:12]!r}, "
            f"the posture document's digest is {posture.digest[:12]!r}"
        )
    if isinstance(record, CustomerAuthorization) and posture.status != "accepted":
        found.append(
            f"posture-status: the posture is {posture.status!r}; no customer page may be "
            "transmitted until the maintainer accepts it"
        )
    if record.region != request.region:
        found.append(f"region: request names {request.region!r}, record {record.record_id!r} covers {record.region!r}")
    if request.source_class not in record.source_classes:
        found.append(
            f"source-class: {request.source_class!r} is not permitted by record {record.record_id!r} "
            f"({', '.join(sorted(record.source_classes))})"
        )
    if isinstance(record, CustomerAuthorization):
        if request.project not in record.projects:
            found.append(
                f"project: {request.project!r} is not a project of record {record.record_id!r} "
                f"({', '.join(sorted(record.projects))})"
            )
        if request.purpose not in record.purposes:
            found.append(
                f"purpose: {request.purpose!r} is not permitted by record {record.record_id!r} "
                f"({', '.join(sorted(record.purposes))})"
            )
        if request.stage not in record.stages:
            found.append(
                f"stage: {request.stage!r} is not authorized by record {record.record_id!r} "
                f"({', '.join(sorted(record.stages))})"
            )
    else:
        if request.project != record.dataset:
            found.append(
                f"project: {request.project!r} is not the dataset of experiment scope "
                f"{record.record_id!r} ({record.dataset!r})"
            )
        if request.purpose != record.purpose:
            found.append(
                f"purpose: {request.purpose!r} is not the purpose of experiment scope "
                f"{record.record_id!r} ({record.purpose!r})"
            )
        if request.stage != EXPERIMENT_STAGE:
            found.append(
                f"stage: {request.stage!r} cannot be authorized by an experiment scope, "
                f"which covers {EXPERIMENT_STAGE!r} only"
            )
    return tuple(found)
