"""The read-only record and history investigation view (#642).

#536 gave an adopted project its ordered week, which answers *what needs
deciding*.  Nothing answered *what does this Utility Conflict say today, what
did it say in July, and who changed it*, so this is the reading half.

The properties under test are the ones the ticket names, and the first of them
is a boundary rather than a feature: the view must expose no way to accept,
resolve, correct, refuse or authorize anything.  That is proved twice — once
against the routing table, which is the only place a mutating door could
actually exist, and once against the rendered HTML, which is where a control
would appear.  Reading the page writes nothing at all, not even a receipt for
having looked.

Authorization is the project's plain *read* boundary, deliberately not the
coordination designation #537 uses: a member enrolled with no designation at
all may still read this project's history.

Nothing here reads a clock.  A revision selects the as-of reading, and the
as-of correctness test proves it by moving the revision and watching the answer
change.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor import access, audit
from corridor.db import engine
from corridor.models import (
    AuditLog,
    DeltaDisposition,
    DeltaFollowUpPlan,
    DeltaRecordDecision,
    DeltaReviewPacketReceipt,
    ExternalReportArtifact,
    ExternalReportRelease,
    Fact,
    Project,
    ProjectRecordRevision,
)
from corridor.operating_mode import adopt_project_baseline
from corridor.packet_review import read_review_items, packet_request
from corridor.principals import HumanPrincipal
from corridor.record_history import (
    APPLIED,
    KEPT_CURRENT,
    OPEN,
    SUPERSEDED,
    SearchTerms,
    UnknownRevision,
    read_record_history,
    readable_terms,
)
from corridor.record_projection import (
    read_current_project_record,
    read_project_record_as_of_revision,
)
from corridor.proposed_deltas import record_delta_supersession
from corridor.report_release import external_report_release_history
from corridor.review_packets import APPLY, KEEP_CURRENT, resolve_review_packet
from corridor.web.app import app, get_human_principal, get_session

from access_support import seed_membership
from packet_review_support import (
    Rendition,
    accept_baseline_fact,
    append_deltas,
    modify,
    register_baseline,
    register_output_template,
    register_source_row,
    subject,
    support,
)


COORDINATOR = HumanPrincipal("local:coordinator")
READER = HumanPrincipal("local:reader")
OUTSIDER = HumanPrincipal("local:outsider")
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
DECIDED_AT = datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc)
ROW = 42
OTHER_ROW = 43

# Every outcome token the decision surfaces submit. None may appear here.
DECISION_TOKENS = (
    "apply",
    "keep_current",
    "edit_and_apply",
    "needs_coordination",
    "defer",
)


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
def project(session: Session) -> Project:
    row = Project(
        slug=f"history-{uuid4().hex[:8]}", name="Record history", is_synthetic=True
    )
    session.add(row)
    session.flush()
    seed_membership(session, row, COORDINATOR)
    return row


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
    with TestClient(app) as made:
        yield made
    app.dependency_overrides.clear()


# --- one adopted project with a real change behind it ----------------------


ACCEPTED = "2026-11-01"
INCOMING = "2026-12-15"


class Adopted:
    """An adopted-baseline project carrying its own accepted values.

    The shape every test here needs: a baseline the incoming value is compared
    against, the customer's own row identifiers, the captured Source Facts and
    their Support Assessments, and the deltas a later source raised.
    """

    def __init__(self, session: Session, project: Project):
        self.session = session
        self.project = project
        self.source = Rendition(session, project, "ucm-2026-08.xlsx")
        self.revision_of: dict[tuple[str, str], int] = {}
        self.fact_of: dict[tuple[str, str], Fact] = {}
        self.renditions: dict[str, Rendition] = {}
        self.baseline = None

    def accepted(self, *numbers: int) -> "Adopted":
        first: int | None = None
        for number in numbers:
            fact, segment = self.source.capture(
                fact_type="committed_date",
                value=ACCEPTED,
                subject_key=subject(number),
                date_value=date.fromisoformat(ACCEPTED),
            )
            revision = accept_baseline_fact(self.session, self.project, fact)
            support(self.session, self.project, fact, segment)
            first = first or revision
            self.revision_of[(subject(number), "committed_date")] = revision
            self.fact_of[(subject(number), "committed_date")] = fact
        assert first is not None
        self.baseline = register_baseline(
            self.session, self.project, self.source.document, first
        )
        for number in numbers:
            register_source_row(
                self.session,
                self.project,
                self.baseline,
                row_number=number,
                business_identity=f"U-{number:03d}",
            )
        return self

    def template(self) -> "Adopted":
        register_output_template(
            self.session, self.project, identity="district-ucm-template", version="v3"
        )
        return self

    def adopt(self) -> "Adopted":
        adopt_project_baseline(
            self.session,
            project_id=self.project.id,
            adopted_by_principal="local:adopter",
            baseline_source_sha256=self.source.document.sha256,
            importer_identity="record_history_fixture",
            importer_version="v1",
            idempotency_key=f"adopt:{uuid4().hex[:10]}",
        )
        self.session.expire_all()
        return self

    def rendition(self, name: str) -> Rendition:
        if name not in self.renditions:
            self.renditions[name] = Rendition(self.session, self.project, name)
        return self.renditions[name]

    def answer(
        self,
        *,
        document: str,
        family: str,
        revision: str,
        value: str,
        number: int = ROW,
    ):
        """One later source's own value for a Promised For, captured and supported."""

        rendition = self.rendition(document)
        fact, segment = rendition.capture(
            fact_type="committed_date",
            value=value,
            subject_key=subject(number),
            date_value=date.fromisoformat(value),
        )
        support(self.session, self.project, fact, segment)
        return append_deltas(
            self.session,
            self.project,
            rendition,
            source_revision=revision,
            source_family=family,
            values=[
                modify(
                    subject_key=subject(number),
                    field_name="committed_date",
                    accepted_value=ACCEPTED,
                    proposed_value=value,
                    baseline_revision=self.revision_of[
                        (subject(number), "committed_date")
                    ],
                )
            ],
            is_complete_enumerative_source=False,
            row_accounting_sealed=False,
        )


