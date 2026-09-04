"""The report diff's baseline rebuilt from the revision it names (#603).

The corpus these run over is built the way the legacy pipeline builds one: a
Ledger record per row, the same source cells captured as typed Facts, and
released structured-cell Record Inclusion projecting each Fact into the
Project Record under its own revision.  That is the only shape in which a
Report Run's revision reference and its retained copy describe the same rows,
so it is the matched corpus the equivalence proof needs.
"""

from datetime import date
from hashlib import sha256

import pytest
from sqlalchemy import select, text

from corridor.adjudicate import dismiss_dependency, set_resolution_strategy
from corridor import changes
from corridor.changes import diff_since_last, record_run
from corridor.db import Session, engine
from corridor.exceptions import evaluate_project
from corridor.fact_decisions import include_structured_cell_fact_by_policy
from corridor.models import (
    ActiveExtractionRun,
    Candidate,
    Dependency,
    Document,
    ExtractedProposal,
    ExtractedProposalFact,
    ExtractionRun,
    Fact,
    FactSource,
    Project,
    ProjectRecordRevision,
    ReportRun,
    SourceSegment,
)
from corridor.principals import HumanPrincipal
from corridor.report_reading import (
    PAYLOAD_SCHEMA_VERSION,
    PROMISED_FOR_PROJECTION_RULE_VERSION,
    digest_is_intact,
    payload_schema_version,
)
from corridor.report_diff_reference import (
    REFERENCE_FIELDS,
    BaselineReading,
    UNREBUILDABLE_FIELDS,
    baseline_for_run,
    ledger_identities,
    prove_diff_equivalence,
    reference_baseline,
)


ACTOR = "local:diff-reference-reviewer"
PRINCIPAL = HumanPrincipal(ACTOR)


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
def corpus(session):
    """One legacy project whose record is also on the spine, three records."""

    return build_corpus(session, slug="diff-reference", rows=3)


class Corpus:
    def __init__(self, project, document, run, dependencies):
        self.project = project
        self.document = document
        self.run = run
        self.dependencies = dependencies
        self._ordinal = 0

    def next_ordinal(self) -> int:
        self._ordinal += 1
        return self._ordinal


