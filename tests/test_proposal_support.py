"""A test fixture's proposal is an extractor's proposal, or it proves nothing."""

from __future__ import annotations

from corridor.candidates import propose
from corridor.models import Document
from corridor.vocabulary import dedupe_hint

from proposal_support import PROMPT_VERSION, proposal


FIELDS = {
    "utility_id": "FOC1-1",
    "external_org": "AT&T Texas (SWBT)",
    "utility_type": "Telecom",
    "station_from": "1149+00",
    "station_to": "1153+17",
}
QUOTE = "FOC1-1 AT&T Texas (SWBT) Telecom 1149+00 1153+17"


def _document() -> Document:
    """One unsaved Document. `propose` reads its two identifiers and nothing else."""

    return Document(
        project_id=7,
        sha256="a" * 64,
        filename="nhhip-seg3c2-utilities-inventory.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )


def test_the_defaults_delegate_the_whole_payload_to_the_extractors_constructor():
    """The seam supplies arguments, never keys.

    A key added to `candidates.propose` has to reach every test that builds a
    proposal here without any of them being edited -- which is only true while
    this module writes no part of the payload itself. Comparing against
    `propose`'s own output is what says so: a hand-written copy of today's
    shape would pass an assertion that listed today's keys and would fail this
    one the moment the extractors' payload moved.
    """

    document = _document()

    built = proposal(document, fields=FIELDS, quote=QUOTE)
    directly = propose(
        document,
        kind="dependency",
        fields=FIELDS,
        page_no=1,
        quote=QUOTE,
        quote_verified=True,
        whole_row=True,
        confidence=1.0,
        prompt_version=PROMPT_VERSION,
        dedupe=dedupe_hint(FIELDS),
        text_source="text_layer",
    )

    assert built.payload_json == directly.payload_json
    assert built.citations_verified == directly.citations_verified
    assert built.kind == directly.kind
    assert built.source_pages == directly.source_pages


def test_the_three_keys_the_hand_written_payloads_omitted_are_carried():
    """`unmapped_columns`, `tier` and `text_source`: of the test tree's 105
    hand-written payloads, four named the first, six the second and twenty the
    third, while `corridor.facts` reads all three. A caller states them here
    and they arrive."""

    candidate = proposal(
        _document(),
        fields=FIELDS,
        quote=QUOTE,
        tier="transcribe",
        text_source="ocr",
        unmapped=["Retain and Protect"],
    )

    assert candidate.payload_json["tier"] == "transcribe"
    assert candidate.payload_json["text_source"] == "ocr"
    assert candidate.payload_json["unmapped_columns"] == ["Retain and Protect"]