def _adopted(session: Session, project: Project) -> Adopted:
    """One adopted project with one later source proposing one change."""

    adopted = Adopted(session, project).accepted(ROW, OTHER_ROW).template()
    adopted.answer(
        document="ucm-2026-09.xlsx",
        family="ucm-workbook",
        revision="2026-09",
        value=INCOMING,
    )
    return adopted.adopt()


def _settle(session: Session, project: Project, outcome: str) -> int:
    """Answer the one open item with ``outcome`` and return its revision."""

    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    item = next(item for item in reading.items if item.children)
    request = packet_request(
        reading,
        item,
        outcome=outcome,
        principal=COORDINATOR,
        decided_at=DECIDED_AT,
        delta_ids=[child.delta_id for child in item.children],
    )
    result = resolve_review_packet(session, request)
    assert result.status == "saved", result
    session.expire_all()
    return int(result.revision_id)


def _write_counts(session: Session, project: Project) -> tuple[int, ...]:
    """Every row family this page could conceivably be accused of writing."""

    spine = tuple(
        session.scalar(
            select(func.count()).select_from(model).where(model.project_id == project.id)
        )
        for model in (
            ProjectRecordRevision,
            DeltaRecordDecision,
            DeltaDisposition,
            DeltaFollowUpPlan,
            DeltaReviewPacketReceipt,
        )
    )
    return (*spine, session.scalar(select(func.count()).select_from(AuditLog)))


# --- current and as-of readings come from the projection -------------------


def test_the_current_reading_is_the_projection_reader(session, project):
    """No second derivation of what the record says now (ADR-0075)."""

    _adopted(session, project)

    history = read_record_history(session, project_id=project.id)
    projected = read_current_project_record(session, project.id)

    assert {
        (row.subject_key, row.field_key, row.current.value_text)
        for row in history.values
    } == {
        (value.subject_key, value.fact_type, value.date_value.isoformat())
        for value in projected
    }
    assert {row.current.revision_id for row in history.values} == {
        value.revision_id for value in projected
    }


def test_the_as_of_reading_is_the_projection_at_that_revision(session, project):
    """"What did it say in July" is the record's own as-of projection."""

    adopted = _adopted(session, project)
    baseline_revision = adopted.revision_of[(subject(ROW), "committed_date")]
    _settle(session, project, APPLY)

    history = read_record_history(
        session,
        project_id=project.id,
        terms=SearchTerms(revision=baseline_revision),
    )
    projected = read_project_record_as_of_revision(
        session, project.id, baseline_revision
    )

    changed = next(row for row in history.values if row.subject_key == subject(ROW))
    assert changed.current.value_text == INCOMING
    assert changed.as_of.value_text == ACCEPTED
    assert changed.changed is True
    assert {
        (row.subject_key, row.field_key, row.as_of.value_text)
        for row in history.values
        if row.as_of
    } == {
        (value.subject_key, value.fact_type, value.date_value.isoformat())
        for value in projected
    }


