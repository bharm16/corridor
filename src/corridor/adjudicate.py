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
    ANSWER_SEPARATOR,
    RESOLUTION_STRATEGIES,
    Assertion,
    AuditLog,
    Candidate,
    Dependency,
    EvidenceLink,
    ExternalOrg,
    Project,
    is_placeholder_party,
)

# Fields the extractor emits that map onto Dependency columns directly.
# Everything else it found is still recorded as an assertion — the ledger
# has no column for `sue_level`, but a source did claim one, and dropping
# that is a loss of evidence rather than a simplification.
DIRECT_COLUMNS = {"station_from", "station_to"}


@dataclass(frozen=True)
class ResolutionVocabulary:
    """How one layout writes each of the canonical resolution strategies.

    The canonical *field* no longer varies: since #97 the extractor puts a
    layout's resolution column under `resolution_strategy` whatever it is
    printed as. Only the **values** differ, because every agency writes
    its alternatives in its own prose — `To be removed` on SR 789, an `X`
    under `509 Relocation Needed` on WSDOT.
    """

    phrases: dict[str, str]

    def read(self, value: str) -> str | None:
        """The canonical strategy, or None when this layout does not say.

        A phrase the vocabulary does not carry reads as None rather than
        being guessed at. The column being present is not the document
        making a claim — SR 789 prints nine distinct phrasings and five
        map; the rest are genuinely other answers, not synonyms nobody has
        got round to listing.

        Whitespace is collapsed before lookup because internal runs of it
        are a transcription artifact of the printed cell, not something the
        document said.

        A value may carry more than one answer, because a layout recording
        its strategy as marked columns can mark more than one (#105). They
        read as a strategy only when they agree on it. Two answers from
        opposite sides of ADR-0009's line are a document that has not
        settled — the same situation SR 789 spells as `To be adjusted or
        relocated`, which this table already declines — and one answer
        beside a phrase nobody has identified is no better, because the
        unread half may be the disagreeing one.
        """
        answers = {
            self.phrases.get(" ".join(part.split()).casefold())
            for part in (value or "").split(ANSWER_SEPARATOR.strip())
            if part.strip()
        }
        return answers.pop() if len(answers) == 1 else None


# WSDOT's Appendix U, which records its strategy as four columns marked `X`
# beneath a spanning `RECOMMENDED RESOLUTION` header rather than as a value
# (#105). The extractor stores the heading a row is marked under, so the
# phrases below are those headings and this table reads them exactly as it
# reads FDOT's prose — which is what makes one canonical value come out of
# two layouts that spell the answer differently.
#
# Every reading is ADR-0009's published table rather than an inference:
#
#   `509 Relocation Needed`   — the facility moves. FDOT Red, and the
#   `ST Relocation Needed`      wording SHRP2 R15B publishes. Which of the
#                               two projects pays for it is a funding fact,
#                               not a resolution strategy, so both read the
#                               same way.
#   `Retain and Protect`      — the facility stays. FDOT Green, and R15B's
#                               `Protect in-place`.
#   `Abandon / Deactivate`    — FDOT Red. Deactivation is scheduled
#                               utility-owner work, not a facility that
#                               stays, and ADR-0009 puts it on the critical
#                               side deliberately — an earlier draft of
#                               that paragraph did not.
#
# Written from 9424, which is the unsealed twin fetched for exactly this
# purpose (ADR-0008), and never from the holdout.
WSDOT_APPENDIX_U = ResolutionVocabulary(
    phrases={
        "509 relocation needed": "relocate",
        "st relocation needed": "relocate",
        "retain and protect": "protect_in_place",
        "abandon / deactivate": "abandon_in_place",
    }
)


