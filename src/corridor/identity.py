"""One conflict, one name — derived under the document's declared scheme.

Before ADR-0030 every reader guessed that `utility_id` alone named a
conflict: admission grouped by it, statements resolved against it, lanes
keyed siblings on it. The guess held for the TxDOT UCM form, whose
numbers are project-unique and whose retired rows exist precisely to
keep them stable — and failed on the first FDOT matrix, where each
External Party's list counts from 1 and nine different conflicts are all
correctly numbered 1.

The scheme is registry metadata a matrix declares at registration, and
this module is the one place identity is derived under it. The scheme is
never inferred from the data: a document that genuinely printed the same
row twice must keep abstaining as a duplicate, not silently become two
conflicts because repetition was read as a numbering style.

An identity is the row's parts, not a formatted string: the stated party
and the stated number. Nothing here bakes a separator into the record —
a Dependency keeps the document's number in `source_ref` and its party
as the External Party relation, and readers that need the pair build it
from those parts under the same scheme.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import (
    NUMBERING_SCHEMES,
    Candidate,
    Dependency,
    Document,
)

PROJECT_UNIQUE = "project-unique"
PER_PARTY = "per-party"


def row_identity(scheme: str, fields: dict) -> tuple[str, str] | None:
    """The name one row answers to, or None when it cannot be named.

    A tuple of (party, number) — party empty under a project-unique
    scheme, so identities from different schemes can never collide by
    accident. None means the fields do not carry what the scheme needs:
    the number always, and under per-party the stated party too.
    """
    if scheme not in NUMBERING_SCHEMES:
        raise ValueError(f"unknown numbering scheme {scheme!r}")
    uid = fields.get("utility_id")
    if not uid:
        return None
    if scheme == PER_PARTY:
        org = str(fields.get("external_org") or "").strip()
        if not org:
            return None
        return (org, str(uid))
    return ("", str(uid))


def document_numbering_schemes(
    session: Session, project_id: int
) -> dict[int, str]:
    """Every document's declared scheme, for keying rows by identity."""
    return dict(
        session.execute(
            select(Document.id, Document.numbering_scheme).where(
                Document.project_id == project_id
            )
        ).all()
    )


def candidate_identity(
    candidate: Candidate, schemes: Mapping[int, str]
) -> tuple[str, str] | None:
    """A Candidate's identity under its source document's declared scheme.

    A document absent from the mapping is read as project-unique — the
    default every document carries — so a caller can pass a mapping built
    from the documents it actually loaded.
    """
    fields = (candidate.payload_json or {}).get("fields", {})
    scheme = schemes.get(candidate.source_document_id, PROJECT_UNIQUE)
    return row_identity(scheme, fields)


def party_matches(session: Session, dependency: Dependency, org: str) -> bool:
    """Whether a stated party names this Dependency's External Party.

    Matching is by the party's recorded name and aliases, casefolded —
    the one sanctioned fuzziness, because one External Party is known by
    many names across documents. Anything looser is Adjudication's
    judgment, not a rule's.
    """
    if dependency.external_org_id is None:
        return False
    from corridor.models import ExternalOrg

    external = session.get(ExternalOrg, dependency.external_org_id)
    if external is None:
        return False
    names = [external.name, *(external.aliases or [])]
    return any(org.casefold() == str(n).casefold() for n in names if n)
