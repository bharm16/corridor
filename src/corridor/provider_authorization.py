"""The authorization check every outbound provider adapter runs before a call (ADR-0094).

ADR-0094's guard is provider-agnostic: a provider is called only after the
posture bound to its document by digest, the signed authorization record and
the request's own boundary agree on every field, and a refusal names every
gap with zero outbound requests. It was built twice. The Textract adapter
(`corridor_pdf_reader/textract_adapter/records.py` and `boundary.py`) proved it
first, and `native_provider_boundary.py` was written "in the shape" of that
adapter: the same three record kinds, the same `mismatches` with the same
field order and sentence grammar, the same refusal type with the same five
fields and `.record()`, the same calls/retries/failed-attempts counting and
the same cost-receipt counters. Two copies of a guard drift, and one already
had: Textract gated posture status for customer records only, the model
provider for every request, and nothing said whether that was meant.

This module holds the shared shape once. Each adapter extends the posture,
the two record kinds and the request boundary with its own fields (Textract:
region, retention and opt-out evidence, the operation and feature set; the
model provider: model, effort, store, base URL, image settings, source-digest
allowlists and three budgets), subclasses `AuthorizationCheck` to add its own
checks at the three seams the shared check leaves open, and constructs its
client only after `authorized` returns. Every refusal sentence, reason string
and receipt key is the one each adapter already emitted.

The posture's own approval is one rule, not two (ADR-0098, #808). A posture
declares two approval facts, each absent or stating what it permits: an
experimental approval and a customer-processing approval. A new live
transmission under an experiment scope needs the first and the recorded
scope; one under a customer authorization needs the second and the signed
record; a posture that is merely proposed, or that records no approval for
the requested use, refuses with zero outbound requests and names the missing
approval. The document's `status` word decides nothing here. Offline replay
of a retained response is not a transmission: the replay modules never enter
this check and never construct a client, and the tests hold them there.

What was tried first: a Protocol over duck-typed records, so each adapter
could keep its own dataclasses untouched. That kept the field lists apart
but not the rule, which is where the drift was; the rule needs one home.
Then (#805) the two adapters' readings of `status` were declared side by
side as `PostureStatusRule` constants, Textract gating customer records on
`accepted` and the model provider gating every request on `approved`; that
made the drift visible and left a live experiment ungated under a proposed
Textract posture, which #808 ended.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar, cast

EXPERIMENT_STAGE = "experiment"
CUSTOMER_STAGES: tuple[str, ...] = ("compatibility", "shadow", "authoritative")


@dataclass(frozen=True)
class TransmissionApproval:
    """One explicit approval, recorded in the posture document, for new live transmissions of one kind.

    It names exactly what it permits (the source classes and purposes; the
    region or model is the posture's own, and the approval belongs to that
    posture and no other) and what it leaves unverified, and who recorded it.
    An experimental approval can never authorize a customer stage: the check
    reads it only for an experiment scope, and the customer-processing
    approval only for a customer authorization.
    """

    source_classes: tuple[str, ...]
    purposes: tuple[str, ...]
    unverified: tuple[str, ...]
    approved_by: str
    approved_on: str


@dataclass(frozen=True)
class ProviderPosture:
    """What a provider is approved to do at all, once, bound to its document by digest.

    `digest` is the SHA-256 of the document's bytes, so confirming or changing
    anything the document records changes the digest, and with it every record
    that accepted the old one. `status` is the document's lifecycle word
    (proposed, accepted, approved); the two approval facts are what the check
    reads, each absent until the document records it (ADR-0098). Each adapter
    adds the fields its provider needs.
    """

    identity: str
    document: str
    digest: str
    provider: str
    permitted_purposes: tuple[str, ...]
    status: str
    experimental_approval: TransmissionApproval | None
    customer_processing_approval: TransmissionApproval | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _record_dict(record: Any) -> dict[str, Any]:
    """`kind` first, then every field, with each frozenset as a sorted list."""
    listed: dict[str, Any] = {"kind": record.kind}
    for field in fields(record):
        value = getattr(record, field.name)
        listed[field.name] = sorted(value) if isinstance(value, frozenset) else value
    return listed


@dataclass(frozen=True)
class CustomerAuthorization:
    """#522's signed, project-specific instance, reduced to the fields the check matches.

    The full instance names much more (credential custody, incident contact,
    audit access, retention of artifacts, termination); those govern people
    and operations, not this check.
    """

    record_id: str
    customer: str
    projects: frozenset[str]
    source_classes: frozenset[str]
    purposes: frozenset[str]
    stages: frozenset[str]
    posture_identity: str
    posture_digest: str
    signed_by: str
    signed_on: str

    kind = "customer-authorization"

    def as_dict(self) -> dict[str, Any]:
        return _record_dict(self)


@dataclass(frozen=True)
class ExperimentScope:
    """The record for public or synthetic experiment material.

    A request under it names the dataset as its project and `experiment` as
    its stage. It can never authorize a customer stage, and a customer
    authorization can never be read as an experiment scope, so no fictional
    customer agreement is ever written to cover reference material.
    """

    record_id: str
    dataset: str
    dataset_digest: str
    purpose: str
    scope: str
    source_classes: frozenset[str]
    posture_identity: str
    posture_digest: str
    recorded_by: str
    recorded_on: str

    kind = "experiment-scope"

    def as_dict(self) -> dict[str, Any]:
        return _record_dict(self)


AuthorizationRecord = CustomerAuthorization | ExperimentScope


@dataclass(frozen=True)
class RequestBoundary:
    """What one request claims about itself, matched against the record.

    `project` is the customer project identity, or the dataset identity when
    the record is an experiment scope. `stage` is one of the customer stages
    or `experiment`.
    """

    project: str
    source_class: str
    purpose: str
    stage: str
    posture_identity: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProviderRefused(RuntimeError):
    """A refusal, with its reason, every failing field, and what went out.

    `reason` is a short code (`authorization-absent`, `authorization-refused`,
    and the adapter's own); `outbound_requests` is how many requests left
    this process before the refusal, which is zero for an authorization
    refusal by construction: the check runs before a transport is ever handed
    a payload. Each adapter names its own `kind` for the record.
    """

    kind = "provider-refusal"

    def __init__(
        self,
        reason: str,
        *,
        detail: str = "",
        mismatches: tuple[str, ...] = (),
        outbound_requests: int = 0,
        request: RequestBoundary | None = None,
    ) -> None:
        super().__init__(f"{reason}: {detail or '; '.join(mismatches) or reason}")
        self.reason = reason
        self.detail = detail
        self.mismatches = tuple(mismatches)
        self.outbound_requests = outbound_requests
        self.request = request

    def recorded_at(self) -> str:
        """The record's clock; an adapter whose other records use another precision overrides it."""
        return datetime.now(timezone.utc).isoformat()

    def record(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "detail": self.detail,
            "mismatches": list(self.mismatches),
            "outbound_requests": self.outbound_requests,
            "request": None if self.request is None else self.request.as_dict(),
            "recorded_at": self.recorded_at(),
        }


class OutboundCounts:
    """Every outbound request is a call, a retry or a failed attempt; nothing else is one.

    A call is a request a response came back for, a retry is a transient
    failure the adapter will try again, a failed attempt is anything else.
    `outbound_requests` is their sum, so a receipt can separate what was
    charged from what was merely attempted, and a refusal can say exactly how
    many requests went out before it. A cache hit never counts.
    """

    def __init__(self) -> None:
        self.attempts = 0
        self.calls = 0
        self.retries = 0
        self.failed_attempts = 0

    @property
    def outbound_requests(self) -> int:
        return self.calls + self.retries + self.failed_attempts

    def counted(self) -> dict[str, int]:
        """The four receipt counters, in the order every receipt lists them."""
        return {
            "calls": self.calls,
            "retries": self.retries,
            "failed_attempts": self.failed_attempts,
            "outbound_requests": self.outbound_requests,
        }


P = TypeVar("P", bound=ProviderPosture)
R = TypeVar("R", bound=RequestBoundary)
C = TypeVar("C", bound=CustomerAuthorization)
E = TypeVar("E", bound=ExperimentScope)


class AuthorizationCheck(Generic[P, R, C, E]):
    """The shared check, extended by one subclass per provider adapter.

    A subclass declares `implementer` (how its refusals name the code that
    implements the posture) and `record_kinds` (its own record types, in the
    order its refusal names them), and overrides the three hooks with its own checks:
    `request_mismatches` runs after the request's posture identity is checked,
    `customer_mismatches` runs for a customer record after the posture is
    matched, `record_mismatches` runs for either record kind before the
    request is matched against it. Every failing field is reported, not the
    first, so one refusal names the whole gap instead of sending the
    maintainer back three times. An empty result is the only thing that lets
    an adapter construct a client.
    """

    implementer: str
    record_kinds: tuple[type[AuthorizationRecord], ...]

    def request_mismatches(self, request: R, posture: P) -> list[str]:
        return []

    def customer_mismatches(self, record: C, request: R, posture: P) -> list[str]:
        return []

    def record_mismatches(self, record: C | E, request: R, posture: P) -> list[str]:
        return []

    def mismatches(self, record: object | None, request: R, posture: P) -> tuple[str, ...]:
        """Every field on which the request is not covered; empty means covered.

        Each entry is `<field>: <what was asked>, <what the record or posture
        allows>`. The posture is checked first because a record naming another
        posture, or an old digest of this one, has accepted terms the adapter
        does not implement, whatever else it says. Which approval the posture
        must record is decided by the record's kind, so an absent record is
        refused as absent and the approval is read once a record is there.
        """
        found: list[str] = []
        if request.posture_identity != posture.identity:
            found.append(
                f"posture-identity: request names {request.posture_identity!r}, "
                f"{self.implementer} implements {posture.identity!r}"
            )
        found.extend(self.request_mismatches(request, posture))
        if request.purpose not in posture.permitted_purposes:
            found.append(
                f"purpose: {request.purpose!r} is not a purpose the posture permits "
                f"({', '.join(posture.permitted_purposes)})"
            )
        if record is None:
            named = " or ".join(kind.kind.replace("-", " ") for kind in self.record_kinds)
            found.append(f"authorization-absent: no {named} was given")
            return tuple(found)
        if not isinstance(record, self.record_kinds):
            found.append(f"record-kind: {type(record).__name__} is not an authorization record")
            return tuple(found)
        matched = cast("C | E", record)
        if matched.posture_identity != posture.identity:
            found.append(
                f"posture-identity: record {matched.record_id!r} accepts {matched.posture_identity!r}, "
                f"{self.implementer} implements {posture.identity!r}"
            )
        if matched.posture_digest != posture.digest:
            found.append(
                f"posture-digest: record {matched.record_id!r} accepts digest {matched.posture_digest[:12]!r}, "
                f"the posture document's digest is {posture.digest[:12]!r}"
            )
        found.extend(self.approval_mismatches(matched, request, posture))
        if isinstance(matched, CustomerAuthorization):
            found.extend(self.customer_mismatches(cast(C, matched), request, posture))
        found.extend(self.record_mismatches(matched, request, posture))
        if request.source_class not in matched.source_classes:
            found.append(
                f"source-class: {request.source_class!r} is not permitted by record {matched.record_id!r} "
                f"({', '.join(sorted(matched.source_classes))})"
            )
        if isinstance(matched, CustomerAuthorization):
            if request.project not in matched.projects:
                found.append(
                    f"project: {request.project!r} is not a project of record {matched.record_id!r} "
                    f"({', '.join(sorted(matched.projects))})"
                )
            if request.purpose not in matched.purposes:
                found.append(
                    f"purpose: {request.purpose!r} is not permitted by record {matched.record_id!r} "
                    f"({', '.join(sorted(matched.purposes))})"
                )
            # A customer record authorizes customer stages only: listing
            # `experiment` among its stages does not make it an experiment scope.
            if request.stage not in matched.stages or request.stage not in CUSTOMER_STAGES:
                found.append(
                    f"stage: {request.stage!r} is not authorized by record {matched.record_id!r} "
                    f"({', '.join(sorted(matched.stages))})"
                )
        else:
            if request.project != matched.dataset:
                found.append(
                    f"project: {request.project!r} is not the dataset of experiment scope "
                    f"{matched.record_id!r} ({matched.dataset!r})"
                )
            if request.purpose != matched.purpose:
                found.append(
                    f"purpose: {request.purpose!r} is not the purpose of experiment scope "
                    f"{matched.record_id!r} ({matched.purpose!r})"
                )
            if request.stage != EXPERIMENT_STAGE:
                found.append(
                    f"stage: {request.stage!r} cannot be authorized by an experiment scope, "
                    f"which covers {EXPERIMENT_STAGE!r} only"
                )
        return tuple(found)

    def approval_mismatches(self, record: C | E, request: R, posture: P) -> list[str]:
        """The one live-transmission rule (ADR-0098): the approval the record kind needs, and what it permits.

        A customer authorization needs the posture's customer-processing
        approval; an experiment scope needs its experimental approval. An
        absent approval, or one that does not permit the request's source
        class or purpose, is named; the document's `status` is quoted so the
        refusal says what the document currently is. A purpose the posture
        itself never permits is already named above and is not named twice.
        """
        if isinstance(record, CustomerAuthorization):
            approval, named, page = posture.customer_processing_approval, "customer-processing approval", "customer"
        else:
            approval, named, page = posture.experimental_approval, "experimental approval", "experiment"
        if approval is None:
            return [
                f"posture-approval: the posture is {posture.status!r} and records no {named}; "
                f"no {page} page may be transmitted until the maintainer records one"
            ]
        found: list[str] = []
        if request.source_class not in approval.source_classes:
            found.append(
                f"posture-approval: the {named} permits source classes "
                f"({', '.join(approval.source_classes)}), not {request.source_class!r}"
            )
        if request.purpose in posture.permitted_purposes and request.purpose not in approval.purposes:
            found.append(
                f"posture-approval: the {named} permits purposes "
                f"({', '.join(approval.purposes)}), not {request.purpose!r}"
            )
        return found

    def authorized(self, record: object | None, request: R, posture: P, *, refuse: type[ProviderRefused]) -> C | E:
        """The record, once it covers the request on every field; otherwise the adapter's refusal.

        The refusal is raised before anything touches the disk or a transport:
        no client, no scope directory, zero outbound requests.
        """
        found = self.mismatches(record, request, posture)
        if found:
            reason = "authorization-absent" if record is None else "authorization-refused"
            raise refuse(reason, mismatches=found, outbound_requests=0, request=request)
        assert isinstance(record, self.record_kinds)
        return cast("C | E", record)
