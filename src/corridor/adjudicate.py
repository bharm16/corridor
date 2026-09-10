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
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.candidates import citations_verified
from corridor.models import (
    ANSWER_SEPARATOR,
    RESOLUTION_STRATEGIES,
    Assertion,
    Candidate,
    Dependency,
    DependencyDismissal,
    Document,
    DocPage,
    EvidenceLink,
    ExtractedProposal,
    ExternalOrg,
    LegacyLedgerArchive,
    OperativeSupport,
    Project,
    is_claim,
    is_placeholder_party,
)
from corridor.verify import (
    normalize,
    quote_appears_on,
    threshold_for,
    unverified_fields,
)


class OrganizationIdentityUnresolved(ValueError):
    """A named source party has no exact registered identity yet (ADR-0051)."""


class ImmutableExtractedProposal(ValueError):
    """New spine-backed proposals are corrected by appending Facts."""
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.operative_support import designate_publication_support
from corridor.measurement_cases import (
    record_candidate_correction_case,
    record_do_not_add_case,
)
from corridor.disputes import apply_staleness_resolutions
from corridor.project_lock import lock_project
from corridor.supersession_review import ordinary_candidate_for_update

REJECT_REASONS = ("duplicate", "wrong", "irrelevant", "bad-citation")
_REF_CODE = re.compile(r"DEP-(\d{5})$")

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
# Contract 9540 prints the same form with different column names, and the
# M7 cold run found it: every one of its 192 rows read as None here,
# because the phrases below were written from 9424 alone and 9424 names
# its columns after its own route number. The structural machinery
# generalised (the marked group, the multi-mark rows, the headings stored
# instead of the marks); the vocabulary did not. Extended after the cold
# run, which is why the gate's numbers do not depend on it — eval scores
# row-finding against the gold labels and never reads this table.
#
# Each addition is the same ADR-0009 line the entries above follow:
# relocation and abandonment commit the owner to work, protection in
# place does not.
WSDOT_APPENDIX_U = ResolutionVocabulary(
    phrases={
        # Contract 9424 (SR 509 Completion Stage 1B).
        "509 relocation needed": "relocate",
        "st relocation needed": "relocate",
        "retain and protect": "protect_in_place",
        "abandon / deactivate": "abandon_in_place",
        # Contract 9540 (SR 167 Completion Stage 1b). `Advance` names when
        # the work happens, not what it is — ADR-0009 is explicit that
        # criticality is about the kind of work and not about a date.
        "relocation": "relocate",
        "advance relocation": "relocate",
        "protection in place": "protect_in_place",
        "abandon/ deactivate": "abandon_in_place",
        "abandon/ deactivate/ remove": "abandon_in_place",
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
    "wsdot-9540": WSDOT_APPENDIX_U,
}


class AlreadyAdjudicated(Exception):
    pass


class InvalidRejectReason(Exception):
    pass


class InvalidCandidateProvenance(Exception):
    """This Candidate's source material does not belong to its project."""


class UnadjudicableKind(Exception):
    """This Candidate is not a kind acceptance knows how to resolve."""


class CandidateAssertsNothing(Exception):
    """This Candidate carries no claim for a Ledger row to be made of."""


class InvalidCandidateScope(Exception):
    """This Candidate is not actionable in the selected declared scope."""


class MalformedCandidateShape(Exception):
    """This Candidate's payload does not fit its source document's shape.

    New-record acceptance materializes typed columns from the payload, and
    a shape it does not recognise refuses rather than guessing — a wrong
    guess mints a Ledger row asserting things no document said (#170).
    """


def require_candidate_action_scope(
    session: Session,
    candidate: Candidate,
    *,
    historical_document_id: int | None,
) -> None:
    """Serialize and re-check scope at the authoritative mutation boundary.

    Null-run Candidates are preserved as history but never actionable. Every
    mutation must resolve through the declared Active Run, with an exact
    historical override when its document is superseded.
    """

    lock_project(session, candidate.project_id)
    session.refresh(candidate)
    if candidate.state != "pending":
        raise AlreadyAdjudicated(
            f"candidate {candidate.id} is already {candidate.state}"
        )

    document = session.get(
        Document,
        candidate.source_document_id,
        populate_existing=True,
    )
    if document is None or document.project_id != candidate.project_id:
        raise InvalidCandidateProvenance(
            "candidate source document is outside its project"
        )

    if candidate.extraction_run_id is None:
        raise InvalidCandidateScope(
            "a Candidate without declared run lineage is not actionable"
        )

    scoped = ordinary_candidate_for_update(
        session,
        candidate.project_id,
        candidate.id,
        historical_document_id=historical_document_id,
    )
    if scoped is None:
        raise InvalidCandidateScope(
            "candidate is outside the declared Active Run and Supersession scope"
        )


def accept_candidate(
    session: Session,
    candidate: Candidate,
    *,
    principal: HumanPrincipal,
    historical_document_id: int | None = None,
) -> Dependency:
    principal = require_human_principal(principal)
    require_candidate_action_scope(
        session,
        candidate,
        historical_document_id=historical_document_id,
    )
    if candidate.kind != "dependency":
        # `make minutes` writes `kind="event"` Candidates into the same
        # table, and the queue serves them. Accepting one used to build a
        # Dependency from fields it does not have — an event carries no
        # `utility_id` and no `utility_type`, so it minted a
        # `utility_relocation` titled "Utility" that no document asserts.
        # Adjudication is the only path into the Ledger; a path that
        # fabricates the record is worse than no path at all.
        raise UnadjudicableKind(
            f"candidate {candidate.id} is a {candidate.kind}, and acceptance "
            "builds a Dependency — an event must be attached to one instead"
        )
    payload, fields, citations = _validate_candidate_provenance(session, candidate)
    if not any(is_claim(value) for value in fields.values()):
        # The mirror of the citation rule, and the reachable half of it: a
        # reviewer who clears every field box and presses accept sends an
        # edit whose `fields` is `{}` — the route keeps only `field_*`
        # inputs that still hold a value — and the Assertion loop below
        # then does not run. What committed was a Dependency with a real
        # verified EvidenceLink and no claims: evidence for nothing, and
        # `_title` naming it "Utility" because there was no utility_type
        # to name it by.
        #
        # `is_claim` rather than truthiness, because it is already this
        # system's answer to "does this value say anything a source could
        # disagree with", and a row whose every field is blank asserts
        # exactly as much as a row with no fields at all.
        raise CandidateAssertsNothing(
            f"candidate {candidate.id} asserts nothing, and a Ledger row is "
            "what a document claims — an edit that empties every field is a "
            "rejection, not an acceptance"
        )

    # Refuse an unsupported source shape before asking for identity work.  A
    # malformed agreement or plan is not an identity question, and surfacing
    # identity first would mask the real safe refusal.
    _materialize(session, candidate, fields, None)
    org = _resolve_org(session, candidate, fields.get("external_org"))
    dependency = _materialize(session, candidate, fields, org)
    session.add(dependency)
    session.flush()

    links = [_evidence_link(session, dependency, citation) for citation in citations]
    # Never empty: provenance refuses a Candidate that cites nothing, above
    # and before any of this. `if links else None` said an Assertion may
    # exist without evidence, which `models.Assertion.evidence_link_id`,
    # its migration and ADR-0001 all deny — so the disagreement was settled
    # by Postgres, as a NOT NULL violation on a half-written acceptance,
    # rather than by this module as a refusal it could name.
    primary = links[0]
    designate_publication_support(
        session, dependency.id, primary.id, principal=principal
    )

    for name, value in fields.items():
        session.add(
            Assertion(
                dependency_id=dependency.id,
                field_name=name,
                asserted_value=value,
                evidence_link_id=primary.id,
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
    # The extractor's rule, not a second one. This read only the per-citation
    # quote flag and dropped `unverified_fields` and `low_confidence_tokens`
    # entirely, so a row that sank in the queue *because a field value was
    # not on its page* came out of acceptance recorded as verified — and the
    # queue's own ordering, `pending_counts` and `eval` all read this column.
    candidate.citations_verified = citations_verified(payload)

    audit.record(
        session,
        principal=principal,
        action=audit.ACCEPT_CANDIDATE,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        after={
            "candidate_id": candidate.id,
            "ref_code": dependency.ref_code,
            "source_ref": dependency.source_ref,
            "fields": fields,
        },
    )
    session.flush()
    apply_staleness_resolutions(session, dependency.id)
    return dependency


def admit_dependency_by_policy(
    session: Session,
    primary: Candidate,
    siblings: list[Candidate],
    *,
    machine_actor: str,
) -> Dependency:
    """Admit one Dependency under an exact deterministic policy.

    Eligibility is the caller's proven premise under ADR-0029 and ADR-0042.
    This shared materialization path records the primary Candidate and any
    corroborating siblings under the system actor, while the Policy Run and
    per-Candidate outcomes preserve the exact authority for the write.
    """
    dependency: Dependency | None = None
    for candidate in (primary, *siblings):
        require_candidate_action_scope(session, candidate, historical_document_id=None)
        payload, fields, citations = _validate_candidate_provenance(session, candidate)
        if candidate is primary:
            if not any(is_claim(value) for value in fields.values()):
                raise CandidateAssertsNothing(
                    f"candidate {candidate.id} asserts nothing, and a Ledger "
                    "row is what a document claims"
                )
            _materialize(session, candidate, fields, None)
            org = _resolve_org(session, candidate, fields.get("external_org"))
            dependency = _materialize(session, candidate, fields, org)
            session.add(dependency)
            session.flush()

        links = [
            _evidence_link(session, dependency, citation) for citation in citations
        ]
        primary_link = links[0]
        if candidate is primary:
            session.add(
                OperativeSupport(
                    dependency_id=dependency.id,
                    evidence_link_id=primary_link.id,
                    role="publication",
                    designated_by=machine_actor,
                )
            )
        for name, value in fields.items():
            session.add(
                Assertion(
                    dependency_id=dependency.id,
                    field_name=name,
                    asserted_value=value,
                    evidence_link_id=primary_link.id,
                    doc_date=None,
                )
            )

        if candidate is primary:
            candidate.state = "accepted"
        else:
            candidate.state = "merged"
            candidate.merged_into = dependency.id
        candidate.adjudicated_at = datetime.now(timezone.utc)
        candidate.citations_verified = citations_verified(payload)

        audit.record(
            session,
            actor=machine_actor,
            action=audit.ADMIT_DEPENDENCY,
            entity_type=audit.DEPENDENCY,
            entity_id=dependency.id,
            after={
                "candidate_id": candidate.id,
                "ref_code": dependency.ref_code,
                "source_ref": dependency.source_ref,
                "role": "admitted" if candidate is primary else "sibling_merged",
                "fields": fields,
            },
        )
    session.flush()
    assert dependency is not None
    apply_staleness_resolutions(session, dependency.id)
    return dependency


def edit_candidate(
    session: Session,
    candidate: Candidate,
    fields: dict[str, str],
    *,
    principal: HumanPrincipal,
    historical_document_id: int | None = None,
) -> Candidate:
    principal = require_human_principal(principal)
    if session.scalar(
        select(ExtractedProposal.id).where(ExtractedProposal.candidate_id == candidate.id)
    ) is not None:
        raise ImmutableExtractedProposal(
            "spine-backed Extracted Proposals cannot be edited; append a Fact correction"
        )
    require_candidate_action_scope(
        session,
        candidate,
        historical_document_id=historical_document_id,
    )

    payload = dict(candidate.payload_json or {})
    original = dict(payload.get("fields") or {})
    if fields != original:
        updated = _edited_payload(session, payload, fields)
        audit_entry = audit.record(
            session,
            principal=principal,
            action=audit.EDIT_CANDIDATE,
            entity_type=audit.CANDIDATE,
            entity_id=candidate.id,
            before={"fields": original},
            after={"fields": fields},
        )
        candidate.payload_json = updated
        candidate.citations_verified = citations_verified(updated)
        record_candidate_correction_case(
            session,
            candidate,
            fields=fields,
            audit_entry=audit_entry,
        )
        session.flush()
    return candidate


def reject_candidate(
    session: Session,
    candidate: Candidate,
    reason: str,
    *,
    principal: HumanPrincipal,
    historical_document_id: int | None = None,
) -> Candidate:
    principal = require_human_principal(principal)
    if reason not in REJECT_REASONS:
        raise InvalidRejectReason(
            f"{reason!r} is not a reject reason; expected one of {REJECT_REASONS}"
        )
    require_candidate_action_scope(
        session,
        candidate,
        historical_document_id=historical_document_id,
    )

    candidate.state = "rejected"
    candidate.adjudicated_at = datetime.now(timezone.utc)
    audit_entry = audit.record(
        session,
        principal=principal,
        action=audit.REJECT_CANDIDATE,
        entity_type=audit.CANDIDATE,
        entity_id=candidate.id,
        after={"reason": reason},
    )
    record_do_not_add_case(
        session,
        candidate,
        reason=reason,
        ruling_type="audit_log",
        ruling_id=audit_entry.id,
        recorded_by=principal.subject,
    )
    session.flush()
    return candidate


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
    audit.record(
        session,
        actor=actor,
        action=audit.SET_RESOLUTION_STRATEGY,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        before={"resolution_strategy": before},
        after={"resolution_strategy": strategy},
    )
    session.flush()
    return dependency


def _edited_payload(session: Session, payload: dict, fields: dict[str, str]) -> dict:
    updated = {**payload, "fields": dict(fields)}
    page_text = _cited_page_text(session, payload)
    # The quote is re-checked against the page it names, not taken on the
    # reviewer's word. Without this a row flagged only on its quote could
    # never be cleared by any gesture: the values re-verified and the
    # citation's own flag stayed as extraction left it, so `edit & accept`
    # was a way to sink a row and not a way to fix one (#212). Re-checking
    # can lower a flag as well as lift one, which is the point.
    updated["citations"] = [
        {
            **citation,
            "verified": _quote_still_holds(session, citation, page_text),
        }
        for citation in (payload.get("citations") or [])
    ]

    if not _transcribes_cells(payload):
        return updated

    if page_text is None:
        updated["unverified_fields"] = sorted(fields)
    else:
        updated["unverified_fields"] = sorted(unverified_fields(fields, page_text))
    updated["low_confidence_tokens"] = _reconcile_low_confidence_tokens(payload, fields)
    return updated


def _quote_still_holds(session: Session, citation: dict, page_text: str | None) -> bool:
    """Whether this citation's quote is on the page it names, now.

    The threshold comes from where the page's text came from, exactly as
    it does at extraction: a spreadsheet's cells are read losslessly, so
    a near-miss there is a real disagreement rather than print damage and
    nothing is spent on tolerance. Hardcoding the printed-page threshold
    let an edit lift a flag the cells rule had correctly set.

    Fails closed on an unreadable page and on an absent quote: a citation
    with nothing to check is not a citation that checks out.
    """
    quote = citation.get("quote")
    if page_text is None or not quote:
        return False
    return quote_appears_on(
        quote, page_text, threshold_for(_citation_text_source(session, citation))
    )


def _citation_text_source(session: Session, citation: dict) -> str | None:
    """How the cited page's text was obtained, or None when unknowable.

    None takes the printed-page tolerance, which is the safe direction
    for a page whose provenance cannot be read: it never *raises* the bar
    a caller expected, and a stricter page still fails its own check when
    the source is known.
    """
    document_id = citation.get("document_id")
    page_no = citation.get("page")
    if document_id is None or page_no is None:
        return None
    return session.scalar(
        select(DocPage.text_source).where(
            DocPage.document_id == document_id,
            DocPage.page_no == page_no,
        )
    )


def _transcribes_cells(payload: dict) -> bool:
    """Does this proposal claim its values are printed where it cites?

    `whole_row` was standing in for this question and is not it. The page
    reader that #766 retired recorded `whole_row=False` whenever the
    assembled row was not contiguous on the page — two tables printed side
    by side, or a leading `Data Source` column that landed elsewhere in
    reading order — and those retained rows are still transcribed cells whose
    `unverified_fields` means exactly what it means everywhere else. 79
    live rows are in that state, and an edit to one kept the extractor's
    reading of the values the reviewer had just replaced: a value typed in
    that is nowhere on the page came out `citations_verified`, which is the
    defect `citations_verified` was unified to abolish.

    `tier` is the direct answer, and all three matrix readers set it. The
    prose extractors leave it None, where a field is the model's phrasing
    of a paragraph and was never expected to appear verbatim — the
    distinction `candidates` keeps deliberately. The whole-row fallback
    holds the 141 `txdot_ucm_v1` rows that predate `tier` on the side they
    are already on.
    """
    if payload.get("tier"):
        return True
    return any(
        citation.get("whole_row") for citation in (payload.get("citations") or [])
    )


def _cited_page_text(session: Session, payload: dict) -> str | None:
    """The text of every page this proposal cites, or None if one is missing.

    Every citation, not only the whole-row ones: the page a value must
    appear on is the page the proposal names, and whether the quote
    happened to span the whole row says nothing about which page that is.

    None fails closed — the caller marks every field unverified rather
    than verifying against a page it could not read.
    """
    texts = []
    for citation in payload.get("citations") or []:
        document_id = citation.get("document_id")
        page_no = citation.get("page")
        if document_id is None or page_no is None:
            return None
        page = session.scalars(
            select(DocPage).where(
                DocPage.document_id == document_id,
                DocPage.page_no == page_no,
            )
        ).first()
        if page is None or not page.text:
            return None
        texts.append(page.text)
    return "\n".join(texts) if texts else None


def _reconcile_low_confidence_tokens(
    payload: dict, fields: dict[str, str]
) -> list[str]:
    values = [normalize(value) for value in fields.values() if value]
    kept = []
    for token in payload.get("low_confidence_tokens") or []:
        needle = normalize(token)
        if needle and any(needle in value for value in values):
            kept.append(token)
    return kept


def _next_ref_code(session: Session, project_id: int) -> str:
    """A ledger identifier we control.

    Source identifiers are not unique — NHHIP's current matrix has two
    distinct Comcast conflicts both labelled FOC14-69 — so keying the
    ledger on them would either collide or silently merge two records.
    """
    lock_project(session, project_id)

    used = [
        int(match.group(1))
        for ref_code in session.scalars(
            select(Dependency.ref_code).where(Dependency.project_id == project_id)
        )
        if (match := _REF_CODE.fullmatch(ref_code or ""))
    ]
    archived_high_watermark = session.scalar(
        select(func.max(LegacyLedgerArchive.ref_code_high_watermark)).where(
            LegacyLedgerArchive.project_id == project_id
        )
    )
    if archived_high_watermark is not None:
        used.append(archived_high_watermark)
    return f"DEP-{max(used, default=0) + 1:05d}"


def _evidence_link(session: Session, dependency: Dependency, citation: dict):
    link = EvidenceLink(
        dependency_id=dependency.id,
        document_id=citation["document_id"],
        page_no=citation["page"],
        quote=citation["quote"],
        verified=citation.get("verified", False),
    )
    session.add(link)
    session.flush()
    return link


def merge_candidate(
    session: Session,
    candidate: Candidate,
    dependency: Dependency,
    *,
    principal: HumanPrincipal,
    historical_document_id: int | None = None,
) -> Dependency:
    """Fold a candidate into an existing Dependency.

    Merging **adds assertions**; it never overwrites the target's field
    values (ADR-0001). The ledger row is an adjudicated conclusion, and a
    second source claiming a different date does not silently replace the
    first — it becomes a competing assertion, which is what makes
    CONTRADICTION computable and what stops the tool doing the very thing it
    exists to prevent.
    """
    principal = require_human_principal(principal)
    if dependency.dismissed_at is not None:
        # The offer list already excludes dismissed targets; the mutation
        # must too, or a stale form files a claim where neither the list
        # nor the exception engine will ever look again (ADR-0032).
        raise AlreadyDismissed(
            f"{dependency.ref_code} was dismissed — a claim cannot merge "
            "into a record nobody is working"
        )
    require_candidate_action_scope(
        session,
        candidate,
        historical_document_id=historical_document_id,
    )
    _, fields, citations = _validate_candidate_provenance(
        session, candidate, dependency=dependency
    )

    links = [_evidence_link(session, dependency, citation) for citation in citations]
    # Never empty, for the same reason and by the same guard as acceptance.
    # Merge is the quieter of the two paths and was the worse of the two
    # outcomes: a citation-less merge added no link and no Assertion, then
    # marked the Candidate `merged`, pointed `merged_into` at a Dependency
    # it had contributed nothing to, and wrote an audit entry saying so.
    # Nothing entered the Ledger and the source row left the queue for
    # good, because merging refuses a Candidate that is no longer pending.
    primary = links[0]
    designate_publication_support(
        session, dependency.id, primary.id, principal=principal
    )

    # No "asserts nothing" rule here, unlike acceptance, and the asymmetry
    # is the point: acceptance builds the record, so a Candidate with no
    # claim leaves nothing for the record to be made of. A merge attaches
    # to a record that already has its claims, and a Candidate carrying
    # only a citation is a second document saying the same conflict exists
    # — corroboration, which is worth keeping.
    for name, value in fields.items():
        session.add(
            Assertion(
                dependency_id=dependency.id,
                field_name=name,
                asserted_value=value,
                evidence_link_id=primary.id,
                doc_date=None,
            )
        )

    candidate.state = "merged"
    candidate.merged_into = dependency.id
    candidate.adjudicated_at = datetime.now(timezone.utc)

    audit.record(
        session,
        principal=principal,
        action=audit.MERGE_CANDIDATE,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        after={
            "candidate_id": candidate.id,
            "merged_into": dependency.ref_code,
            "fields": fields,
        },
    )
    session.flush()
    apply_staleness_resolutions(session, dependency.id)
    return dependency


def _validated_candidate_payload(candidate: Candidate) -> tuple[dict, dict, list[dict]]:
    payload = candidate.payload_json
    if not isinstance(payload, dict):
        raise InvalidCandidateProvenance("candidate payload must be an object")

    fields = payload.get("fields", {})
    if not isinstance(fields, dict):
        raise InvalidCandidateProvenance("candidate fields must be an object")
    for name, value in fields.items():
        if not isinstance(name, str):
            raise InvalidCandidateProvenance("candidate field names must be strings")
        if value is not None and not isinstance(value, str):
            raise InvalidCandidateProvenance(
                f"candidate field {name!r} must be a string or null"
            )

    citations = payload.get("citations", [])
    if not isinstance(citations, list):
        raise InvalidCandidateProvenance("candidate citations must be a list")

    return payload, fields, citations


def _validate_candidate_provenance(
    session: Session, candidate: Candidate, *, dependency: Dependency | None = None
) -> tuple[dict, dict, list[dict]]:
    if dependency is not None and candidate.project_id != dependency.project_id:
        raise InvalidCandidateProvenance(
            "candidate and dependency belong to a different project"
        )

    payload, fields, citations = _validated_candidate_payload(candidate)

    source_document = session.get(Document, candidate.source_document_id)
    if source_document is None:
        raise InvalidCandidateProvenance(
            f"source document {candidate.source_document_id} does not exist"
        )
    if source_document.project_id != candidate.project_id:
        raise InvalidCandidateProvenance(
            "candidate source document belongs to a different project"
        )

    if not citations:
        # Every rule below is about a citation, and none of them run on an
        # empty list — so the shape of each citation was checked and the
        # existence of one was not. A Candidate is "a Dependency or event
        # proposed by an extractor, with its citations" (CONTEXT.md), and
        # a row that cites nothing is the proposal without the thing that
        # makes it answerable.
        raise InvalidCandidateProvenance(
            "candidate cites nothing, and a Ledger row's whole claim is "
            "that it points at a page of a document"
        )

    for citation in citations:
        if not isinstance(citation, dict):
            raise InvalidCandidateProvenance("candidate citation must be an object")

        if "document_id" not in citation:
            raise InvalidCandidateProvenance("citation document is missing")
        if "page" not in citation:
            raise InvalidCandidateProvenance("cited page is missing")
        if "quote" not in citation:
            raise InvalidCandidateProvenance("citation quote is missing")

        document_id = citation.get("document_id")
        page_no = citation.get("page")
        quote = citation.get("quote")
        if document_id is None:
            raise InvalidCandidateProvenance("citation document is missing")
        if page_no is None:
            raise InvalidCandidateProvenance("cited page is missing")
        if not isinstance(document_id, int) or isinstance(document_id, bool):
            raise InvalidCandidateProvenance("citation document must be an integer")
        if not isinstance(page_no, int) or isinstance(page_no, bool) or page_no <= 0:
            raise InvalidCandidateProvenance("cited page must be a positive integer")
        if not isinstance(quote, str) or not quote.strip():
            raise InvalidCandidateProvenance(
                "citation quote must be a non-empty string"
            )
        if "verified" in citation and not isinstance(citation["verified"], bool):
            raise InvalidCandidateProvenance("citation verified flag must be a boolean")
        if "whole_row" in citation and not isinstance(citation["whole_row"], bool):
            raise InvalidCandidateProvenance(
                "citation whole_row flag must be a boolean"
            )

        document = session.get(Document, document_id)
        if document is None:
            raise InvalidCandidateProvenance(
                f"citation document {document_id} does not exist"
            )
        if document.project_id != candidate.project_id:
            raise InvalidCandidateProvenance(
                "candidate citation document belongs to a different project"
            )

        page = session.scalars(
            select(DocPage).where(
                DocPage.document_id == document_id,
                DocPage.page_no == page_no,
            )
        ).first()
        if page is None:
            raise InvalidCandidateProvenance(
                f"cited page {page_no} is not on citation document {document_id}"
            )

    return payload, fields, citations


def _resolve_org(
    session: Session, candidate: Candidate, name: str | None
) -> ExternalOrg | None:
    # A placeholder is the document declining to name a party, not a party
    # called `NA` (#77). Minting one puts 86 of Project A's rows behind an
    # owner nobody can chase and inflates every report that groups by
    # party. The Assertion still records what the document printed.
    if not name or is_placeholder_party(name):
        return None
    # The registry matching boundary lives in one module.  It preserves why an
    # exact spelling resolved and, for the replay-gated tiers, refuses to turn
    # an unfamiliar string into a silently minted organization.
    from corridor.organization_identity import resolve_for_record_inclusion

    resolution = resolve_for_record_inclusion(session, candidate)
    if resolution.external_org_id is None:
        raise OrganizationIdentityUnresolved(
            "External Organization identity is not established; confirm the "
            "source spelling before adding this record"
        )
    org = session.get(ExternalOrg, resolution.external_org_id)
    if org is None:  # pragma: no cover - receipt has a foreign key
        raise OrganizationIdentityUnresolved("resolved External Organization no longer exists")
    return org


def _materialize(
    session: Session,
    candidate: Candidate,
    fields: dict,
    org: ExternalOrg | None,
) -> Dependency:
    """Build the typed Dependency the source document's shape asserts.

    Materialization is chosen by the source document's type — the honest
    discriminator — never by sniffing the payload. A shape acceptance does
    not recognise refuses (MalformedCandidateShape) rather than minting a
    mislabeled record; merge is untouched, because merge attaches claims to
    a Dependency somebody already materialized.
    """
    doc_type = session.scalar(
        select(Document.doc_type).where(Document.id == candidate.source_document_id)
    )
    common = {
        "project_id": candidate.project_id,
        "ref_code": _next_ref_code(session, candidate.project_id),
        "external_org_id": org.id if org else None,
    }

    if doc_type == "matrix":
        return Dependency(
            source_ref=fields.get("utility_id"),
            # A utility conflict matrix row is a potential relocation. The
            # candidate's `utility_type` is the kind of utility (Telecom,
            # Gas), not the kind of dependency.
            dep_type="utility_relocation",
            title=_title(fields),
            location_desc=_location(fields),
            station_from=fields.get("station_from"),
            station_to=fields.get("station_to"),
            external_contact=fields.get("external_org_contact"),
            # None when the document asserted no strategy this layout's
            # vocabulary recognises (ADR-0009).
            resolution_strategy=_asserted_strategy(session, candidate, fields),
            cost_responsibility=fields.get("cost_responsibility"),
            **common,
        )

    if doc_type == "agreement":
        title = (fields.get("title") or "").strip()
        obligation = (fields.get("obligation") or "").strip()
        if not title or not obligation:
            missing = "title" if not title else "obligation"
            raise MalformedCandidateShape(
                f"candidate {candidate.id} is an agreement claim with no "
                f"{missing} — acceptance will not guess what was obligated"
            )
        return Dependency(
            dep_type="agreement",
            title=title,
            notes=obligation,
            # The historical free-text closure prompt is retained only on the
            # Extracted Proposal/audit chain.  ADR-0052 retired it from the
            # Project Record; standard selectors and cited fields now govern
            # documentation readiness.
            cost_responsibility=fields.get("cost_responsibility"),
            **common,
        )

    raise MalformedCandidateShape(
        f"candidate {candidate.id} comes from a {doc_type!r} document, "
        "which acceptance has no materializer for"
    )


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


DISMISS_REASONS = ("duplicate", "not-a-conflict", "wrong")


class InvalidDismissReason(Exception):
    """The stated reason is not one this system records."""


class AlreadyDismissed(Exception):
    """This record has already left the working list."""


def dismiss_dependency(
    session: Session,
    dependency: Dependency,
    reason: str,
    *,
    principal: HumanPrincipal,
) -> Dependency:
    """Take a junk record off the working list, with a reason and a name.

    Rows enter mechanically now (ADR-0029), so junk reaches the record —
    a duplicate, a row that is not a conflict at all. Dismissing is how a
    reviewer clears it, and it is not a delete: the row, its Evidence,
    its Assertions and its history stay exactly where they are, so anyone
    asking why a conflict left the list gets an answer with a name and a
    date on it (ADR-0032).
    """
    dismisser = require_human_principal(principal)
    if reason not in DISMISS_REASONS:
        raise InvalidDismissReason(
            f"{reason!r} is not a dismiss reason; expected one of {DISMISS_REASONS}"
        )
    lock_project(session, dependency.project_id)
    # Re-read under the lock. The caller loaded this row before taking it,
    # so two reviewers dismissing at once would both see a null and both
    # write — leaving two immutable, contradictory reasons and a
    # projection naming only one of them.
    session.refresh(dependency)
    if dependency.dismissed_at is not None:
        raise AlreadyDismissed(f"{dependency.ref_code} was already dismissed")

    dismissal = DependencyDismissal(
        dependency_id=dependency.id,
        reason=reason,
        dismissed_by=dismisser.subject,
    )
    session.add(dismissal)
    session.flush([dismissal])
    dependency.dismissed_at = dismissal.dismissed_at

    # The dismissal row holds the reason and the name; the entry names the
    # dismissal rather than repeating it (#604).
    audit.record(
        session,
        principal=dismisser,
        action=audit.DISMISS_DEPENDENCY,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        decided_by=audit.DecisionIdentity(
            kind=audit.DEPENDENCY_DISMISSAL, identity=dismissal.id
        ),
    )
    session.flush()
    return dependency
