"""A later UCM revision becomes Source Facts and Proposed Deltas (#606).

The adopted baseline is the record; a later revision of the same workbook is a
source that disagrees with it in places. These tests fix what "in places" means:
which rows are the same row across revisions, which changes batch, which reach a
person on their own, what a partial export may never say, and what the act is
forbidden to write.

Every workbook is synthetic (``later_revision_support``). No test reads a clock:
the reading's cutoff is a declared instant and every comparison is against a
stored value or an identifier.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, text

from corridor import audit
from corridor.analytics import EventFamily, capture_events
from corridor.config import settings
from corridor.field_mapping_manifest import MappingDeclaration, declared_field_mapping
from corridor.baseline_workbook import read_baseline_workbook
from corridor.later_revision import (
    ABSENT_FROM_REVISION,
    AMBIGUOUS_IDENTITY,
    COMPARED,
    NEW_SUBJECT,
    NOT_ADOPTABLE,
    REVISION_COMPARISON_RULE_VERSION,
    ROW_DISPOSITIONS,
    UNCHANGED_UNDER_AMBIGUOUS_IDENTITY,
    WITHHELD_UNSEALED,
    LaterRevisionRefused,
    capture_later_revision,
)
from corridor.models import AuditLog, Document, Fact, FactDecision, ProjectRecordRevision, ProposedDelta, SourceDelivery
from corridor.review_packet_reading import (
    HELD_OUT_APPARENT_REMOVAL,
    HELD_OUT_OWNER_MISMATCH,
    HELD_OUT_POSSIBLE_NEW_CONFLICT,
    read_open_deltas,
)

from later_revision_support import (
    BASELINE_ROWS,
    HEADINGS,
    PRINCIPAL,
    adopt,
    declared,
    burst_workbooks,
    deliver,
    workbook_bytes,
)


# The reading's cutoff. Declared, never taken from a clock: a date the test
# chose is the only thing a deferral or an elapsed promise may be judged against
# (#488).
AS_OF = datetime(2026, 9, 3, tzinfo=timezone.utc)

# The adopted subject identities of the three baseline rows, which is what the
# accepted record keys them by.
UC1 = "Utility Conflicts!3"
UC2 = "Utility Conflicts!4"
UC3 = "Utility Conflicts!5"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return tmp_path / "files"


def _capture(session, project, manifest, body, tmp_path, **overrides):
    staged, envelope = deliver(session, project, body)
    return capture_later_revision(
        session,
        project=project,
        staged=staged,
        envelope=envelope,
        manifest=manifest,
        declaration=declared(**overrides),
        images_dir=tmp_path / "images",
    )


def _deltas(session, project):
    return tuple(
        session.scalars(
            select(ProposedDelta)
            .where(ProposedDelta.project_id == project.id)
            .order_by(ProposedDelta.id)
        ).all()
    )


def _changed(rows, index, column, value):
    """One baseline row list with a single cell replaced."""

    copied = [list(row) for row in rows]
    copied[index][HEADINGS.index(column)] = value
    return copied


# --- what a later revision proposes ----------------------------------------


def test_only_a_changed_value_becomes_a_proposed_delta(
    session, project, tmp_path, store
):
    _revision, manifest = adopt(
        session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path
    )
    later = _changed(BASELINE_ROWS, 0, "Size", "18 in")
    later = _changed(later, 2, "Resolution Strategy Selected (from Resolution Alternatives)", "Adjust")

    capture = _capture(
        session, project, manifest, workbook_bytes(tmp_path / "b.xlsx", later), tmp_path
    )

    proposed = {
        (row.target_subject_identity, row.target_field, row.change_type)
        for row in _deltas(session, project)
    }
    assert proposed == {
        (UC1, "size", "modify"),
        (UC3, "resolution_strategy", "modify"),
    }
    # Everything else the revision restates is captured and agreed, not proposed.
    assert capture.values_agreed > 20
    assert len(capture.fact_ids) == capture.values_agreed + 2
    modified = {row.target_field: row for row in _deltas(session, project)}
    assert modified["size"].accepted_value == "12 in"
    assert modified["size"].proposed_value == "18 in"
    assert modified["size"].comparison_rule_version == REVISION_COMPARISON_RULE_VERSION
    assert modified["size"].source_revision == capture.content_sha256


def test_a_row_that_moved_down_the_sheet_is_still_the_same_row(
    session, project, tmp_path, store
):
    """Identity is the conflict number, never the printed row number."""

    _revision, manifest = adopt(
        session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path
    )
    inserted = [
        ["UC-0", "Atmos Energy", "Gas", "6 in", "Steel", "SR-BL",
         "1100+00", "1101+00", "Relocate", "2026-02-01", "", "", "UCM-1000", ""],
        *_changed(BASELINE_ROWS, 1, "Size", "10 in"),
    ]

    capture = _capture(
        session, project, manifest, workbook_bytes(tmp_path / "b.xlsx", inserted), tmp_path
    )

    # UC-2 now sits at worksheet row 5, and its change still targets the subject
    # it was adopted as at worksheet row 4.
    moved = [row for row in capture.accounting.rows if row.business_identity == "UC-2"]
    assert [(row.row_number, row.subject_identity) for row in moved] == [(5, UC2)]
    changes = {
        (row.target_type, row.target_subject_identity, row.target_field)
        for row in _deltas(session, project)
    }
    assert ("existing_subject", UC2, "size") in changes
    # The genuinely new row is proposed under the customer's own number.
    assert ("proposed_subject", "UC-0", None) in changes
    assert [row.disposition for row in capture.accounting.rows] == [
        NEW_SUBJECT,
        COMPARED,
        COMPARED,
        COMPARED,
    ]


def test_a_changed_utility_owner_reaches_its_own_focused_item(
    session, project, tmp_path, store
):
    """The identity question is not buried in a batch of routine edits."""

    _revision, manifest = adopt(
        session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path
    )
    later = _changed(BASELINE_ROWS, 0, "Utility Owner", "Oncor")
    later = _changed(later, 1, "Size", "9 in")

    _capture(
        session, project, manifest, workbook_bytes(tmp_path / "b.xlsx", later), tmp_path
    )

    reading = read_open_deltas(session, project_id=project.id, as_of=AS_OF)
    owner = next(
        row for row in _deltas(session, project) if row.target_field == "external_org"
    )
    assert owner.accepted_value == "CenterPoint Energy"
    assert owner.proposed_value == "Oncor"
    item = reading.item_for(owner.id)
    assert item.held_out_reason == HELD_OUT_OWNER_MISMATCH
    assert item.delta_ids == (owner.id,)


def test_compatible_changes_of_one_revision_reach_one_item(
    session, project, tmp_path, store
):
    """The burst fixture #527 batches: forty-four routine changes, one item."""

    baseline, later, expected = burst_workbooks(tmp_path)
    _revision, manifest = adopt(session, project, baseline, tmp_path)

    capture = _capture(session, project, manifest, later, tmp_path)

    assert expected >= 40
    assert len(capture.delta_ids) == expected
    reading = read_open_deltas(session, project_id=project.id, as_of=AS_OF)
    assert len(reading.items) == 1
    (item,) = reading.items
    assert item.grouping_key_kind == "source_revision"
    assert item.held_out_reason is None
    assert len(item.delta_ids) == expected
    assert {row.target_field for row in _deltas(session, project)} == {"size"}


