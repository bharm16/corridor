"""Adjudication: the only path from a Candidate into the Ledger.

Accepting does not copy a payload into a row. It records **assertions** —
this document, on this page, claimed this value for this field — and the
Dependency's own field values become the adjudicated conclusion drawn from
them (ADR-0001). That distinction is what lets the ledger show competing
sources instead of silently keeping whichever was written last.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.models import (
    Assertion,
    AuditLog,
    Candidate,
    Dependency,
    EvidenceLink,
    ExternalOrg,
)

# Fields the extractor emits that map onto Dependency columns directly.
# Everything else it found is still recorded as an assertion — the ledger
# has no column for `sue_level`, but a source did claim one, and dropping
# that is a loss of evidence rather than a simplification.
DIRECT_COLUMNS = {"station_from", "station_to"}


class AlreadyAdjudicated(Exception):
    pass


def accept_candidate(
    session: Session, candidate: Candidate, *, actor: str
) -> Dependency:
    if candidate.state != "pending":
        raise AlreadyAdjudicated(
            f"candidate {candidate.id} is already {candidate.state}"
        )

    fields = candidate.payload_json.get("fields", {})
    citations = candidate.payload_json.get("citations", [])

    org = _resolve_org(session, fields.get("external_org"))

    dependency = Dependency(
        project_id=candidate.project_id,
        ref_code=_next_ref_code(session, candidate.project_id),
        source_ref=fields.get("utility_id"),
        # A utility conflict matrix row is a potential relocation. The
        # candidate's `utility_type` is the kind of utility (Telecom, Gas),
        # not the kind of dependency.
        dep_type="utility_relocation",
        title=_title(fields),
        location_desc=_location(fields),
        station_from=fields.get("station_from"),
        station_to=fields.get("station_to"),
        external_org_id=org.id if org else None,
        status="identified",
        criticality="normal",
    )
    session.add(dependency)
    session.flush()

    links = [
        _evidence_link(session, dependency, citation) for citation in citations
    ]
    primary = links[0] if links else None

    for name, value in fields.items():
        session.add(
            Assertion(
                dependency_id=dependency.id,
                field_name=name,
                asserted_value=value,
                evidence_link_id=primary.id if primary else None,
                doc_date=None,
            )
        )

    candidate.state = "accepted"
    candidate.adjudicated_at = datetime.now(timezone.utc)
    candidate.citations_verified = all(c.get("verified") for c in citations)

    session.add(
        AuditLog(
            actor=actor,
            action="accept_candidate",
            entity_type="dependency",
            entity_id=dependency.id,
            before_json=None,
            after_json={
                "candidate_id": candidate.id,
                "ref_code": dependency.ref_code,
                "source_ref": dependency.source_ref,
                "fields": fields,
            },
        )
    )
    session.flush()
    return dependency


def _next_ref_code(session: Session, project_id: int) -> str:
    """A ledger identifier we control.

    Source identifiers are not unique — NHHIP's current matrix has two
    distinct Comcast conflicts both labelled FOC14-69 — so keying the
    ledger on them would either collide or silently merge two records.
    """
    used = (
        session.scalar(
            select(func.count())
            .select_from(Dependency)
            .where(Dependency.project_id == project_id)
        )
        or 0
    )
    return f"DEP-{used + 1:05d}"


def _evidence_link(session: Session, dependency: Dependency, citation: dict):
    link = EvidenceLink(
        dependency_id=dependency.id,
        document_id=citation["document_id"],
        page_no=citation["page"],
        quote=citation["quote"],
        verified=bool(citation.get("verified")),
        # Never set on acceptance. Readiness is a separate, deliberate
        # judgment that this evidence meets the dependency's bar (ADR-0002).
        satisfies_requirement=False,
    )
    session.add(link)
    session.flush()
    return link


def merge_candidate(
    session: Session,
    candidate: Candidate,
    dependency: Dependency,
    *,
    actor: str,
) -> Dependency:
    """Fold a candidate into an existing Dependency.

    Merging **adds assertions**; it never overwrites the target's field
    values (ADR-0001). The ledger row is an adjudicated conclusion, and a
    second source claiming a different date does not silently replace the
    first — it becomes a competing assertion, which is what makes
    CONTRADICTION computable and what stops the tool doing the very thing it
    exists to prevent.
    """
    if candidate.state != "pending":
        raise AlreadyAdjudicated(
            f"candidate {candidate.id} is already {candidate.state}"
        )

    fields = candidate.payload_json.get("fields", {})
    citations = candidate.payload_json.get("citations", [])

    links = [_evidence_link(session, dependency, citation) for citation in citations]
    primary = links[0] if links else None

    for name, value in fields.items():
        session.add(
            Assertion(
                dependency_id=dependency.id,
                field_name=name,
                asserted_value=value,
                evidence_link_id=primary.id if primary else None,
                doc_date=None,
            )
        )

    candidate.state = "merged"
    candidate.merged_into = dependency.id
    candidate.adjudicated_at = datetime.now(timezone.utc)

    session.add(
        AuditLog(
            actor=actor,
            action="merge_candidate",
            entity_type="dependency",
            entity_id=dependency.id,
            before_json=None,
            after_json={
                "candidate_id": candidate.id,
                "merged_into": dependency.ref_code,
                "fields": fields,
            },
        )
    )
    session.flush()
    return dependency


def _resolve_org(session: Session, name: str | None) -> ExternalOrg | None:
    if not name:
        return None
    # Exact-name matching only in v0. Alias resolution — collapsing "AT&T"
    # and "AT&T Texas (SWBT)" into one party — is M3's job, and doing it
    # badly here would silently merge distinct owners.
    org = session.scalars(
        select(ExternalOrg).where(ExternalOrg.name == name)
    ).first()
    if org is None:
        org = ExternalOrg(name=name, org_type="utility", aliases=[])
        session.add(org)
        session.flush()
    return org


def _title(fields: dict) -> str:
    utility_type = fields.get("utility_type") or "Utility"
    org = fields.get("external_org")
    return f"{utility_type} — {org}" if org else utility_type


def _location(fields: dict) -> str | None:
    parts = [
        fields.get("alignment"),
        fields.get("location_start"),
        fields.get("location_end"),
    ]
    joined = " / ".join(p for p in parts if p)
    return joined or None
