"""Adjudication: the only path from a Candidate into the Ledger.

Accepting does not copy a payload into a row. It records **assertions** —
this document, on this page, claimed this value for this field — and the
Dependency's own field values become the adjudicated conclusion drawn from
them (ADR-0001). That distinction is what lets the ledger show competing
sources instead of silently keeping whichever was written last.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.models import (
    CRITICALITIES,
    Assertion,
    AuditLog,
    Candidate,
    Dependency,
    EvidenceLink,
    ExternalOrg,
    Project,
)

# Fields the extractor emits that map onto Dependency columns directly.
# Everything else it found is still recorded as an assertion — the ledger
# has no column for `sue_level`, but a source did claim one, and dropping
# that is a loss of evidence rather than a simplification.
DIRECT_COLUMNS = {"station_from", "station_to"}


@dataclass(frozen=True)
class CriticalitySignal:
    """How one layout says a facility must move before construction."""

    field: str
    critical: frozenset[str]
    not_critical: frozenset[str]

    def read(self, value: str) -> str | None:
        """`critical`, `normal`, or None when the layout does not say.

        A value the signal does not recognise reads as None rather than as
        `normal`. The column being present is not the document making a
        claim, and guessing at an unfamiliar value is the heuristic
        ADR-0007 rules out.
        """
        cleaned = (value or "").strip().casefold()
        if cleaned in self.critical:
            return "critical"
        if cleaned in self.not_critical:
            return "normal"
        return None


# Deliberately empty. **Nothing in this corpus asserts a criticality**, and
# ADR-0009 explains why: no document records one. SHRP2 R15B and TxDOT's own
# published template both record a *resolution strategy* — relocate, protect
# in place, change the highway design, except from policy — and criticality
# is a reading of that, not a field of its own.
#
# `nhhip-3c2` was listed here and has been removed. Its `Potential Conflict
# (Yes, No, Abandoned)` column says a conflict **exists**; it never says how
# the conflict resolves. Project A's document is a Utility Inventory, not a
# Utility Conflict Matrix — its filename says so — and reading `Y` as
# critical marked 71% of its rows on a claim the document does not make.
#
# The two layouts that do record a strategy cannot be read through this
# table's shape, which matches one field against a value set:
#
# - `fdot-sr789` — `Recommended Conflict Resolution`, now captured under the
#   canonical `resolution_strategy` field on all 66 rows (#97).
# - WSDOT 9424 / 9540 — four columns marked `X` beneath a spanning
#   `RECOMMENDED RESOLUTION` header, which is a different shape entirely.
#
# Replacing this with an asserted `resolution_strategy` is the schema work
# ADR-0009 sets out. Until then nothing is asserted, which is correct: the
# alternative is a number that measures our guess.
CRITICALITY_SIGNALS: dict[str, CriticalitySignal] = {}


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
    criticality = _asserted_criticality(session, candidate, fields)

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
        # None when no signal was read. Not `normal` — see ADR-0007.
        criticality=criticality,
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

    # Criticality is an ordinary adjudicated field, so it earns an
    # Assertion citing the same Evidence as every other one — the document
    # said this, on this page. Absent when the document did not say, which
    # is what makes "unlabelled" countable rather than inferred from a
    # Dependency that merely looks unremarkable.
    if criticality is not None:
        session.add(
            Assertion(
                dependency_id=dependency.id,
                field_name="criticality",
                asserted_value=criticality,
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


def _asserted_criticality(
    session: Session, candidate: Candidate, fields: dict
) -> str | None:
    """What this row's document says about how much it matters.

    None on every path that is not a document making the claim: a layout
    whose signal nobody has identified, a revision that does not print the
    column, a blank cell, an unrecognised value. All four are "the document
    did not say", and the enum cannot hold that — which is exactly why the
    column is nullable and nothing defaults it.
    """
    project = session.get(Project, candidate.project_id)
    signal = CRITICALITY_SIGNALS.get(project.slug if project else None)
    if signal is None:
        return None
    return signal.read(fields.get(signal.field) or "")


def set_criticality(
    session: Session,
    dependency: Dependency,
    criticality: str | None,
    *,
    actor: str,
) -> Dependency:
    """A reviewer's judgement, which outranks the document's claim.

    The Assertions are untouched: the document still says what it said, and
    the Dependency's value is the adjudicated conclusion (ADR-0001). That
    is what keeps the two distinguishable — an override is visible as a
    stored value that disagrees with the criticality Assertion beneath it,
    and the audit log names who did it.

    Overriding is legitimate here in a way it is not for readiness: the
    matrix may say `N` about a duct bank that sits under the only haul
    road. ADR-0007 turns on that difference.
    """
    if criticality is not None and criticality not in CRITICALITIES:
        raise ValueError(
            f"{criticality!r} is not a criticality; expected one of "
            f"{', '.join(CRITICALITIES)} or None for 'no judgement'"
        )

    before = dependency.criticality
    dependency.criticality = criticality
    session.add(
        AuditLog(
            actor=actor,
            action="set_criticality",
            entity_type="dependency",
            entity_id=dependency.id,
            before_json={"criticality": before},
            after_json={"criticality": criticality},
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