# --- what a later revision may never say -----------------------------------


def test_a_partial_revision_never_reads_as_a_deletion(
    session, project, tmp_path, store
):
    _revision, manifest = adopt(
        session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path
    )

    capture = _capture(
        session,
        project,
        manifest,
        workbook_bytes(tmp_path / "b.xlsx", BASELINE_ROWS[:2]),
        tmp_path,
    )

    assert _deltas(session, project) == ()
    (withheld,) = capture.accounting.removals
    assert withheld.subject_identity == UC3
    assert withheld.business_identity == "UC-3"
    assert withheld.reason == ABSENT_FROM_REVISION
    assert withheld.proposed is False
    assert withheld.withheld_reason == WITHHELD_UNSEALED


def test_a_complete_sealed_revision_proposes_the_absent_row_as_a_removal(
    session, project, tmp_path, store
):
    _revision, manifest = adopt(
        session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path
    )

    capture = _capture(
        session,
        project,
        manifest,
        workbook_bytes(tmp_path / "b.xlsx", BASELINE_ROWS[:2]),
        tmp_path,
        is_complete_enumerative_source=True,
        row_accounting_sealed=True,
    )

    (removal,) = _deltas(session, project)
    assert removal.change_type == "apparent_removal"
    assert removal.target_subject_identity == UC3
    assert removal.accepted_value["utility_id"] == "UC-3"
    assert removal.proposed_value is None
    assert capture.accounting.removals[0].proposed is True
    reading = read_open_deltas(session, project_id=project.id, as_of=AS_OF)
    assert reading.item_for(removal.id).held_out_reason == HELD_OUT_APPARENT_REMOVAL


