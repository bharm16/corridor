"""The coverage reading Corridor derives, and the one a person confirms (#675).

Every instant here is declared. Nothing in this file reads a clock: the cutoff,
the confirmation instant and each delivery's recorded arrival are all supplied,
because a test that asked the machine what time it was could not prove that the
same sources produce the same reading tomorrow.

The tests are grouped the way the ticket's rules are: what the machine derives,
what a person may add, what a person may never do, when a declaration may be
reused, and how a Proposed Delta reaches the delivery boundary its issue was
confirmed against.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from corridor.access import COORDINATION, enroll_member
from corridor.consequence_levels import (
    CAN_WAIT,
    MUST_HANDLE,
    OUTSIDE_BOUNDARY_SENTENCE,
    consequence_level,
)
from corridor.packet_review import read_review_items
from corridor.issue_content import (
    COVERAGE_ALL_REQUIRED_SOURCES_READ,
    UCM_RENDERER_IDENTITY,
    UCM_RENDERER_VERSION,
    ChangeFacts,
    effective_issue_content,
)
from corridor.later_revision import capture_later_revision
from corridor.source_revision_declaration import RevisionDeclaration
from corridor.issue_coverage import (
    ANNOTATION_LIMIT,
    CoverageAnnotation,
    CoverageExclusion,
    CoverageLine,
    CoverageRefused,
    confirm_coverage,
    declared_lines,
    delivery_ids_for_deltas,
    deltas_outside_coverage_boundary,
    derive_coverage_reading,
    latest_declaration,
    load_declaration,
    reusable_declaration,
)
from corridor.issue_profile import (
    CoverageRequirement,
    RendererRevision,
    UPDATED_UCM,
    effective_issue_inventory,
)
from corridor.models import Document, IssueCoverageDeclaration, Project, SourceDelivery
from corridor.principals import HumanPrincipal
from corridor.source_delivery import (
    DISPOSITION_QUARANTINED,
    DISPOSITION_TRANSIENT_FAILURE,
    DeliveryBinding,
    DeliveryObservation,
    confirm_delivery,
    record_delivery,
    take_delivery,
)

from later_revision_support import (
    BASELINE_ROWS,
    HEADINGS,
    PRINCIPAL,
    adopt,
    deliver,
    workbook_bytes,
)
from packet_review_support import (
    Rendition,
    append_deltas,
    configure_issue,
    modify,
    subject,
)


COORDINATOR = HumanPrincipal("local:coordinator")
OPERATOR = HumanPrincipal("local:operator")

JANUARY = datetime(2026, 1, 5, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 3, 2, 6, 0, tzinfo=timezone.utc)
CONFIRMED_AT = datetime(2026, 3, 2, 7, 0, tzinfo=timezone.utc)

UCM_RENDERER = RendererRevision(UCM_RENDERER_IDENTITY, UCM_RENDERER_VERSION)
EVERY_SOURCE_READ = CoverageRequirement(
    requirement=COVERAGE_ALL_REQUIRED_SOURCES_READ,
    statement="Every source delivered for this issue is read before it goes out.",
)


@pytest.fixture
def adopted(session, tmp_path):
    """One adopted, configured project with no source but its own baseline."""

    project = Project(
        slug=f"issue-coverage-{uuid4().hex[:8]}",
        name="Issue Coverage",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()
    enroll_member(
        session,
        project_id=project.id,
        email="coordinator@example.test",
        principal=COORDINATOR,
        display_name="Coordinator",
        designations=[COORDINATION],
        operator=OPERATOR,
    )
    body = workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS)
    revision_id, manifest = adopt(session, project, body, tmp_path)
    return _Adopted(
        project=project,
        revision_id=revision_id,
        template_bytes=body,
        manifest=manifest,
    )


class _Adopted:
    def __init__(self, project, revision_id, template_bytes, manifest=None):
        self.project = project
        self.revision_id = revision_id
        self.template_bytes = template_bytes
        # The mapping revision the project registered, which a later revision
        # of the same workbook must be read through.
        self.manifest = manifest


def _configure(session, adopted, *, coverage=()):
    return configure_issue(
        session,
        adopted.project,
        principal=COORDINATOR,
        effective_from=JANUARY,
        ucm=UCM_RENDERER,
        coverage=tuple(coverage),
    )


def _deliver(
    session,
    adopted,
    *,
    name: str,
    received_at: datetime,
    disposition: str | None = None,
    refusal_reason: str | None = None,
    document: Document | None = None,
    uploaded_by: str = "",
):
    """One delivery in the ledger, at a declared arrival instant.

    ``uploaded_by`` makes it the delivery a person handed over through the
    product rather than one a connector fetched (#823); the two differ in what
    is supposed to happen next, which is what the reading has to say.
    """

    binding = (
        DeliveryBinding(
            customer="acme-utilities",
            project_id=adopted.project.id,
            project_slug=adopted.project.slug,
            transport="push",
            channel="product_upload",
            configuration_identity="product-upload",
            delivered_by_principal=uploaded_by,
        )
        if uploaded_by
        else DeliveryBinding(
            customer="acme-utilities",
            project_id=adopted.project.id,
            project_slug=adopted.project.slug,
            transport="pull",
            channel="shared-files",
            configuration_identity="shared-files-v1",
            configuration_version="1",
        )
    )
    observation = DeliveryObservation(
        external_identity=name,
        external_version="1",
        content_digest=f"{abs(hash(name)):064x}"[:64].replace("-", "0"),
        bytes_reference=f"objects/{name}",
    )
    if disposition is None:
        recorded = take_delivery(
            session,
            binding,
            observation,
            service_identity="test",
            run_identity=uuid4().hex,
        )
    else:
        recorded = record_delivery(
            session,
            binding,
            observation,
            disposition=disposition,
            service_identity="test",
            run_identity=uuid4().hex,
            refusal_reason=refusal_reason,
        )
    row = session.get(SourceDelivery, recorded.delivery_id)
    # The recorded arrival is declared, never a clock reading: the watermark
    # rule turns on it, so a test that let `now()` set it would prove nothing.
    row.received_at = received_at
    if document is not None:
        document.source_delivery_id = row.id
        session.add(document)
    session.flush()
    return row


def _document(session, adopted, *, filename: str, parse_status: str = "parsed"):
    row = Document(
        project_id=adopted.project.id,
        sha256=f"{abs(hash(filename)):064x}"[:64].replace("-", "0"),
        filename=filename,
        doc_type="matrix",
        numbering_scheme="project-unique",
        pages=1,
        parse_status=parse_status,
    )
    session.add(row)
    session.flush()
    return row


def _deltas(session, adopted, rendition, *, revision="UCM workbook revision D"):
    """One unresolved proposed change carried by this rendition's document."""

    rendition.capture(
        fact_type="committed_date",
        value="2026-06-01",
        subject_key=subject(3),
    )
    return tuple(
        int(row.id)
        for row in append_deltas(
            session,
            adopted.project,
            rendition,
            source_revision=revision,
            values=[
                modify(
                    subject_key=subject(3),
                    field_name="committed_date",
                    accepted_value="2026-03-01",
                    proposed_value="2026-06-01",
                    baseline_revision=adopted.revision_id,
                )
            ],
        )
    )


def _read(session, adopted, *, cutoff=CUTOFF):
    return derive_coverage_reading(
        session,
        project_id=adopted.project.id,
        cutoff=cutoff,
        inventory=effective_issue_inventory(session, adopted.project.id, cutoff),
    )


def _confirm(session, adopted, reading, **overrides):
    arguments = {
        "project_id": adopted.project.id,
        "reading": reading,
        "confirmed_reading_digest": reading.reading_digest,
        "principal": COORDINATOR,
        "confirmed_at": CONFIRMED_AT,
        "idempotency_key": f"coverage:{reading.reading_digest}",
    }
    arguments.update(overrides)
    return confirm_coverage(session, **arguments)


def _state(reading, source_name: str) -> str:
    for line in reading.lines:
        if line.source_name == source_name:
            return line.state
    raise AssertionError(f"{source_name} is not in the reading: {reading.lines}")


# --- what the machine derives ----------------------------------------------


def test_a_project_with_no_profile_has_no_coverage_to_confirm(session, adopted):
    """An absence, not an empty reading.

    "This project has read nothing" and "this project is not yet configured to
    issue anything" are different facts, and only one of them is a state a
    coordinator can confirm.
    """

    assert _read(session, adopted) is None


def test_every_delivery_and_every_unlinked_document_is_a_line(session, adopted):
    _configure(session, adopted)
    processed = _document(session, adopted, filename="Revised UCM.xlsx")
    _deliver(
        session,
        adopted,
        name="revised-ucm",
        received_at=CUTOFF - timedelta(days=2),
        document=processed,
    )
    _document(session, adopted, filename="Corpus key dates.pdf")

    reading = _read(session, adopted)

    assert _state(reading, "Revised UCM.xlsx") == "read"
    # A document registered through no transport is still a source, and still
    # read: leaving it out would let coverage look complete while the file the
    # record was built from failed.
    assert _state(reading, "Corpus key dates.pdf") == "read"


def test_a_failed_processing_receipt_reads_as_not_read(session, adopted):
    _configure(session, adopted)
    failed = _document(
        session, adopted, filename="Meeting minutes.pdf", parse_status="failed"
    )
    _deliver(
        session,
        adopted,
        name="minutes",
        received_at=CUTOFF - timedelta(days=1),
        document=failed,
    )

    reading = _read(session, adopted)

    assert _state(reading, "Meeting minutes.pdf") == "failed"
    assert "processing failed" in reading.lines[0].detail


def test_a_quarantined_delivery_reads_as_not_read_and_quotes_its_evidence(
    session, adopted
):
    _configure(session, adopted)
    _deliver(
        session,
        adopted,
        name="held-back",
        received_at=CUTOFF - timedelta(days=1),
        disposition=DISPOSITION_QUARANTINED,
        refusal_reason="the scanner flagged the attachment",
    )

    reading = _read(session, adopted)

    assert _state(reading, "held-back") == "failed"
    assert "the scanner flagged the attachment" in reading.lines[0].detail


def test_a_transient_failure_is_not_read_and_is_not_called_a_refusal(
    session, adopted
):
    """The provider said nothing about the delivery, which is its own fact."""

    _configure(session, adopted)
    _deliver(
        session,
        adopted,
        name="timed-out",
        received_at=CUTOFF - timedelta(days=1),
        disposition=DISPOSITION_TRANSIENT_FAILURE,
        refusal_reason="the provider returned a 500",
    )

    reading = _read(session, adopted)

    assert _state(reading, "timed-out") == "failed"
    assert "did not complete" in reading.lines[0].detail


def test_a_stored_delivery_nobody_processed_is_not_read(session, adopted):
    _configure(session, adopted)
    _deliver(session, adopted, name="untouched", received_at=CUTOFF - timedelta(1))

    reading = _read(session, adopted)

    assert _state(reading, "untouched") == "failed"
    assert "no processing receipt" in reading.lines[0].detail


def test_coverage_separates_an_unconfirmed_upload_from_a_confirmed_one(
    session, adopted
):
    """Stored is not admitted, and the reading says which of the two it is.

    A delivery is stored the moment Corridor holds the exact bytes, which for a
    product upload happens before the person has decided anything. Reading an
    upload nobody confirmed as "no processing receipt records it" described a
    failure that had not happened; what had happened is that nobody had asked
    for it to be read yet (#823).
    """

    _configure(session, adopted)
    staged = _deliver(
        session,
        adopted,
        name="handed-over.xlsx",
        received_at=CUTOFF - timedelta(days=1),
        uploaded_by=COORDINATOR.subject,
    )

    reading = _read(session, adopted)
    assert _state(reading, "handed-over.xlsx") == "failed"
    assert "nobody has confirmed it" in reading.lines[0].detail

    confirm_delivery(session, delivery=staged, principal=COORDINATOR)

    confirmed = _read(session, adopted)
    assert _state(confirmed, "handed-over.xlsx") == "failed"
    assert "confirmed, and no processing receipt" in confirmed.lines[0].detail
    # The distinction is inside the digest a coordinator confirms, so the two
    # readings are not interchangeable.
    assert confirmed.reading_digest != reading.reading_digest


def test_the_watermark_is_the_prefix_before_the_first_late_arrival(session, adopted):
    """Membership is an identity boundary, not a timestamp comparison.

    The watermark is the last delivery before the first one that arrived after
    the cutoff, so the included set is a genuine prefix of the append-only
    ledger. A boundary that admitted a later id while excluding an earlier one
    would not be a boundary a Proposed Delta could be compared against.
    """

    _configure(session, adopted)
    first = _deliver(session, adopted, name="a", received_at=CUTOFF - timedelta(2))
    second = _deliver(session, adopted, name="b", received_at=CUTOFF - timedelta(1))
    third = _deliver(session, adopted, name="c", received_at=CUTOFF + timedelta(1))
    assert first.id < second.id < third.id

    reading = _read(session, adopted)

    assert reading.through_source_delivery_id == second.id
    assert _state(reading, "c") == "late"
    assert "after this issue's cutoff" in reading.line(f"delivery:{third.id}").detail


def test_a_project_with_no_delivery_has_an_explicit_absent_watermark(
    session, adopted
):
    """``None`` is the honest watermark, and it is never read as everything."""

    _configure(session, adopted)

    assert _read(session, adopted).through_source_delivery_id is None


# --- what a person may add --------------------------------------------------


def test_confirming_records_the_derived_reading_and_the_declaration_apart(
    session, adopted
):
    _configure(session, adopted)
    document = _document(session, adopted, filename="Revised UCM.xlsx")
    _deliver(
        session,
        adopted,
        name="revised-ucm",
        received_at=CUTOFF - timedelta(1),
        document=document,
    )
    reading = _read(session, adopted)

    declaration = _confirm(session, adopted, reading)

    assert declaration.derived_reading_digest == reading.reading_digest
    assert declaration.declaration_digest != reading.reading_digest
    assert declaration.confirmed_by_principal == COORDINATOR.subject
    assert declaration.cutoff_at == CUTOFF
    assert declaration.through_source_delivery_id == reading.through_source_delivery_id
    assert {line.source_name: line.state for line in declared_lines(declaration)}[
        "Revised UCM.xlsx"
    ] == "read"


def test_a_bounded_annotation_is_recorded_beside_the_line_it_names(session, adopted):
    _configure(session, adopted)
    _deliver(session, adopted, name="untouched", received_at=CUTOFF - timedelta(1))
    reading = _read(session, adopted)
    key = reading.lines[0].source_key

    declaration = _confirm(
        session,
        adopted,
        reading,
        annotations=(CoverageAnnotation(key, "the utility resent this by post"),),
    )

    assert "the utility resent this by post" in declaration.declaration
    # And the machine's own line is untouched by it.
    assert {
        line.source_name: line.state for line in declared_lines(declaration)
    }["untouched"] == "failed"


def test_an_optional_source_may_be_left_out_with_a_reason(session, adopted):
    """A permitted exclusion, where the project requires nothing of coverage."""

    _configure(session, adopted)
    _deliver(session, adopted, name="untouched", received_at=CUTOFF - timedelta(1))
    reading = _read(session, adopted)
    key = reading.lines[0].source_key

    declaration = _confirm(
        session,
        adopted,
        reading,
        exclusions=(
            CoverageExclusion(key, "the customer asked for this week without it"),
        ),
    )

    lines = {line.source_name: line for line in declared_lines(declaration)}
    assert lines["untouched"].state == "excluded"
    assert lines["untouched"].detail == "the customer asked for this week without it"


def test_an_annotation_longer_than_the_bound_is_refused(session, adopted):
    _configure(session, adopted)
    _deliver(session, adopted, name="untouched", received_at=CUTOFF - timedelta(1))
    reading = _read(session, adopted)

    with pytest.raises(CoverageRefused, match="at most"):
        _confirm(
            session,
            adopted,
            reading,
            annotations=(
                CoverageAnnotation(
                    reading.lines[0].source_key, "x" * (ANNOTATION_LIMIT + 1)
                ),
            ),
        )


# --- what a person may never do ---------------------------------------------


def test_a_required_source_cannot_be_excluded(session, adopted):
    """ADR-0086 makes unmet required coverage a blocker, not a declaration.

    The way past a blocked issue is a source that gets read. A declaration that
    could settle it would let a package claim a review nobody performed.
    """

    _configure(session, adopted, coverage=[EVERY_SOURCE_READ])
    failed = _document(
        session, adopted, filename="Meeting minutes.pdf", parse_status="failed"
    )
    _deliver(
        session,
        adopted,
        name="minutes",
        received_at=CUTOFF - timedelta(1),
        document=failed,
    )
    reading = _read(session, adopted)

    assert reading.excludable_source_keys == frozenset()
    with pytest.raises(CoverageRefused, match="requires for an issue"):
        _confirm(
            session,
            adopted,
            reading,
            exclusions=(
                CoverageExclusion(
                    reading.lines[0].source_key, "we are issuing without it"
                ),
            ),
        )


def test_a_coordinator_cannot_relabel_a_failed_source_as_read(session, adopted):
    """The forbidden act has no shape this seam can be given.

    ``confirm_coverage`` takes annotations and exclusions and nothing else, so
    a caller holding a failed line has no argument that would make it read.
    The state that reaches the declaration is derived, and this asserts the
    derived one survives every human addition the seam does accept.
    """

    _configure(session, adopted)
    failed = _document(
        session, adopted, filename="Meeting minutes.pdf", parse_status="failed"
    )
    _deliver(
        session,
        adopted,
        name="minutes",
        received_at=CUTOFF - timedelta(1),
        document=failed,
    )
    reading = _read(session, adopted)
    key = reading.lines[0].source_key

    declaration = _confirm(
        session,
        adopted,
        reading,
        annotations=(CoverageAnnotation(key, "I read the minutes myself"),),
        exclusions=(CoverageExclusion(key, "we are issuing without it"),),
    )

    states = {line.source_name: line.state for line in declared_lines(declaration)}
    assert states["Meeting minutes.pdf"] == "excluded"
    assert states["Meeting minutes.pdf"] != "read"


def test_a_late_source_cannot_be_declared_part_of_this_issue(session, adopted):
    _configure(session, adopted)
    _deliver(session, adopted, name="a", received_at=CUTOFF - timedelta(1))
    late = _deliver(session, adopted, name="b", received_at=CUTOFF + timedelta(1))
    reading = _read(session, adopted)

    declaration = _confirm(session, adopted, reading)

    states = {line.source_name: line.state for line in declared_lines(declaration)}
    assert states["b"] == "late"
    assert declaration.through_source_delivery_id != late.id


def test_confirming_a_digest_other_than_the_one_shown_is_refused(session, adopted):
    """The whole reason the digest travels through the form.

    A submission composed against an older reading would otherwise be recorded
    as a confirmation of whatever the database happened to say when it landed.
    """

    _configure(session, adopted)
    _deliver(session, adopted, name="a", received_at=CUTOFF - timedelta(2))
    shown = _read(session, adopted)
    # A source arrives between the reading and the confirmation.
    _deliver(session, adopted, name="b", received_at=CUTOFF - timedelta(1))
    current = _read(session, adopted)
    assert current.reading_digest != shown.reading_digest

    with pytest.raises(CoverageRefused, match="changed after it was shown"):
        _confirm(
            session,
            adopted,
            current,
            confirmed_reading_digest=shown.reading_digest,
        )


def test_a_naive_confirmation_instant_is_refused(session, adopted):
    _configure(session, adopted)
    reading = _read(session, adopted)

    with pytest.raises(CoverageRefused, match="time-zone-aware"):
        _confirm(
            session, adopted, reading, confirmed_at=datetime(2026, 3, 2, 7, 0)
        )


def test_a_confirmed_declaration_cannot_be_edited(session, adopted):
    """It is a record of something that happened, so PostgreSQL refuses one."""

    from sqlalchemy.exc import DBAPIError

    _configure(session, adopted)
    declaration = _confirm(session, adopted, _read(session, adopted))
    session.flush()

    with pytest.raises(DBAPIError, match="immutable"):
        session.execute(
            IssueCoverageDeclaration.__table__.update()
            .where(IssueCoverageDeclaration.id == declaration.id)
            .values(confirmed_by_principal="local:someone-else")
        )


# --- reuse -------------------------------------------------------------------


def test_a_declaration_is_reused_while_every_input_is_unchanged(session, adopted):
    _configure(session, adopted)
    _deliver(session, adopted, name="a", received_at=CUTOFF - timedelta(1))
    declaration = _confirm(session, adopted, _read(session, adopted))

    assert (
        reusable_declaration(
            session, project_id=adopted.project.id, reading=_read(session, adopted)
        ).id
        == declaration.id
    )


def test_a_new_delivery_ends_the_reuse(session, adopted):
    """A newly received source is never silently omitted from an issue."""

    _configure(session, adopted)
    _deliver(session, adopted, name="a", received_at=CUTOFF - timedelta(2))
    _confirm(session, adopted, _read(session, adopted))

    _deliver(session, adopted, name="b", received_at=CUTOFF - timedelta(1))

    assert (
        reusable_declaration(
            session, project_id=adopted.project.id, reading=_read(session, adopted)
        )
        is None
    )


def test_a_newly_failed_source_ends_the_reuse(session, adopted):
    _configure(session, adopted)
    document = _document(session, adopted, filename="Revised UCM.xlsx")
    _deliver(
        session,
        adopted,
        name="a",
        received_at=CUTOFF - timedelta(1),
        document=document,
    )
    _confirm(session, adopted, _read(session, adopted))

    document.parse_status = "failed"
    session.flush()

    assert (
        reusable_declaration(
            session, project_id=adopted.project.id, reading=_read(session, adopted)
        )
        is None
    )


def test_resolving_a_delta_does_not_end_the_reuse(session, adopted, tmp_path):
    """The one case the reuse rule exists to permit.

    The reading binds no accepted revision, so a coordinator who settles a
    Proposed Delta is not asked to confirm the same coverage a second time.
    """

    _configure(session, adopted)
    _deliver(session, adopted, name="a", received_at=CUTOFF - timedelta(1))
    declaration = _confirm(session, adopted, _read(session, adopted))
    before = _read(session, adopted).reading_digest

    rendition = Rendition(session=session, project=adopted.project, name="later.xlsx")
    _deltas(session, adopted, rendition)

    after = _read(session, adopted)
    assert after.reading_digest != before, (
        "the new document is a new source and the reading must say so"
    )
    # ...and once that source is in the confirmed reading, nothing about the
    # record moving changes it again.
    renewed = _confirm(session, adopted, after)
    assert renewed.id != declaration.id
    assert (
        reusable_declaration(
            session, project_id=adopted.project.id, reading=_read(session, adopted)
        ).id
        == renewed.id
    )


# --- the boundary a Proposed Delta is compared against -----------------------


def test_a_delta_dereferences_the_delivery_that_carried_its_source(
    session, adopted, tmp_path
):
    _configure(session, adopted)
    rendition = Rendition(session=session, project=adopted.project, name="later.xlsx")
    delivery = _deliver(
        session,
        adopted,
        name="later-ucm",
        received_at=CUTOFF - timedelta(1),
        document=rendition.document,
    )
    delta_ids = _deltas(session, adopted, rendition)

    found = delivery_ids_for_deltas(
        session, project_id=adopted.project.id, delta_ids=delta_ids
    )

    assert set(found.values()) == {delivery.id}


def test_a_delta_whose_source_arrived_after_the_boundary_can_wait(
    session, adopted, tmp_path
):
    """ADR-0085's second Can wait limb, closed by identity and not by a clock."""

    _configure(session, adopted)
    _deliver(session, adopted, name="in-scope", received_at=CUTOFF - timedelta(2))
    declaration = _confirm(session, adopted, _read(session, adopted))

    rendition = Rendition(session=session, project=adopted.project, name="late.xlsx")
    _deliver(
        session,
        adopted,
        name="late-ucm",
        received_at=CUTOFF + timedelta(1),
        document=rendition.document,
    )
    delta_ids = _deltas(session, adopted, rendition)

    outside = deltas_outside_coverage_boundary(
        session,
        project_id=adopted.project.id,
        delta_ids=delta_ids,
        declaration=declaration,
    )
    assert outside == frozenset(delta_ids)

    content = effective_issue_content(
        session, effective_issue_inventory(session, adopted.project.id, CUTOFF)
    )
    level = consequence_level(
        content,
        ChangeFacts(field="committed_date", change_type="modify"),
        source_outside_coverage_boundary=True,
    )
    assert level.name == CAN_WAIT
    assert level.reasons == (OUTSIDE_BOUNDARY_SENTENCE,)


def test_the_boundary_limb_outranks_a_customer_policy(session, adopted):
    """A source the issue excluded cannot be something the issue must state.

    Ranking it under "Must handle before this issue" would tell a coordinator
    to settle, before Friday, a difference that arrived after the week they are
    closing.
    """

    from corridor.issue_profile import DecisionBlockingPolicy

    configure_issue(
        session,
        adopted.project,
        principal=COORDINATOR,
        effective_from=JANUARY,
        ucm=UCM_RENDERER,
        policies=(
            DecisionBlockingPolicy(
                policy="resolve_before_issue:v1",
                required_decision="field:committed_date",
                statement="A moved Promised For is decided before this issue.",
            ),
        ),
    )
    content = effective_issue_content(
        session, effective_issue_inventory(session, adopted.project.id, CUTOFF)
    )
    change = ChangeFacts(field="committed_date", change_type="modify")

    assert consequence_level(content, change).name == MUST_HANDLE
    assert (
        consequence_level(
            content, change, source_outside_coverage_boundary=True
        ).name
        == CAN_WAIT
    )


def test_a_delta_that_dereferences_no_delivery_claims_nothing(
    session, adopted, tmp_path
):
    """A reading never claims a check it did not make.

    A document registered through the corpus path carries no delivery, so
    nothing about it has been shown to be late. Calling it "Can wait" would be
    an assertion nobody's records support.
    """

    _configure(session, adopted)
    _deliver(session, adopted, name="in-scope", received_at=CUTOFF - timedelta(2))
    declaration = _confirm(session, adopted, _read(session, adopted))
    rendition = Rendition(session=session, project=adopted.project, name="corpus.xlsx")
    delta_ids = _deltas(session, adopted, rendition)

    assert rendition.document.source_delivery_id is None
    assert (
        deltas_outside_coverage_boundary(
            session,
            project_id=adopted.project.id,
            delta_ids=delta_ids,
            declaration=declaration,
        )
        == frozenset()
    )


def test_without_a_confirmed_declaration_nothing_is_outside_the_boundary(
    session, adopted, tmp_path
):
    _configure(session, adopted)
    rendition = Rendition(session=session, project=adopted.project, name="late.xlsx")
    _deliver(
        session,
        adopted,
        name="late-ucm",
        received_at=CUTOFF + timedelta(1),
        document=rendition.document,
    )
    delta_ids = _deltas(session, adopted, rendition)

    assert latest_declaration(
        session, project_id=adopted.project.id, cutoff=CUTOFF
    ) is None
    assert (
        deltas_outside_coverage_boundary(
            session,
            project_id=adopted.project.id,
            delta_ids=delta_ids,
            declaration=None,
        )
        == frozenset()
    )


def test_a_declaration_of_another_project_cannot_be_loaded(session, adopted):
    _configure(session, adopted)
    declaration = _confirm(session, adopted, _read(session, adopted))
    other = Project(slug=f"other-{uuid4().hex[:8]}", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()

    with pytest.raises(CoverageRefused, match="no confirmed coverage declaration"):
        load_declaration(
            session, project_id=other.id, declaration_id=declaration.id
        )


def test_a_late_delivery_can_wait_end_to_end_from_intake(session, adopted, tmp_path):
    """The Can wait limb fires for a delta whose Document intake linked (#687).

    Nothing here writes `documents.source_delivery_id`. The link is made by the
    later-revision intake itself, in the transaction that registers the
    revision, from the very delivery row that path already refuses to run
    without. That is the whole point of the ticket: before it, the limb was
    complete and inert because no ingress path wrote the column.

    The reading is taken twice, and the first time arms the second. Before any
    coverage is confirmed the change is not "Can wait" — so the fixture is a
    delta that genuinely would reach this issue, and the second reading is a
    change of level rather than a level it always had. No instant here comes
    from a clock: both recorded arrivals are declared, and the boundary itself
    is compared by append-only identity.
    """

    _configure(session, adopted)
    in_scope = _deliver(
        session, adopted, name="in-scope-ucm", received_at=CUTOFF - timedelta(days=2)
    )

    revised = [list(row) for row in BASELINE_ROWS]
    revised[0][HEADINGS.index("Promised For")] = "2026-06-01"
    staged, envelope = deliver(
        session,
        adopted.project,
        workbook_bytes(tmp_path / "ucm-later.xlsx", revised),
    )
    late = session.scalars(
        select(SourceDelivery).where(
            SourceDelivery.idempotency_key == envelope.idempotency_key
        )
    ).one()
    # Declared, exactly as `_deliver` declares its own: the watermark rule turns
    # on this instant, so letting the server clock set it would make the
    # boundary an accident of when the suite ran.
    late.received_at = CUTOFF + timedelta(days=1)
    session.flush()

    capture = capture_later_revision(
        session,
        project=adopted.project,
        staged=staged,
        envelope=envelope,
        manifest=adopted.manifest,
        declaration=RevisionDeclaration(declared_by=PRINCIPAL),
        images_dir=tmp_path / "images",
    )

    # Intake wrote the link, and it names the delivery this revision arrived on.
    document = session.get(Document, capture.document_id)
    assert document.source_delivery_id == late.id
    assert capture.delta_ids

    def _levels():
        reading = read_review_items(
            session, project_id=adopted.project.id, as_of=CUTOFF
        )
        return [
            child.consequence
            for item in reading.items
            for child in item.children
            if child.delta_id in capture.delta_ids
        ]

    before = _levels()
    assert before, "the revision produced no readable child to judge"
    assert all(level.name != CAN_WAIT for level in before), (
        "with no coverage confirmed this change must still reach the issue, or "
        f"the fixture proves nothing: {[level.name for level in before]}"
    )

    reading = _read(session, adopted)
    assert reading.through_source_delivery_id == in_scope.id
    declaration = _confirm(session, adopted, reading)
    assert declaration.through_source_delivery_id < late.id

    after = _levels()
    assert [level.name for level in after] == [CAN_WAIT] * len(after)
    assert all(level.reasons == (OUTSIDE_BOUNDARY_SENTENCE,) for level in after)