def test_moving_the_revision_moves_the_answer(session, project):
    """The as-of column is bound to the revision, not to a constant."""

    adopted = _adopted(session, project)
    baseline_revision = adopted.revision_of[(subject(ROW), "committed_date")]
    applied_revision = _settle(session, project, APPLY)
    assert applied_revision > baseline_revision

    before = read_record_history(
        session, project_id=project.id, terms=SearchTerms(revision=baseline_revision)
    )
    after = read_record_history(
        session, project_id=project.id, terms=SearchTerms(revision=applied_revision)
    )

    def promised(history):
        row = next(row for row in history.values if row.subject_key == subject(ROW))
        return row.as_of.value_text

    assert promised(before) == ACCEPTED
    assert promised(after) == INCOMING
    assert promised(before) != promised(after)


def test_a_revision_this_project_does_not_hold_is_refused(session, project, client):
    """A mistyped revision is answered, never quietly treated as "current"."""

    _adopted(session, project)
    highest = max(row.revision_id for row in read_record_history(
        session, project_id=project.id
    ).revisions)

    with pytest.raises(UnknownRevision):
        read_record_history(
            session, project_id=project.id, terms=SearchTerms(revision=highest + 5000)
        )

    page = client.get(f"/record/{project.slug}?revision={highest + 5000}")

    assert page.status_code == 200
    assert f"Revision {highest + 5000} does not belong to this project" in page.text


# --- the Source Facts and sources behind a value ---------------------------


def test_a_value_names_the_source_fact_and_the_exact_passage(session, project, client):
    """Criterion 2: the Source Facts behind a value and where they came from."""

    adopted = _adopted(session, project)
    fact = adopted.fact_of[(subject(ROW), "committed_date")]

    history = read_record_history(session, project_id=project.id)
    row = next(row for row in history.values if row.subject_key == subject(ROW))

    assert row.current.fact.fact_id == fact.id
    assert [source.filename for source in row.current.sources] == [
        "ucm-2026-08.xlsx"
    ]
    reference = row.current.sources[0]
    assert reference.exact_text == ACCEPTED
    assert reference.locator.startswith("sheet Utility Conflicts, cell ")

    body = client.get(f"/record/{project.slug}").text
    assert "ucm-2026-08.xlsx" in body
    assert reference.locator in body
    assert ACCEPTED in body


def test_support_assessments_for_a_value_are_shown_with_their_authority(
    session, project, client
):
    """Criterion 3: the Support Assessments recorded for the accepted value."""

    _adopted(session, project)

    history = read_record_history(session, project_id=project.id)
    row = next(row for row in history.values if row.subject_key == subject(ROW))

    assert [
        (assessment.evidence_role, assessment.assessment, assessment.effective)
        for assessment in row.current.assessments
    ] == [("value_support", "supported", True)]
    assert row.current.assessments[0].authority == "local:assessor"
    assert [
        source.filename for source in row.current.assessments[0].segments
    ] == ["ucm-2026-08.xlsx"]

    body = client.get(f"/record/{project.slug}").text
    assert "Support Assessments recorded for this value" in body
    assert "value_support" in body


# --- Proposed Delta and resolution history ---------------------------------


def test_an_applied_change_keeps_its_decision_and_its_revision(session, project):
    """Criterion 4: resolution history, bound to the revision it produced."""

    _adopted(session, project)
    revision = _settle(session, project, APPLY)

    history = read_record_history(session, project_id=project.id)

    assert len(history.deltas) == 1
    entry = history.deltas[0]
    assert entry.standing == APPLIED
    assert entry.decided_by == COORDINATOR.subject
    assert entry.revision_id == revision
    assert entry.accepted_value_text == ACCEPTED
    assert entry.proposed_value_text == INCOMING


def test_a_refused_change_is_kept_and_named(session, project, client):
    """A change the coordinator answered Keep current does not disappear."""

    _adopted(session, project)
    _settle(session, project, KEEP_CURRENT)

    history = read_record_history(session, project_id=project.id)

    assert [entry.standing for entry in history.deltas] == [KEPT_CURRENT]
    assert KEPT_CURRENT in client.get(f"/record/{project.slug}").text


