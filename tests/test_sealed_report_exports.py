"""Decode the retained exports once, then check every consumer's frozen contract.

Three regression tests used to decode the same 154 pages five times: once for
the text digest, once for release validation, twice for rehearsal validation,
and once for storage semantics. This test retains all five checks against an
actual pypdf reading of the exact sealed bytes. The cache holds that runtime
reading; the original pinned digests and authored field expectations are
unchanged. Small generated PDFs in each consumer's own tests still exercise
its unpatched reader, including missing fields and mixed label profiles.
"""

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
from types import SimpleNamespace

from pypdf import PdfReader
import pytest

from corridor import report_release, sh99_coordinator_rehearsal, storage_baseline
from pdf_fixture_support import PdfFixture


SEALED_EXPORTS = (
    Path(__file__).resolve().parents[1]
    / "artifacts/product-proving/sh99-9a4342d-two-pass-passed"
)


class _ReadSealedPdfOnce:
    """Memoize real per-page decoding only for one exact input and default options.

    The cache lasts one export case and wraps genuine pypdf pages. Requests
    for different bytes or reader/extraction options fail and are also recorded:
    a consumer that catches the failure cannot turn it into an expected refusal
    and silently pass the retained-byte test.
    """

    def __init__(self, raw: bytes):
        self.raw = raw
        self.reader = PdfReader(BytesIO(raw))
        self.decoded_pages = 0
        self.invalid_requests = []
        for page in self.reader.pages:
            page.extract_text = self._memoize(page.extract_text)

    def __call__(self, stream, *args, **kwargs):
        if stream.getvalue() != self.raw or args or kwargs:
            self.invalid_requests.append("different PDF bytes or reader options")
            raise AssertionError(self.invalid_requests[-1])
        return self.reader

    def _memoize(self, extract_text):
        result = None

        def extract_once(*args, **kwargs):
            nonlocal result
            if args or kwargs:
                self.invalid_requests.append("different text extraction options")
                raise AssertionError(self.invalid_requests[-1])
            if result is None:
                result = extract_text()
                self.decoded_pages += 1
            return result

        return extract_once


def _equistar_statement():
    return SimpleNamespace(
        event=SimpleNamespace(
            event_type="commitment",
            timing_direction=None,
            stated_party="Equistar",
            description=(
                "Equistar to provide a chain of title on the ROW agreement that is "
                "in DOW’s name (Due date of 01/2025)."
            ),
        ),
        timings=(SimpleNamespace(text="01/2025", precision="month"),),
        plan=SimpleNamespace(
            internal_owner="Bryce Harmon",
            next_action="Confirm the External Party and Commitment Scope",
            action_due_date=None,
            next_action_decision=object(),
            milestone_impact="not_applicable",
        ),
    )


