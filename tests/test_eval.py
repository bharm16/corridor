import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.eval import (
    GoldRecord,
    GoldSet,
    MalformedGoldSet,
    evaluate,
    extracted_documents,
    gold_for_documents,
    gold_from_page_text,
    load_gold,
    render,
)
from corridor.eval import _SEQUENTIAL_ID, _UTILITY_ID
from corridor.models import Candidate, DocPage, Document, Project


def scanned(*records):
    """An enumeration read off the page text.

    It could look for the two known row shapes and nothing else, which is
    what `gold_for_documents` produces over a project whose documents
    print both.
    """
    return GoldSet(tuple(records), (_UTILITY_ID, _SEQUENTIAL_ID))


def authored(*records):
    """A hand-authored enumeration: it read the grid, so it sees every row."""
    return GoldSet(tuple(records), None)

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


def add_page(session, document, page_no, text):
    page = DocPage(document_id=document.id, page_no=page_no, text=text)
    session.add(page)
    session.flush()
    return page


def make_candidate(session, project, document, uid, page=1, prompt_version="txdot_ucm_v1"):
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
        prompt_version=prompt_version,
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
    assert gold.records[2].page == 2
    # It read the grid, so no id is beyond it.
    assert gold.reach is None


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


def test_the_enumeration_covers_every_document_not_just_one(session, project, document):
    """Five revisions of the same matrix all number their pages from 1.

    Keyed by page number alone they collapse onto each other and the
    enumeration silently shrinks to one document's worth — which reads as
    a precision collapse, because every row of the four documents that
    were overwritten becomes spurious. Recall against a gold set that is
    missing three quarters of its rows is not a number.
    """
    revision = Document(
        project_id=project.id,
        sha256="a" * 64,
        filename="matrix-earlier.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(revision)
    session.flush()
    add_page(session, document, 1, "E92 \nCenterPoint Energy \n")
    add_page(session, revision, 1, "W139 \nCity of Houston \n")

    gold = gold_for_documents(session, [document.id, revision.id])

    assert sorted(g.source_ref for g in gold) == ["E92", "W139"]


# ------------------------------------------------------------------- scoring


def test_recall_and_precision_against_the_enumeration(session, project, document):
    for uid in ("FOC1-1", "FOC1-2"):
        make_candidate(session, project, document, uid)

    gold = scanned(GoldRecord("FOC1-1"), GoldRecord("FOC1-2"), GoldRecord("E92"))
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
    """`FOC9-9` rather than an arbitrary string: spurious now means the
    enumeration *could* have found this id and did not, which is a real
    disagreement. An id of a shape it cannot look for is unrecognised
    instead (#90), and conflating the two is the defect."""
    make_candidate(session, project, document, "FOC1-1")
    make_candidate(session, project, document, "FOC9-9")

    result = evaluate(session, slug=project.slug, gold=scanned(GoldRecord("FOC1-1")))
    assert result.spurious == ["FOC9-9"]
    assert result.precision == 0.5


def test_a_repeated_id_is_counted_twice_not_collapsed(session, project, document):
    """Ids repeat within a revision — 47 reused in one Project A matrix.

    Collapsing to a set would let a dropped row hide behind its twin and
    report full recall for half the data.
    """
    make_candidate(session, project, document, "FOC14-69")

    gold = scanned(GoldRecord("FOC14-69"), GoldRecord("FOC14-69"))
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
    result = evaluate(session, slug=project.slug, gold=scanned(GoldRecord("FOC1-1")))
    assert result.prompt_versions == {"txdot_ucm_v1": 1}
    assert result.models == {"deterministic": 1}


def test_the_output_states_what_it_cannot_measure(session, project, document):
    """A matrix is not ground truth for its own omissions."""
    make_candidate(session, project, document, "FOC1-1")
    result = evaluate(session, slug=project.slug, gold=scanned(GoldRecord("FOC1-1")))
    assert "omissions" in render(result)
    assert result.recall == 1.0


def test_an_empty_ledger_scores_zero_not_one(session, project, document):
    """Nothing extracted is 0% recall, never a vacuous 100%."""
    result = evaluate(session, slug=project.slug, gold=scanned(GoldRecord("FOC1-1")))
    assert result.recall == 0.0
    assert result.precision == 0.0
    assert result.missing == ["FOC1-1"]


# ------------------------------------------- one extractor at a time (#68)


def test_two_extractors_on_one_project_are_scored_separately(
    session, project, document
):
    """Both paths' Candidates coexist while the migration is undecided.

    Pooled they are meaningless — every row appears twice, so recall reads
    100% and precision reads 50% no matter how either extractor did.
    """
    make_candidate(session, project, document, "FOC1-1")
    make_candidate(session, project, document, "FOC1-1", prompt_version="matrix_vision_v1")
    make_candidate(session, project, document, "FOC9-9", prompt_version="matrix_vision_v1")

    old = evaluate(
        session, slug=project.slug, gold=scanned(GoldRecord("FOC1-1")),
        prompt_version="txdot_ucm_v1",
    )
    new = evaluate(
        session, slug=project.slug, gold=scanned(GoldRecord("FOC1-1")),
        prompt_version="matrix_vision_v1",
    )

    assert (old.extracted_total, old.precision) == (1, 1.0)
    assert (new.extracted_total, new.precision) == (2, 0.5)
    assert new.spurious == ["FOC9-9"]


def test_the_documents_scored_are_the_ones_that_extractor_read(
    session, project, document
):
    """Otherwise the comparison is not "on the same documents".

    A revision the new path has not run over yet would count every one of
    its rows as missed, and report the backlog as a recall failure.
    """
    other = Document(
        project_id=project.id,
        sha256="c" * 64,
        filename="matrix-other.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(other)
    session.flush()
    make_candidate(session, project, document, "FOC1-1")
    make_candidate(session, project, other, "E92")
    make_candidate(session, project, document, "FOC1-1", prompt_version="matrix_vision_v1")

    assert extracted_documents(session, project.id) == {document.id, other.id}
    assert extracted_documents(
        session, project.id, prompt_version="matrix_vision_v1"
    ) == {document.id}


# --------------------------------------------- enumerating unfamiliar layouts


def test_a_sequential_layout_is_enumerated_when_the_prefixed_one_finds_nothing():
    """FDOT numbers its conflicts 1, 2, 3 and follows each with stationing.

    A bare integer alone on a line is otherwise indistinguishable from an
    offset or a sheet number, so this shape is tried only where the
    prefixed-id shape found nothing at all.
    """
    gold = gold_from_page_text(
        {
            1: (
                "Conflict # \nStation Begin \nStation End \nOffset \n"
                "1 \n203+40.00 \n206+40.00 \n30.00' RT. \nBTV, Size UNK \n"
                "2 \n206+93.00 \n206+93.00 \n54.00' RT. \nBTV Pedestal \n"
            )
        }
    )

    assert [g.source_ref for g in gold] == ["1", "2"]


def test_a_prefixed_layout_never_falls_through_to_the_sequential_one():
    """The fallback would be ruinous here: TxDOT prints an offset after
    every station, so `303 \\n1153+17` reads as a row that does not exist.
    Measured at ~600 phantom rows per revision, which is why the shape is
    a fallback and not a second pattern applied alongside the first."""
    gold = gold_from_page_text(
        {
            1: (
                "FOC1-1 \nAT&T Texas (SWBT) \nTelecom \n1149+00 \n303 \n"
                "1153+17 \n309 \nR \n"
            )
        }
    )

    assert [g.source_ref for g in gold] == ["FOC1-1"]


def test_a_page_of_numbers_with_no_stationing_enumerates_nothing():
    """The sequential shape needs the station to anchor it, or every sheet
    number on the page becomes a row."""
    assert gold_from_page_text({1: "12 \n13 \n14 \nSheet index \n"}).records == ()


# ------------------------------------- a measurement that cannot be made


def test_an_empty_enumeration_is_not_a_score(session, project, document):
    """0% recall says the extractor found nothing. An empty gold set says
    we could not check. Reporting the second as the first is the failure
    this guards — it read as total extraction failure on a document that
    had in fact extracted 66 correct rows."""
    make_candidate(session, project, document, "1")

    result = evaluate(session, slug=project.slug, gold=scanned())

    assert result.unmeasurable is True
    rendered = render(result)
    assert "could not be enumerated" in rendered
    assert "0.0%" not in rendered


def test_a_real_enumeration_is_measurable_even_when_recall_is_zero(
    session, project, document
):
    """A genuine 0% must still be reported as 0%."""
    result = evaluate(session, slug=project.slug, gold=scanned(GoldRecord("FOC1-1")))

    assert result.unmeasurable is False
    assert result.recall == 0.0
    assert "0.0%" in render(result)


# ------------------------------------------------- critical recall (#87)


CRITICAL_GOLD = """source_ref,page,critical
FOC1-1,1,yes
FOC1-2,1,no
E92,2,
"""


def test_a_gold_set_can_declare_which_rows_are_critical(tmp_path):
    path = tmp_path / "gold.csv"
    path.write_text(CRITICAL_GOLD)

    gold = load_gold(path)

    assert [g.critical for g in gold] == [True, False, False]


def test_a_gold_set_without_the_column_loads_exactly_as_before(tmp_path):
    """The loader's existing contract is unchanged; `critical` is optional.

    `None` rather than `False`: a gold set that never mentions criticality
    has not judged these rows non-critical, and the difference is what
    separates NOT MEASURED from a recall of zero.
    """
    path = tmp_path / "gold.csv"
    path.write_text(GOLD)

    gold = load_gold(path)

    assert [g.source_ref for g in gold] == ["FOC1-1", "FOC1-2", "E92"]
    assert [g.critical for g in gold] == [None, None, None]


def test_an_unreadable_criticality_label_is_refused(tmp_path):
    """A typo must not quietly mean "not critical".

    Recall's denominator is the labeled critical set. A label the loader
    does not understand, silently read as false, shrinks that denominator —
    which reports high recall precisely because a row went missing from the
    measurement. Same failure ADR-0007 rejects reviewer-assigned
    criticality for.
    """
    path = tmp_path / "gold.csv"
    path.write_text("source_ref,critical\nFOC1-1,ture\n")

    with pytest.raises(MalformedGoldSet, match="ture"):
        load_gold(path)


def test_critical_recall_is_scored_from_the_gold_labels(session, project, document):
    """Of the rows the gold set calls critical, how many were found.

    The extractor classifies nothing — this is answered by the labels
    alone, which is why it does not wait on the Ledger-side work (#86).
    """
    make_candidate(session, project, document, "FOC1-1")
    make_candidate(session, project, document, "E92")

    gold = scanned(
        GoldRecord("FOC1-1", critical=True),
        GoldRecord("FOC1-2", critical=True),
        GoldRecord("E92"),
    )
    result = evaluate(session, slug=project.slug, gold=gold)

    assert result.critical_gold_total == 2
    assert result.critical_matched == 1
    assert result.critical_recall == 0.5
    assert result.critical_missing == ["FOC1-2"]
    # The overall numbers are untouched by the labels.
    assert result.recall == pytest.approx(2 / 3)


def test_a_gold_set_labeling_nothing_critical_is_not_a_recall_of_zero(
    session, project, document
):
    """#82's precedent, one metric further in.

    A gate reporting 0% because nobody labeled anything reads as total
    extraction failure — and on a holdout ADR-0008 spends once, that is
    unrecoverable.
    """
    make_candidate(session, project, document, "FOC1-1")

    result = evaluate(
        session, slug=project.slug, gold=scanned(GoldRecord("FOC1-1", critical=False))
    )

    assert result.critical_unmeasurable is True
    assert "critical recall  NOT MEASURED" in render(result)


def test_a_gold_set_with_no_critical_column_says_so_rather_than_erroring(
    session, project, document
):
    """Everything else scores unchanged, and the absence is stated."""
    make_candidate(session, project, document, "FOC1-1")

    result = evaluate(session, slug=project.slug, gold=scanned(GoldRecord("FOC1-1")))
    rendered = render(result)

    assert result.unmeasurable is False
    assert result.recall == 1.0
    assert result.critical_unmeasurable is True
    assert "does not label criticality" in rendered


def test_a_genuine_critical_recall_of_zero_is_reported_as_zero(
    session, project, document
):
    """The labeled set is not empty; the extractor simply missed it."""
    make_candidate(session, project, document, "E92")

    result = evaluate(
        session, slug=project.slug, gold=scanned(GoldRecord("FOC1-1", critical=True))
    )

    assert result.critical_unmeasurable is False
    assert result.critical_recall == 0.0
    assert "critical recall 0.0%" in render(result)


def test_critical_recall_is_scoped_to_one_extractor(session, project, document):
    """The gate's per-prompt-version scoping applies here too, or the two
    extraction paths stop being comparable on the metric that gates M7."""
    make_candidate(session, project, document, "FOC1-1")
    make_candidate(
        session, project, document, "FOC1-2", prompt_version="matrix_tiered_v1"
    )

    gold = scanned(GoldRecord("FOC1-1", critical=True), GoldRecord("FOC1-2", critical=True))

    old = evaluate(
        session, slug=project.slug, gold=gold, prompt_version="txdot_ucm_v1"
    )
    new = evaluate(
        session, slug=project.slug, gold=gold, prompt_version="matrix_tiered_v1"
    )

    assert (old.critical_matched, old.critical_recall) == (1, 0.5)
    assert (new.critical_matched, new.critical_recall) == (1, 0.5)
    assert old.critical_missing == ["FOC1-2"]
    assert new.critical_missing == ["FOC1-1"]


def test_the_page_text_enumeration_labels_nothing_critical(session, project, document):
    """Criticality is asserted by the document's own signal column, and
    `gold_from_page_text` reads ids off the text stream. It cannot know, so
    it must not claim — the default run reports critical recall as
    unmeasurable rather than as zero."""
    add_page(session, document, 1, "FOC1-1 \nAT&T Texas (SWBT) \nTelecom \n")
    make_candidate(session, project, document, "FOC1-1")

    gold = gold_for_documents(session, {document.id})
    result = evaluate(session, slug=project.slug, gold=gold)

    assert [g.source_ref for g in gold] == ["FOC1-1"]
    assert result.recall == 1.0
    assert result.critical_unmeasurable is True


# ------------------------------- what the enumeration could not read (#90)


def test_an_id_shape_the_enumeration_cannot_read_is_not_spurious(
    session, project, document
):
    """The defect this ticket exists for.

    SH 99 numbers its conflicts `C1`, `PL4`, `OH C45`, `CP2`, `ET1` — none
    of which `_UTILITY_ID` carries. The enumeration recognised 504 of its
    1,401 rows and reported the other 897 as spurious extractions, which
    printed as `precision 36.0%`. That reads as "the extractor invented two
    thirds of these rows". It invented none of them; the enumeration could
    not see them.
    """
    make_candidate(session, project, document, "FOC1-1")
    make_candidate(session, project, document, "C448")
    make_candidate(session, project, document, "OH C45")

    result = evaluate(session, slug=project.slug, gold=scanned(GoldRecord("FOC1-1")))

    assert result.spurious == []
    assert result.unrecognized == ["C448", "OH C45"]
    assert result.recognized_total == 1


def test_a_recognisable_id_the_gold_set_lacks_is_still_spurious(
    session, project, document
):
    """Recognition is about the id's *shape*, not whether it matched.

    An id the enumeration knows how to look for and did not find is a real
    disagreement, and this change must not launder those away — that is the
    detection capability the whole metric exists for.
    """
    make_candidate(session, project, document, "FOC1-1")
    make_candidate(session, project, document, "FOC9-9")

    result = evaluate(session, slug=project.slug, gold=scanned(GoldRecord("FOC1-1")))

    assert result.spurious == ["FOC9-9"]
    assert result.unrecognized == []


def test_coverage_is_the_share_of_extracted_rows_the_enumeration_can_read(
    session, project, document
):
    for uid in ("FOC1-1", "FOC1-2", "C1", "C2"):
        make_candidate(session, project, document, uid)

    result = evaluate(
        session,
        slug=project.slug,
        gold=scanned(GoldRecord("FOC1-1"), GoldRecord("FOC1-2")),
    )

    assert result.recognized_total == 2
    assert result.extracted_total == 4
    assert result.coverage == 0.5
    assert result.partial_coverage is True


def test_precision_is_scoped_to_the_rows_the_enumeration_could_adjudicate(
    session, project, document
):
    """Over the recognised rows, and never presented as a whole-document
    figure. The bare number is what was misread."""
    make_candidate(session, project, document, "FOC1-1")
    make_candidate(session, project, document, "C448")

    result = evaluate(session, slug=project.slug, gold=scanned(GoldRecord("FOC1-1")))
    rendered = render(result)

    assert result.precision_over_recognized == 1.0
    assert "NOT MEASURED as a whole-document figure" in rendered
    assert "over the 1 recognised rows" in rendered
    assert "unrecognised, not spurious" in rendered


def test_full_coverage_reports_precision_exactly_as_before(
    session, project, document
):
    """Project B recognises all 66 of its rows, and its published numbers
    must not move because of this change."""
    make_candidate(session, project, document, "FOC1-1")
    make_candidate(session, project, document, "FOC9-9")

    result = evaluate(session, slug=project.slug, gold=scanned(GoldRecord("FOC1-1")))
    rendered = render(result)

    assert result.coverage == 1.0
    assert result.partial_coverage is False
    assert result.precision == 0.5
    assert "precision 50.0%" in rendered
    assert "NOT MEASURED as a whole-document figure" not in rendered


def test_recall_is_untouched_by_what_the_enumeration_cannot_read(
    session, project, document
):
    """The claim this change rests on: only precision's accounting moves."""
    make_candidate(session, project, document, "FOC1-1")
    make_candidate(session, project, document, "C448")

    gold = scanned(GoldRecord("FOC1-1"), GoldRecord("E92"))
    result = evaluate(session, slug=project.slug, gold=gold)

    assert result.gold_total == 2
    assert result.matched == 1
    assert result.missing == ["E92"]
    assert result.recall == 0.5


def test_a_repeated_unreadable_id_is_counted_every_time(
    session, project, document
):
    """No multiplicity ceiling, deliberately.

    An extractor emitting one unknown-shape id five times from a page
    printing it once should show five unrecognised rows, not one. Keying
    this on a set would excuse exactly the duplication the count exists to
    surface.
    """
    for _ in range(3):
        make_candidate(session, project, document, "C448")

    result = evaluate(session, slug=project.slug, gold=scanned(GoldRecord("FOC1-1")))

    assert result.unrecognized == ["C448", "C448", "C448"]
    assert result.recognized_total == 0


def test_nothing_recognisable_is_still_not_a_precision_of_zero(
    session, project, document
):
    """#82's rule, reached by the other road: an enumeration that can read
    none of a document reports no precision rather than 0%."""
    make_candidate(session, project, document, "C448")

    result = evaluate(session, slug=project.slug, gold=scanned(GoldRecord("FOC1-1")))
    rendered = render(result)

    assert result.recognized_total == 0
    assert result.coverage == 0.0
    assert result.precision_over_recognized is None
    # Coverage genuinely is 0.0% and says so. What must not appear is a
    # precision of zero — the extractor is not being told it invented this
    # row, only that nothing here could check it.
    assert "coverage 0.0%" in rendered
    assert "precision  NOT MEASURED" in rendered
    assert "nothing to score" in rendered
    assert "precision 0.0%" not in rendered


def test_a_hand_authored_gold_set_makes_every_surplus_row_spurious(
    session, project, document
):
    """The M7 holdout's own ids match no shape this scanner knows.

    `gold/wsdot-9540.machine.csv` enumerates the whole document by grid
    reading, and `PSEN-G-1001` is not a `_UTILITY_ID` or a
    `_SEQUENTIAL_ID`. Under the old union-of-both-shapes rule one
    genuinely spurious row would have printed `precision NOT MEASURED as
    a whole-document figure` and written `"precision": null` into the
    gate artifact — on a holdout that is spent once (ADR-0008).
    """
    make_candidate(session, project, document, "PSEN-G-1001")
    make_candidate(session, project, document, "TCPR-P-1043")

    result = evaluate(
        session,
        slug=project.slug,
        gold=authored(GoldRecord("PSEN-G-1001")),
    )

    assert result.unrecognized == []
    assert result.spurious == ["TCPR-P-1043"]
    assert result.coverage == 1.0
    assert result.partial_coverage is False
    assert result.precision == 0.5


def test_a_matched_row_is_within_reach_by_definition(session, project, document):
    """Coverage claimed every matched row was an id shape it could read.

    On the holdout that claim was false for all 192 of them — matched rows
    were never shape-tested at all, so the headline stated something
    nobody had checked.
    """
    make_candidate(session, project, document, "PSEN-G-1001")

    result = evaluate(
        session, slug=project.slug, gold=authored(GoldRecord("PSEN-G-1001"))
    )

    assert result.matched == 1
    assert result.coverage == 1.0
    assert "every one of 1 extracted rows" in render(result)


def test_an_enumeration_reaches_only_the_shape_it_actually_used(
    session, project, document
):
    """#90's defect in the opposite direction.

    `gold_from_page_text` tries the two shapes in order and stops at the
    first that finds anything, because their discriminators are inverted.
    A prefixed layout therefore never runs the sequential shape — so `303`
    is an id that enumeration provably could not have found, and calling
    it spurious charges the extractor for the scanner's blind spot.
    """
    page = "FOC1-1 \nAT&T Texas \nFOC1-2 \nCenterPoint \n"
    gold = gold_from_page_text({1: page})
    assert [r.source_ref for r in gold] == ["FOC1-1", "FOC1-2"]

    assert gold.can_adjudicate("FOC9-9") is True
    assert gold.can_adjudicate("303") is False
    assert gold.can_adjudicate("OH C45") is False
    assert gold.can_adjudicate("") is False
