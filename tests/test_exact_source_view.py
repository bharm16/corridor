"""The exact-source view: one cited passage, opened where it was read (#831).

The customer-journey audit found that source inspection is weaker than the
displayed citations suggest: Review prints a filename, a locator and the quoted
words, and a coordinator who wants the sentence above the quoted one leaves the
product to find the file.  The pilot measures that as manual reconstruction
time, so the surrounding context is the product.

What this file proves is the part that is easy to get subtly wrong, and the
part the ticket is mostly about: **three different facts stay three different
facts.**

* The passage was looked for and found, or looked for and not found, or its
  recorded location cannot be re-read — the Source Passage Check, in the words
  ADR-0082 and ADR-0094 fixed and #600 approved.
* Nobody could look at all, because the registered bytes did not arrive. That
  is a retrieval failure in Corridor's storage, it is logged as one, and it is
  never printed as *Not found at cited location* — which would be a statement
  about a customer's document that no reader made — nor as *No cited location
  recorded*, which would be false, because the location is recorded and is
  exactly what could not be reached.
* What the source says around the passage, which is Corridor's own reading of
  it and is labelled as such, beside the original file itself.

The nearby context comes from the neighbouring Source Segments rather than from
``doc_pages``: ADR-0068 made the segment the owner of cited text, and #680
revoked ``doc_pages`` and ``page_render_derivatives`` from the web capability,
so a boundary-admitted route may not read either.  That is why a spreadsheet
citation opens as the populated cells around it and a page citation as the
passages around it.
"""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO, StringIO
import json
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import Workbook
import pytest

import corridor.web.app
from corridor import telemetry, web_boundary
from corridor.models import Document, SourceSegment
from corridor.object_storage import store_bytes
from corridor.packet_review import SourceAnswer, SourceReference
from corridor.presentation import source_passage_check_label
from corridor.principals import HumanPrincipal
from corridor.source_append import SegmentValues, append_source_segments
from corridor.source_passage_view import (
    RETRIEVAL_FAILURE_EVENT,
    SourcePassageNotFound,
    read_source_passage,
)
from corridor.web.app import app, get_human_principal, get_session

from packet_review_support import accept_baseline_fact, subject
from source_capture_support import SHEET, Rendition


TEMPLATE_ROOT = Path(corridor.web.app.__file__).resolve().parent / "templates"


COORDINATOR = HumanPrincipal("local:exact-source-coordinator")
OUTSIDER = HumanPrincipal("local:exact-source-outsider")

#: One sheet neighbourhood, appended in the workbook's own row-major order so
#: the segment ordinals are the order the cells appear in.
CELLS = (
    ("B4", "Owner"),
    ("C4", "Promised for"),
    ("D4", "Status"),
    ("B5", "Equistar"),
    ("C5", "2026-04-01"),
    ("D5", "Design"),
    ("B6", "Dominion"),
    ("C6", "2026-05-15"),
    ("D6", "Field"),
)

#: Every state the Source Passage Check can report, in its customer words. A
#: screen that is not reporting a check must print none of them.
EVERY_CHECK_STATE = tuple(
    source_passage_check_label(status)
    for status in ("valid", "invalid", "not_checked", "not_re_readable")
)


# --- the deployment and the sources under it --------------------------------


@pytest.fixture
def client(session):
    """The application on this test's transaction, as one signed-in member."""

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
    with TestClient(app) as opened:
        yield opened
    app.dependency_overrides.clear()


@pytest.fixture
def logs():
    """Capture the process log stream, then restore the deployed default."""

    stream = StringIO()
    telemetry.configure_logging(
        role=telemetry.ROLE_WORKER, environment="test", stream=stream
    )
    yield stream
    telemetry.configure_logging(role=telemetry.ROLE_WEB)