def test_sealed_exports_preserve_release_rehearsal_and_storage_contracts(monkeypatch):
    """Exact bytes, all pages and ordered text retain every sealed-output proof."""
    statement = _equistar_statement()
    equistar = {
        "external_party": "Equistar",
        "supported_statement": statement.event.description,
        "timing": "01/2025",
        "timing_precision": "month",
        "statement_type": "Commitment",
        "commitment_scope": "Scope not yet known",
        "open_status": "Open · past due",
        "internal_owner": statement.plan.internal_owner,
        "next_action": statement.plan.next_action,
        "action_due": "Date not yet known (date not yet known)",
        "milestone_impact": "Not applicable",
    }
    legacy_labels = {
        "external_party": "External Party",
        "supported_statement": "Supported statement",
        "internal_owner": "Internal Owner",
        "next_action": "Next Action",
        "action_due": "Action Due",
        "milestone_impact": "Milestone Impact",
    }
    altered = SimpleNamespace(
        event=statement.event,
        timings=statement.timings,
        plan=SimpleNamespace(
            **{**vars(statement.plan), "next_action": "Confirm the revised plan"}
        ),
    )
    manifest = json.loads((SEALED_EXPORTS / "manifest.json").read_text())
    frozen_exports = []
    for name, artifact_digest, text_digest, content_digest in (
        (
            "pass-1-approved-export.pdf",
            "eb54a8615cccd989f951ed63ae5a249c93ee68b9ef2559c65c9dbf2d6cbcef14",
            "f33c974f352167aa9e8560e4a32b1147a016491beefc5a82af6f2f7c513a439c",
            "d7dde27a4d4f3003003e536522377ca220cccaf21b53dce50b1d7a4ca5e19882",
        ),
        (
            "pass-2-approved-export.pdf",
            "59ebf093c6488fa552656029ec83bed5521d7347c38b33a2db2cc5386888010d",
            "6addb7e679afd9eb9c968abb5107a60d77ac44435c26665e8942dc97b6a891b9",
            "8d4cc80caced8b8719ea05390fd9e917d33817befb0a8246cd84e29389c1ee69",
        ),
    ):
        raw = (SEALED_EXPORTS / name).read_bytes()
        assert sha256(raw).hexdigest() == manifest["files"][name]["sha256"]
        read_once = _ReadSealedPdfOnce(raw)
        assert len(read_once.reader.pages) == 77
        visible = report_release._normalized_visible_text(
            "\n".join(page.extract_text() for page in read_once.reader.pages)
        )
        # These digests cover every page and its ordered text, so missing pages,
        # lost whitespace, and reordered rows fail independently of the consumers.
        assert sha256(visible.encode()).hexdigest() == text_digest
        for field_id, label in legacy_labels.items():
            assert visible.count(
                report_release._normalized_visible_text(f"{label} {equistar[field_id]}")
            ) == 1

        with monkeypatch.context() as readers:
            for consumer in (
                report_release,
                sh99_coordinator_rehearsal,
                storage_baseline,
            ):
                readers.setattr(consumer, "PdfReader", read_once)

            # Current release labels must refuse this legacy-labelled export.
            with pytest.raises(
                report_release.ReleaseRefusal,
                match="PDF does not contain its frozen External Party statement fields",
            ):
                report_release._validate_party_statement_pdf_context(
                    raw, {"party_statement_display": [{"report_fields": equistar}]}
                )

            # The retained legacy reading passes, but cannot support changed facts.
            text = sh99_coordinator_rehearsal._require_report_pdf_contents(
                raw, (statement,)
            )
            assert "Equistar" in text and text.count("EXTERNAL PARTY COMMITMENTS") == 1
            with pytest.raises(
                ValueError,
                match="omits required Report fields: Confirm the revised plan",
            ):
                sh99_coordinator_rehearsal._require_report_pdf_contents(raw, (altered,))

            frozen = storage_baseline._freeze_pdf(SEALED_EXPORTS / name)
        assert not read_once.invalid_requests
        assert read_once.decoded_pages == 77
        assert frozen["artifact"]["sha256"] == artifact_digest
        assert frozen["content"]["page_count"] == 77
        assert frozen["sha256"] == content_digest
        frozen_exports.append(frozen)

    first, second = frozen_exports
    stamp = re.compile(r"Generated .*? UTC")
    assert stamp.sub("Generated <stamp> UTC", first["content"]["pages"][0]["text"]) == (
        stamp.sub("Generated <stamp> UTC", second["content"]["pages"][0]["text"])
    )
    assert first["content"]["pages"][0]["text"].startswith(
        "Readiness — SH 99 Grand Parkway Segment B-1 Generated 2026-08-27 00:57 UTC"
    )


def test_report_consumers_refuse_unreadable_bytes_without_a_cached_reader():
    with pytest.raises(report_release.ReleaseRefusal, match="cannot be read"):
        report_release._validate_party_statement_pdf_context(
            b"not a pdf", {"party_statement_display": [{"report_fields": {}}]}
        )
    with pytest.raises(ValueError, match="released PDF bytes are not readable"):
        sh99_coordinator_rehearsal._require_report_pdf_contents(
            b"not a pdf", (_equistar_statement(),)
        )


def test_read_once_cannot_supply_cached_text_for_other_bytes_or_options():
    fixture = PdfFixture()
    fixture.add_page().text((72, 72), "One real reading")
    raw = fixture.tobytes()
    read_once = _ReadSealedPdfOnce(raw)
    page = read_once(BytesIO(raw)).pages[0]
    assert "One real reading" in page.extract_text()
    assert "One real reading" in page.extract_text()
    assert read_once.decoded_pages == 1
    with pytest.raises(AssertionError, match="different PDF bytes"):
        read_once(BytesIO(raw[:-10]))
    with pytest.raises(AssertionError, match="reader options"):
        read_once(BytesIO(raw), strict=True)
    with pytest.raises(AssertionError, match="text extraction options"):
        page.extract_text(extraction_mode="layout")
    assert len(read_once.invalid_requests) == 3
    assert read_once.decoded_pages == 1