def build_corpus(session, *, slug: str, rows: int) -> Corpus:
    project = Project(slug=slug, name=slug, is_synthetic=True)
    session.add(project)
    session.flush()
    document = Document(
        project_id=project.id,
        sha256=sha256(slug.encode()).hexdigest(),
        filename="matrix.xlsx",
        doc_type="matrix",
        numbering_scheme="project-unique",
        pages=1,
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    extraction = ExtractionRun(
        document_id=document.id,
        prompt_version="diff_reference_fixture_v1",
        outcome="completed",
        candidate_count=0,
        page_errors=0,
    )
    session.add(extraction)
    session.flush()
    session.add(
        ActiveExtractionRun(
            document_id=document.id, extraction_run_id=extraction.id
        )
    )
    corpus = Corpus(project, document, extraction, [])
    for index in range(1, rows + 1):
        corpus.dependencies.append(_row(session, corpus, index))
    return corpus


def _row(session, corpus, index: int):
    """One Ledger record, its Candidate, and its three accepted cell values."""

    dependency = Dependency(
        project_id=corpus.project.id,
        ref_code=f"DEP-{index:05d}",
        dep_type="utility_relocation",
        title=f"Utility conflict {index}",
        internal_owner="Bryce",
        resolution_strategy="protect_in_place",
        need_date=date(2027, 1, index),
    )
    session.add(dependency)
    session.flush()
    candidate = Candidate(
        project_id=corpus.project.id,
        kind="dependency",
        payload_json={"fields": {}},
        source_document_id=corpus.document.id,
        source_pages=[1],
        prompt_version=corpus.run.prompt_version,
        citations_verified=True,
        state="accepted",
        merged_into=dependency.id,
    )
    session.add(candidate)
    session.flush()
    proposal = ExtractedProposal(
        project_id=corpus.project.id,
        document_id=corpus.document.id,
        extraction_run_id=corpus.run.id,
        candidate_id=candidate.id,
        kind="dependency",
        subject_key=f"Utility Conflicts!{index + 1}",
        candidate_metadata_json={"state": "pending", "source_pages": [1]},
    )
    session.add(proposal)
    session.flush()
    include_cell(
        session,
        corpus,
        proposal,
        fact_type="resolution_strategy",
        printed=dependency.resolution_strategy,
    )
    include_cell(
        session,
        corpus,
        proposal,
        fact_type="need_date",
        printed=dependency.need_date.isoformat(),
        date_value=dependency.need_date,
    )
    return dependency


def include_cell(
    session,
    corpus,
    proposal,
    *,
    fact_type: str,
    printed: str,
    date_value: date | None = None,
):
    """Capture one source cell as a Fact and project it into the record."""

    ordinal = corpus.next_ordinal()
    identity = f"{proposal.subject_key}:{fact_type}:{printed}:{ordinal}"
    segment = SourceSegment(
        project_id=corpus.project.id,
        document_id=corpus.document.id,
        kind="spreadsheet_cell",
        exact_text=printed,
        content_sha256=sha256(identity.encode()).hexdigest(),
        ordinal=ordinal,
        sheet_name="Utility Conflicts",
        cell_range=f"D{ordinal}",
    )
    session.add(segment)
    session.flush()
    fact = Fact(
        project_id=corpus.project.id,
        document_id=corpus.document.id,
        extraction_run_id=corpus.run.id,
        fact_type=fact_type,
        subject_kind="source_row",
        subject_key=proposal.subject_key,
        text_value=None if date_value else printed,
        date_value=date_value,
        transformation="iso_date_cell_v1" if date_value else "trim_cell_text_v1",
        recorded_by="extractor:diff_reference_fixture_v1",
        content_sha256=sha256(f"fact:{identity}".encode()).hexdigest(),
    )
    session.add(fact)
    session.flush()
    session.add(
        FactSource(
            project_id=corpus.project.id,
            document_id=corpus.document.id,
            fact_id=fact.id,
            source_segment_id=segment.id,
            role="value_source",
            ordinal=1,
        )
    )
    session.add(
        ExtractedProposalFact(
            project_id=corpus.project.id,
            document_id=corpus.document.id,
            extraction_run_id=corpus.run.id,
            proposal_id=proposal.id,
            fact_id=fact.id,
            ordinal=ordinal,
        )
    )
    session.flush()
    return include_structured_cell_fact_by_policy(
        session, fact, idempotency_key=f"diff-reference:{identity}"
    )


def publish(session, corpus):
    return record_run(
        session,
        corpus.project.id,
        evaluation=evaluate_project(session, corpus.project.id),
    )


def diff(session, corpus):
    return diff_since_last(
        session,
        corpus.project.id,
        evaluation=evaluate_project(session, corpus.project.id),
    )


# --- the reference itself ---------------------------------------------------


def test_the_reference_rebuilds_the_record_fields_the_spine_carries(
    session, corpus
):
    revision = session.scalar(
        select(ProjectRecordRevision.id)
        .where(ProjectRecordRevision.project_id == corpus.project.id)
        .order_by(ProjectRecordRevision.id.desc())
    )

    rebuilt = reference_baseline(
        session, corpus.project.id, revision_id=revision
    )

    assert rebuilt.revision_id == revision
    assert set(rebuilt.dependencies) == {d.ref_code for d in corpus.dependencies}
    first = rebuilt.dependencies["DEP-00001"]
    assert first["resolution_strategy"] == "protect_in_place"
    assert first["need_date"] == "2027-01-01"
    assert first["id"] == corpus.dependencies[0].id
    assert rebuilt.covers("DEP-00001", "resolution_strategy")
    assert not rebuilt.covers("DEP-00001", "ready")


def test_the_reference_reads_the_record_as_it_stood_at_that_revision(
    session, corpus
):
    """An earlier revision does not see a value accepted after it."""

    dependency = corpus.dependencies[0]
    before = session.scalar(
        select(ProjectRecordRevision.id)
        .where(ProjectRecordRevision.project_id == corpus.project.id)
        .order_by(ProjectRecordRevision.id.desc())
    )
    proposal = session.scalars(
        select(ExtractedProposal).where(
            ExtractedProposal.project_id == corpus.project.id,
            ExtractedProposal.subject_key == "Utility Conflicts!2",
        )
    ).one()
    include_cell(
        session, corpus, proposal, fact_type="resolution_strategy", printed="remove"
    )
    after = session.scalar(
        select(ProjectRecordRevision.id)
        .where(ProjectRecordRevision.project_id == corpus.project.id)
        .order_by(ProjectRecordRevision.id.desc())
    )

    assert after > before
    earlier = reference_baseline(session, corpus.project.id, revision_id=before)
    later = reference_baseline(session, corpus.project.id, revision_id=after)
    assert earlier.dependencies[dependency.ref_code]["resolution_strategy"] == (
        "protect_in_place"
    )
    assert later.dependencies[dependency.ref_code]["resolution_strategy"] == "remove"


def test_a_run_with_no_revision_reference_rebuilds_nothing(session, corpus):
    """Absence is an answer: a project off the spine has nothing to rebuild."""

    rebuilt = reference_baseline(session, corpus.project.id, revision_id=None)

    assert rebuilt.revision_id is None
    assert rebuilt.dependencies == {}
    assert rebuilt.covered_field_count == 0


def test_a_fact_belonging_to_no_ledger_record_is_left_out(session, corpus):
    """A Fact reaches a Ledger row only through its Candidate's merge.

    Guessing one would put a record's values under another record's ref code,
    which is the diff reporting a change on the wrong utility.
    """

    proposal = session.scalars(
        select(ExtractedProposal).where(
            ExtractedProposal.project_id == corpus.project.id,
            ExtractedProposal.subject_key == "Utility Conflicts!3",
        )
    ).one()
    candidate = session.get(Candidate, proposal.candidate_id)
    orphaned = session.get(Dependency, candidate.merged_into).ref_code
    candidate.merged_into = None
    session.flush()
    revision = session.scalar(
        select(ProjectRecordRevision.id)
        .where(ProjectRecordRevision.project_id == corpus.project.id)
        .order_by(ProjectRecordRevision.id.desc())
    )

    rebuilt = reference_baseline(
        session, corpus.project.id, revision_id=revision
    )

    assert orphaned == "DEP-00002"
    assert orphaned not in rebuilt.dependencies
    assert set(rebuilt.dependencies) == {"DEP-00001", "DEP-00003"}


# --- the baseline the diff compares against ---------------------------------


def test_the_diff_baseline_is_built_from_the_run_revision(session, corpus):
    run = publish(session, corpus)

    reading = baseline_for_run(session, run)

    assert reading.revision_id == run.revision_id
    assert reading.revision_id is not None
    assert reading.source == "revision_reference"
    assert reading.drift == ()
    # Both reference fields, for all three records, were answered by the
    # revision and agreed with the copy.
    assert reading.compared_fields == len(REFERENCE_FIELDS) * 3


def test_the_baseline_keeps_the_fields_no_revision_can_answer(session, corpus):
    run = publish(session, corpus)

    reading = baseline_for_run(session, run)

    entry = reading.dependencies["DEP-00001"]
    for field in UNREBUILDABLE_FIELDS:
        assert field in entry
    assert entry["constraint_alerts"] == run.snapshot_json["dependencies"][
        "DEP-00001"
    ]["constraint_alerts"]


def test_a_legacy_write_that_reaches_no_fact_is_reported_as_drift(
    session, corpus
):
    """The copy stands and the disagreement is visible, not silently resolved.

    ``set_resolution_strategy`` is a released legacy command that writes the
    Dependency column and no Fact, so for a project in legacy operating mode
    the copy is the accepted value and the spine holds the source's reading.
    Preferring the reference would report an escalation that did not happen.
    """

    set_resolution_strategy(
        session, corpus.dependencies[0], "relocate", actor=ACTOR
    )
    run = publish(session, corpus)

    reading = baseline_for_run(session, run)

    assert reading.source == "revision_reference_with_drift"
    [drift] = reading.drift
    assert (drift.ref_code, drift.field) == ("DEP-00001", "resolution_strategy")
    assert (drift.retained, drift.referenced) == ("relocate", "protect_in_place")
    # The copy stands: the baseline still says what the report published, so
    # the escalation to a critical strategy is still reported next week.
    assert reading.dependencies["DEP-00001"]["resolution_strategy"] == "relocate"


def test_a_spine_value_that_the_ledger_never_took_is_reported_as_drift(
    session, corpus
):
    """The other direction, and the reason drift is reported rather than fixed.

    ``load_project`` writes the Ledger column and projects the same cell into
    the record in one pass, so the two move together.  When only the record
    moves, the reference is ahead of the reading the report actually published
    — and a baseline that quietly took the newer value would report next week
    that a strategy changed in a week when the report never said so.
    """

    run = publish(session, corpus)
    proposal = session.scalars(
        select(ExtractedProposal).where(
            ExtractedProposal.project_id == corpus.project.id,
            ExtractedProposal.subject_key == "Utility Conflicts!2",
        )
    ).one()
    include_cell(
        session, corpus, proposal, fact_type="resolution_strategy", printed="remove"
    )
    later = publish(session, corpus)

    assert baseline_for_run(session, run).drift == ()
    [drift] = baseline_for_run(session, later).drift
    assert (drift.retained, drift.referenced) == ("protect_in_place", "remove")
    assert baseline_for_run(session, later).dependencies["DEP-00001"][
        "resolution_strategy"
    ] == "protect_in_place"


def test_the_ledger_identity_is_resolved_rather_than_copied(session, corpus):
    """A run written before #96 stored no identity; the Ledger has it.

    ADR-0032 makes the dismissed/closed distinction the one this report cannot
    get wrong, and finding the dismissal decision needs the record's identity.
    A change derived from such a run used to cite nothing.
    """

    run = publish(session, corpus)
    stripped = dict(run.snapshot_json)
    stripped["dependencies"] = {
        ref: {key: value for key, value in entry.items() if key != "id"}
        for ref, entry in stripped["dependencies"].items()
    }
    run.snapshot_json = stripped
    session.flush()

    reading = baseline_for_run(session, run)

    assert reading.identities_resolved == 3
    assert reading.dependencies["DEP-00001"]["id"] == corpus.dependencies[0].id
    assert ledger_identities(session, corpus.project.id)["DEP-00001"] == (
        corpus.dependencies[0].id
    )


def test_a_change_from_an_identity_less_run_reaches_its_record(session, corpus):
    """The one deliberate correction this pass makes (#603).

    Four stored runs predate #96 and recorded no identity for their entries.
    A record that leaves the working list between such a run and the next one
    used to be reported as merely ``closed``, citing nothing, because the
    dismissal decision could not be found without the identity.  ADR-0032
    names that as the mistake this report cannot afford: telling an external
    reader a utility conflict was resolved when it was thrown out as junk.
    Resolving the identity from the Ledger fixes it, and this is the one thing
    the diff now says differently.
    """

    run = publish(session, corpus)
    stripped = dict(run.snapshot_json)
    stripped["dependencies"] = {
        ref: {key: value for key, value in entry.items() if key != "id"}
        for ref, entry in stripped["dependencies"].items()
    }
    run.snapshot_json = stripped
    session.flush()
    dismissed = corpus.dependencies[0]
    dismiss_dependency(session, dismissed, "duplicate", principal=PRINCIPAL)

    result = diff(session, corpus)

    [change] = [c for c in result.changes if c.ref_code == dismissed.ref_code]
    assert change.kind == "dismissed"
    assert change.dependency_id == dismissed.id
    assert "duplicate" in change.detail


def test_a_diff_reports_the_baseline_it_was_computed_from(session, corpus):
    publish(session, corpus)

    result = diff(session, corpus)

    assert result.baseline_source == "revision_reference"
    assert result.baseline_drift == ()


# --- the equivalence proof --------------------------------------------------


def test_the_reference_says_what_the_retained_copy_said(session, corpus):
    """The proof #603 exists for, over this project's own retained runs."""

    publish(session, corpus)
    diff(session, corpus)
    publish(session, corpus)

    proof = prove_diff_equivalence(session, corpus.project.id)

    assert proof.runs_compared == 2
    assert proof.runs_with_a_reference == 2
    assert proof.rows_compared == 6
    assert proof.fields_compared == 12
    assert proof.drift == ()
    assert proof.passed
    assert "no disagreement" in proof.report()


def test_the_proof_holds_over_a_wider_corpus_of_readings(session):
    """A wider matched corpus: 25 records, four weeks, movement between them.

    The three-record fixture proves the mechanism; this proves it does not
    depend on the corpus being tiny or static.  A value is re-accepted on the
    spine between every pair of readings, so each retained run names a
    different revision and each has to be rebuilt as of its own — which is the
    property a single-revision corpus cannot test.
    """

    wide = build_corpus(session, slug="diff-reference-wide", rows=25)
    proposals = {
        proposal.subject_key: proposal
        for proposal in session.scalars(
            select(ExtractedProposal).where(
                ExtractedProposal.project_id == wide.project.id
            )
        )
    }
    revisions = []
    for week, strategy in enumerate(
        ("protect_in_place", "adjust_vertical", "change_design", "policy_exception"), 1
    ):
        run = publish(session, wide)
        revisions.append(run.revision_id)
        # The real pipeline moves both together — ``run_dependency_admission``
        # writes the Ledger column and ``include_current_structured_cell_facts``
        # projects the same cell into the record — so a matched corpus does too.
        include_cell(
            session,
            wide,
            proposals[f"Utility Conflicts!{week + 1}"],
            fact_type="resolution_strategy",
            printed=strategy,
        )
        wide.dependencies[week - 1].resolution_strategy = strategy
        session.flush()

    proof = prove_diff_equivalence(session, wide.project.id)

    assert len(set(revisions)) == 4
    assert proof.runs_compared == 4
    assert proof.runs_with_a_reference == 4
    assert proof.rows_compared == 100
    assert proof.fields_compared == 200
    assert proof.drift == ()
    assert proof.passed


def test_the_proof_does_not_pass_over_nothing(session, corpus):
    """A proof with no compared field proves nothing and says so."""

    proof = prove_diff_equivalence(session, corpus.project.id)

    assert proof.runs_compared == 0
    assert proof.fields_compared == 0
    assert not proof.passed


def test_the_proof_names_every_field_the_two_disagree_on(session, corpus):
    publish(session, corpus)
    set_resolution_strategy(
        session, corpus.dependencies[1], "relocate", actor=ACTOR
    )
    publish(session, corpus)

    proof = prove_diff_equivalence(session, corpus.project.id)

    assert not proof.passed
    [drift] = proof.drift
    assert drift.ref_code == "DEP-00002"
    assert "DEP-00002.resolution_strategy" in proof.report()


def _copy_only_baseline(session, run):
    """The baseline the diff read before #603: the retained copy, alone."""

    return BaselineReading(
        dependencies=dict((run.snapshot_json or {}).get("dependencies", {})),
        revision_id=None,
        covered_fields=0,
        compared_fields=0,
        drift=(),
        identities_resolved=0,
    )


def test_the_diff_is_unchanged_by_reading_the_reference(
    session, corpus, monkeypatch
):
    """The whole risk: the answers must not move because the inputs did.

    Two weeks of real movement — a record escalated to a critical strategy by a
    legacy command that writes no Fact, a value re-accepted on the spine, and a
    record added to the Ledger — with the second week diffed twice against the
    same published reading: once with the baseline rebuilt from the run's
    revision reference, once from the retained copy alone.  Every change, kind,
    detail and record identity must match, and the baseline the reference
    rebuilds is one it disagrees with, so agreement is not had for free.
    """

    publish(session, corpus)
    # A legacy command escalates a record and writes no Fact, and the reading
    # published straight after it carries that escalation in its copy while
    # the revision it names still holds the source's value: the baseline this
    # week is diffed against is one that drifts.
    set_resolution_strategy(
        session, corpus.dependencies[0], "relocate", actor=ACTOR
    )
    escalation = diff(session, corpus)
    assert any(
        "relocate" in change.detail and "critical" in change.detail
        for change in escalation.changes
    )
    publish(session, corpus)
    proposal = session.scalars(
        select(ExtractedProposal).where(
            ExtractedProposal.project_id == corpus.project.id,
            ExtractedProposal.subject_key == "Utility Conflicts!3",
        )
    ).one()
    include_cell(
        session,
        corpus,
        proposal,
        fact_type="need_date",
        printed="2027-06-01",
        date_value=date(2027, 6, 1),
    )
    corpus.dependencies.append(_row(session, corpus, 4))

    referenced = diff(session, corpus)
    monkeypatch.setattr(changes, "baseline_for_run", _copy_only_baseline)
    copied = diff(session, corpus)

    assert copied.baseline_source == "retained_copy_no_reference"
    assert referenced.baseline_source == "revision_reference_with_drift"
    assert referenced.changes == copied.changes
    assert "new" in {change.kind for change in referenced.changes}
    assert referenced.previous_run_id == copied.previous_run_id
    assert referenced.ruleset_changed == copied.ruleset_changed
    assert referenced.configuration_changed == copied.configuration_changed


def test_the_retained_reading_is_the_only_witness_of_what_was_published(
    session, corpus
):
    """Why the retained payload is the occurrence's own (ADR-0092).

    Nothing derives the documentation requirement, the evaluated Constraint
    Alerts, or the statement-projected Promised For from a revision, so a run
    stripped of its payload cannot be rebuilt from its reference — which is
    why the payload is evidence rather than a cache.
    """

    run = publish(session, corpus)
    run.snapshot_json = {"dependencies": {}}
    session.flush()

    reading = baseline_for_run(session, run)

    assert reading.dependencies == {}
    assert reading.covered_fields == len(REFERENCE_FIELDS) * 3
    assert set(UNREBUILDABLE_FIELDS) == {
        "documentation_requirement_met",
        "constraint_alerts",
        "published_promised_for",
    }


def _rewrite_as_version_one(session, run):
    """The retained payload as it was written before #633, keys and all."""

    payload = dict(run.snapshot_json)
    payload.pop("payload_schema_version", None)
    payload.pop("content_sha256", None)
    payload.pop("promised_for_projection_rule_version", None)
    payload["dependencies"] = {
        ref: {
            "id": entry.get("id"),
            "resolution_strategy": entry.get("resolution_strategy"),
            "need_date": entry.get("need_date"),
            "committed_date": entry["published_promised_for"],
            "ready": entry["documentation_requirement_met"],
            "exceptions": entry["constraint_alerts"],
        }
        for ref, entry in payload["dependencies"].items()
    }
    run.snapshot_json = payload
    session.flush()
    return run


def test_a_version_one_payload_still_diffs_without_being_rewritten(
    session, corpus
):
    """Retained readings are evidence, so the reader moves, not the rows.

    Every payload written before #633 uses ``ready``, ``exceptions`` and
    ``committed_date``. Rewriting them to the governed names would edit what a
    past report published; translating on read costs one function and leaves
    the evidence alone.
    """

    run = _rewrite_as_version_one(session, publish(session, corpus))
    assert payload_schema_version(run.snapshot_json) == "report-reading-v1"

    reading = baseline_for_run(session, run)

    entry = reading.dependencies["DEP-00001"]
    for field in UNREBUILDABLE_FIELDS:
        assert field in entry
    assert set(entry).isdisjoint({"ready", "exceptions", "committed_date"})
    # And the row is untouched on disk: the translation is a read, not a write.
    session.expire(run)
    assert "ready" in run.snapshot_json["dependencies"]["DEP-00001"]


def test_a_change_against_a_version_one_baseline_is_still_reported(
    session, corpus
):
    """The translation must not lose a change, which is the only risk in it."""

    _rewrite_as_version_one(session, publish(session, corpus))
    set_resolution_strategy(
        session, corpus.dependencies[0], "relocate", actor=ACTOR
    )

    reported = diff(session, corpus)

    assert reported.of_kind("escalated")
    assert {change.ref_code for change in reported.of_kind("escalated")} == {
        "DEP-00001"
    }


def test_a_recorded_reading_is_sealed_and_survives_the_database_intact(
    session, corpus
):
    """The digest is evidence, so it has to hold after a JSONB round trip.

    A digest that only matched the in-memory dict would prove nothing about
    the retained row, which is the only thing anyone reads a year later.
    """

    run = publish(session, corpus)
    session.expire(run)

    payload = run.snapshot_json
    assert payload_schema_version(payload) == PAYLOAD_SCHEMA_VERSION
    assert digest_is_intact(payload) is True
    assert payload["promised_for_projection_rule_version"] == (
        PROMISED_FOR_PROJECTION_RULE_VERSION
    )


def test_a_recorded_reading_names_the_statement_its_promised_for_came_from(
    session, corpus
):
    """The projected date is bound to the identity it was projected from.

    Without the binding, ``published_promised_for`` is a bare date that reads
    like the record's own ``committed_date`` Fact again — the exact confusion
    the rename exists to end.
    """

    run = publish(session, corpus)
    session.expire(run)

    [entry] = list(run.snapshot_json["dependencies"].values())[:1]
    assert "committed_date" not in entry
    assert "ready" not in entry
    assert "exceptions" not in entry
    assert "published_promised_for" in entry
    assert "documentation_requirement_met" in entry
    assert "constraint_alerts" in entry
    # No statement was recorded for this corpus, so the binding is an explicit
    # absence rather than an invented lineage.
    assert entry["promised_for_statement"] is None


def test_the_stored_payload_column_is_marked_a_retained_reading_not_a_cache(
    session,
):
    """#602 called it a cache; #633 corrects the database's own description.

    The comment is the instruction a future implementer reads. Left saying
    "rebuildable compatibility cache ... retained only until #603", it reads
    as permission to delete published evidence once a proof it never covered
    passed.
    """

    comments = {
        table: session.scalar(
            text(
                f"select col_description('{table}'::regclass, attnum) "
                f"from pg_attribute where attrelid = '{table}'::regclass "
                "and attname = 'snapshot_json'"
            )
        )
        for table in ("report_runs", "scheduled_report_publications")
    }

    for table, comment in comments.items():
        assert comment is not None, table
        assert "Report Reading payload" in comment, table
        assert "not a cache" in comment.lower(), table
        assert "cache TTL" in comment, table
        assert "rebuildable compatibility cache" not in comment.lower(), table


def test_the_baseline_is_only_ever_read(session, corpus):
    """A rebuild that wrote anything would be a second writer of the record."""

    run = publish(session, corpus)
    before = session.scalar(
        select(ProjectRecordRevision.id)
        .where(ProjectRecordRevision.project_id == corpus.project.id)
        .order_by(ProjectRecordRevision.id.desc())
    )
    runs_before = session.scalar(
        select(ReportRun.id)
        .where(ReportRun.project_id == corpus.project.id)
        .order_by(ReportRun.id.desc())
    )

    baseline_for_run(session, run)
    prove_diff_equivalence(session, corpus.project.id)
    session.flush()

    assert session.scalar(
        select(ProjectRecordRevision.id)
        .where(ProjectRecordRevision.project_id == corpus.project.id)
        .order_by(ProjectRecordRevision.id.desc())
    ) == before
    assert session.scalar(
        select(ReportRun.id)
        .where(ReportRun.project_id == corpus.project.id)
        .order_by(ReportRun.id.desc())
    ) == runs_before
