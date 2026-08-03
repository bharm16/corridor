import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.eval import (
    GoldRecord,
    MalformedGoldSet,
    evaluate,
    gold_from_page_text,
    load_gold,
    render,
)
from corridor.models import Candidate, Document, Project

GOLD = """source_ref,page
FOC1-1,1
FOC1-2,1
E92,2
"""


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
def project(session):
    p = Project(slug="eval-test", name="Eval Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


@pytest.fixture
def document(session, project):
    d = Document(
        project_id=project.id,
        sha256="f" * 64,
        filename="matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=2,
    )
    session.add(d)
    session.flush()
    return d


def make_candidate(session, project, document, uid, page=1):
    c = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": {"utility_id": uid, "external_org": "MT AT&T"},
            "citations": [{"document_id": document.id, "page": page, "quote": uid,
                           "verified": True, "whole_row": True}],
            "confidence": 1.0,
        },
        source_document_id=document.id,
        source_pages=[page],
        confidence=1.0,
        prompt_version="txdot_ucm_v1",
        citations_verified=True,
    )
    session.add(c)
    session.flush()
    return c


# ----------------------------------------------------------------- gold sets


def test_a_gold_set_loads(tmp_path):
    path = tmp_path / "gold.csv"
    path.write_text(GOLD)
    gold = load_gold(path)
    assert [g.source_ref for g in gold] == ["FOC1-1", "FOC1-2", "E92"]
    assert gold[2].page == 2


def test_a_gold_set_without_source_ref_is_refused(tmp_path):
    """Guessing at the key would produce a number rather than an error."""
    path = tmp_path / "gold.csv"
    path.write_text("utility,page\nFOC1-1,1\n")
    with pytest.raises(MalformedGoldSet, match="source_ref"):
        load_gold(path)


def test_an_empty_gold_set_is_refused(tmp_path):
    path = tmp_path / "gold.csv"
    path.write_text("source_ref,page\n")
    with pytest.raises(MalformedGoldSet):
        load_gold(path)


def test_an_enumeration_can_be_read_off_the_page_text():
    """The independent path: text stream, not `find_tables()`.

    PyMuPDF emits these tables cell per line, so a row's id sits alone on
    its line with the owner beneath it.
    """
    gold = gold_from_page_text(
        {
            1: "FOC1-23 \nAT&T Texas (SWBT) \nTelecom \n1149+00 \n303 \nR \nY \nB \n",
            2: "E92 \nCenterPoint Energy \nElectric \nW139 \nCity of Houston \nWater \n",
        }
    )
    refs = [g.source_ref for g in gold]
    assert refs == ["FOC1-23", "E92", "W139"]
    # Stationing and offsets are not ids.
    assert "1149" not in refs and "303" not in refs


def test_a_notes_citation_is_not_counted_as_a_row():
    """The Notes column cites other conflicts, and the text stream wraps.

    `Nance Street, ties into / FOC1-6 / 1139+43` leaves the id alone on a
    line exactly as a real row does. What separates them is what follows:
    a row is followed by its owner, a citation by stationing. Counting
    these inflates the enumeration and charges the extractor for rows that
    were never there — it cost 136 phantom misses before the check.
    """
    gold = gold_from_page_text(
        {
            1: (
                "Nance Street, ties into \nFOC1-6 \n1139+43 \n183 \n"
                "FOC1-9 \nAT&T Texas (SWBT) \nTelecom \n"
            )
        }
    )
    assert [g.source_ref for g in gold] == ["FOC1-9"]


def test_an_inline_mention_is_not_counted_as_a_row():
    gold = gold_from_page_text(
        {1: "appears to be part of FOC1-105 \nFOC1-102 \nAT&T Texas (SWBT) \n"}
    )
    assert [g.source_ref for g in gold] == ["FOC1-102"]


# ------------------------------------------------------------------- scoring


def test_recall_and_precision_against_the_enumeration(session, project, document):
    for uid in ("FOC1-1", "FOC1-2"):
        make_candidate(session, project, document, uid)

    gold = [GoldRecord("FOC1-1"), GoldRecord("FOC1-2"), GoldRecord("E92")]
    result = evaluate(session, slug=project.slug, gold=gold)

    assert result.matched == 2
    assert result.gold_total == 3
    assert result.extracted_total == 2
    assert result.recall == pytest.approx(2 / 3)
    assert result.precision == 1.0
    assert result.missing == ["E92"]
    assert result.spurious == []


def test_an_extracted_row_that_is_not_in_the_enumeration_is_spurious(
    session, project, document
):
    make_candidate(session, project, document, "FOC1-1")
    make_candidate(session, project, document, "GHOST-1")

    result = evaluate(session, slug=project.slug, gold=[GoldRecord("FOC1-1")])
    assert result.spurious == ["GHOST-1"]
    assert result.precision == 0.5


def test_a_repeated_id_is_counted_twice_not_collapsed(session, project, document):
    """Ids repeat within a revision — 47 reused in one Project A matrix.

    Collapsing to a set would let a dropped row hide behind its twin and
    report full recall for half the data.
    """
    make_candidate(session, project, document, "FOC14-69")

    gold = [GoldRecord("FOC14-69"), GoldRecord("FOC14-69")]
    result = evaluate(session, slug=project.slug, gold=gold)

    assert result.gold_total == 2
    assert result.matched == 1
    assert result.recall == 0.5
    assert result.missing == ["FOC14-69"]


def test_the_versions_that_produced_the_number_are_recorded(
    session, project, document
):
    """A figure that moved must be attributable to code or to data."""
    make_candidate(session, project, document, "FOC1-1")
    result = evaluate(session, slug=project.slug, gold=[GoldRecord("FOC1-1")])
    assert result.prompt_versions == {"txdot_ucm_v1": 1}
    assert result.models == {"deterministic": 1}


def test_the_output_states_what_it_cannot_measure(session, project, document):
    """A matrix is not ground truth for its own omissions."""
    make_candidate(session, project, document, "FOC1-1")
    result = evaluate(session, slug=project.slug, gold=[GoldRecord("FOC1-1")])
    assert "omissions" in render(result)
    assert result.recall == 1.0


def test_an_empty_ledger_scores_zero_not_one(session, project, document):
    """Nothing extracted is 0% recall, never a vacuous 100%."""
    result = evaluate(session, slug=project.slug, gold=[GoldRecord("FOC1-1")])
    assert result.recall == 0.0
    assert result.precision == 0.0
    assert result.missing == ["FOC1-1"]