def logged(stream: StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def workbook_bytes() -> bytes:
    """One sheet holding exactly the cells above, as issued workbook bytes."""

    book = Workbook()
    sheet = book.active
    sheet.title = SHEET
    for cell, value in CELLS:
        sheet[cell] = value
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def registered_workbook(session, project):
    """A registered spreadsheet Document whose bytes really are in the store."""

    data = workbook_bytes()
    digest = sha256(data).hexdigest()
    store_bytes(data, sha256=digest, suffix=".xlsx")
    rendition = Rendition(
        session, project, "ucm-r3.xlsx", document_sha256=digest, registry_id="UCM-3"
    )
    segments = {cell: rendition.segment(value, cell=cell) for cell, value in CELLS}
    return rendition, segments, data


def registered_prose(session, project, *, bytes_in_store: bool = True):
    """A registered PDF and the retained prose passages of its first page.

    ``prose_span`` is the kind ADR-0094 retired the reader for, so the check on
    one is *Cited location cannot be re-read* whatever the bytes say. The bytes
    still matter: without them nobody could even establish that much, which is
    the distinction the storage cases below turn on.
    """

    data = b"%PDF-1.7 minutes of the coordination meeting"
    digest = sha256(data).hexdigest()
    if bytes_in_store:
        store_bytes(data, sha256=digest, suffix=".pdf")
    document = Document(
        project_id=project.id,
        sha256=digest,
        filename="coordination-minutes.pdf",
        doc_type="minutes",
        numbering_scheme="project-unique",
        pages=3,
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    passages = (
        "Equistar confirmed the relocation design is complete.",
        "The signed exhibit will be submitted by 1 April 2026.",
        "Dominion asked for the revised profile before mobilising.",
    )
    offset = 0
    values = []
    for ordinal, words in enumerate(passages, start=1):
        values.append(
            SegmentValues(
                kind="prose_span",
                exact_text=words,
                content_sha256=sha256(words.encode("utf-8")).hexdigest(),
                ordinal=ordinal,
                page_no=2,
                start_offset=offset,
                end_offset=offset + len(words),
            )
        )
        offset += len(words) + 1
    appended = append_source_segments(
        session,
        project_id=project.id,
        document_id=document.id,
        recorded_verbal_origin_id=None,
        segments=values,
    )
    return document, appended


# --- the passage, opened where it was read ----------------------------------


def test_a_cited_cell_opens_with_the_sheet_row_and_column_around_it(
    session, member_project, client
):
    """A workbook citation, at its sheet, row and cell, with its neighbours.

    The point of the ticket in one case: the coordinator is not re-reading the
    quoted value, they are reading what is beside it. The row header above the
    cell and the owner in the same row are what make "2026-04-01" mean
    something.
    """

    project = member_project(COORDINATOR)
    _rendition, segments, _data = registered_workbook(session, project)

    page = client.get(
        f"/sources/{project.slug}/passage/{segments['C5'].id}"
    )

    assert page.status_code == 200, page.text
    assert source_passage_check_label("valid") in page.text
    # The cited place, named as the source names it.
    assert SHEET in page.text and "C5" in page.text
    # Its neighbours: the column heading above it and the row it belongs to.
    for neighbour in ("Owner", "Promised for", "Equistar", "Design", "Dominion"):
        assert neighbour in page.text, neighbour
    # The revision and the rendition, identified.
    assert "ucm-r3.xlsx" in page.text
    assert "UCM-3" in page.text
    assert "XLSX" in page.text


def test_a_cited_page_passage_opens_with_the_passages_around_it(
    session, member_project, client
):
    """A page citation reads with the sentences before and after it."""

    project = member_project(COORDINATOR)
    _document, passages = registered_prose(session, project)

    page = client.get(f"/sources/{project.slug}/passage/{passages[1].id}")

    assert page.status_code == 200, page.text
    for words in ("Equistar confirmed", "signed exhibit", "revised profile"):
        assert words in page.text, words
    assert "page 2" in page.text


def test_the_view_shows_the_source_it_read_and_offers_the_original_beside_it(
    session, member_project, client
):
    """Nothing derived is offered as the document; the original is a link.

    The audit asked for a clear distinction between the original and a derived
    rendition. Everything the view prints is Corridor's reading of the file, so
    the file itself has to be reachable from the same screen or the distinction
    is words only.
    """

    project = member_project(COORDINATOR)
    rendition, segments, _data = registered_workbook(session, project)

    page = client.get(f"/sources/{project.slug}/passage/{segments['C5'].id}")

    assert page.status_code == 200, page.text
    assert (
        f'href="/sources/{project.slug}/document/{rendition.document.id}/original"'
        in page.text
    )
    assert "Corridor's reading of that file" in " ".join(page.text.split())
    assert rendition.document.sha256 in page.text


# --- three states, kept three states ----------------------------------------


def test_a_passage_that_is_not_at_its_cited_location_says_exactly_that(
    session, member_project, client
):
    """The locator was replayed and the workbook did not hold the passage."""

    project = member_project(COORDINATOR)
    rendition, _segments, _data = registered_workbook(session, project)
    absent = rendition.segment("a value this workbook does not hold", cell="Z9")

    page = client.get(f"/sources/{project.slug}/passage/{absent.id}")

    assert page.status_code == 200, page.text
    assert source_passage_check_label("invalid") in page.text
    assert source_passage_check_label("valid") not in page.text


def test_a_retained_prose_citation_reads_as_one_that_cannot_be_re_read(
    session, member_project
):
    """ADR-0094 retired the reader, so nothing opened the page to look.

    Read through the reading rather than the screen, because what matters is
    that the *status* is the fourth one: a screen assertion alone would pass on
    a view that had fallen through to ``invalid`` and happened to print
    something similar.
    """

    project = member_project(COORDINATOR)
    _document, passages = registered_prose(session, project)

    view = read_source_passage(
        session, project_id=project.id, segment_id=passages[0].id
    )

    assert view.passage_check_status == "not_re_readable"
    assert view.passage_check_state == source_passage_check_label("not_re_readable")
    assert view.retrieval_failure is None


def test_source_bytes_that_cannot_be_retrieved_are_never_a_finding_about_the_document(
    session, member_project, client, logs
):
    """The state this ticket exists for, and the two mislabels it refuses.

    The document is registered and its bytes are not in the store. Nothing was
    opened, so the screen reports no Source Passage Check at all — not *Not
    found at cited location*, which asserts that a reader went to the cited
    place, and not *No cited location recorded*, which asserts there was
    nothing to go to. It says the original could not be retrieved, and Corridor
    logs that as a retrieval failure so an operator can find the object.
    """

    project = member_project(COORDINATOR)
    document, passages = registered_prose(session, project, bytes_in_store=False)

    page = client.get(f"/sources/{project.slug}/passage/{passages[0].id}")

    assert page.status_code == 200, page.text
    for state in EVERY_CHECK_STATE:
        assert state not in page.text, state
    said = " ".join(page.text.split())
    assert "Corridor could not retrieve the original file for this document" in said
    assert "It is not a finding about the document" in said
    assert "it does not mean the passage is missing from the source" in said
    # The retained wording is still provable from its own digest, which is a
    # different receipt and stays available while the file does not.
    assert passages[0].content_sha256 in page.text

    [failure] = [
        line for line in logged(logs) if line["event"] == RETRIEVAL_FAILURE_EVENT
    ]
    assert failure["level"] == "ERROR"
    assert failure["failure"] == "ObjectMissing"
    assert failure["document_id"] == document.id
    assert failure["source_segment_id"] == passages[0].id


def test_the_reading_never_reports_a_check_and_a_retrieval_failure_together(
    session, member_project
):
    """Exactly one of the two is set, so no caller can read past the other."""

    project = member_project(COORDINATOR)
    _rendition, segments, _data = registered_workbook(session, project)
    _document, unavailable = registered_prose(session, project, bytes_in_store=False)

    checked = read_source_passage(
        session, project_id=project.id, segment_id=segments["C5"].id
    )
    missing = read_source_passage(
        session, project_id=project.id, segment_id=unavailable[0].id
    )

    assert checked.passage_check_status is not None
    assert checked.retrieval_failure is None
    assert checked.source_bytes_unavailable is False
    assert missing.passage_check_status is None
    assert missing.passage_check_state is None
    assert missing.source_bytes_unavailable is True


# --- who may open it --------------------------------------------------------


def test_a_member_of_another_project_reaches_neither_the_view_nor_the_bytes(
    session, member_project
):
    """Enrolled somewhere else is not enrolled here, and answers as missing."""

    ours = member_project(COORDINATOR)
    theirs = member_project(OUTSIDER)
    rendition, segments, _data = registered_workbook(session, ours)

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: OUTSIDER
    try:
        with TestClient(app) as outsider:
            view = outsider.get(f"/sources/{ours.slug}/passage/{segments['C5'].id}")
            original = outsider.get(
                f"/sources/{ours.slug}/document/{rendition.document.id}/original"
            )
            # Their own project cannot be used as the door either: the passage
            # belongs to another project, so it is missing from this one.
            borrowed = outsider.get(
                f"/sources/{theirs.slug}/passage/{segments['C5'].id}"
            )
    finally:
        app.dependency_overrides.clear()

    assert view.status_code == 404
    assert original.status_code == 404
    assert borrowed.status_code == 404
    assert segments["C5"].exact_text not in borrowed.text


def test_the_reading_itself_refuses_another_project_rather_than_answering(
    session, member_project
):
    """The refusal is in the reading, not only in the route that calls it."""

    ours = member_project(COORDINATOR)
    theirs = member_project(OUTSIDER)
    _rendition, segments, _data = registered_workbook(session, ours)

    with pytest.raises(SourcePassageNotFound):
        read_source_passage(
            session, project_id=theirs.id, segment_id=segments["C5"].id
        )


def test_a_member_receives_the_registered_original_byte_for_byte(
    session, member_project, client
):
    """The refusals above refuse something: this member gets the file."""

    project = member_project(COORDINATOR)
    rendition, _segments, data = registered_workbook(session, project)

    served = client.get(
        f"/sources/{project.slug}/document/{rendition.document.id}/original"
    )

    assert served.status_code == 200, served.text
    assert served.content == data
    assert sha256(served.content).hexdigest() == rendition.document.sha256
    assert served.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def test_an_original_whose_bytes_are_gone_refuses_and_is_logged_as_a_retrieval_failure(
    session, member_project, client, logs
):
    """A download of bytes the store no longer holds is an operational fact."""

    project = member_project(COORDINATOR)
    document, _passages = registered_prose(session, project, bytes_in_store=False)

    served = client.get(
        f"/sources/{project.slug}/document/{document.id}/original"
    )

    assert served.status_code == 404
    assert not served.content.startswith(b"%PDF")
    [failure] = [
        line for line in logged(logs) if line["event"] == RETRIEVAL_FAILURE_EVENT
    ]
    assert failure["document_id"] == document.id


# --- the screen a keyboard and a screen reader receive ----------------------


def test_the_view_performs_no_act_and_therefore_moves_no_focus(
    session, member_project, client
):
    """A pure reading announces nothing and lands nobody past its heading.

    `docs/accessibility-acceptance-checklist.md` section 4: a screen that
    performs no act moves no focus when it loads, and offers a user-activated
    skip link instead. Every region it can reach keeps `tabindex="-1"` so the
    keyboard can be returned to it.
    """

    project = member_project(COORDINATOR)
    _rendition, segments, _data = registered_workbook(session, project)

    page = client.get(f"/sources/{project.slug}/passage/{segments['C5'].id}")

    assert "autofocus" not in page.text
    assert 'href="#cited-passage"' in page.text
    assert 'id="cited-passage" tabindex="-1"' in page.text
    assert 'aria-labelledby="cited-passage-heading"' in page.text
    assert page.text.count("<h1>") == 1
    assert "<h3" not in page.text


def test_every_state_on_the_view_carries_its_own_words(
    session, member_project, client
):
    """No state is a colour: the check prints its label, and so does the cell.

    The cited cell is marked with a class *and* with text only a screen reader
    reads, because weight alone tells a listener nothing about which of nine
    cells is the cited one.
    """

    project = member_project(COORDINATOR)
    _rendition, segments, _data = registered_workbook(session, project)

    page = client.get(f"/sources/{project.slug}/passage/{segments['C5'].id}")

    assert source_passage_check_label("valid") in page.text
    assert "this is the cited cell" in page.text
    assert '<caption>' in page.text
    assert 'scope="col"' in page.text and 'scope="row"' in page.text


# --- the surfaces that link here, and the one route that stays out ----------


def test_a_review_citation_is_a_link_to_the_passage_it_names():
    """Both places Review prints "where it was read" open the exact source.

    Read out of the template rather than out of a rendered packet: the two
    columns are on different items — the cross-source question's per-source
    rows and a single revision's children — and what has to be true of both is
    that the citation is the link, not a sentence beside one.
    """

    review = (TEMPLATE_ROOT / "review.html").read_text(encoding="utf-8")

    links = [
        line
        for line in review.splitlines()
        if '/sources/{{ project.slug }}/passage/' in line
    ]
    assert len(links) == 3, links
    assert any("row.source_segment_id" in line for line in links)
    assert any("child.source.source_segment_id" in line for line in links)
    # The third is #836's: the correction control cites the capture it would
    # report, and a citation there is a link for the same reason the other two
    # are -- a person deciding whether an extraction is wrong needs to open the
    # passage, not read its locator. The count stays exact so a fourth citation
    # has to be as deliberate as this one was.
    assert any(
        "correction_view.capture.source_segment_id" in line for line in links
    )
    # The reading behind those links carries the address, not only the words.
    assert "source_segment_id" in SourceReference.__dataclass_fields__
    assert "source_segment_id" in SourceAnswer.__dataclass_fields__


def test_the_record_view_opens_the_passage_behind_an_accepted_value(
    session, member_project, client
):
    """An accepted value on the Record view links to where it was captured.

    The Record view already held the address — every ``record_history``
    source reference carries its ``source_segment_id`` — and printed it as
    words. This is the same citation as a link, proved by rendering the page
    rather than by reading the template, because what has to be true is that
    the id in the href is the segment this value really came from.
    """

    project = member_project(COORDINATOR)
    rendition = Rendition(session, project, "ucm-record.xlsx")
    fact, segment = rendition.capture(
        fact_type="committed_date", value="2026-04-01", subject_key=subject(42)
    )
    accept_baseline_fact(session, project, fact)

    page = client.get(f"/record/{project.slug}")

    assert page.status_code == 200, page.text
    assert "What each value was captured from" in page.text
    assert (
        f'href="/sources/{project.slug}/passage/{segment.id}"' in page.text
    ), "the accepted value's own passage is not linked"


def test_every_record_citation_site_is_a_link_to_its_passage():
    """All four places the Record view prints a citation, not just the one.

    Three of them need a fixture this module does not build — a native source
    decision's Source Fact, native publication support, and #837's recorded
    reply that was read off an exact passage — so the template is read for
    those and the rendered case above proves the address is right.

    The fourth is the one #837's correspondence history added: a reply linked
    to a Source Segment is evidence, and evidence a reader cannot open is the
    assumption ADR-0090 retired the legacy STALE alert for. It is counted here
    so that a later change which prints that citation as words again fails
    beside the other three rather than quietly.
    """

    record = (TEMPLATE_ROOT / "record_history.html").read_text(encoding="utf-8")

    links = [
        line
        for line in record.splitlines()
        if "/sources/{{ project.slug }}/passage/" in line
    ]
    assert len(links) == 4, links
    assert sum("source.source_segment_id" in line for line in links) == 2
    assert sum("support.source_segment_id" in line for line in links) == 1
    assert sum(
        "reply.response.source_segment_id" in line for line in links
    ) == 1


def test_the_follow_up_bundle_links_only_the_citations_the_record_resolves():
    """The bundle's citation list offers a passage only where one is recorded.

    The behaviour behind this is in ``tests/test_follow_up_bundles.py`` and the
    rendered screen is in ``tests/test_follow_up_chase_screen.py``; what is
    read here is that the markup is guarded by the reference's own resolved
    ids rather than printing a link for every reference.
    """

    workflow = (TEMPLATE_ROOT / "project_workflow.html").read_text(encoding="utf-8")

    assert "reference.source_segment_ids" in workflow
    [link] = [
        line
        for line in workflow.splitlines()
        if "/sources/{{ project.slug }}/passage/" in line
    ]
    assert "{{ segment_id }}" in link


def test_the_source_register_opens_each_document_it_lists():
    """The register names sources; naming one is now a way of opening it."""

    register = (TEMPLATE_ROOT / "source_uploads.html").read_text(encoding="utf-8")

    assert (
        'href="/sources/{{ project.slug }}/document/{{ row.document_id }}/original"'
        in register
    )


def test_the_view_is_admitted_to_the_boundary_and_the_legacy_image_route_is_not():
    """The pilot serves the new view; nothing here revives `/page-image`."""

    enabled = set(web_boundary.PILOT_ROUTES)

    assert ("GET", "/sources/{slug}/passage/{segment_id}") in enabled
    assert ("GET", "/sources/{slug}/document/{document_id}/original") in enabled
    assert ("GET", "/page-image/{document_id}/{page_no}") not in enabled
    assert web_boundary.unprotected_route_relations() == ()


def test_the_admitted_readings_name_only_relations_the_revoke_protects():
    """`doc_pages` is revoked, so the context cannot come from page text."""

    for key in (
        ("GET", "/sources/{slug}/passage/{segment_id}"),
        ("GET", "/sources/{slug}/document/{document_id}/original"),
    ):
        relations = web_boundary.PILOT_ROUTES[key].relations
        assert relations <= web_boundary.PROTECTED_RELATIONS, key
        assert "doc_pages" not in relations
        assert "page_render_derivatives" not in relations
    assert (
        SourceSegment.__tablename__
        in web_boundary.PILOT_ROUTES[
            ("GET", "/sources/{slug}/passage/{segment_id}")
        ].relations
    )