def test_a_superseded_change_is_kept_and_named(session, project, client):
    """A change a later source revision replaced is history, not nothing."""

    adopted = _adopted(session, project)
    later = adopted.answer(
        document="ucm-2026-10.xlsx",
        family="ucm-workbook",
        revision="2026-10",
        value="2027-02-01",
    )
    prior = read_record_history(session, project_id=project.id).deltas[0]
    record_delta_supersession(
        session,
        project_id=project.id,
        prior_delta_id=prior.delta_id,
        superseding_delta_id=later[0].id,
        reason="newer_source_revision",
    )
    session.flush()

    history = read_record_history(session, project_id=project.id)

    standings = {entry.delta_id: entry.standing for entry in history.deltas}
    assert standings[prior.delta_id] == SUPERSEDED
    assert standings[later[0].id] == OPEN
    body = client.get(f"/record/{project.slug}").text
    assert SUPERSEDED in body
    assert f"Superseded by proposed change {later[0].id}" in body


def test_the_packet_act_that_touched_a_change_is_named(session, project, client):
    """Criterion 5: which guided packet act settled this proposed change."""

    _adopted(session, project)
    _settle(session, project, APPLY)

    history = read_record_history(session, project_id=project.id)

    packets = history.deltas[0].packets
    assert len(packets) == 1
    assert packets[0].outcome == APPLY
    assert packets[0].decided_by_principal == COORDINATOR.subject
    assert packets[0].undone is False
    assert f"packet act {packets[0].receipt_id}" in client.get(
        f"/record/{project.slug}"
    ).text


def test_the_selected_revision_says_what_it_settled(session, project, client):
    """"Who changed it" is answered by the revision timeline itself."""

    _adopted(session, project)
    revision = _settle(session, project, APPLY)

    history = read_record_history(
        session, project_id=project.id, terms=SearchTerms(revision=revision)
    )

    assert [entry.delta_id for entry in history.revision_changes] == [
        history.deltas[0].delta_id
    ]
    assert any(
        entry.revision_id == revision and entry.authority == COORDINATOR.subject
        for entry in history.revisions
    )
    body = client.get(f"/record/{project.slug}?revision={revision}").text
    assert f"What revision {revision} settled" in body


# --- release history and project-scoped audit ------------------------------


def test_release_history_is_the_release_reader(session, project, client):
    """Criterion 6, read through the one reader that already owns it."""

    _adopted(session, project)
    artifact = ExternalReportArtifact(
        project_id=project.id,
        artifact_name="coordination-report-2026-09.pdf",
        pdf_bytes=b"%PDF-1.4 fixture",
        pdf_sha256=sha256(b"%PDF-1.4 fixture").hexdigest(),
        evaluated_on=date(2026, 9, 2),
        ruleset_version="v1",
        evaluation_context_json={},
        provenance_mode="all-supported-sources",
        record_context_json={"dependencies": [{"ref_code": "C-1"}]},
    )
    session.add(artifact)
    session.flush()
    session.add(
        ExternalReportRelease(
            project_id=project.id,
            artifact_id=artifact.id,
            artifact_name=artifact.artifact_name,
            content_storage="artifact",
            pdf_sha256=artifact.pdf_sha256,
            evaluated_on=artifact.evaluated_on,
            ruleset_version=artifact.ruleset_version,
            provenance_mode=artifact.provenance_mode,
            released_by="local:releaser",
            released_by_display="A Releaser",
            released_at=DECIDED_AT,
        )
    )
    session.flush()

    history = read_record_history(session, project_id=project.id)

    assert history.releases == external_report_release_history(session, project.id)
    assert [release.artifact_name for release in history.releases] == [
        "coordination-report-2026-09.pdf"
    ]
    body = client.get(f"/record/{project.slug}").text
    assert "coordination-report-2026-09.pdf" in body
    assert "A Releaser" in body


