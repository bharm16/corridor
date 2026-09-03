"""The Source Passage Check: one mechanical status, presented as itself.

ADR-0082 renames the quote-on-page flag ``locator_validation_status`` and
strips "verified" from customer displays unless the word names the object.
These tests hold three separate promises: the status is mechanical and
replayable from the original bytes, ``EvidenceLink.verified`` survives only as
the projection ``status == valid``, and no customer surface prints a bare
"verified" beside a value.
"""

from __future__ import annotations

import ast
from hashlib import sha256
from pathlib import Path
import re

from openpyxl import Workbook
import pymupdf
import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.exceptions import RULES, format_exception_name
from corridor.export import COLUMNS
from corridor.ingest import ingest_document
from corridor.locator_validation import (
    INVALID,
    LOCATOR_VALIDATION_STATUSES,
    NOT_CHECKED,
    VALID,
    cited_passage_locator_validation_status,
    evidence_link_locator_validation_status,
    evidence_link_verified,
    recorded_verbal_statement_locator_validation_status,
    source_segment_locator_validation_status,
)
from corridor.models import Project, SourceSegment
from corridor.presentation import (
    documentation_review_label,
    field_label,
    label,
    provenance_label,
    resolution_strategy_label,
    source_passage_check_label,
    statement_type_label,
)
from corridor.source_segments import recorded_verbal_statement_segment

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "corridor"
TEMPLATES = SOURCE / "web" / "templates"


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


@pytest.fixture
def project(session):
    project = Project(
        slug="locator-validation-test",
        name="Locator Validation Test",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()
    return project


@pytest.fixture
def workbook(tmp_path):
    path = tmp_path / "utility-conflicts.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "Utility Conflicts"
    sheet.append(["Conflict ID", "Owner"])
    sheet.append(["UC-1", "CenterPoint"])
    book.save(path)
    return path


def _ingest(session, project, path, tmp_path, doc_type="matrix"):
    return ingest_document(
        session,
        project_id=project.id,
        path=path,
        doc_type=doc_type,
        images_dir=tmp_path / "images",
    )


def _segments(session, document):
    return list(
        session.scalars(
            select(SourceSegment)
            .where(SourceSegment.document_id == document.id)
            .order_by(SourceSegment.ordinal)
        ).all()
    )


class _StoredCheck:
    """The one attribute a legacy evidence link contributes to the check."""

    def __init__(self, verified: bool) -> None:
        self.verified = verified


# --- The status is mechanical and replayable ------------------------------------


def test_a_workbook_cell_locator_that_recovers_its_text_is_valid(
    session, project, workbook, tmp_path
):
    document = _ingest(session, project, workbook, tmp_path)

    statuses = [
        source_segment_locator_validation_status(document, segment, workbook)
        for segment in _segments(session, document)
    ]

    assert statuses and set(statuses) == {VALID}


def test_the_same_bytes_and_locator_always_give_the_same_answer(
    session, project, workbook, tmp_path
):
    """Replayable: the status is recomputed, never remembered."""

    document = _ingest(session, project, workbook, tmp_path)
    segment = _segments(session, document)[0]

    first = source_segment_locator_validation_status(document, segment, workbook)
    second = source_segment_locator_validation_status(document, segment, workbook)

    assert (first, second) == (VALID, VALID)


def test_a_locator_that_no_longer_recovers_its_stored_text_is_invalid(
    session, project, workbook, tmp_path
):
    document = _ingest(session, project, workbook, tmp_path)
    segment = _segments(session, document)[0]
    segment.cell_range = "Z99"

    assert (
        source_segment_locator_validation_status(document, segment, workbook)
        == INVALID
    )


def test_a_tampered_digest_is_invalid_rather_than_raising(
    session, project, workbook, tmp_path
):
    """A caller asking for a status gets one; the replay still fails closed."""

    document = _ingest(session, project, workbook, tmp_path)
    segment = _segments(session, document)[0]
    segment.content_sha256 = "0" * 64

    assert (
        source_segment_locator_validation_status(document, segment, workbook)
        == INVALID
    )


def test_bytes_that_are_not_the_registered_document_are_invalid(
    session, project, workbook, tmp_path
):
    document = _ingest(session, project, workbook, tmp_path)
    segment = _segments(session, document)[0]
    other = tmp_path / "other.xlsx"
    other.write_bytes(workbook.read_bytes() + b"tampered")

    assert (
        source_segment_locator_validation_status(document, segment, other) == INVALID
    )


def test_a_prose_span_locator_validates_against_the_registered_pdf(
    session, project, tmp_path
):
    path = tmp_path / "coordination-minutes.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text(
            (72, 72),
            "Meeting notes and attendance.\n"
            "Action Items:\n"
            "1. Equistar will submit the signed exhibit by March 2025.\n"
            "Meeting Notes",
        )
        pdf.save(path)
    document = _ingest(session, project, path, tmp_path, doc_type="minutes")

    segments = _segments(session, document)

    assert segments
    assert {
        source_segment_locator_validation_status(document, segment, path)
        for segment in segments
    } == {VALID}