def test_a_repeated_conflict_number_is_never_paired_by_row_position(
    session, project, tmp_path, store
):
    """Two rows under one number stay two subjects, and are never guessed apart."""

    duplicated = [
        *BASELINE_ROWS,
        ["UC-1", "Google Fiber", "Telecom", "2 in", "HDPE", "SR-BL",
         "1220+00", "1221+00", "Adjust", "2026-07-01", "", "", "UCM-1004", ""],
    ]
    _revision, manifest = adopt(
        session, project, workbook_bytes(tmp_path / "a.xlsx", duplicated), tmp_path
    )
    later = _changed(duplicated, 3, "Size", "3 in")

    capture = _capture(
        session,
        project,
        manifest,
        workbook_bytes(tmp_path / "b.xlsx", later),
        tmp_path,
        is_complete_enumerative_source=True,
        row_accounting_sealed=True,
    )

    dispositions = {
        row.source_row_key: row.disposition for row in capture.accounting.rows
    }
    # The unchanged half of the pair says nothing new; the changed half cannot be
    # attached to either accepted subject, so it is its own focused item.
    assert dispositions["Utility Conflicts!3"] == UNCHANGED_UNDER_AMBIGUOUS_IDENTITY
    assert dispositions["Utility Conflicts!6"] == AMBIGUOUS_IDENTITY
    assert dispositions["Utility Conflicts!4"] == COMPARED
    rows = _deltas(session, project)
    assert [row.target_type for row in rows] == ["proposed_subject"]
    assert rows[0].target_subject_identity == "UC-1#2"
    reading = read_open_deltas(session, project_id=project.id, as_of=AS_OF)
    assert (
        reading.item_for(rows[0].id).held_out_reason
        == HELD_OUT_POSSIBLE_NEW_CONFLICT
    )


def test_capture_writes_no_accepted_value(session, project, tmp_path, store):
    revision, manifest = adopt(
        session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path
    )
    before = session.execute(
        text(
            "select subject_key, fact_type, text_value from current_project_record "
            "where project_id = :p order by subject_key, fact_type"
        ),
        {"p": project.id},
    ).all()
    decisions = session.scalar(
        select(func.count()).select_from(FactDecision).where(
            FactDecision.project_id == project.id
        )
    )
    # The comparison is only meaningful against a record that holds something.
    assert len(before) > 30 and decisions > 30

    _capture(
        session,
        project,
        manifest,
        workbook_bytes(
            tmp_path / "b.xlsx", _changed(BASELINE_ROWS, 0, "Size", "18 in")
        ),
        tmp_path,
        is_complete_enumerative_source=True,
        row_accounting_sealed=True,
    )

    assert (
        session.execute(
            text(
                "select subject_key, fact_type, text_value from "
                "current_project_record where project_id = :p "
                "order by subject_key, fact_type"
            ),
            {"p": project.id},
        ).all()
        == before
    )
    assert session.scalar(
        select(func.count()).select_from(FactDecision).where(
            FactDecision.project_id == project.id
        )
    ) == decisions
    assert session.scalar(
        select(func.max(ProjectRecordRevision.id)).where(
            ProjectRecordRevision.project_id == project.id
        )
    ) == revision


# --- what the capture retains ----------------------------------------------


def test_the_exact_bytes_digest_and_external_version_are_retained(
    session, project, tmp_path, store
):
    _revision, manifest = adopt(
        session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path
    )
    body = workbook_bytes(tmp_path / "b.xlsx", _changed(BASELINE_ROWS, 0, "Size", "18 in"))

    capture = _capture(session, project, manifest, body, tmp_path)

    document = session.get(Document, capture.document_id)
    assert document.sha256 == capture.content_sha256
    assert document.project_id == project.id
    delivery = session.scalars(
        select(SourceDelivery).where(
            SourceDelivery.project_id == project.id,
            SourceDelivery.content_sha256 == capture.content_sha256,
            SourceDelivery.disposition == "stored",
        )
    ).one()
    assert delivery.external_identity == capture.external_identity == "UCM workbook revision D"
    assert delivery.external_version == capture.external_version
    assert capture.field_mapping.content_sha256 == manifest.content_sha256