def test_the_audit_trail_is_scoped_to_this_project(session, project, client):
    """Criterion 7: this project's own entries, and no other project's."""

    _adopted(session, project)
    other = Project(slug=f"other-{uuid4().hex[:8]}", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    mine = audit.record(
        session,
        principal=COORDINATOR,
        action=audit.CONFIRM_SOURCE_INTAKE,
        entity_type=audit.PROJECT,
        entity_id=project.id,
        after={"note": "mine"},
    )
    audit.record(
        session,
        principal=COORDINATOR,
        action=audit.CONFIRM_SOURCE_INTAKE,
        entity_type=audit.PROJECT,
        entity_id=other.id,
        after={"note": "theirs"},
    )

    history = read_record_history(session, project_id=project.id)

    entry_ids = {entry.entry_id for entry in history.audit}
    assert mine.id in entry_ids
    assert all(
        entry.entity_type != "project" or entry.entity_id == project.id
        for entry in history.audit
    )
    assert audit.CONFIRM_SOURCE_INTAKE in client.get(f"/record/{project.slug}").text


# --- search ----------------------------------------------------------------


def test_search_narrows_by_utility_conflict(session, project):
    """Criterion 8, first facet: the customer's own row identifier."""

    _adopted(session, project)
    everything = read_record_history(session, project_id=project.id)
    assert {row.subject_key for row in everything.values} == {
        subject(ROW),
        subject(OTHER_ROW),
    }

    narrowed = read_record_history(
        session, project_id=project.id, terms=SearchTerms(conflict="U-042")
    )

    assert {row.subject_key for row in narrowed.values} == {subject(ROW)}
    assert {entry.subject_key for entry in narrowed.deltas} == {subject(ROW)}


def test_search_narrows_by_source(session, project):
    """Second facet: the file a value was captured from, and a delta's source."""

    _adopted(session, project)

    by_baseline = read_record_history(
        session, project_id=project.id, terms=SearchTerms(source="ucm-2026-08")
    )
    by_later = read_record_history(
        session, project_id=project.id, terms=SearchTerms(source="2026-09")
    )

    assert by_baseline.values
    assert by_baseline.deltas == ()
    assert by_later.values == ()
    assert {entry.source_revision for entry in by_later.deltas} == {"2026-09"}


def test_search_narrows_by_field(session, project):
    """Third facet: the field, by its stored key or its customer label."""

    _adopted(session, project)

    by_key = read_record_history(
        session, project_id=project.id, terms=SearchTerms(field="committed_date")
    )
    by_label = read_record_history(
        session, project_id=project.id, terms=SearchTerms(field="Promised for")
    )
    by_other = read_record_history(
        session, project_id=project.id, terms=SearchTerms(field="Required by")
    )

    assert {row.field_key for row in by_key.values} == {"committed_date"}
    assert {row.subject_key for row in by_label.values} == {
        row.subject_key for row in by_key.values
    }
    assert by_other.values == ()


def test_search_by_revision_selects_the_as_of_reading(session, project, client):
    """Fourth facet: a revision, which is what "as of" means here."""

    adopted = _adopted(session, project)
    baseline_revision = adopted.revision_of[(subject(ROW), "committed_date")]
    _settle(session, project, APPLY)

    body = client.get(f"/record/{project.slug}?revision={baseline_revision}").text

    assert f"Value as of revision {baseline_revision}" in body
    assert "changed since that revision" in body


def test_a_revision_that_is_not_a_number_is_no_revision_at_all(session, project):
    """A query string carries text; the reading refuses to invent a revision."""

    assert readable_terms(revision="not-a-revision").revision is None
    assert readable_terms(revision=" 12 ").revision == 12
    assert readable_terms(revision="").revision is None


# --- the constraint that matters: no second decision door ------------------


def test_the_view_exposes_no_mutating_route(session, project, client):
    """The routing table is where a second door would have to exist.

    Asserting the *rendered HTML* alone would pass a page that quietly served a
    POST at the same prefix, so the methods FastAPI actually registered are
    what this checks. Nothing under `/record` answers anything but a read.
    """

    mounted = {
        (route.path, frozenset(route.methods))
        for route in app.routes
        if getattr(route, "path", "").startswith("/record")
    }

    assert mounted == {("/record/{slug}", frozenset({"GET"}))}
    for method in ("post", "put", "patch", "delete"):
        answered = getattr(client, method)(f"/record/{project.slug}")
        assert answered.status_code == 405, method


def test_the_page_carries_no_decision_control(session, project, client):
    """ADR-0085's exactly-once rule survives a second surface showing the work."""

    _adopted(session, project)

    body = client.get(f"/record/{project.slug}").text

    assert 'method="post"' not in body.lower()
    assert body.lower().count("<form") == 1
    assert 'method="get"' in body
    for token in DECISION_TOKENS:
        assert f'value="{token}"' not in body
    assert "csrf" not in body.lower()
    # The one link back to where a proposed change is actually decided.
    assert f'href="/review/{project.slug}"' in body


def test_reading_the_record_history_records_nothing(session, project, client):
    """Looking is not an act, so there is no receipt for having looked."""

    _adopted(session, project)
    before = _write_counts(session, project)

    for query in ("", "?conflict=U-042", "?field=Promised for", "?source=ucm"):
        assert client.get(f"/record/{project.slug}{query}").status_code == 200

    session.expire_all()
    assert _write_counts(session, project) == before


def test_the_reading_takes_no_time_from_a_clock(session, project):
    """An as-of view is exactly where a stray `datetime.now()` would hide."""

    import inspect

    from corridor import record_history

    assert "datetime.now" not in inspect.getsource(record_history)
    assert "utcnow" not in inspect.getsource(record_history)
    first = read_record_history(session, project_id=project.id)
    second = read_record_history(session, project_id=project.id)
    assert first.values == second.values
    assert first.deltas == second.deltas


# --- authorization ---------------------------------------------------------


def test_a_non_member_is_answered_exactly_like_a_missing_project(
    session, project, client
):
    """Project existence never leaks, exactly as every other surface (#331)."""

    _adopted(session, project)
    app.dependency_overrides[get_human_principal] = lambda: OUTSIDER

    assert client.get(f"/record/{project.slug}").status_code == 404
    assert client.get("/record/no-such-project").status_code == 404


def test_whoever_may_read_the_project_may_read_its_history(session, project, client):
    """The boundary is *read*, not the coordination designation #537 uses.

    A member enrolled with no designation at all may read the project, so they
    may read what its record says and how it got there.
    """

    _adopted(session, project)
    seed_membership(session, project, READER, designations=[])
    membership = access.resolve_membership(session, READER.subject, project.id)
    assert membership is not None and membership.designations == frozenset()
    app.dependency_overrides[get_human_principal] = lambda: READER

    page = client.get(f"/record/{project.slug}")

    assert page.status_code == 200
    assert "Record history" in page.text


# --- how it reads ----------------------------------------------------------


def test_exactly_one_region_takes_focus(session, project, client):
    """A keyboard user lands on the answer, or on the question before one."""

    _adopted(session, project)

    unasked = client.get(f"/record/{project.slug}").text
    asked = client.get(f"/record/{project.slug}?conflict=U-042").text

    for body in (unasked, asked):
        assert body.count("autofocus") == 1
    assert re.search(r'id="search" tabindex="-1" autofocus', unasked)
    assert re.search(r'id="values" tabindex="-1" autofocus', asked)


def test_every_section_is_a_region_bound_to_its_own_heading(session, project, client):
    """`docs/accessibility-acceptance-checklist.md` §2, for this screen."""

    _adopted(session, project)

    body = client.get(f"/record/{project.slug}").text

    for name in ("search", "values", "deltas", "revisions", "releases", "audit"):
        assert f'aria-labelledby="{name}-heading"' in body
        assert f'<h2 id="{name}-heading">' in body


def test_the_page_reads_as_one_document_with_descending_headings(
    session, project, client
):
    """One `main`, one `h1`, no skipped level, and every table captioned."""

    _adopted(session, project)

    body = client.get(f"/record/{project.slug}").text

    assert body.count("<main>") == 1
    assert body.count("<h1>") == 1
    levels = [int(level) for level in re.findall(r"<h([1-6])[ >]", body)]
    assert levels[0] == 1
    for previous, level in zip(levels, levels[1:]):
        assert level <= previous + 1, "a heading level is skipped"
    assert body.count('<table class="record"') == body.count("<caption>")


def test_every_search_control_carries_a_visible_bound_label(
    session, project, client
):
    """§3: a label bound by `for`, and a hint bound by `aria-describedby`."""

    _adopted(session, project)

    body = client.get(f"/record/{project.slug}").text

    for control in ("q-conflict", "q-source", "q-field", "q-revision"):
        assert f'<label for="{control}">' in body
        assert f'id="{control}"' in body
        assert f'id="{control}-hint"' in body
        assert f'aria-describedby="{control}-hint"' in body
    assert 'role="search"' in body
    # Every named landing place stays focusable for a later return (§4).
    assert body.count('tabindex="-1"') == 6