def test_a_recorded_verbal_statement_validates_on_its_own_digest():
    """ADR-0033: a verbal has no document, so the digest is the whole check."""

    segment = recorded_verbal_statement_segment(
        project_id=1,
        recorded_verbal_origin_id=2,
        exact_text="They will pull the pole in March.",
    )

    assert recorded_verbal_statement_locator_validation_status(segment) == VALID

    segment.content_sha256 = sha256(b"different words").hexdigest()

    assert recorded_verbal_statement_locator_validation_status(segment) == INVALID


def test_the_legacy_page_locator_replays_through_the_same_predicate():
    page = "The utility owner will relocate the 12-inch main by March 2025."

    assert (
        cited_passage_locator_validation_status(
            "relocate the 12-inch main by March 2025", page
        )
        == VALID
    )
    assert (
        cited_passage_locator_validation_status("abandon the main in place", page)
        == INVALID
    )


def test_no_locator_to_dereference_is_not_run_rather_than_failed():
    """"Never checked" and "checked and absent" are different facts."""

    assert cited_passage_locator_validation_status(None, "any page text") == (
        NOT_CHECKED
    )
    assert cited_passage_locator_validation_status("a quote", None) == NOT_CHECKED
    assert evidence_link_locator_validation_status(None) == NOT_CHECKED


# --- The retired flag is only a projection ---------------------------------------


def test_verified_is_exactly_the_projection_of_a_valid_status():
    """ADR-0082 keeps the column as ``locator_validation_status == valid``."""

    assert evidence_link_verified(VALID) is True
    assert evidence_link_verified(INVALID) is False
    assert evidence_link_verified(NOT_CHECKED) is False
    assert set(LOCATOR_VALIDATION_STATUSES) == {VALID, INVALID, NOT_CHECKED}

    with pytest.raises(ValueError, match="unknown locator validation status"):
        evidence_link_verified("passed")


def test_a_stored_link_reads_back_as_a_status_not_a_boolean():
    assert evidence_link_locator_validation_status(_StoredCheck(True)) == VALID
    assert evidence_link_locator_validation_status(_StoredCheck(False)) == INVALID


def test_no_screen_reads_the_retired_column():
    """"No new code reads it" is a property of the tree, not a convention.

    ADR-0081 froze the legacy writers and readers where they stand, and #458
    removes the column with them, so this does not claim the whole codebase
    has moved. It claims the display path has: every screen and every label
    adapter asks ``locator_validation`` for a status, so what a customer sees
    can no longer be a raw boolean.
    """

    attribute = re.compile(
        r"(?<![\w.])(?:EvidenceLink|link|_link|a|assertion)\.verified\b"
    )
    display = [
        *sorted(TEMPLATES.glob("*.html")),
        SOURCE / "presentation.py",
        SOURCE / "export.py",
        SOURCE / "briefing.py",
        SOURCE / "web" / "app.py",
        SOURCE / "web" / "queue.py",
    ]
    offenders = {
        path.name for path in display if attribute.search(path.read_text())
    }

    assert offenders == set(), f"these screens read the retired flag: {offenders}"


def test_locator_validation_never_reads_the_support_relation():
    """ADR-0082: the check is mechanical and may not stand in for support."""

    source = (SOURCE / "locator_validation.py").read_text()
    tree = ast.parse(source)
    docstring_end = tree.body[0].end_lineno if ast.get_docstring(tree) else 0
    body = "\n".join(
        line
        for line in source.splitlines()[docstring_end:]
        if not line.strip().startswith("#")
    )

    for forbidden in (
        "support_assessment",
        "SupportAssessment",
        "operative_support",
        "satisfies_requirement",
    ):
        assert forbidden not in body, f"the check reads {forbidden}"


# --- Nothing shows the flag as a bare "verified" ---------------------------------


_WORD = re.compile(r"\b(?:un)?verif(?:y|ied|ication|ies)\b", re.IGNORECASE)
_MARKUP = re.compile(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}|<[^>]*>", re.DOTALL)

# ADR-0082 permits "verified" where it names the object. The revision-change
# explanation names one completed document-revision comparison, not the
# passage check, so the word is the object's name and not a status beside a
# value. Every other customer-facing occurrence is a defect.
NAMES_ITS_OBJECT = ("the verified comparison",)


