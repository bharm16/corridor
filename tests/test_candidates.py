import pytest

from corridor.adjudicate import accept_candidate
from corridor.candidates import citations_verified, dedupe_hint, propose
from corridor.db import Session, engine
from corridor.geometry import dedupe_hint as matrix_hint
from corridor.models import DocPage, Document, Project

PAYLOAD_KEYS = {
    "kind",
    "fields",
    "citations",
    "confidence",
    "unverified_fields",
    "unmapped_columns",
    "low_confidence_tokens",
    "tier",
    "dedupe_hint",
    "text_source",
}


@pytest.fixture
def session():
    connection = engine.connect()
    trans = connection.begin()
    s = Session(bind=connection)
    yield s
    s.close()
    trans.rollback()
    connection.close()


@pytest.fixture
def document(session):
    project = Project(slug="cand-test", name="Candidate Test", is_synthetic=True)
    session.add(project)
    session.flush()
    doc = Document(
        project_id=project.id,
        sha256="d" * 64,
        filename="matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(doc)
    session.flush()
    session.add(
        DocPage(
            document_id=doc.id,
            page_no=1,
            text="FOC1-1 AT&T Texas Telecom",
            image_path="/tmp/corridor-missing-page.png",
        )
    )
    session.flush()
    return doc


def _propose(document, **kwargs):
    defaults = dict(
        kind="dependency",
        fields={"utility_id": "FOC1-1", "external_org": "AT&T Texas"},
        page_no=1,
        quote="FOC1-1 AT&T Texas Telecom",
        quote_verified=True,
        whole_row=True,
        confidence=1.0,
        prompt_version="test_v1",
        dedupe="AT&T Texas|Telecom|1149+00-1153+17",
        text_source="text",
    )
    return propose(document, **{**defaults, **kwargs})


# ------------------------------------------------------------- the key set


def test_every_proposal_carries_the_same_keys(document):
    """A reader should not have to know which extractor produced a row.

    The four payload literals disagreed on six keys, so `web/queue.py` read
    each through `or []` and could not tell an absent key from an empty
    value.
    """
    assert set(_propose(document).payload_json) == PAYLOAD_KEYS


def test_a_prose_proposal_carries_the_matrix_keys_empty(document):
    """Empty is a statement; missing was a guess.

    An obligation read out of an agreement is the model's phrasing of a
    paragraph, so no field value is expected to appear verbatim and
    `unverified_fields` is genuinely empty rather than unasked.
    """
    payload = _propose(document, kind="dependency", tier=None).payload_json

    assert payload["unverified_fields"] == []
    assert payload["unmapped_columns"] == []
    assert payload["low_confidence_tokens"] == []
    assert payload["tier"] is None


# ------------------------------------------------------ citations_verified


def test_a_quote_that_is_not_on_the_page_is_not_verified(document):
    assert _propose(document, quote_verified=False).citations_verified is False


def test_a_field_value_that_is_not_on_the_page_sinks_the_row(document):
    """`extract_sheet` and `extract_matrix` both folded this in; the prose
    extractors had no such list, and each spelled the conjunction itself."""
    candidate = _propose(document, unverified=["station_to"])

    assert candidate.citations_verified is False


def test_a_token_the_model_hesitated_on_sinks_the_row(document):
    assert _propose(document, low_confidence=["7"]).citations_verified is False


def test_a_proposal_with_nothing_cited_is_not_verified():
    """`all()` over an empty list is True, which would read as verified."""
    assert citations_verified({"citations": []}) is False


def test_accepting_does_not_relax_the_verdict(session, document):
    """Acceptance recomputed this from the quote flag alone.

    A row that sank in the queue because a field value was not on its page
    came out of acceptance recorded as verified — and the queue's ordering,
    `pending_counts` and `eval` all read this column. One rule now, asked of
    the payload, so an edited payload gets the same question the extractor
    asked of the original.
    """
    candidate = _propose(document, unverified=["station_to"])
    session.add(candidate)
    session.flush()
    assert candidate.citations_verified is False

    accept_candidate(session, candidate, actor="tester")

    assert candidate.citations_verified is False


def test_accepting_a_clean_row_still_records_it_verified(session, document):
    candidate = _propose(document)
    session.add(candidate)
    session.flush()

    accept_candidate(session, candidate, actor="tester")

    assert candidate.citations_verified is True


# ------------------------------------------------------------ dedupe hints


def test_blank_parts_are_kept_so_shorter_rows_do_not_collide():
    """`merge.rank_matches` blocks on this, so dropping a blank would group
    a row missing its stationing with one that merely matches on the rest."""
    assert dedupe_hint("AT&T", "", "1149+00-1153+17") == "AT&T||1149+00-1153+17"


def test_the_matrix_hint_is_built_by_the_shared_join():
    """Which fields discriminate is per document kind; the separator is not."""
    fields = {
        "external_org": "AT&T Texas",
        "utility_type": "Telecom",
        "station_from": "1149+00",
        "station_to": "1153+17",
    }

    assert matrix_hint(fields) == dedupe_hint(
        "AT&T Texas", "Telecom", "1149+00-1153+17"
    )
