"""What operations may do about a source the pass stopped taking, and its receipt (#842).

The 2026-09-10 customer-journey audit found a source that had failed to process
could become a permanent dead end ("Processing failures currently risk becoming
permanent dead ends"). Two of ``project_processing._eligible_documents``'s four
exclusions are permanent by design and correct: a document whose parse failed is
never re-parsed by the standing pass, because identical bytes make re-ingest a
no-op; and a document whose terminal reading is ``unreadable`` or ``no_matrix``
is never re-read, because an unhandled layout is a standing condition rather
than a transient one. Nothing offered a way out of either. The legacy operations
screens are outside the pilot boundary and stay there, so the coordinator's page
could name the blocked source and its owner (#840, #841) and there was no act
behind that owner at all.

**What this module adds is an act, not an authority.** It re-admits one source
to the ordinary pass under an attributable receipt; the pass then reads it
through every gate it always reads through. So a repair never means bytes were
sent anywhere, and a repair that "succeeded" means only that the ordinary
reading will be attempted again. That is the whole of the mechanical power here,
and it is deliberately small: the audit's requirement was that operations get a
*repeatable, attributable* procedure, and that hidden engineering rescue stop
being free.

**The receipt is the point.** It is one ``audit_log`` entry, written in the same
transaction as the re-parse or the re-admission it authorizes, and two readers
depend on it: ``project_processing`` takes a permanently-excluded source again
when a repair receipt covers the reading that excluded it, and
``source_register`` prints the repair standing on the row a coordinator is
looking at. An engineer who fixes a project by hand through this module leaves
the same visible line an operator does, which is what the audit asked for.

**Three things a repair may not do, and one it cannot.**

- It may not release a recorded hold. ``processing_holds`` owns the
  stage-aware answer over a held document, and this calls it rather than
  restating it; a restriction keeps its own owner and its own act, and nothing
  here releases one. A repair re-reads a source, so what it asks is whether
  document reading is permitted: a source held only against semantic extraction
  is not what this procedure is about, and its own exclusion is act 2's.
- It may not supply a customer authorization. A source whose retained Processing
  Failures name the provider boundary's ``authorization-absent`` or
  ``authorization-refused`` is not a mechanical failure: reading it again reads
  it the same way and is refused the same way, and the missing thing is #522's
  signed customer decision, which is a person's act outside this system.
- It may not change what a captured Source Fact says. A source that was read is
  refused, because "read it again" is not how a capture that is wrong about its
  source is put right; and structurally this module writes no Source Fact,
  Source Segment, Proposed Delta or record decision, and takes no argument that
  could name a value.

**The third procedure named in #842 is here now**, because the decisions it
depended on are accepted: ADR-0100 for the correction request and ADR-0101 for
what a validated result may do to the obsolete proposal.
``correct_captured_reading`` opens one retained report by its recorded
identity, re-reads the capture from the passage the coordinator named, and
records what the ordinary comparison then established. Three things about it
are deliberate.

- **It takes no value.** The corrected reading is materialized from the
  selected passage's own retained text through ``materialize_segment_value``,
  the same materializer every capture goes through. The coordinator's expected
  interpretation is the reason an investigation happened and is never evidence
  (ADR-0084, ADR-0100), and there is no argument here through which it could
  become one.
- **It invents no comparison.** The recomparison is
  ``proposed_delta_comparison.compare_stated_subjects`` under the rule version
  the original proposal was raised with, because two comparison rules mean a
  value that agrees on one path and proposes a change on the other (ADR-0101).
- **It decides nothing about the record.** It writes a Source Fact, its
  Support Assessment, at most one replacement proposal, and the correction
  result; no accepted value, no revision, no disposition. Where the corrected
  value still differs, a coordinator decides it in Review as they always would.
- **It re-asks whether the reported passage applies at all (#945).** Being a
  retained passage of this source is necessary and is not sufficient: the
  selected cell may describe another Utility Conflict, or the same one under
  another field. ``correction_applicability`` answers that from source
  structure alone, and the answer is taken *before* anything is written, so an
  inapplicable selection produces no Source Fact, no Support Assessment, no
  retirement and no replacement proposal. What it does produce is an
  investigation that says it could not be substantiated, which is a truthful
  receipt of what was attempted rather than a claim that a correction happened.
  The verdict and its exact inputs then travel to
  ``record_capture_correction_result``, which derives the whole thing again
  from the same retained rows inside the writing transaction -- so a caller
  that assembles its own convenient Support Assessment and calls the command
  directly is refused there too.

**Why this procedure asks a different gate from the two above.** A re-parse
asks whether *document reading* is permitted. A re-capture reads the retained
source and then writes a Source Fact from it, which is semantic extraction, so
it asks ``processing_holds.assert_may_extract_semantics`` -- the same
authoritative stage-aware check, asked for the stage this act actually
performs. #919's rule that an operation needs both a valid permission and no
applicable prohibiting hold is honoured with both halves: the permission for
this act is the technical-operations designation read live from the roster, and
the hold check is ``processing_holds``'. #827's onboarding permissions are not
the applicable permission here and are deliberately not consulted: every one of
``onboarding_authorization.ONBOARDING_OPERATIONS`` is a pre-activation act, and
a Review item exists only after activation, so requiring an onboarding grant
would fail closed on every project that can reach this seam.

**A corrected-mapping repair names the mapping it reads under.**
``CORRECTED_MAPPING`` says the source is being read again because the
customer's field mapping was corrected, and the receipt records the exact
registration in force -- identity and version -- so a later reader can tell
whether a changed reading followed a changed mapping. It does not adjudicate
whether that registration is *the* correction: ``field_mapping_manifest``'s own
rule is that nothing here concludes a form changed meaning, because that
conclusion is a person's and is recorded as the registration itself. Making
that registration is a separate act with a separate authority: #829's
``format_replacement`` offers and validates one, and PostgreSQL's
``register_baseline_format`` refuses a changed mapping revision unless the
person approving it holds the project-coordination designation (#597).
Operations supplies and re-reads; it does not approve what a customer's column
means. A project with no mapping registered at all has nothing to have
corrected, and the declaration is refused.

**A receipt names the reading it repaired, and is spent by the next one.** It
records the newest Extraction Run this source had when the repair was made, and
both readers compare against *that* rather than against a wall clock: the
standing pass takes the source again only while no newer run exists, so a
source that fails the same way a second time is excluded again instead of being
re-admitted for ever by one old receipt, and the register can say whether the
source has been read since without asking what time it is.

**No clock.** The comparisons above are between two recorded row identities,
and the one time comparison -- a mapping registration against the reading it
must post-date -- is between two of PostgreSQL's own instants. Two readings of
the same records reach the same answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor import (
    access,
    audit,
    capture_correction,
    capture_correction_retirement,
    processing_holds,
    refusals,
)
from corridor.baseline_adoption import effective_baseline_formats
from corridor.correction_applicability import APPLICABLE
from corridor.delta_resolution import current_accepted_revision_id
from corridor.facts import fact_identity_digest
from corridor.materializer import FactValidationError, materialize_segment_value
from corridor.models import (
    AuditLog,
    CaptureCorrectionRequest,
    Document,
    Fact,
    ExtractionRun,
    PageProcessingFailure,
    ProposedDelta,
    SourceSegment,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.proposed_deltas import create_proposed_delta_group
from corridor.provider_authorization import (
    AUTHORIZATION_ABSENT,
    AUTHORIZATION_REFUSED,
)
from corridor.source_append import append_fact, append_support_assessment
from corridor.source_segments import source_segment_locator_words


# --- The procedures --------------------------------------------------------
#
# What the operator declares they are doing, recorded on the receipt so a
# reader can tell a bare re-read from one the customer's corrected form is the
# reason for. Neither name is a customer-facing label: both appear only on the
# runbook's own arguments and inside the receipt.

RETRY_PROCESSING = "retry_processing"
CORRECTED_MAPPING = "corrected_mapping"
PROCEDURES: tuple[str, ...] = (RETRY_PROCESSING, CORRECTED_MAPPING)

#: #842's third procedure, which is not one of `PROCEDURES`: it takes a
#: correction-request id rather than a document id, re-admits nothing to the
#: standing pass, and leaves a receipt of its own. It is named here so the
#: receipt and the register agree on one word for it.
SOURCE_GROUNDED_RECAPTURE = "source_grounded_recapture"

#: Why the standing pass had stopped taking this source, recorded on the
#: receipt beside the procedure. These are ``_eligible_documents``'s own two
#: permanent exclusions, spelled the way it spells them.
FAILED_PARSE = "failed_parse"
UNREADABLE_PERMANENT = "unreadable_permanent"

#: The extraction outcomes the standing pass treats as permanent. It lives here
#: rather than in ``project_processing`` because the repair and the exclusion
#: have to mean the same thing by construction: a repair offered for an outcome
#: the pass does not exclude would re-admit a source that was never held, and a
#: pass excluding an outcome no repair covers would be the dead end again.
PERMANENT_FAILURE_OUTCOMES: tuple[str, ...] = ("unreadable", "no_matrix")

#: The reasons a provider authorization check refuses, as a routed page's
#: retained Processing Failure records them.
_AUTHORIZATION_REASONS = frozenset({AUTHORIZATION_ABSENT, AUTHORIZATION_REFUSED})


class OperationsRepairRefused(refusals.Refusal, ValueError):
    """This repair is not one operations may perform, and nothing was written.

    ``reason`` is a stable machine code and ``str(exc)`` is the sentence a
    person reads. The codes are ``unknown_document``, ``not_designated``,
    ``held_in_quarantine``, ``authorization_missing``, ``already_read``,
    ``not_blocked``, ``unknown_procedure``, ``no_registered_mapping``,
    ``parse_not_recoverable``, and the re-capture's own ``unknown_request``
    and ``unknown_delta``. A refusal the record-decision command raised is a
    ``capture_correction.CaptureCorrectionRefused`` carrying the command's own
    sentence, not one of these.
    """

    refusal_kind = refusals.CONFLICT

    def __init__(
        self, reason: str, message: str, *, kind: str = refusals.CONFLICT
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.refusal_kind = kind


@dataclass(frozen=True, slots=True)
class RepairOutcome:
    """What one repair did, in the words its own receipt carries."""

    document_id: int
    procedure: str
    blocked_by: str
    outcome: str
    detail: str
    read_through_run_id: int | None
    under_mapping: str
    audit_id: int


@dataclass(frozen=True, slots=True)
class RepairReceipt:
    """The newest recorded repair of one source, as a reader gets it back."""

    document_id: int
    procedure: str
    blocked_by: str
    outcome: str
    detail: str
    read_through_run_id: int | None
    under_mapping: str
    performed_by: str
    performed_at: datetime | None


def repair_source_processing(
    session: Session,
    *,
    document_id: int,
    principal: HumanPrincipal,
    procedure: str = RETRY_PROCESSING,
    images_dir: str | None = None,
) -> RepairOutcome:
    """Re-admit one blocked source to the standing pass, attributably.

    Runs in the caller's transaction, so a rolled-back caller leaves no receipt
    and no claim that a repair happened. Every refusal below is raised before
    anything is written.
    """

    actor = require_human_principal(principal)
    if procedure not in PROCEDURES:
        raise OperationsRepairRefused(
            "unknown_procedure",
            f"{procedure!r} is not a repair operations performs. The two are "
            f"{RETRY_PROCESSING!r} and {CORRECTED_MAPPING!r}.",
            kind=refusals.MALFORMED_INPUT,
        )
    document = session.get(Document, document_id)
    if document is None:
        raise OperationsRepairRefused(
            "unknown_document",
            f"document {document_id} is not a registered source.",
            kind=refusals.NOT_OFFERED,
        )
    _require_technical_operations(session, actor, int(document.project_id))
    _refuse_held_bytes(session, int(document.id))
    _refuse_missing_customer_authorization(session, int(document.id))
    blocked_by = _blocked_by(session, document)
    under_mapping = (
        _registered_mapping(session, document)
        if procedure == CORRECTED_MAPPING
        else ""
    )
    read_through = session.scalar(
        select(func.max(ExtractionRun.id)).where(
            ExtractionRun.document_id == int(document.id)
        )
    )

    if blocked_by == FAILED_PARSE:
        # The bounded re-parse #350 already built, which is the only thing that
        # can move a failed parse: ordinary re-ingest of identical bytes is a
        # no-op. It writes its own RECOVER_DOCUMENT_PARSE receipt, so this act
        # leaves two entries -- what operations asked for, and what the parse
        # did -- rather than one that has to mean both. Imported here because
        # that module reaches the ingest engines and this one does not.
        from corridor.location_discovery import recover_document_parse

        recovery = recover_document_parse(
            session,
            document_id=int(document.id),
            principal=actor,
            images_dir=images_dir,
        )
        if recovery.outcome not in ("recovered", "still_failed"):
            # The re-parse refused rather than ran -- a sealed holdout is the
            # one way that happens from here. Recording a repair receipt for a
            # refusal would say operations acted when it did not, and would
            # re-admit a source nothing re-read.
            raise OperationsRepairRefused(
                "parse_not_recoverable",
                recovery.reason
                or f"the bounded re-parse answered {recovery.outcome!r}.",
                kind=refusals.NOT_OFFERED,
            )
        outcome, detail = recovery.outcome, (recovery.reason or "")
    else:
        outcome, detail = "readmitted", (
            "the next standing pass reads this source again; it was excluded "
            "as permanently unreadable until this repair"
        )

    entry = audit.record(
        session,
        principal=actor,
        action=audit.REPAIR_SOURCE_PROCESSING,
        entity_type=audit.DOCUMENT,
        entity_id=int(document.id),
        after={
            "procedure": procedure,
            "blocked_by": blocked_by,
            "outcome": outcome,
            "detail": detail,
            "sha256": document.sha256,
            # Which reading this repair is of. Recorded rather than compared
            # against a clock: the receipt and the run it repairs can share one
            # transaction's ``now()``, and "the newest run when I acted" is the
            # fact both readers actually want.
            "read_through_run_id": None if read_through is None else int(read_through),
            # Which mapping revision the re-read happens under, where the
            # operator declared one is the reason. Named rather than judged:
            # deciding that a form changed meaning is the registration, and it
            # was made by somebody holding a designation this act does not.
            "under_mapping": under_mapping,
        },
    )
    return RepairOutcome(
        document_id=int(document.id),
        procedure=procedure,
        blocked_by=blocked_by,
        outcome=outcome,
        detail=detail,
        read_through_run_id=None if read_through is None else int(read_through),
        under_mapping=under_mapping,
        audit_id=int(entry.id),
    )


def repair_receipts(
    session: Session, *, document_ids: Sequence[int]
) -> dict[int, RepairReceipt]:
    """The newest recorded repair of each of these sources, where there is one.

    One grouped statement rather than one per source: the register asks this
    for a whole page of deliveries at once.
    """

    ids = [int(value) for value in dict.fromkeys(document_ids)]
    if not ids:
        return {}
    newest = session.execute(
        select(AuditLog.entity_id, func.max(AuditLog.id))
        .where(
            # Both receipted procedures, because #842 asks that each appear on
            # the source register and the register has one cell for "what
            # operations last did to this source". `repaired_through` below is
            # deliberately not widened: it decides re-admission to the standing
            # pass, and a corrected capture re-admits nothing.
            AuditLog.action.in_(
                (audit.REPAIR_SOURCE_PROCESSING, audit.CORRECT_CAPTURED_READING)
            ),
            AuditLog.entity_type == audit.DOCUMENT,
            AuditLog.entity_id.in_(ids),
        )
        .group_by(AuditLog.entity_id)
    ).all()
    if not newest:
        return {}
    entries = session.scalars(
        select(AuditLog).where(
            AuditLog.id.in_([int(entry_id) for _, entry_id in newest])
        )
    ).all()
    return {
        int(entry.entity_id): RepairReceipt(
            document_id=int(entry.entity_id),
            procedure=str((entry.after_json or {}).get("procedure") or ""),
            blocked_by=str((entry.after_json or {}).get("blocked_by") or ""),
            outcome=str((entry.after_json or {}).get("outcome") or ""),
            detail=str((entry.after_json or {}).get("detail") or ""),
            read_through_run_id=_run_id(entry),
            under_mapping=str((entry.after_json or {}).get("under_mapping") or ""),
            performed_by=str(entry.human_principal or entry.actor),
            performed_at=entry.ts,
        )
        for entry in entries
    }


def repaired_through(
    session: Session, *, document_ids: Sequence[int]
) -> dict[int, int]:
    """The newest reading each of these sources has been repaired through.

    Separate from ``repair_receipts`` because the standing pass needs only this
    one number, and because the answer is the highest run id any of a source's
    receipts covers rather than the newest receipt's: two repairs of the same
    source do not undo each other.
    """

    ids = [int(value) for value in dict.fromkeys(document_ids)]
    if not ids:
        return {}
    covered: dict[int, int] = {}
    for entry in session.scalars(
        select(AuditLog).where(
            AuditLog.action == audit.REPAIR_SOURCE_PROCESSING,
            AuditLog.entity_type == audit.DOCUMENT,
            AuditLog.entity_id.in_(ids),
        )
    ).all():
        run_id = _run_id(entry)
        if run_id is None:
            continue
        document_id = int(entry.entity_id)
        covered[document_id] = max(covered.get(document_id, 0), run_id)
    return covered


def _run_id(entry: AuditLog) -> int | None:
    """The reading one receipt was taken through, or nothing when it named none."""

    value = (entry.after_json or {}).get("read_through_run_id")
    return int(value) if isinstance(value, int) else None


# --- What the repair refuses ------------------------------------------------


def _require_technical_operations(
    session: Session, actor: HumanPrincipal, project_id: int
) -> None:
    """The technical-operations designation, read live from the roster.

    ``access.resolve_membership`` is the one reader every project surface uses,
    so a withdrawn designation is felt here at once and this is not a second
    opinion about who holds one.
    """

    membership = access.resolve_membership(session, actor.subject, project_id)
    if membership is None or not membership.has(access.TECHNICAL_OPERATIONS):
        raise OperationsRepairRefused(
            "not_designated",
            "repairing a source is a technical-operations act, and this "
            "principal does not hold that designation on this project.",
            kind=refusals.NOT_AUTHORIZED,
        )


# --- ADR-0101's source-grounded re-capture (#842) ---------------------------


@dataclass(frozen=True, slots=True)
class CaptureCorrectionOutcome:
    """What one source-grounded re-capture established, and what it retired."""

    request_id: int
    delta_id: int
    outcome: str
    result_id: int
    retirement_id: int | None
    corrected_fact_id: int | None
    replacement_delta_id: int | None
    accepted_revision_id: int | None
    comparison_rule_version: str
    #: What the retained source structure said about the reported passage
    #: (#945). ``applicable`` is the only verdict a corrected capture may be
    #: written under; the other three are recorded as themselves.
    applicability_verdict: str
    finding: str
    authorized_by_principal: str
    executed_by: str
    audit_id: int

    @property
    def retired(self) -> bool:
        return self.retirement_id is not None


def correct_captured_reading(
    session: Session,
    *,
    request_id: int,
    principal: HumanPrincipal,
    performed_at: datetime,
    executed_by: str | None = None,
    substantiated: bool = True,
    finding: str = "",
) -> CaptureCorrectionOutcome:
    """Re-read one challenged capture from its own source, and record the result.

    Runs in the caller's transaction, so a rolled-back caller leaves no
    correction result, no retirement, and no claim that one happened. Every
    refusal is raised before anything is written.

    ``principal`` is the responsible operations actor, who must hold the
    technical-operations designation; ``executed_by`` is the identity that
    performed the work and defaults to that same person. They stay distinct
    because a queued re-capture running under a service identity does not
    become the author of the decision to correct (ADR-0101, ADR-0081).

    ``substantiated=False`` records an investigation that could not be
    substantiated, with operations' own ``finding``. It retires nothing and the
    proposal stays open: "the source did not establish this assertion" is not
    "the source matches the accepted value", and no Source Fact claiming
    absence or equality is written (ADR-0101).

    A reported passage the retained source structure does not hold to this
    subject and this field reaches the same outcome by the same route (#945).
    The applicability verdict is taken before the first write, so an
    inapplicable selection leaves no corrected Source Fact, no Support
    Assessment, no retirement and no replacement proposal -- only the report
    that was already retained and a receipt saying what the investigation
    found.
    """

    actor = require_human_principal(principal)
    performer = (executed_by or actor.subject).strip() or actor.subject
    request = session.get(CaptureCorrectionRequest, int(request_id))
    if request is None:
        raise OperationsRepairRefused(
            "unknown_request",
            f"extraction-error report {request_id} is not a recorded report.",
            kind=refusals.NOT_OFFERED,
        )
    project_id = int(request.project_id)
    document_id = int(request.document_id)
    _require_technical_operations(session, actor, project_id)
    # The one authoritative stage-aware check, called rather than restated
    # (#919). A re-capture reads the retained source and writes a Source Fact
    # from it, so the stage it asks about is semantic extraction -- which also
    # answers the reading prohibition, because a document nobody may read is a
    # document nobody may extract from.
    _refuse_held_extraction(session, document_id)
    _refuse_missing_customer_authorization(session, document_id)

    # The exact capture the report named, by recorded identity with its digest
    # checked, never by the query that reconstructs a capture for a screen.
    capture = capture_correction.stored_challenged_capture(session, request)
    delta = session.get(ProposedDelta, int(request.delta_id))
    if delta is None or int(delta.project_id) != project_id:
        raise OperationsRepairRefused(
            "unknown_delta",
            f"proposed change {request.delta_id} is not this project's.",
            kind=refusals.NOT_OFFERED,
        )
    rule_version = delta.comparison_rule_version
    accepted_revision_id = current_accepted_revision_id(
        session,
        project_id=project_id,
        subject_identity=delta.target_subject_identity,
        field_name=delta.target_field,
    )

    selected = session.get(SourceSegment, int(request.selected_source_segment_id))
    challenged_capture_row = session.get(Fact, capture.fact_id)
    field = delta.target_field or capture.field
    # Asked before anything is written, and asked of source structure alone: a
    # correction may assert a value only where the retained evidence
    # establishes that the value applies to this subject and this field (#945).
    # The verdict cannot be moved by the Fact this procedure is about to
    # append, because no Fact is an input to it.
    applicability = capture_correction.passage_applicability(
        session, capture, selected
    )

    if not substantiated:
        return _record_correction(
            session,
            request=request,
            delta=delta,
            capture=capture,
            applicability=applicability,
            outcome=capture_correction_retirement.INCONCLUSIVE,
            corrected_fact_id=None,
            support_assessment_id=None,
            accepted_revision_id=None,
            replacement_delta_id=None,
            rule_version=rule_version,
            finding=finding.strip() or UNSUBSTANTIATED_FINDING,
            actor=actor,
            performer=performer,
            performed_at=performed_at,
        )

    if applicability.verdict != APPLICABLE:
        # Nothing is written but the receipt. A retained report and a recorded
        # refusal are evidence of what was attempted; they are not a claim that
        # the correction succeeded, and the proposal stays open for the
        # coordinator it was always waiting on.
        return _record_correction(
            session,
            request=request,
            delta=delta,
            capture=capture,
            applicability=applicability,
            outcome=capture_correction_retirement.INCONCLUSIVE,
            corrected_fact_id=None,
            support_assessment_id=None,
            accepted_revision_id=None,
            replacement_delta_id=None,
            rule_version=rule_version,
            finding=finding.strip() or applicability.investigation_sentence,
            actor=actor,
            performer=performer,
            performed_at=performed_at,
        )

    try:
        # The corrected value comes out of the retained passage's own text,
        # through the materializer every capture goes through. There is no
        # argument to this procedure a value could arrive by.
        value = materialize_segment_value(session, field, selected)
    except FactValidationError as exc:
        # A passage that does not read as this field is not proof the source
        # says something else; it is an investigation that could not be
        # substantiated, recorded as itself.
        return _record_correction(
            session,
            request=request,
            delta=delta,
            capture=capture,
            applicability=applicability,
            outcome=capture_correction_retirement.INCONCLUSIVE,
            corrected_fact_id=None,
            support_assessment_id=None,
            accepted_revision_id=None,
            replacement_delta_id=None,
            rule_version=rule_version,
            finding=(
                f"the reported passage does not read as {field}: {exc}. The "
                "proposed change stays open."
            ),
            actor=actor,
            performer=performer,
            performed_at=performed_at,
        )

    corrected = append_fact(
        session,
        project_id=project_id,
        document_id=document_id,
        # The reading that was wrong. `ck_facts_source_binding` requires a
        # document-bound Fact to name the run that read the document, and the
        # honest answer is the run whose reading this corrects: the corrected
        # capture then sits in the same source lineage the delta's own reading
        # walks, so the screen finds it where it looks. What separates it from
        # that run's own output is `recorded_by`, and the identity digest
        # below, which names this report and this passage rather than a model.
        extraction_run_id=(
            None
            if challenged_capture_row is None
            else challenged_capture_row.extraction_run_id
        ),
        subject_kind="source_row",
        subject_key=capture.subject_identity,
        recorded_by=f"operations:{SOURCE_GROUNDED_RECAPTURE}",
        content_sha256=fact_identity_digest(
            # Not an Extraction Run: this capture's identity is the report it
            # answers and the passage it was read from, which is stable and
            # reproducible, so an exact replay of the same correction appends
            # the same Fact rather than a second one.
            run_identity={
                "capture_correction_request_id": int(request.id),
                "document_id": document_id,
                "source_segment_id": int(selected.id),
                "procedure": SOURCE_GROUNDED_RECAPTURE,
            },
            subject_kind="source_row",
            subject_key=capture.subject_identity,
            value=value,
        ),
        value=value,
    )
    # ADR-0082's source-backed class: the corrected capture is held to the
    # retained passage by an effective Support Assessment, which is what the
    # command checks before it retires anything.
    support = append_support_assessment(
        session,
        project_id=project_id,
        proposition_kind="source_fact",
        fact_id=int(corrected.id),
        extracted_proposal_id=None,
        source_segment_ids=(int(selected.id),),
        evidence_role="value_support",
        assessment="supported",
        human_principal=actor.subject,
        released_policy=None,
        ruleset_version=None,
        assessed_at=performed_at,
    )

    recomparison = capture_correction_retirement.recompare_corrected_capture(
        session,
        delta,
        corrected_value=value.scalar,
        comparison_rule_version=rule_version,
        accepted_revision_id=accepted_revision_id,
    )
    replacement_delta_id = None
    if not recomparison.establishes_no_difference:
        # The corrected result still differs, so the difference is proposed as
        # an ordinary Proposed Delta and a coordinator decides it in Review.
        # It is not a supersession of the original: the cause is a correction
        # to a capture, not a newer source version (ADR-0101).
        replacements = create_proposed_delta_group(
            session,
            project_id=project_id,
            source_family=delta.source_family,
            source_revision=delta.source_revision,
            document_id=document_id,
            deltas=recomparison.replacement,
        )
        replacement_delta_id = int(replacements[0].id) if replacements else None

    return _record_correction(
        session,
        request=request,
        delta=delta,
        capture=capture,
        applicability=applicability,
        outcome=recomparison.outcome,
        corrected_fact_id=int(corrected.id),
        support_assessment_id=int(support.id),
        accepted_revision_id=accepted_revision_id,
        replacement_delta_id=replacement_delta_id,
        rule_version=rule_version,
        finding=finding.strip() or _recomparison_finding(recomparison, selected),
        actor=actor,
        performer=performer,
        performed_at=performed_at,
    )


#: What an investigation nobody could substantiate says when operations gave no
#: words of their own. It claims no correction, which is the whole point.
UNSUBSTANTIATED_FINDING = (
    "the retained source did not establish the reported correction, so no "
    "corrected capture was recorded and the proposed change stays open"
)


def _recomparison_finding(recomparison, selected: SourceSegment) -> str:
    """What the recomparison established, in the words a later reader needs."""

    where = source_segment_locator_words(selected)
    if recomparison.establishes_no_difference:
        return (
            f"the capture was re-read from {where} and compared against the "
            f"accepted record under rule "
            f"{recomparison.comparison_rule_version}; the corrected value "
            f"matches the accepted value, so there is no difference to propose"
        )
    return (
        f"the capture was re-read from {where} and compared against the "
        f"accepted record under rule {recomparison.comparison_rule_version}; "
        f"the corrected value still differs, so a corrected proposal was "
        f"raised for Review"
    )


def _record_correction(
    session: Session,
    *,
    request: CaptureCorrectionRequest,
    delta: ProposedDelta,
    capture,
    applicability,
    outcome: str,
    corrected_fact_id: int | None,
    support_assessment_id: int | None,
    accepted_revision_id: int | None,
    replacement_delta_id: int | None,
    rule_version: str,
    finding: str,
    actor: HumanPrincipal,
    performer: str,
    performed_at: datetime,
) -> CaptureCorrectionOutcome:
    """Write the result and its receipt, in the caller's one transaction.

    The result goes through the record-decision role's command, which is where
    every one of ADR-0101's six conditions is established; the receipt is the
    ordinary operations receipt, so this act leaves the same visible line on
    the source register that the other two procedures leave.
    """

    recorded = capture_correction_retirement.record_correction_result(
        session,
        project_id=int(request.project_id),
        request_id=int(request.id),
        delta_id=int(delta.id),
        challenged_fact_id=capture.fact_id,
        challenged_fact_sha256=capture.fact_content_sha256,
        corrected_fact_id=corrected_fact_id,
        corrected_support_assessment_id=support_assessment_id,
        accepted_revision_id=accepted_revision_id,
        comparison_rule_version=rule_version,
        applicability=applicability,
        outcome=outcome,
        replacement_delta_id=replacement_delta_id,
        finding=finding,
        authorized_by_principal=actor.subject,
        executed_by=performer,
        recorded_at=performed_at,
    )
    entry = audit.record(
        session,
        principal=actor,
        action=audit.CORRECT_CAPTURED_READING,
        entity_type=audit.DOCUMENT,
        entity_id=int(request.document_id),
        after={
            "procedure": SOURCE_GROUNDED_RECAPTURE,
            "request_id": int(request.id),
            "delta_id": int(delta.id),
            "outcome": outcome,
            "detail": finding,
            "result_id": recorded.result_id,
            "retirement_id": recorded.retirement_id,
            "corrected_fact_id": corrected_fact_id,
            "applicability_verdict": applicability.verdict,
            "replacement_delta_id": replacement_delta_id,
            "accepted_revision_id": accepted_revision_id,
            "comparison_rule_version": rule_version,
            # The responsible actor is the audit entry's own principal; the
            # identity that performed the work is recorded beside it, because
            # a worker executing part of the procedure is not its author.
            "executed_by": performer,
        },
    )
    return CaptureCorrectionOutcome(
        request_id=int(request.id),
        delta_id=int(delta.id),
        outcome=outcome,
        result_id=recorded.result_id,
        retirement_id=recorded.retirement_id,
        corrected_fact_id=corrected_fact_id,
        replacement_delta_id=replacement_delta_id,
        accepted_revision_id=accepted_revision_id,
        comparison_rule_version=rule_version,
        applicability_verdict=applicability.verdict,
        finding=finding,
        authorized_by_principal=actor.subject,
        executed_by=performer,
        audit_id=int(entry.id),
    )


def _refuse_held_extraction(session: Session, document_id: int) -> None:
    """A source nobody may extract from is not re-captured either.

    The gate is ``processing_holds``', called rather than restated, and the
    recorded reasons are the words a person reads. Nothing here releases a
    restriction: clearing one has to cite the evidence or configuration change
    that removed its cause, which is ``processing_holds.release_hold``'s act
    and not this one (#919).
    """

    try:
        processing_holds.assert_may_extract_semantics(session, document_id)
    except processing_holds.ProcessingHoldInForce as exc:
        raise OperationsRepairRefused(
            "held_in_quarantine",
            f"this source is held and is not processed: {exc}. Correcting a "
            "capture does not release held bytes, and nothing here lifts a "
            "hold.",
        ) from exc


def _refuse_held_bytes(session: Session, document_id: int) -> None:
    """A document nobody may read keeps its own owner and its own act.

    The gate is ``processing_holds``', called rather than restated, and the
    recorded reasons are the words a person reads. Nothing here releases a
    restriction: a generic release control over every hold is exactly what #842
    refuses, and the act that lifts one is ``processing_holds.release_hold``,
    which needs the evidence that removed its cause (#919).
    """

    try:
        processing_holds.assert_may_read_document(session, document_id)
    except processing_holds.ProcessingHoldInForce as exc:
        raise OperationsRepairRefused(
            "held_in_quarantine",
            f"this source is held and is not processed: {exc}. A repair "
            "does not release held bytes, and nothing here lifts a hold.",
        ) from exc


def _refuse_missing_customer_authorization(
    session: Session, document_id: int
) -> None:
    """A reading refused for want of an authorization is not repaired by re-reading.

    The retained Processing Failure records the boundary's own reason, so this
    asks the record rather than re-deriving whether a project is authorized.
    """

    reason = session.scalar(
        select(PageProcessingFailure.error_type)
        .where(
            PageProcessingFailure.document_id == document_id,
            PageProcessingFailure.error_type.in_(sorted(_AUTHORIZATION_REASONS)),
        )
        .order_by(PageProcessingFailure.id.desc())
        .limit(1)
    )
    if reason is None:
        return
    raise OperationsRepairRefused(
        "authorization_missing",
        f"this source was not read because the processing it needs was "
        f"refused: {reason}. Reading it again reads it the same way and is "
        "refused the same way. What is missing is the customer's own signed "
        "authorization for that processing, which operations cannot supply "
        "here.",
        kind=refusals.NOT_AUTHORIZED,
    )


def _blocked_by(session: Session, document: Document) -> str:
    """Which permanent exclusion holds this source, or a refusal saying none does.

    The two are ``project_processing``'s own, asked in its order: the parse
    first, because a document that never parsed has no reading to be permanent
    about.
    """

    if document.parse_status == "failed":
        return FAILED_PARSE
    if document.parse_status != "parsed":
        raise OperationsRepairRefused(
            "not_blocked",
            f"this source's parse is {document.parse_status!r}, which the "
            "standing pass reaches on its own. There is nothing to re-admit.",
            kind=refusals.NOT_OFFERED,
        )
    outcomes = set(
        session.scalars(
            select(ExtractionRun.outcome).where(
                ExtractionRun.document_id == int(document.id)
            )
        ).all()
    )
    if "completed" in outcomes:
        raise OperationsRepairRefused(
            "already_read",
            "this source was read. Reading it again is not how a capture that "
            "is wrong about its source is put right, and nothing here changes "
            "what a captured fact says.",
            kind=refusals.NOT_OFFERED,
        )
    if outcomes & set(PERMANENT_FAILURE_OUTCOMES):
        return UNREADABLE_PERMANENT
    raise OperationsRepairRefused(
        "not_blocked",
        "the standing pass takes this source again on its own, so there is "
        "nothing to re-admit.",
        kind=refusals.NOT_OFFERED,
    )


def _registered_mapping(session: Session, document: Document) -> str:
    """The mapping revision in force, named, or a refusal when there is none.

    A project that has registered no field mapping has nothing to have
    corrected, so the declaration is refused rather than recorded against
    nothing. Where there is one, its identity and version go on the receipt:
    that is what makes "it was read again under the corrected mapping" a
    checkable statement rather than an operator's word for it.
    """

    registered = effective_baseline_formats(
        session, int(document.project_id)
    ).get("field_mapping")
    if registered is None:
        raise OperationsRepairRefused(
            "no_registered_mapping",
            "this project has registered no field mapping, so there is no "
            "corrected mapping for this source to be read under. Registering "
            "one is the project-coordination designation's act, not this one.",
            kind=refusals.NOT_OFFERED,
        )
    return f"{registered.format_identity} {registered.format_version}"


__all__ = [
    "CORRECTED_MAPPING",
    "SOURCE_GROUNDED_RECAPTURE",
    "UNSUBSTANTIATED_FINDING",
    "CaptureCorrectionOutcome",
    "correct_captured_reading",
    "FAILED_PARSE",
    "PERMANENT_FAILURE_OUTCOMES",
    "PROCEDURES",
    "RETRY_PROCESSING",
    "UNREADABLE_PERMANENT",
    "OperationsRepairRefused",
    "RepairOutcome",
    "RepairReceipt",
    "repair_receipts",
    "repair_source_processing",
    "repaired_through",
]