def _customer_labels() -> dict[str, str]:
    """Every customer word these adapters can return, by where it comes from."""

    labels: dict[str, str] = {}
    for key in (
        "constraint",
        "constraint_log",
        "cited_passage",
        "documentation_review",
        "evidence",
        "provenance",
        "source_passage_check",
        "supporting_documents",
        "required_documents",
        "constraint_alerts",
        "record_conclusion",
        "coordination_decision",
        "assertion",
        "derivation",
        "verbal",
    ):
        labels[f"label({key})"] = label(key)
    for rule in RULES:
        labels[f"exception_name({rule})"] = format_exception_name(rule)
    for status in LOCATOR_VALIDATION_STATUSES:
        labels[f"source_passage_check_label({status})"] = source_passage_check_label(
            status
        )
    for field in (
        "external_org",
        "evidence_required",
        "committed_date",
        "resolution_strategy",
        "need_date",
    ):
        labels[f"field_label({field})"] = field_label(field)
    for kind in ("assertion", "derivation", "verbal", "evidence", "cited"):
        labels[f"provenance_label({kind})"] = provenance_label(kind)
    for event in ("commitment", "committed_date_change", "closure"):
        labels[f"statement_type_label({event})"] = statement_type_label(event)
    for strategy in ("relocate", "remove", "abandon_in_place"):
        labels[f"resolution_strategy_label({strategy})"] = resolution_strategy_label(
            strategy
        )
    for sufficient in (True, False):
        labels[f"documentation_review_label({sufficient})"] = (
            documentation_review_label(sufficient)
        )
    for index, column in enumerate(COLUMNS):
        labels[f"export column {index}"] = column
    return labels


def _receipt_and_explanation_text() -> dict[str, str]:
    """The literal customer sentences receipts and explanations carry.

    A Constraint Alert's detail is the sentence a Coordination Report, an
    export, a release receipt, and a change explanation all quote, so reading
    the engine's own string literals covers every one of them at the source.
    """

    text: dict[str, str] = {}
    for name in ("exceptions.py", "presentation.py", "export.py"):
        tree = ast.parse((SOURCE / name).read_text())
        # A docstring explains the code to the next engineer and is never
        # printed, so only the literals a reader can end up holding count.
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(
                node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            )
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                text[f"{name}:{node.lineno}"] = node.value
    return text


def test_no_customer_surface_shows_the_flag_as_verified():
    """The gate ADR-0082 asks for, over templates, labels, exports, receipts.

    Templates are read as the prose a reader sees: Jinja tags and HTML tags
    are stripped first, so an attribute or a status identifier in the code is
    not mistaken for a word on the page.
    """

    offenders: list[str] = []

    for path in sorted(TEMPLATES.glob("*.html")):
        prose = _MARKUP.sub(" ", path.read_text())
        for match in _WORD.finditer(prose):
            window = prose[max(0, match.start() - 60) : match.end() + 60]
            if any(phrase in " ".join(window.split()) for phrase in NAMES_ITS_OBJECT):
                continue
            offenders.append(f"{path.name}: {' '.join(window.split())}")

    for where, value in {
        **_customer_labels(),
        **_receipt_and_explanation_text(),
    }.items():
        if _WORD.search(value) and not any(
            phrase in " ".join(value.split()) for phrase in NAMES_ITS_OBJECT
        ):
            offenders.append(f"{where}: {' '.join(value.split())}")

    assert offenders == [], "\n".join(offenders)


def test_the_flag_is_presented_as_the_source_passage_check_with_its_state():
    """The state names where the passage was looked for, not a verdict (#600).

    Passed, Failed, and Not run each read as a judgment on the value or on
    inspected work, and "Not run" also implied a scheduled check that was
    skipped rather than a locator there was never anything to replay.
    """

    assert label("source_passage_check") == "Source passage check"
    assert [source_passage_check_label(s) for s in (VALID, INVALID, NOT_CHECKED)] == [
        "Found at cited location",
        "Not found at cited location",
        "No cited location recorded",
    ]


def test_the_three_concepts_stay_visibly_distinct():
    """The check, Supporting Documentation in Use, and Record Inclusion.

    A passed check is not a designation that a passage backs this value, and
    neither is a record decision. The Constraint page keeps them in separate
    columns, and the words themselves do not overlap.
    """

    distinct = {
        label("source_passage_check"),
        label("documentation_review"),
        label("supporting_documents"),
        label("record_conclusion"),
    }
    assert len(distinct) == 4

    dependency_page = (TEMPLATES / "dependency.html").read_text()
    header = dependency_page[
        dependency_page.index("<th>Source document</th>") :
    ].split("</tr>")[0]
    assert "<th>Source passage check</th>" in header
    assert "<th>Documentation review</th>" in header
