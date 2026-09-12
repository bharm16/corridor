"""Give every minutes source question a permitted ending (#833).

A `minutes_captures` outcome the reader emits as a question
(`status == "unresolved"`) was display-only: the Review page listed it and the
Work page counted it, and nothing could settle one. This module records the
ending, append-only, bound to the exact question — never by mutating the
immutable capture outcome.

The endings are the reason-to-action matrix recorded on #833:

- **resolve** (Row 2): the coordinator supplies the identity, scope or timing
  the reader could not establish, cited to the source, and the statement
  re-enters the *existing* capture+comparison path
  (`minutes_spine.resolve_question_into_deltas`) to produce the Proposed Delta
  the source would have produced. No value is fabricated and no accepted record
  moves.
- **interpret** (Row 3): the coordinator records that the source does **not**
  make the proposed assertion — Required By is not Promised For, a report is
  not a completion. This never becomes the assertion.
- **clarify** (Row 4): genuinely insufficient evidence is retained as a named
  clarification request; the question stays readable but stops counting as
  waiting on the coordinator's judgement.
- **exclude** (Row 5): the statement is recorded as outside the declared
  project scope, with a reason.

There is **no generic Dismiss**: `record_question_disposition` refuses a
`resolve` of a reason the matrix does not make resolvable (so completion and
Required By can never be resolved into a fabricated assertion), and every other
ending carries its own evidence.

Why a new relation rather than the packet Follow-up Plan path: that path
references a Proposed Delta by `delta_id`, and a source question has no delta;
borrowing it would mean fabricating one, which #833 forbids. So the disposition
is written to `minutes_question_dispositions`, whose foreign keys reference the
source question directly (the capture revision and the wording Source Segment).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from corridor import digests
from corridor.minutes_spine import (
    RESOLVABLE_REASONS,
    QuestionResolution,
    resolve_question_into_deltas,
)
from corridor.models import MinutesCapture, MinutesQuestionDisposition
from corridor.principals import HumanPrincipal
from corridor.source_append import append_minutes_question_disposition


class MinutesQuestionDispositionRefused(ValueError):
    """A disposition cannot be recorded as asked, and nothing was written."""


# --- The matrix, as data both the reader and the recorder read --------------

#: Every reason code a question can carry, mapped to its matrix row.
PRIMARY_ROW = {
    "attribution_unresolved": 2,
    "person_attribution_unresolved": 2,
    "predecessor_unresolved": 2,
    "new_commitment_has_predecessor": 2,
    "scope_unresolved": 2,
    "new_timing_unresolved": 2,
    "required_by_is_not_promised_timing": 3,
    "completion_not_explicit": 3,
    "unproposed_source_statement": 3,
    "source_statement_unresolved": 4,
    "draft": 5,
    "quoted_history": 5,
    "meeting_metadata": 5,
    "agenda": 5,
    "speaker_label": 5,
}

#: Which resolution control a resolvable reason presents on the form.
RESOLUTION_DIMENSION = {
    "attribution_unresolved": "organization",
    "person_attribution_unresolved": "person",
    "predecessor_unresolved": "predecessor",
    "new_commitment_has_predecessor": "new_or_predecessor",
    "scope_unresolved": "scope",
    "new_timing_unresolved": "timing",
}


def permitted_actions(reason_code: str) -> tuple[str, ...]:
    """The permitted endings for a question, its primary (matrix) action first.

    A Row-2 reason offers resolve, or — if the evidence is not there — a
    clarification or an out-of-scope exclusion, but never an *interpretation*
    that the source makes no assertion, because it does. A Row-3 reason offers
    exactly that interpretation. Nothing offers a generic dismiss.
    """

    row = PRIMARY_ROW.get(reason_code, 4)
    if row == 2:
        return ("resolve", "clarify", "exclude")
    if row == 3:
        return ("interpret", "exclude", "clarify")
    if row == 5:
        return ("exclude", "clarify")
    return ("clarify", "interpret", "exclude")


def question_identity(source_family: str, wording_text: str, reason_codes) -> str:
    """The stable carry key for one source question (#833).

    A digest of the source family, the normalized wording, and the sorted
    reason set. A later revision that changed the wording or the question
    re-derives a different identity and re-asks it; only an identical statement
    with an identical question carries a prior answer.
    """

    return digests.ascii_escaped_sha256(
        {
            "family": source_family,
            "wording": " ".join(wording_text.split()).casefold(),
            "reasons": sorted(reason_codes),
        }
    )


def read_question_dispositions(
    session: Session, *, project_id: int, source_family: str
) -> dict[str, MinutesQuestionDisposition]:
    """The effective (latest-generation) disposition per question identity."""

    effective: dict[str, MinutesQuestionDisposition] = {}
    for row in session.scalars(
        select(MinutesQuestionDisposition)
        .where(
            MinutesQuestionDisposition.project_id == project_id,
            MinutesQuestionDisposition.source_family == source_family,
        )
        .order_by(MinutesQuestionDisposition.decision_generation)
    ):
        effective[row.question_identity] = row
    return effective


@dataclass(frozen=True)
class DispositionRequest:
    """One coordinator's disposition of one source question, as the page sends it."""

    project_id: int
    capture_id: int
    source_family: str
    segment_id: int
    question_identity: str
    reason_code: str
    disposition: str
    decision_generation: int
    interpretation: str = ""
    exclusion_reason: str = ""
    clarification_question: str = ""
    responsible_party: str = ""
    resolution: QuestionResolution | None = None
    supersedes_id: int | None = None


def record_question_disposition(
    session: Session,
    request: DispositionRequest,
    *,
    principal: HumanPrincipal,
) -> MinutesQuestionDisposition:
    """Append one disposition; a resolve first produces its Proposed Deltas.

    The delta production and the disposition are one savepoint: a resolution
    that cannot re-enter comparison writes nothing at all, and a refusal leaves
    the transaction usable so the screen can re-render it.
    """

    if request.disposition not in {"resolve", "interpret", "exclude", "clarify"}:
        raise MinutesQuestionDispositionRefused(
            "that is not a permitted disposition of a source question"
        )
    capture = session.get(MinutesCapture, request.capture_id)
    if capture is None or capture.project_id != request.project_id:
        raise MinutesQuestionDispositionRefused(
            "this source question is not part of this project's reading"
        )
    outcome = next(
        (
            row
            for row in capture.output_json["outcomes"]
            if row.get("segment_id") == request.segment_id
            and row.get("status") == "unresolved"
        ),
        None,
    )
    if outcome is None:
        raise MinutesQuestionDispositionRefused(
            "this source question is no longer part of the latest reading"
        )

    if request.disposition == "resolve" and request.reason_code not in RESOLVABLE_REASONS:
        raise MinutesQuestionDispositionRefused(
            "this question cannot be resolved into a proposal; record what the "
            "source does or does not say, a clarification request, or an "
            "out-of-scope exclusion instead"
        )

    detail = _detail(request)
    produced: list[int] | None = None
    evidence_segment_id: int | None = None
    try:
        with session.begin_nested():
            if request.disposition == "resolve":
                if request.resolution is None:
                    raise MinutesQuestionDispositionRefused(
                        "a resolution must name the evidence it is bound to"
                    )
                replayed = resolve_question_into_deltas(
                    session, capture, outcome, request.resolution
                )
                if replayed["status"] != "captured":
                    raise MinutesQuestionDispositionRefused(
                        "resolving this left the statement unresolved for "
                        + ", ".join(replayed["reasons"])
                        + "; that must be answered too"
                    )
                produced = list(replayed["delta_ids"])
                evidence_segment_id = request.resolution.scope_segment_id
            content = digests.ascii_escaped_sha256(
                {
                    "identity": request.question_identity,
                    "disposition": request.disposition,
                    "reason": request.reason_code,
                    "generation": request.decision_generation,
                    "detail": detail,
                    "deltas": sorted(produced) if produced else None,
                }
            )
            row = append_minutes_question_disposition(
                session,
                project_id=request.project_id,
                capture_id=request.capture_id,
                source_family=request.source_family,
                segment_id=request.segment_id,
                question_identity=request.question_identity,
                disposition=request.disposition,
                reason_code=request.reason_code,
                detail=detail,
                content_sha256=content,
                decided_by=principal.subject,
                decision_generation=request.decision_generation,
                evidence_segment_id=evidence_segment_id,
                produced_delta_ids=produced,
                supersedes_id=request.supersedes_id,
            )
    except DBAPIError as exc:
        raise MinutesQuestionDispositionRefused(_map_refusal(exc)) from exc
    return row


def _detail(request: DispositionRequest) -> dict:
    if request.disposition == "interpret":
        return {"interpretation": request.interpretation.strip()}
    if request.disposition == "exclude":
        return {"exclusion_reason": request.exclusion_reason.strip()}
    if request.disposition == "clarify":
        return {
            "clarification_question": request.clarification_question.strip(),
            "responsible_party": request.responsible_party.strip(),
        }
    binding = request.resolution
    return {
        "resolves": sorted(binding.resolves) if binding else [],
        "organization_id": binding.organization_id if binding else None,
        "person_id": binding.person_id if binding else None,
        "predecessor_subject_key": binding.predecessor_subject_key if binding else None,
        "keep_as_new_commitment": bool(binding and binding.keep_as_new_commitment),
        "scope_subject_keys": list(binding.scope_subject_keys) if binding else [],
        "scope_segment_id": binding.scope_segment_id if binding else None,
        "timing_ref": binding.timing_ref if binding else None,
    }


def _map_refusal(exc: DBAPIError) -> str:
    text = str(getattr(exc, "orig", exc))
    if "answered again" in text:
        return (
            "this question was answered again while this page was open; "
            "re-open it and decide against what now stands"
        )
    if "foreign" in text or "outside" in text:
        return "this disposition names something outside this project's reading"
    return "this disposition could not be recorded, and nothing was written"