def test_a_revision_nobody_took_delivery_of_is_refused(
    session, project, tmp_path, store
):
    """Retention is checked, not claimed."""

    _revision, manifest = adopt(
        session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path
    )
    body = workbook_bytes(tmp_path / "b.xlsx", _changed(BASELINE_ROWS, 0, "Size", "18 in"))
    staged, envelope = deliver(session, project, body)
    session.query(SourceDelivery).filter(
        SourceDelivery.idempotency_key == envelope.idempotency_key
    ).delete()

    with pytest.raises(LaterRevisionRefused, match="no stored delivery"):
        capture_later_revision(
            session,
            project=project,
            staged=staged,
            envelope=envelope,
            manifest=manifest,
            declaration=declared(),
            images_dir=tmp_path / "images",
        )


def test_unknown_columns_and_unreadable_values_are_kept_not_dropped(
    session, project, tmp_path, store
):
    _revision, manifest = adopt(
        session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path
    )
    headings = [*HEADINGS, "Early TxDOT Utility Activity"]
    later = [[*row, ""] for row in BASELINE_ROWS]
    later[0][-1] = "Yes"
    later[1][HEADINGS.index("Promised For")] = "TBD"

    capture = _capture(
        session,
        project,
        manifest,
        workbook_bytes(tmp_path / "b.xlsx", later, headings=headings),
        tmp_path,
    )

    assert [column.heading for column in capture.accounting.retained_columns] == [
        "Early TxDOT Utility Activity"
    ]
    assert capture.accounting.retained_cell_count == 1
    unreadable = capture.accounting.unsupported_values
    assert [(item.field, item.exact_text) for item in unreadable] == [
        ("committed_date", "TBD")
    ]
    # An unreadable cell is retained as source text and never guessed into a value.
    assert all(
        row.target_field != "committed_date" for row in _deltas(session, project)
    )


def test_only_a_declared_heading_carries_a_reference_out_of_the_workbook(
    session, project, tmp_path, store
):
    """#597: a role comes from the registered declaration, never from a spelling."""

    _revision, manifest = adopt(
        session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path
    )
    later = _changed(BASELINE_ROWS, 0, "Size", "18 in")

    capture = _capture(
        session, project, manifest, workbook_bytes(tmp_path / "b.xlsx", later), tmp_path
    )

    first = capture.accounting.rows[0]
    assert first.external_system_id == "UCM-1001"
    assert first.source_url == "https://ucm.example/records/1001"
    assert [column.heading for column in capture.accounting.retained_columns] == []


def test_an_undeclared_heading_carries_no_reference_and_is_still_kept(
    session, project, tmp_path, store
):
    _revision, manifest = adopt(
        session,
        project,
        workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS),
        tmp_path,
        declaration=MappingDeclaration(),
    )
    later = _changed(BASELINE_ROWS, 0, "Size", "18 in")

    capture = _capture(
        session, project, manifest, workbook_bytes(tmp_path / "b.xlsx", later), tmp_path
    )

    assert all(row.external_system_id is None for row in capture.accounting.rows)
    assert all(row.source_url is None for row in capture.accounting.rows)
    # Nothing is lost by declining to guess: the cells stay retained.
    assert {column.heading for column in capture.accounting.retained_columns} == {
        "UCM Record ID",
        "Record URL",
    }
    assert capture.accounting.retained_cell_count == 4


def test_every_row_of_the_revision_reaches_a_disposition(
    session, project, tmp_path, store
):
    _revision, manifest = adopt(
        session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path
    )
    later = [
        *BASELINE_ROWS,
        ["UC-9", "Not Used", "", "", "", "", "", "", "", "", "", "", "", ""],
    ]

    capture = _capture(
        session, project, manifest, workbook_bytes(tmp_path / "b.xlsx", later), tmp_path
    )

    receipt = capture.accounting.receipt
    assert receipt["detected_row_count"] == len(later)
    assert receipt["accounted_row_count"] == len(later)
    assert receipt["unaccounted_rows"] == []
    assert receipt["skipped_row_count"] == 1
    assert [row.disposition for row in capture.accounting.rows][-1] == NOT_ADOPTABLE
    assert capture.accounting.rows[-1].reason == "retired_row"
    assert {row.disposition for row in capture.accounting.rows} <= set(
        ROW_DISPOSITIONS
    )
    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.action == audit.CAPTURE_LATER_SOURCE_REVISION,
            AuditLog.entity_id == capture.document_id,
        )
    ).one()
    assert entry.human_principal == PRINCIPAL.subject
    retained = entry.after_json["row_accounting"]
    assert [row["source_row_key"] for row in retained["rows"]] == [
        row.source_row_key for row in capture.accounting.rows
    ]
    assert entry.after_json["external_version"] == capture.external_version


