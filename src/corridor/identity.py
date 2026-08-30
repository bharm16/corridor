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
    Project,
    is_placeholder_party,
)

PROJECT_UNIQUE = "project-unique"
PER_PARTY = "per-party"


def party_canonical_names(session: Session) -> dict[str, str]:
    """Every registered spelling of a party, mapped to one canonical one.

    An External Party is known by many names across documents — that is
    what its aliases are for — and under a per-party scheme the party is
    half of a conflict's name. Keying on the stated spelling made one
    company's two registered names two different conflicts with the same
    number, each invisible to the other's `already_admitted` check, so
    one conflict entered the record twice with the disagreement between
    them split across both rows instead of raised as a Dispute.

    Resolution is exact against a recorded name or alias, casefolded and
    whitespace-collapsed. Nothing here guesses: a spelling nobody has
    registered stays its own party, which is the safe direction — it
    surfaces as a row to look at rather than silently merging two
    companies.
    """
    from corridor.models import ExternalOrg

    # A collision is not made deterministic by identifier order.  The whole
    # point of ADR-0051 is that two registry parties surviving means a person
    # must decide; omitting the spelling here lets the admission boundary
    # retain that visible residue instead of silently choosing the oldest row.
    candidates: dict[str, set[str]] = {}
    for org in session.scalars(select(ExternalOrg).order_by(ExternalOrg.id)):
        target = normalize_party(org.name)
        for spelling in (org.name, *(org.aliases or [])):
            if spelling:
                candidates.setdefault(normalize_party(spelling), set()).add(target)
    return {
        spelling: next(iter(targets))
        for spelling, targets in candidates.items()
        if len(targets) == 1
    }


def canonical_party(
    name: str, aliases: Mapping[str, str] | None = None
) -> str:
    """One party's name, resolved through its registered spellings.

    The single place a stated party becomes a comparable value, so the
    identity key and the party-agreement check can never read one
    company's two names differently again.
    """
    stated = normalize_party(name)
    return (aliases or {}).get(stated, stated)


def row_identity(
    scheme: str, fields: dict, aliases: Mapping[str, str] | None = None
) -> tuple[str, str] | None:
    """The name one row answers to, or None when it cannot be named.

    A tuple of (party, number) — party empty under a project-unique
    scheme, so identities from different schemes can never collide by
    accident. None means the fields do not carry what the scheme needs:
    the number always, and under per-party a party that actually names
    somebody.

    The party half is normalized before it becomes a key. Two revisions
    spelling one company `AT&T Texas` and `AT&T TEXAS` are stating the
    same conflict, and keying on the raw string made them two records
    with the same number — the exact duplication a declared scheme
    exists to prevent. Normalizing here and nowhere else keeps the key
    and the party-agreement check reading the same string.
    """
    if scheme not in NUMBERING_SCHEMES:
        raise ValueError(f"unknown numbering scheme {scheme!r}")
    uid = fields.get("utility_id")
    if not uid:
        return None
    if scheme == PER_PARTY:
        org = fields.get("external_org")
        # A document declining to name an owner names nothing, so it
        # cannot be half of a name. `N/A` on two different rows is not
        # one party twice, and treating it as one would merge unrelated
        # conflicts — or, because no External Party ever resolves from
        # it, admit the same row twice.
        if not str(org or "").strip() or is_placeholder_party(org):
            return None
        return (canonical_party(org, aliases), str(uid))
    return ("", str(uid))


def normalize_party(name: str) -> str:
    """One spelling of a party name, for use as a key.

    Legal suffixes, punctuation, whitespace, and case are the trivial
    spelling variants ADR-0051 permits a rule to collapse.  Nothing more
    semantic happens here: similarity remains human residue.
    """
    from corridor.organization_identity import normalize_organization

    return normalize_organization(name)


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
    candidate: Candidate,
    schemes: Mapping[int, str],
    aliases: Mapping[str, str] | None = None,
) -> tuple[str, str] | None:
    """A Candidate's identity under its source document's declared scheme.

    A document absent from the mapping is read as project-unique — the
    default every document carries — so a caller can pass a mapping built
    from the documents it actually loaded. `aliases` collapses a party's
    registered spellings; omitting it keys on the stated spelling, which
    is right for a caller comparing rows inside one document.
    """
    fields = (candidate.payload_json or {}).get("fields", {})
    scheme = schemes.get(candidate.source_document_id, PROJECT_UNIQUE)
    return row_identity(scheme, fields, aliases)


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
    wanted = normalize_party(org)
    return any(normalize_party(n) == wanted for n in names if n)


def is_project_side_party(project: Project, party: str) -> bool:
    """Whether a stated party is declared to be the project's own side.

    The configuration is an explicit boundary, not a resemblance check: an
    internal action item cannot become an External Party commitment because a
    human happened to type it into a phone-call form.
    """
    return any(
        party.casefold() == str(own_party).casefold()
        for own_party in (project.project_side_parties or [])
    )
