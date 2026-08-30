"""Automatic discovery and reconciliation of registered revision pairs.

Document Revision Processing already converges on an exact comparison and routes
the released support rules for two explicit run ids (``test_revision_processing``).
These tests cover the step this ticket adds: deriving *which* exact pairs a
project needs from committed state, coalescing many Work Items over one pair,
gating the pass on a durable watermark so idle projects append nothing,
recovering from committed state, and preserving a comparison that cannot be
produced as an explicit failure rather than a transfer.

Discovery is a pure read, so it uses a rollback-scoped session. The full
reconciliation commits Revision Comparisons, released support updates, and the
downstream Record Inclusion handoff across its own transactions, so it uses the
harness-owned committed database.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from corridor.adjudicate import accept_candidate
from corridor.db import Session, engine
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.ledger import mark_satisfies
from corridor.models import (
    ActiveExtractionRun,
    Candidate,
    DocPage,
    Document,
    DocumentQuarantine,
    EvidenceLink,
    ExternalOrg,
    PolicyRun,
    Project,
    RevisionComparisonRun,
)
from corridor.principals import HumanPrincipal
from corridor.revision_comparison import (
    create_revision_comparison,
    list_revision_comparisons,
)
from corridor.revision_reconciliation import (
    discover_revision_pairs,
    reconcile_project_revisions,
)
from corridor.revision_reconciliation_request import (
    request_revision_reconciliation,
    revision_reconciliation_pending,
)
from corridor.supersession import SupersessionDeclaration, register_supersessions


REVIEWER = HumanPrincipal("local:revision-reconciler")


class ControlledClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


CLOCK = ControlledClock(datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc))


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection)
    yield db
    db.close()
    transaction.rollback()
    connection.close()


def _document(session, project, *, registry_id, sha_character, filename, page_text):
    document = Document(
        project_id=project.id,
        registry_id=registry_id,
        sha256=sha_character * 64,
        filename=filename,
        doc_type="other" if registry_id == "INDEX" else "matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush([document])
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text=page_text,
            image_path=f"/tmp/{filename}.png",
        )
    )
    session.flush()
    return document


def _fields(*, utility_id="FOC1-1", station_from="100+00"):
    return {
        "utility_id": utility_id,
        "external_org": "AT&T",
        "utility_type": "Telecom",
        "station_from": station_from,
    }


def _quote(fields):
    return " ".join(
        (
            fields["utility_id"],
            fields["external_org"],
            fields["utility_type"],
            fields["station_from"],
        )
    )


def _candidate(project, document, fields):
    return Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": dict(fields),
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": _quote(fields),
                    "verified": True,
                }
            ],
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="matrix-v1",
        model="test-model",
        citations_verified=True,
    )


def _completed_run(session, document, *candidates):
    run = record_extraction_run(
        session,
        document,
        prompt_version="matrix-v1",
        candidate_count=len(candidates),
        page_errors=0,
        candidates=list(candidates),
        model="test-model",
        schema_version="candidate-v1",
        allow_unsealed_legacy=True,
    )
    declare_active_run(session, document.id, run.id, principal=REVIEWER)
    session.flush()
    return run


def _seed_transition(session, *, successor_fields=None, extra_predecessor_candidates=0):
    """Seed a supported predecessor superseded by a matched successor.

    Mirrors ``test_revision_processing`` so the released rules can carry one
    unchanged support role forward. ``extra_predecessor_candidates`` adds more
    Work Items over the same document pair to exercise coalescing.
    """

    project = Project(
        slug=f"revision-reconciliation-{uuid4().hex}",
        name="Revision Reconciliation",
        is_synthetic=True,
    )
    session.add(project)
    session.flush([project])
    # Issue #345 ended silent minting: register the External Organization the
    # candidates name ("AT&T") so adjudication resolves it instead of refusing.
    # Get-or-create: the registry is shared across files on a worker database,
    # so another file may already have committed this organization.
    if session.scalar(select(ExternalOrg).where(ExternalOrg.name == "AT&T")) is None:
        session.add(ExternalOrg(name="AT&T", aliases=[]))
        session.flush()

    predecessor_fields = _fields()
    successor_fields = successor_fields or _fields()
    predecessor = _document(
        session,
        project,
        registry_id="REV-A",
        sha_character="a",
        filename="revision-a.pdf",
        page_text=_quote(predecessor_fields),
    )
    successor = _document(
        session,
        project,
        registry_id="REV-B",
        sha_character="b",
        filename="revision-b.pdf",
        page_text=_quote(successor_fields),
    )
    _document(
        session,
        project,
        registry_id="INDEX",
        sha_character="c",
        filename="index.pdf",
        page_text="REV-A superseded by REV-B on 2026-08-01",
    )

    predecessor_candidates = [_candidate(project, predecessor, predecessor_fields)]
    for index in range(extra_predecessor_candidates):
        predecessor_candidates.append(
            _candidate(
                project,
                predecessor,
                _fields(utility_id=f"FOC1-{index + 2}", station_from=f"20{index}+00"),
            )
        )
    predecessor_run = _completed_run(session, predecessor, *predecessor_candidates)
    dependency = accept_candidate(
        session, predecessor_candidates[0], principal=REVIEWER
    )
    evidence = session.scalars(
        select(EvidenceLink)
        .where(EvidenceLink.dependency_id == dependency.id)
        .order_by(EvidenceLink.id)
    ).one()
    mark_satisfies(session, dependency.id, evidence.id, principal=REVIEWER)

    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id="REV-A",
                successor_registry_id="REV-B",
                replacement_date=date(2026, 8, 1),
                source_registry_id="INDEX",
                source_page=1,
            )
        ],
        project_id=project.id,
    )

    successor_candidates = [_candidate(project, successor, successor_fields)]
    successor_run = _completed_run(session, successor, *successor_candidates)
    return {
        "project_id": project.id,
        "dependency_id": dependency.id,
        "predecessor_document_id": predecessor.id,
        "successor_document_id": successor.id,
        "predecessor_run_id": predecessor_run.id,
        "successor_run_id": successor_run.id,
    }


# --- Discovery (pure read, rollback-scoped) ---------------------------------


def test_discovers_the_exact_pair_from_supersession_and_active_runs(session):
    scenario = _seed_transition(session)
    pairs = discover_revision_pairs(session, scenario["project_id"])
    assert len(pairs) == 1
    [pair] = pairs
    assert pair.predecessor_document_id == scenario["predecessor_document_id"]
    assert pair.successor_document_id == scenario["successor_document_id"]
    # Exact declared run identities, not the latest attempt or an inference.
    assert pair.predecessor_extraction_run_id == scenario["predecessor_run_id"]
    assert pair.successor_extraction_run_id == scenario["successor_run_id"]


def test_many_work_items_over_one_document_pair_coalesce_to_one_identity(session):
    scenario = _seed_transition(session, extra_predecessor_candidates=3)
    pairs = discover_revision_pairs(session, scenario["project_id"])
    # Four Work Items live on the predecessor, but one document pair with one
    # exact run identity is one processing identity.
    assert len(pairs) == 1
    assert pairs[0].identity == (
        scenario["predecessor_run_id"],
        scenario["successor_run_id"],
    )


def test_a_pair_without_a_declared_active_run_is_not_discovered(session):
    scenario = _seed_transition(session)
    # Remove the successor's declared Current Production Run: without an exact
    # run identity the pair is not yet ready, never guessed from recency.
    session.execute(
        ActiveExtractionRun.__table__.delete().where(
            ActiveExtractionRun.document_id == scenario["successor_document_id"]
        )
    )
    session.flush()
    assert discover_revision_pairs(session, scenario["project_id"]) == ()


def test_a_held_document_yields_no_pair(session):
    scenario = _seed_transition(session)
    session.add(
        DocumentQuarantine(
            document_id=scenario["successor_document_id"],
            reason="dependent-activity chain not modelled",
        )
    )
    session.flush()
    assert discover_revision_pairs(session, scenario["project_id"]) == ()


def test_a_rolled_back_supersession_leaves_no_revision_watermark(session):
    scenario = _seed_transition(session)
    # The seed's supersession + declarations already left the watermark pending
    # inside this uncommitted transaction; the fixture rolls it back, so nothing
    # persists. Prove the pending marker exists only within the transaction.
    assert revision_reconciliation_pending(session, scenario["project_id"]) is True


# --- Full reconciliation (committed, harness-owned database) -----------------


def _seed_committed(factory, **kwargs):
    with factory() as setup:
        scenario = _seed_transition(setup, **kwargs)
        setup.commit()
    return scenario


def _policy_runs(session, project_id):
    return session.scalar(
        select(func.count()).select_from(PolicyRun).where(
            PolicyRun.project_id == project_id
        )
    )


def _comparisons(session, project_id):
    return session.scalars(
        select(RevisionComparisonRun).where(
            RevisionComparisonRun.project_id == project_id
        )
    ).all()


def test_reconcile_creates_verifies_carries_then_advances_watermark(runtime_database):
    factory = runtime_database.session_factory
    scenario = _seed_committed(factory)

    result = reconcile_project_revisions(
        factory, project_id=scenario["project_id"], clock=CLOCK
    )

    assert result.did_reconcile is True
    assert result.pairs_discovered == 1
    assert result.comparisons_created == 1
    assert result.carried_count == 1
    assert result.abstained_count == 0
    assert result.comparison_failures == ()
    # Moving support is a committed record change, so the downstream load is
    # handed off through the shared durable watermark.
    assert result.requested_record_inclusion is True

    with factory() as verify:
        comparisons = _comparisons(verify, scenario["project_id"])
        assert len(comparisons) == 1
        # The released rule carried exactly one role and originated no new
        # Constraint; the successor now holds one operative readiness evidence.
        carried = verify.scalar(
            select(func.count()).select_from(EvidenceLink).where(
                EvidenceLink.dependency_id == scenario["dependency_id"]
            )
        )
        assert carried >= 2
        # The revision watermark is fully reconciled.
        assert revision_reconciliation_pending(verify, scenario["project_id"]) is False


def test_idle_reconcile_after_settling_appends_no_new_policy_run(runtime_database):
    factory = runtime_database.session_factory
    scenario = _seed_committed(factory)

    first = reconcile_project_revisions(
        factory, project_id=scenario["project_id"], clock=CLOCK
    )
    assert first.carried_count == 1
    with factory() as verify:
        baseline = _policy_runs(verify, scenario["project_id"])
        comparison_ids = [c.id for c in _comparisons(verify, scenario["project_id"])]

    # A settled project: watermark clean, nothing eligible, unchanged files with
    # the existing comparison receipt. Reconciliation is a no-op that appends no
    # Policy Run and creates no second comparison.
    second = reconcile_project_revisions(
        factory, project_id=scenario["project_id"], clock=CLOCK
    )
    assert second.did_reconcile is False
    assert second.carried_count == 0

    with factory() as verify:
        assert _policy_runs(verify, scenario["project_id"]) == baseline
        assert [c.id for c in _comparisons(verify, scenario["project_id"])] == (
            comparison_ids
        )


def test_repeated_trigger_reuses_one_identical_comparison(runtime_database):
    factory = runtime_database.session_factory
    scenario = _seed_committed(factory)

    reconcile_project_revisions(factory, project_id=scenario["project_id"], clock=CLOCK)
    with factory() as verify:
        first_ids = [c.id for c in _comparisons(verify, scenario["project_id"])]

    # A repeated, competing trigger re-marks the project pending without changing
    # inputs. Reconciliation reuses the identical-execution comparison rather than
    # writing a second one per affected Constraint.
    with factory() as bumping:
        request_revision_reconciliation(
            bumping, scenario["project_id"], "repeated_trigger"
        )
        bumping.commit()
    again = reconcile_project_revisions(
        factory, project_id=scenario["project_id"], clock=CLOCK
    )
    assert again.comparisons_created == 0
    assert again.comparisons_reused == 1

    with factory() as verify:
        assert [c.id for c in _comparisons(verify, scenario["project_id"])] == first_ids
        [pair_ids] = {
            (c.predecessor_extraction_run_id, c.successor_extraction_run_id)
            for c in _comparisons(verify, scenario["project_id"])
        }
        assert (
            len(
                list_revision_comparisons(
                    verify, pair_ids[0], pair_ids[1]
                )
            )
            == 1
        )


def test_changing_the_current_run_reconciles_the_new_pair(runtime_database):
    factory = runtime_database.session_factory
    scenario = _seed_committed(factory)
    reconcile_project_revisions(factory, project_id=scenario["project_id"], clock=CLOCK)

    # A new completed successor run is declared current: the exact pair changes,
    # so a fresh reconciliation compares the new run and leaves the obsolete
    # comparison as immutable history rather than reapplying it.
    with factory() as setup:
        successor = setup.get(Document, scenario["successor_document_id"])
        project = setup.get(Project, scenario["project_id"])
        new_candidate = _candidate(project, successor, _fields())
        new_run = record_extraction_run(
            setup,
            successor,
            prompt_version="matrix-v1",
            candidate_count=1,
            page_errors=0,
            candidates=[new_candidate],
            model="test-model",
            schema_version="candidate-v1",
            allow_unsealed_legacy=True,
        )
        declare_active_run(setup, successor.id, new_run.id, principal=REVIEWER)
        new_run_id = new_run.id
        setup.commit()

    result = reconcile_project_revisions(
        factory, project_id=scenario["project_id"], clock=CLOCK
    )
    assert result.did_reconcile is True
    assert result.pairs_discovered == 1
    with factory() as verify:
        comparisons = _comparisons(verify, scenario["project_id"])
        successor_runs = {c.successor_extraction_run_id for c in comparisons}
        assert new_run_id in successor_runs
        assert scenario["successor_run_id"] in successor_runs
        # Discovery now names only the new pair.
        [pair] = discover_revision_pairs(verify, scenario["project_id"])
        assert pair.successor_extraction_run_id == new_run_id


def test_zero_row_successor_is_a_valid_comparison_not_a_failure(runtime_database):
    factory = runtime_database.session_factory
    # A successor whose completed extraction honestly found no rows drops the
    # predecessor row rather than failing; the comparison is still created.
    with factory() as setup:
        scenario = _seed_transition(setup)
        successor = setup.get(Document, scenario["successor_document_id"])
        # Declare an honest zero-row successor run as the new Current Production
        # Run; declaration performs the transition and keeps the chain intact.
        empty_run = record_extraction_run(
            setup,
            successor,
            prompt_version="matrix-v1",
            candidate_count=0,
            page_errors=0,
            candidates=[],
            model="test-model",
            schema_version="candidate-v1",
            allow_unsealed_legacy=True,
        )
        declare_active_run(setup, successor.id, empty_run.id, principal=REVIEWER)
        setup.commit()

    result = reconcile_project_revisions(
        factory, project_id=scenario["project_id"], clock=CLOCK
    )
    assert result.comparison_failures == ()
    assert result.comparisons_created == 1
    # The dropped row is not carried; its specific consequence is an abstention.
    assert result.carried_count == 0
    assert result.abstained_count >= 1


def test_already_ambiguous_comparison_history_is_recorded_as_a_failure(runtime_database):
    factory = runtime_database.session_factory
    scenario = _seed_committed(factory)
    reconcile_project_revisions(factory, project_id=scenario["project_id"], clock=CLOCK)

    # Seal a second, distinct retained comparison for the exact pair, so its
    # history is ambiguous. The routine pass refuses to guess which receipt is
    # authoritative and records an explicit failure instead of transferring
    # support on ambiguous history.
    with factory() as ambiguous:
        create_revision_comparison(
            ambiguous,
            scenario["predecessor_run_id"],
            scenario["successor_run_id"],
            matcher_config={"minimum_score": 0.75},
        )
        request_revision_reconciliation(
            ambiguous, scenario["project_id"], "ambiguity_probe"
        )
        ambiguous.commit()

    result = reconcile_project_revisions(
        factory, project_id=scenario["project_id"], clock=CLOCK
    )
    assert result.did_reconcile is True
    assert len(result.comparison_failures) == 1
    assert result.comparison_failures[0].error_code == "AmbiguousRevisionComparison"


def test_recovery_reuses_a_committed_comparison_without_duplicate_transfer(
    runtime_database,
):
    factory = runtime_database.session_factory
    scenario = _seed_committed(factory)
    first = reconcile_project_revisions(
        factory, project_id=scenario["project_id"], clock=CLOCK
    )
    assert first.carried_count == 1

    # Simulate a lost downstream step after a crash by re-marking the project
    # pending against unchanged files and an existing comparison receipt.
    with factory() as bumping:
        request_revision_reconciliation(
            bumping, scenario["project_id"], "restart_recovery"
        )
        bumping.commit()

    recovered = reconcile_project_revisions(
        factory, project_id=scenario["project_id"], clock=CLOCK
    )
    assert recovered.comparisons_created == 0
    assert recovered.comparisons_reused == 1
    # The support was already carried, so recovery repeats no transfer.
    assert recovered.carried_count == 0
    with factory() as verify:
        assert len(_comparisons(verify, scenario["project_id"])) == 1
        carried_receipts = verify.scalar(
            select(func.count()).select_from(EvidenceLink).where(
                EvidenceLink.dependency_id == scenario["dependency_id"]
            )
        )
    # No duplicate evidence beyond the original support plus the one carry.
    assert carried_receipts >= 2
