"""Public draft-to-preview behavior for source-bound Key dates (#363)."""

from __future__ import annotations

from datetime import date
from hashlib import sha256

import pytest
from sqlalchemy import select

from corridor.key_date_drafting import (
    KeyDateDraftBudget,
    KeyDateDraftRow,
    KeyDateDraftRuntimeOutput,
    QuarantinedSequencing,
    SourceBoundKeyDateDraftRequest,
    StaleKeyDateDraft,
    draft_key_dates,
    preview_drafted_key_dates,
)
from corridor.milestones import confirm_import
from corridor.models import (
    DocPage,
    Document,
    KeyDateDraftReceipt,
    KeyDateDraftRowReceipt,
    Milestone,
    Project,
)
from corridor.principals import HumanPrincipal


RECORDER = HumanPrincipal("local:key-date-draft-test")


def _source(session, project, *, text: str, sha: str | None = None):
    document = Document(
        project_id=project.id,
        sha256=sha or sha256(text.encode()).hexdigest(),
        filename="schedule-summary.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=2, text=text, text_source="cells"))
    session.flush()
    return document


def _request(project, document, *, pages=(2,), source_sha256=None):
    return SourceBoundKeyDateDraftRequest(
        project_id=project.id,
        document_id=document.id,
        source_sha256=source_sha256 or document.sha256,
        allowed_pages=pages,
        requested_by=RECORDER,
    )


class _Runtime:
    identity = {
        "adapter": "fake-key-date-draft-v1",
        "model": "fake-model",
        "prompt_version": "key-date-draft-test-v1",
        "configuration": {"temperature": 0},
    }

    def __init__(self, output):
        self.output = output
        self.calls = []

    def draft(self, request, pages, budget):
        self.calls.append((request, pages, budget))
        return self.output


def _supported_output(quote):
    return KeyDateDraftRuntimeOutput(
        rows=(
            KeyDateDraftRow(
                code="UTIL-CLEAR",
                name="Utility clearance",
                scheduled_for="2026-11-01",
                precision="day",
                page_no=2,
                quote=quote,
            ),
        ),
        usage={"input_tokens": 11, "output_tokens": 7, "elapsed_ms": 4, "spend_usd_micros": 0},
    )


def test_source_bound_draft_previews_then_ordinary_human_confirm_imports(session, project):
    quote = "UTIL-CLEAR | Utility clearance | 2026-11-01"
    document = _source(session, project, text=f"Schedule summary\n{quote}")
    runtime = _Runtime(_supported_output(quote))

    drafted = draft_key_dates(session, _request(project, document), runtime=runtime)

    assert len(runtime.calls) == 1
    assert drafted.status == "drafted"
    assert drafted.rows[0].source.document_id == document.id
    assert drafted.rows[0].source.document_sha256 == document.sha256
    assert drafted.rows[0].source.page_no == 2
    assert drafted.rows[0].source.quote == quote
    assert session.scalars(select(Milestone)).all() == []

    preview = preview_drafted_key_dates(session, drafted)
    assert preview.rows[0].code == "UTIL-CLEAR"
    assert preview.rows[0].need_date == date(2026, 11, 1)
    assert preview.source_name == "schedule-summary.pdf"

    confirm_import(
        session,
        project_id=project.id,
        content=drafted.csv_content,
        source_name=preview.source_name,
        expected_sha256=preview.source_sha256,
        expected_predecessors=preview.predecessors,
        principal=RECORDER,
    )
    assert session.scalar(select(Milestone.code)) == "UTIL-CLEAR"


def test_wrong_project_stale_bytes_and_unallowed_pages_refuse_before_runtime(session, project):
    document = _source(session, project, text="UTIL-CLEAR | Utility clearance | 2026-11-01")
    runtime = _Runtime(_supported_output("UTIL-CLEAR | Utility clearance | 2026-11-01"))
    other = Project(slug="other-key-date-drafts", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()

    with pytest.raises(StaleKeyDateDraft, match="source bytes"):
        draft_key_dates(
            session,
            _request(project, document, source_sha256="0" * 64),
            runtime=runtime,
        )
    with pytest.raises(StaleKeyDateDraft, match="project"):
        draft_key_dates(session, _request(other, document), runtime=runtime)
    with pytest.raises(StaleKeyDateDraft, match="page"):
        draft_key_dates(session, _request(project, document, pages=(1,)), runtime=runtime)

    assert runtime.calls == []
    assert session.scalars(select(KeyDateDraftReceipt)).all() == []


def test_unsupported_precision_and_fabricated_citation_stay_unresolved(session, project):
    quote = "UTIL-CLEAR | Utility clearance | November 2026"
    document = _source(session, project, text=quote)
    output = KeyDateDraftRuntimeOutput(
        rows=(
            KeyDateDraftRow(
                code="UTIL-CLEAR",
                name="Utility clearance",
                scheduled_for="2026-11",
                precision="month",
                page_no=2,
                quote=quote,
            ),
            KeyDateDraftRow(
                code="FABRICATED",
                name="Fabricated event",
                scheduled_for="2026-12-01",
                precision="day",
                page_no=2,
                quote="not in the authorized source",
            ),
        ),
        usage={"input_tokens": 11, "output_tokens": 7, "elapsed_ms": 4, "spend_usd_micros": 0},
    )

    drafted = draft_key_dates(session, _request(project, document), runtime=_Runtime(output))

    assert drafted.rows == ()
    assert [item.reason for item in drafted.unresolved] == [
        "source date is not day-precise",
        "citation is not on an authorized source page",
    ]
    assert session.scalars(select(Milestone)).all() == []


def test_sequencing_is_quarantined_and_never_silently_imported(session, project):
    quote = "UTIL-CLEAR | Utility clearance | 2026-11-01"
    relationship = "UTIL-RELO must finish before UTIL-CLEAR"
    document = _source(session, project, text=f"{quote}\n{relationship}")
    output = KeyDateDraftRuntimeOutput(
        rows=_supported_output(quote).rows,
        sequencing=(QuarantinedSequencing(page_no=2, quote=relationship),),
        usage={"input_tokens": 11, "output_tokens": 7, "elapsed_ms": 4, "spend_usd_micros": 0},
    )

    drafted = draft_key_dates(session, _request(project, document), runtime=_Runtime(output))

    assert drafted.rows == ()
    assert drafted.quarantined_sequencing[0].quote == relationship
    assert "sequencing" in drafted.unresolved[0].reason
    assert session.scalars(select(Milestone)).all() == []


def test_receipt_is_bounded_redacted_and_explicitly_non_authoritative(session, project):
    page_text = "SECRET schedule notes\nUTIL-CLEAR | Utility clearance | 2026-11-01"
    document = _source(session, project, text=page_text)

    drafted = draft_key_dates(
        session,
        _request(project, document),
        runtime=_Runtime(_supported_output("UTIL-CLEAR | Utility clearance | 2026-11-01")),
        budget=KeyDateDraftBudget(
            max_rows=3,
            max_input_chars=500,
            max_output_tokens=20,
            max_elapsed_ms=1_000,
            max_spend_usd_micros=1_000,
        ),
    )

    receipt = session.get(KeyDateDraftReceipt, drafted.receipt_id)
    [row] = session.scalars(
        select(KeyDateDraftRowReceipt).where(KeyDateDraftRowReceipt.receipt_id == receipt.id)
    ).all()
    assert receipt.non_authoritative is True
    assert receipt.requested_by == RECORDER.subject
    assert receipt.source_document_id == document.id
    assert receipt.source_sha256 == document.sha256
    assert receipt.allowed_pages_json == [2]
    assert receipt.budget_json == {
        "max_rows": 3,
        "max_input_chars": 500,
        "max_output_tokens": 20,
        "max_elapsed_ms": 1_000,
        "max_spend_usd_micros": 1_000,
    }
    assert page_text not in str(receipt.configuration_json)
    assert page_text not in str(receipt.usage_json)
    assert row.source_quote == "UTIL-CLEAR | Utility clearance | 2026-11-01"
