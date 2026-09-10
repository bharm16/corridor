"""The records the outbound boundary matches, and the matching rule (#732, ADR-0094).

Three record kinds, kept apart on purpose. The provider posture says what the
provider is approved to do at all, once, and is bound to its document by
digest. A customer authorization is #522's signed, project-specific instance
accepting that posture for named projects, source classes, purposes, stages
and a region. An experiment scope is the record for public or synthetic data:
it names the dataset, the purpose and the scope, and authorizes the
experiment stage and nothing else, so no fictional customer agreement is ever
written to cover reference material.

The shape of those records and the rule that matches them are
`corridor.provider_authorization`'s, shared with the native model-provider
boundary; this module adds what is Textract's. The posture carries the
operation, feature set, region and the three pieces of operations evidence;
every record and request carries the region; and `TextractAuthorizationCheck`
adds the region checks and the evidence checks to the shared `mismatches`.
That shared rule compares a request against a record on every field and
returns every field that fails, not the first one, so a refusal names the
whole gap. An empty result is the only thing that lets the boundary construct
a client. Earlier drafts checked the first failing field and returned; a
refusal that said "project" when region and stage were also wrong would have
sent the maintainer back three times.

The posture records no approval yet (ADR-0098): it is `proposed`, and its
document states neither an experimental approval nor a customer-processing
approval, so every new live Textract transmission is refused, experiments
included, until the maintainer records the approval in a new revision.
Offline replay of retained responses (`replay.py`) never enters this check.
"""

from __future__ import annotations

from dataclasses import dataclass

from corridor import provider_authorization

PROVIDER = "aws-textract"
OPERATION = "AnalyzeDocument"
FEATURE_TYPES: tuple[str, ...] = ("TABLES",)
NATIVE_GEOMETRY_PURPOSE = "native-table-geometry-assistance"


@dataclass(frozen=True)
class ProviderPosture(provider_authorization.ProviderPosture):
    """The reusable provider posture, as `docs/operations/textract-provider-posture.md` records it.

    `digest` is the SHA-256 of that document's bytes; the test that checks it
    is what makes "the adapter binds to the posture" a true sentence. The
    three `unverified` fields are the maintainer's to confirm, and confirming
    them changes the document, the digest, and therefore this record. So does
    recording either approval: an experimental approval names the public and
    synthetic source classes and purposes it permits and the fields it leaves
    unverified; a customer-processing approval comes with the verified fields.
    """

    operation: str
    feature_types: tuple[str, ...]
    region: str
    retention: str
    ai_services_opt_out: str
    permissions: str


PROVIDER_POSTURE = ProviderPosture(
    identity="aws-textract-analyze-document-tables-posture-2",
    document="docs/operations/textract-provider-posture.md",
    digest="1dbe087d66f756f56f559c0233cc0fa1fd4fce8dbf9544709139d7c1c1b22e71",
    provider=PROVIDER,
    operation=OPERATION,
    feature_types=FEATURE_TYPES,
    region="us-east-2",
    permitted_purposes=("scanned-page-reading", "image-region-reading", NATIVE_GEOMETRY_PURPOSE, "extraction-measurement"),
    retention="unverified",
    ai_services_opt_out="unverified",
    permissions="unverified",
    status="proposed",
    experimental_approval=None,
    customer_processing_approval=None,
)


@dataclass(frozen=True)
class CustomerAuthorization(provider_authorization.CustomerAuthorization):
    """#522's signed instance, with the region the adapter matches."""

    region: str


@dataclass(frozen=True)
class ExperimentScope(provider_authorization.ExperimentScope):
    """The recorded scope for public or synthetic experiment data, with its region."""

    region: str


AuthorizationRecord = CustomerAuthorization | ExperimentScope


@dataclass(frozen=True)
class RequestBoundary(provider_authorization.RequestBoundary):
    """What one request claims about itself, with the region it names."""

    region: str


class TextractAuthorizationCheck(
    provider_authorization.AuthorizationCheck[ProviderPosture, RequestBoundary, CustomerAuthorization, ExperimentScope]
):
    """The shared check plus Textract's region and operations-evidence checks."""

    implementer = "the adapter"
    record_kinds = (CustomerAuthorization, ExperimentScope)

    def request_mismatches(self, request: RequestBoundary, posture: ProviderPosture) -> list[str]:
        if request.region != posture.region:
            return [f"region: request names {request.region!r}, the posture is for {posture.region!r}"]
        return []

    def customer_mismatches(self, record: CustomerAuthorization, request: RequestBoundary, posture: ProviderPosture) -> list[str]:
        # Status alone cannot turn unknown operations evidence into verified
        # facts. The maintainer records evidence for the actual calling account
        # and workload role in the exact posture document before asserting these
        # states; the boundary does not query AWS or infer them from a signature.
        found: list[str] = []
        for field, state, required in (
            ("retention", posture.retention, "verified"),
            ("ai-services-opt-out", posture.ai_services_opt_out, "optOut"),
            ("permissions", posture.permissions, "verified"),
        ):
            if state != required:
                found.append(
                    f"posture-{field}: the posture records {state!r}; customer processing "
                    f"requires {required!r} with evidence for the actual calling account and workload role"
                )
        return found

    def record_mismatches(self, record: AuthorizationRecord, request: RequestBoundary, posture: ProviderPosture) -> list[str]:
        if record.region != request.region:
            return [f"region: request names {request.region!r}, record {record.record_id!r} covers {record.region!r}"]
        return []


AUTHORIZATION_CHECK = TextractAuthorizationCheck()


def mismatches(
    record: object | None,
    request: RequestBoundary,
    posture: ProviderPosture = PROVIDER_POSTURE,
) -> tuple[str, ...]:
    """Every field on which the request is not covered; empty means covered."""
    return AUTHORIZATION_CHECK.mismatches(record, request, posture)