# Each layout's resolution vocabulary, identified in writing before that
# layout is adjudicated (ADR-0009). Keyed by project rather than applied
# corpus-wide, because prose that means one thing on one form means another
# elsewhere and a canonical field name is not a safe carrier for meaning —
# the lesson #85 paid for when SH 99 filled `potential_conflict` from
# `Early TxDOT Utility Activity`.
#
# A project absent here asserts **no strategy at all**, which is the right
# answer rather than a gap:
#
# - `nhhip-3c2` and `sh99-grand-parkway` print no resolution column. The
#   first is a Utility Inventory (its filename says so) and the second is
#   TxDOT's template without the `Resolution Strategy Selected` field. They
#   record that conflicts exist and never how they resolve.
RESOLUTION_VOCABULARIES: dict[str, ResolutionVocabulary] = {
    # FDOT SR 789's `Recommended Conflict Resolution`, all nine printed
    # phrasings across its 66 rows. Four map; five are declined, and the
    # declines are the point rather than an omission:
    #
    #   "To be adjusted or relocated"   — the document offers two answers
    #                                     on opposite sides of the line
    #   "To be monitored and adjusted"  — conditional; nothing is committed
    #   "…as needed"                      to yet
    #   "To be replaced with 24\"X36\"    — a replacement that stays in the
    #    handhole and adjusted…"          same horizontal alignment reads
    #                                     as both Red and Brown
    #   "To be adjusted"                — ADR-0009's Brown is specifically
    #                                     "adjusted **vertically** but to
    #                                     remain in the same horizontal
    #                                     alignment". A bare "adjusted"
    #                                     does not say which, and this form
    #                                     prints the specific sibling too
    #                                     ("…to proposed grade", 18 rows),
    #                                     which is evidence they are not
    #                                     the same claim.
    #
    # Each is a strategy the document genuinely has not settled, so the
    # Ledger records none. A reviewer can override; nobody may invent.
    "fdot-sr789": ResolutionVocabulary(
        phrases={
            "to be removed": "remove",
            'to be removed. new 2" hdpe conduit to be installed': "remove",
            "to be relocated": "relocate",
            "to be adjusted to proposed grade": "adjust_vertical",
        }
    ),
    # The vocabulary belongs to the layout and the key belongs to the
    # project, so a second contract printing this same Appendix U registers
    # its slug here against the reading already settled above. That is a
    # one-line registration rather than a vocabulary written at measuring
    # time, which is the thing ADR-0008 says cannot be discovered
    # afterwards.
    "wsdot-9424": WSDOT_APPENDIX_U,
}


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
    strategy = _asserted_strategy(session, candidate, fields)

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
        # None when the document asserted no strategy this layout's
        # vocabulary recognises (ADR-0009).
        resolution_strategy=strategy,
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

    # No second Assertion for the strategy, deliberately. `criticality` was
    # never a field the extractor emitted, so it needed one written by
    # hand; `resolution_strategy` *is* one, and the loop above has already
    # recorded what the document printed. Adding a canonical-token
    # Assertion beside it would give one `field_name` two `asserted_value`s
    # behind the same EvidenceLink — `To be removed` and `remove` — which
    # is precisely how a CONTRADICTION is computed, so every mapped SR 789
    # row would contradict itself. 52 of 66 on the shipped vocabulary.
    #
    # The division is ADR-0001's: the Assertion preserves what the source
    # said, the column holds the conclusion drawn from it.

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


def _asserted_strategy(
    session: Session, candidate: Candidate, fields: dict
) -> str | None:
    """What this row's document says is to be done about the conflict.

    None on every path that is not a document making the claim: a layout
    whose vocabulary nobody has identified, a revision that does not print
    a resolution column, a blank cell, a phrase the vocabulary does not
    carry. All four are "the document did not say", and the enum cannot
    hold that — which is why the column is nullable and nothing defaults
    it.
    """
    project = session.get(Project, candidate.project_id)
    vocabulary = RESOLUTION_VOCABULARIES.get(project.slug if project else None)
    if vocabulary is None:
        return None
    return vocabulary.read(fields.get("resolution_strategy") or "")


def set_resolution_strategy(
    session: Session,
    dependency: Dependency,
    strategy: str | None,
    *,
    actor: str,
) -> Dependency:
    """A reviewer's judgement, which outranks the document's claim.

    The Assertions are untouched: the document still says what it said, and
    the Dependency's value is the adjudicated conclusion (ADR-0001). The
    audit log names who overrode it and what it held before, which is what
    makes an override recoverable.

    Note the two are no longer directly comparable the way they were under
    `criticality`, where both sides held the same token. The Assertion now
    holds the document's prose (`To be removed`) and the column holds the
    canonical conclusion (`remove`), so "does the stored value disagree
    with its Assertion" is a question about this project's vocabulary
    rather than a string comparison. The audit trail is the reliable
    record of an override, not the diff.

    Overriding is legitimate here in a way it is not for readiness: the
    matrix may say `Retain and Protect` about a duct bank that sits under
    the only haul road. ADR-0009 turns on that difference.
    """
    if strategy is not None and strategy not in RESOLUTION_STRATEGIES:
        raise ValueError(
            f"{strategy!r} is not a resolution strategy; expected one of "
            f"{', '.join(RESOLUTION_STRATEGIES)} or None for 'no judgement'"
        )

    before = dependency.resolution_strategy
    dependency.resolution_strategy = strategy
    session.add(
        AuditLog(
            actor=actor,
            action="set_resolution_strategy",
            entity_type="dependency",
            entity_id=dependency.id,
            before_json={"resolution_strategy": before},
            after_json={"resolution_strategy": strategy},
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
    # A placeholder is the document declining to name a party, not a party
    # called `NA` (#77). Minting one puts 86 of Project A's rows behind an
    # owner nobody can chase and inflates every report that groups by
    # party. The Assertion still records what the document printed.
    if not name or is_placeholder_party(name):
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