# --- the boundary a later revision is read through -------------------------


def test_a_later_revision_is_processed_only_under_the_registered_mapping(
    session, project, tmp_path, store
):
    _revision, _manifest = adopt(
        session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path
    )
    body = workbook_bytes(tmp_path / "b.xlsx", _changed(BASELINE_ROWS, 0, "Size", "18 in"))
    staged, envelope = deliver(session, project, body)
    # A manifest built from the later file is a different declaration, whatever
    # it is named: its digest is bound to its own proved example.
    unregistered = declared_field_mapping(
        read_baseline_workbook(staged.stored_path), MappingDeclaration()
    )

    with pytest.raises(LaterRevisionRefused, match="mapping revision in force"):
        capture_later_revision(
            session,
            project=project,
            staged=staged,
            envelope=envelope,
            manifest=unregistered,
            declaration=declared(),
            images_dir=tmp_path / "images",
        )


def test_the_registered_mapping_is_resolved_from_stored_state(
    session, project, tmp_path, store
):
    """A caller no longer reconstructs the approved declaration to be believed (#622).

    The declaration is stored beside its registration (#610), so omitting the
    manifest reads back the same mapping revision the project registered. The
    proof is equivalence rather than a digest alone: the same delivery captured
    with the manifest supplied produces the very same Facts and Proposed Deltas,
    which it can only do if the resolved declaration is the registered one.
    """

    _revision, manifest = adopt(
        session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path
    )
    body = workbook_bytes(tmp_path / "b.xlsx", _changed(BASELINE_ROWS, 0, "Size", "18 in"))
    staged, envelope = deliver(session, project, body)
    arguments = {
        "project": project,
        "staged": staged,
        "envelope": envelope,
        "declaration": declared(),
        "images_dir": tmp_path / "images",
    }

    resolved = capture_later_revision(session, **arguments)
    supplied = capture_later_revision(session, manifest=manifest, **arguments)

    assert resolved.delta_ids
    assert resolved.field_mapping.content_sha256 == manifest.content_sha256
    assert supplied.delta_ids == resolved.delta_ids
    assert supplied.fact_ids == resolved.fact_ids


def test_a_registration_that_stores_no_declaration_refuses_by_name(
    session, project, tmp_path, store
):
    """The shape every registration had before #610, and what it can prove.

    The command alone writes identity, version and digest — no declaration — so
    a registration made through it is exactly one made before the declaration
    was stored beside it. The absence is stated by name; it is never read as an
    empty mapping, and never quietly replaced by one rebuilt from the delivered
    file. The registered revision is unchanged, so a caller that still holds the
    declaration is served by the same registration the resolution could not.
    """

    _revision, manifest = adopt(
        session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path
    )
    session.scalar(
        select(
            func.register_baseline_format(
                project.id,
                "field_mapping",
                manifest.identity,
                manifest.version,
                manifest.content_sha256,
                PRINCIPAL.subject,
                "register-without-a-declaration",
            )
        )
    )
    session.expire_all()
    body = workbook_bytes(tmp_path / "b.xlsx", _changed(BASELINE_ROWS, 0, "Size", "18 in"))
    staged, envelope = deliver(session, project, body)
    arguments = {
        "project": project,
        "staged": staged,
        "envelope": envelope,
        "declaration": declared(),
        "images_dir": tmp_path / "images",
    }

    with pytest.raises(LaterRevisionRefused, match="stores no declaration"):
        capture_later_revision(session, **arguments)

    supplied = capture_later_revision(session, manifest=manifest, **arguments)
    assert supplied.delta_ids


def test_a_revision_whose_columns_left_the_mapping_is_refused(
    session, project, tmp_path, store
):
    _revision, manifest = adopt(
        session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path
    )
    renamed = [*HEADINGS]
    renamed[HEADINGS.index("Size")] = "Facility Diameter"

    with pytest.raises(LaterRevisionRefused, match="registered mapping revision"):
        _capture(
            session,
            project,
            manifest,
            workbook_bytes(tmp_path / "b.xlsx", BASELINE_ROWS, headings=renamed),
            tmp_path,
        )


def test_a_project_with_no_adopted_baseline_has_no_later_revision(
    session, project, tmp_path, store
):
    body = workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS)
    staged, envelope = deliver(session, project, body)
    manifest = declared_field_mapping(
        read_baseline_workbook(staged.stored_path), MappingDeclaration()
    )

    with pytest.raises(LaterRevisionRefused, match="adopted no baseline"):
        capture_later_revision(
            session,
            project=project,
            staged=staged,
            envelope=envelope,
            manifest=manifest,
            declaration=declared(),
            images_dir=tmp_path / "images",
        )


# --- replay and measurement -------------------------------------------------


def test_capturing_the_same_revision_twice_creates_nothing_new(
    session, project, tmp_path, store
):
    _revision, manifest = adopt(
        session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path
    )
    body = workbook_bytes(tmp_path / "b.xlsx", _changed(BASELINE_ROWS, 0, "Size", "18 in"))
    staged, envelope = deliver(session, project, body)
    arguments = {
        "project": project,
        "staged": staged,
        "envelope": envelope,
        "manifest": manifest,
        "declaration": declared(),
        "images_dir": tmp_path / "images",
    }

    first = capture_later_revision(session, **arguments)
    second = capture_later_revision(session, **arguments)

    assert second.delta_ids == first.delta_ids
    assert second.fact_ids == first.fact_ids
    assert second.document_id == first.document_id
    assert len(_deltas(session, project)) == len(first.delta_ids)


def test_a_second_later_revision_restating_a_value_is_captured(
    session, project, tmp_path, store
):
    """The third delivery is the ordinary case, and it used to fail.

    A Source Fact is what *one source* says, so two revisions restating the
    same value are two Facts.  The Fact digest omitted the rendition while
    ``SourceSegment.content_sha256`` is ``sha256(exact_text)`` and carries no
    document either, so the second revision to leave a value alone produced a
    digest the first revision already owned; ``append_fact`` handed back the
    earlier revision's Fact and supporting it with this revision's segment was
    refused by ``ck_support_assessment_rendition``.

    Nearly every real second revision leaves some value alone, so this was the
    main path rather than a corner of it.
    """

    _, manifest = adopt(
        session, project, workbook_bytes(tmp_path / "base.xlsx", BASELINE_ROWS), tmp_path
    )
    first = _changed(BASELINE_ROWS, 0, "Size", '8"')
    second = _changed(first, 1, "Size", '10"')

    _capture(
        session,
        project,
        manifest,
        workbook_bytes(tmp_path / "r1.xlsx", first),
        tmp_path,
    )
    session.flush()
    staged, envelope = deliver(
        session,
        project,
        workbook_bytes(tmp_path / "r2.xlsx", second),
        filename="r2.xlsx",
        external_identity="UCM workbook revision E",
        material=f"second-{project.slug}",
    )
    capture_later_revision(
        session,
        project=project,
        staged=staged,
        envelope=envelope,
        manifest=manifest,
        declaration=declared(),
        images_dir=tmp_path / "images",
    )

    facts = session.scalars(
        select(Fact).where(Fact.project_id == project.id)
    ).all()
    renditions = {fact.document_id for fact in facts if fact.document_id is not None}
    # Three documents captured facts: the adopted baseline and both revisions.
    assert len(renditions) == 3
    # The value revision two left alone is captured by revision two as its own
    # Fact rather than resolving to revision one's.
    by_document: dict[int, set[tuple[str, str]]] = {}
    for fact in facts:
        if fact.document_id is None:
            continue
        by_document.setdefault(fact.document_id, set()).add(
            (fact.subject_key, fact.fact_type)
        )
    captured = [keys for keys in by_document.values() if (UC1, "size") in keys]
    assert len(captured) == 3


def test_the_proposed_delta_creation_event_is_emitted(
    session, project, tmp_path, store
):
    _revision, manifest = adopt(
        session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path
    )
    body = workbook_bytes(tmp_path / "b.xlsx", _changed(BASELINE_ROWS, 0, "Size", "18 in"))

    with capture_events() as collected:
        capture = _capture(session, project, manifest, body, tmp_path)

    (event,) = collected.by_family(EventFamily.PROPOSED_DELTA_CREATION)
    assert event.payload["project_id"] == project.id
    assert event.payload["source_revision"] == capture.content_sha256
    assert event.payload["delta_ids"] == list(capture.delta_ids)
    assert event.binding.mapping_identity == f"{manifest.identity}:{manifest.version}"
    assert collected.by_family(EventFamily.SOURCE_ARRIVAL)
    assert collected.by_family(EventFamily.SOURCE_CAPTURE)
