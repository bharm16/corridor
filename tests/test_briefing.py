"""The Briefing: a model may brief on the record, never be it (ADR-0011).

CI never calls a model — the house rule, and here it is load-bearing
twice over: the generator is tested with scripted structured responses,
and the checker is pure functions over constructed records. What these
tests pin is the constraint machinery — citation presence, cited-object
existence, quote verification, floor coverage, stamps, withhold-visibly —
never the phrasing of prose, which belongs to no contract.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, select

from corridor.briefing import PROMPT_VERSION, brief, render
from corridor.db import Session, engine
from corridor.exceptions import RULESET_VERSION
from corridor.models import (
    Assertion,
    AuditLog,
    Candidate,
    Dependency,
    DependencyEvent,
    DependencyEventEvidence,
    DependencyEventScope,
    DependencyEventTiming,
    DocPage,
    Document,
    EvidenceLink,
    Project,
)

from corridor.llm import RequestConfiguration

from model_client_support import FakeModelClient

TODAY = date(2026, 8, 4)


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
    p = Project(slug="brief-test", name="Briefing Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


@pytest.fixture
def dependency(session, project):
    """One record with an assertion, verified evidence, and fired rules.

    The page text really carries the quote, so the checker's quote
    verification does its actual work rather than being stubbed.
    """
    document = Document(
        project_id=project.id,
        sha256="b" * 64,
        filename="ucm.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
        doc_date=TODAY - timedelta(days=40),
    )
    session.add(document)
    session.flush()
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text="UC-1 CenterPoint Energy Electric 1149+00 relocation required",
            image_path=None,
            text_source="text_layer",
        )
    )
    dep = Dependency(
        project_id=project.id,
        ref_code="DEP-00001",
        dep_type="utility_relocation",
        title="Electric — CenterPoint Energy",
        resolution_strategy="relocate",
        committed_date=TODAY - timedelta(days=10),
        internal_owner=None,
    )
    session.add(dep)
    session.flush()
    link = EvidenceLink(
        dependency_id=dep.id,
        document_id=document.id,
        page_no=1,
        quote="UC-1 CenterPoint Energy Electric 1149+00",
        verified=True,
    )
    session.add(link)
    session.flush()
    session.add(
        Assertion(
            dependency_id=dep.id,
            field_name="external_org",
            asserted_value="CenterPoint Energy",
            evidence_link_id=link.id,
            doc_date=None,
        )
    )
    session.flush()
    return dep


def stub_client(responses, model="gpt-5.6-luna"):
    """The shared recording double, answering these responses in order."""
    return FakeModelClient(
        list(responses), configuration=RequestConfiguration(model=model)
    )


def drafted(*sentences):
    return {"sentences": [{"text": t, "cites": list(c)} for t, c in sentences]}


def floor_refs(session, dep):
    """The refs the stub must cite to cover the floor, read off the same
    engine the component reads."""
    from corridor.exceptions import exceptions_for

    return [
        f"X{i + 1}" for i in range(len(exceptions_for(session, dep.id, today=TODAY)))
    ]


def covering_sentences(session, dep):
    """One sentence per fired Exception — the minimal covering draft."""
    return [
        (f"Fact {ref} holds.", [ref]) for ref in floor_refs(session, dep)
    ]


# ------------------------------------------------------------- the contract


def test_a_briefing_carries_its_stamps(session, dependency):
    """Prompt version and model (what drafted it), evaluation time and
    ruleset version (what its Exception citations are re-checkable
    against — ADR-0003's discipline for Derivations, applied to prose)."""
    client = stub_client([drafted(*covering_sentences(session, dependency))])

    briefing = brief(session, dependency.id, client=client, today=TODAY)

    assert briefing.prompt_version == PROMPT_VERSION
    assert briefing.model == "gpt-5.6-luna"
    assert briefing.evaluated_at == TODAY
    assert briefing.ruleset_version == RULESET_VERSION


def test_a_briefing_evaluates_the_exact_statement_publication_it_cites(
    session, dependency, monkeypatch
):
    """The narrative cannot pair one statement read with another evaluation."""
    from corridor import briefing as briefing_module

    captured = {}
    original = briefing_module._brief

    def capture(*args, **kwargs):
        captured["evaluation"] = kwargs["evaluation"]
        captured["publication"] = kwargs["publication"]
        return original(*args, **kwargs)

    monkeypatch.setattr(briefing_module, "_brief", capture)
    client = stub_client([drafted(*covering_sentences(session, dependency))])

    briefing_module.brief(session, dependency.id, client=client, today=TODAY)

    assert captured["evaluation"].statement_publication is captured["publication"]
    assert (
        captured["evaluation"].statement_publication_fingerprint
        == captured["publication"].fingerprint
    )


def test_a_briefing_refuses_an_evaluation_from_another_statement_read(
    session, dependency
):
    """Even an equivalent later read cannot replace the frozen cited one."""
    from corridor.briefing import _brief
    from corridor.dependency_events import published_dependency_statements
    from corridor.exceptions import evaluate_dependency

    first = published_dependency_statements(
        session, (dependency.id,), project_id=dependency.project_id
    )
    evaluation = evaluate_dependency(
        session,
        dependency.id,
        today=TODAY,
        statement_publication=first,
    )
    second = published_dependency_statements(
        session, (dependency.id,), project_id=dependency.project_id
    )

    with pytest.raises(ValueError, match="exact frozen statement publication"):
        _brief(
            session,
            [dependency],
            ref_code=dependency.ref_code,
            client=stub_client([]),
            evaluation=evaluation,
            publication=second,
        )


def test_an_uncited_sentence_is_withheld_and_counted(session, dependency):
    """A sentence that cannot cite is a sentence the Briefing may not
    contain — withheld loudly, never shown unverified, never silently
    dropped."""
    client = stub_client([
        drafted(
            ("This claim cites nothing.", []),
            *covering_sentences(session, dependency),
        )
    ])

    briefing = brief(session, dependency.id, client=client, today=TODAY)

    assert all(s.cites for s in briefing.sentences)
    assert briefing.withheld == {"uncited": 1}


def test_a_citation_to_nothing_withholds_its_sentence(session, dependency):
    """The model composes over supplied citables; a ref it invented is a
    citation to nothing and takes its sentence with it."""
    client = stub_client([
        drafted(
            ("A confident claim.", ["E99"]),
            *covering_sentences(session, dependency),
        )
    ])

    briefing = brief(session, dependency.id, client=client, today=TODAY)

    assert briefing.withheld == {"unknown citation": 1}
    assert not any("confident" in s.text for s in briefing.sentences)


def test_an_evidence_quote_absent_from_its_page_withholds_the_sentence(
    session, dependency
):
    """The same split verify.py holds everywhere: presence on the page is
    mechanical, truth is not. A cited quote that is not on its cited page
    fails the mechanical half."""
    # Scoped to the fixture record: `make demo` commits real rows to the
    # same database, and an unscoped .first() silently mutates one of
    # those instead — the exact trap test_report.py documents.
    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).first()
    link.quote = "words that appear on no page"
    session.flush()

    client = stub_client([
        drafted(
            ("Backed by the record.", ["E1"]),
            *covering_sentences(session, dependency),
        )
    ])

    briefing = brief(session, dependency.id, client=client, today=TODAY)

    assert briefing.withheld == {"quote not on page": 1}


def test_an_uncovered_floor_refuses_the_whole_draft(session, dependency):
    """Constraint 4: the model may explain an Exception, never bury one.
    A draft that leaves any fired Exception unreferenced is refused as a
    whole — there is no partially-honest briefing."""
    refs = floor_refs(session, dependency)
    assert len(refs) > 1
    client = stub_client([
        drafted(("Only the first fact.", [refs[0]]))
    ])

    briefing = brief(session, dependency.id, client=client, today=TODAY)

    assert briefing.refused
    assert "floor" in briefing.refusal_reason
    assert briefing.sentences == ()


def test_a_sentence_lost_to_withholding_can_uncover_the_floor(
    session, dependency
):
    """Coverage is judged on the sentences that survive the checker: a
    fired Exception cited only by a withheld sentence is a buried
    Exception."""
    refs = floor_refs(session, dependency)
    covering = covering_sentences(session, dependency)
    # The sentence covering the last ref also cites an invented object,
    # so it is withheld — taking its coverage with it.
    broken = [(f"Fact {refs[-1]} holds.", [refs[-1], "E99"])]
    client = stub_client([drafted(*covering[:-1], *broken)])

    briefing = brief(session, dependency.id, client=client, today=TODAY)

    assert briefing.refused


def test_nothing_is_persisted(session, dependency):
    """The Briefing is a view. Deleting every briefing loses no fact —
    and generating one writes none, to any table at all."""
    from sqlalchemy import func

    from corridor.models import Base

    def counts():
        return {
            table.name: session.scalar(
                select(func.count()).select_from(table)
            )
            for table in Base.metadata.sorted_tables
        }

    before = counts()
    client = stub_client([drafted(*covering_sentences(session, dependency))])

    brief(session, dependency.id, client=client, today=TODAY)

    assert counts() == before


def test_the_prompt_supplies_the_citables_by_reference(session, dependency):
    """The model composes over objects the component names — it is never
    asked to invent a citation format. The user message therefore carries
    every ref the checker will accept."""
    client = stub_client([drafted(*covering_sentences(session, dependency))])

    brief(session, dependency.id, client=client, today=TODAY)

    user = client.calls[0].user
    assert "E1" in user
    assert "A1" in user
    for ref in floor_refs(session, dependency):
        assert ref in user


def test_the_prompt_is_loaded_from_its_checkout_not_the_process_cwd(
    session, dependency, tmp_path, monkeypatch
):
    client = stub_client([drafted(*covering_sentences(session, dependency))])
    monkeypatch.chdir(tmp_path)

    briefing = brief(session, dependency.id, client=client, today=TODAY)

    assert not briefing.refused
    assert "The final floor line is not negotiable" in client.calls[0].system


def test_the_prompt_attributes_a_verbal_backed_committed_date(session, dependency):
    committed_date = TODAY + timedelta(days=60)
    event = DependencyEvent(
        project_id=dependency.project_id,
        affected_external_org_id=None,
        stated_external_org_id=None,
        scope_mode="selected",
        event_type="commitment",
        source_kind="verbal",
        stated_party="CenterPoint Energy",
        event_date=TODAY - timedelta(days=1),
        description="CenterPoint said the relocation will finish in June.",
        created_by="local:phone-coordinator",
    )
    session.add(event)
    session.flush()
    session.add_all(
        (
            DependencyEventScope(event_id=event.id, dependency_id=dependency.id),
            DependencyEventTiming(
                event_id=event.id,
                kind="new",
                text=committed_date.isoformat(),
                precision="day",
                start_date=committed_date,
                end_date=committed_date,
            ),
        )
    )
    dependency.committed_date = committed_date
    session.flush()
    client = stub_client([drafted(*covering_sentences(session, dependency))])

    brief(session, dependency.id, client=client, today=TODAY)

    user = client.calls[0].user
    assert "[V1]" in user
    assert "CenterPoint Energy told local:phone-coordinator" in user
    assert f"on {TODAY - timedelta(days=1)}" in user


def test_the_prompt_uses_the_current_exact_day_statement_over_a_stale_scalar(
    session, dependency
):
    """The model receives the same Committed Date that readers publish."""
    committed_date = TODAY + timedelta(days=60)
    event = DependencyEvent(
        project_id=dependency.project_id,
        affected_external_org_id=None,
        stated_external_org_id=None,
        scope_mode="selected",
        event_type="commitment",
        source_kind="cited",
        stated_party="CenterPoint Energy",
        event_date=TODAY - timedelta(days=1),
        description="CenterPoint will finish relocation in October.",
        created_by="corridor:event-admission",
    )
    session.add(event)
    session.flush()
    session.add_all(
        (
            DependencyEventScope(event_id=event.id, dependency_id=dependency.id),
            DependencyEventTiming(
                event_id=event.id,
                kind="new",
                text=committed_date.isoformat(),
                precision="day",
                start_date=committed_date,
                end_date=committed_date,
            ),
        )
    )
    session.flush()
    direct_evidence = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).one()
    event_evidence = EvidenceLink(
        dependency_id=None,
        document_id=direct_evidence.document_id,
        page_no=direct_evidence.page_no,
        quote="CenterPoint will finish relocation in October.",
        verified=True,
    )
    session.add(event_evidence)
    session.flush()
    session.add(
        DependencyEventEvidence(
            evidence_link_id=event_evidence.id,
            event_id=event.id,
            recorded_by="corridor:event-admission",
        )
    )
    session.flush()
    stale_date = dependency.committed_date
    client = stub_client([drafted(*covering_sentences(session, dependency))])

    brief(session, dependency.id, client=client, today=TODAY)

    user = client.calls[0].user
    assert f"committed {committed_date};" in user
    assert f"committed {stale_date};" not in user


def test_the_prompt_suppresses_a_stale_scalar_after_a_month_statement(
    session, dependency
):
    """Month precision is not a fabricated day in the model's source record."""
    event = DependencyEvent(
        project_id=dependency.project_id,
        affected_external_org_id=None,
        stated_external_org_id=None,
        scope_mode="selected",
        event_type="commitment",
        source_kind="cited",
        stated_party="CenterPoint Energy",
        event_date=TODAY - timedelta(days=1),
        description="CenterPoint now expects completion in October 2026.",
        created_by="corridor:event-admission",
    )
    session.add(event)
    session.flush()
    session.add_all(
        (
            DependencyEventScope(event_id=event.id, dependency_id=dependency.id),
            DependencyEventTiming(
                event_id=event.id,
                kind="new",
                text="October 2026",
                precision="month",
                start_date=date(2026, 10, 1),
                end_date=date(2026, 10, 31),
            ),
        )
    )
    session.flush()
    stale_date = dependency.committed_date
    client = stub_client([drafted(*covering_sentences(session, dependency))])

    brief(session, dependency.id, client=client, today=TODAY)

    user = client.calls[0].user
    assert "committed —;" in user
    assert f"committed {stale_date};" not in user


def test_the_prompt_withholds_an_unverified_cited_statement_date(
    session, dependency
):
    """A cited date needs the event's own verified Evidence to be publishable."""
    committed_date = TODAY - timedelta(days=1)
    event = DependencyEvent(
        project_id=dependency.project_id,
        affected_external_org_id=None,
        stated_external_org_id=None,
        scope_mode="selected",
        event_type="commitment",
        source_kind="cited",
        stated_party="CenterPoint Energy",
        event_date=TODAY - timedelta(days=1),
        description="CenterPoint will finish relocation in October.",
        created_by="corridor:event-admission",
    )
    session.add(event)
    session.flush()
    session.add_all(
        (
            DependencyEventScope(event_id=event.id, dependency_id=dependency.id),
            DependencyEventTiming(
                event_id=event.id,
                kind="new",
                text=committed_date.isoformat(),
                precision="day",
                start_date=committed_date,
                end_date=committed_date,
            ),
        )
    )
    session.flush()
    stale_date = dependency.committed_date
    client = stub_client([drafted(*covering_sentences(session, dependency))])

    brief(session, dependency.id, client=client, today=TODAY)

    user = client.calls[0].user
    assert "committed —;" in user
    assert f"committed {stale_date};" not in user
    assert f"committed {committed_date};" not in user
    assert "OVERDUE" not in user


# ------------------------------------------------------------- the render


def test_the_render_shows_sentences_with_their_markers(session, dependency):
    source_text = "Ready Milestone Road: Evidence and Verbal remain the source wording."
    refs = floor_refs(session, dependency)
    client = stub_client([drafted((source_text, refs))])

    out = render(brief(session, dependency.id, client=client, today=TODAY))

    assert "DEP-00001" in out
    assert PROMPT_VERSION in out
    assert RULESET_VERSION in out
    assert "[X1" in out
    assert "Coordination summary — AI draft" in out
    assert "constraint alerts checked" in out
    assert source_text in out
    assert "Briefing —" not in out


def test_withheld_counts_are_visible_in_the_render(session, dependency):
    client = stub_client([
        drafted(
            ("This claim cites nothing.", []),
            *covering_sentences(session, dependency),
        )
    ])

    out = render(brief(session, dependency.id, client=client, today=TODAY))

    assert "1 sentence withheld: uncited" in out


def test_a_refused_briefing_renders_as_a_refusal(session, dependency):
    refs = floor_refs(session, dependency)
    client = stub_client([drafted(("Only one.", [refs[0]]))])

    out = render(brief(session, dependency.id, client=client, today=TODAY))

    assert "REFUSED" in out
    assert "floor" in out


def test_withheld_counts_show_even_on_a_refusal(session, dependency):
    """A refusal explains itself fully: what was buried AND what was
    withheld on the way. The reader diagnosing a refused draft needs
    both."""
    refs = floor_refs(session, dependency)
    client = stub_client([
        drafted(
            ("Cites nothing.", []),
            (f"Fact {refs[0]} holds.", [refs[0]]),
        )
    ])

    out = render(brief(session, dependency.id, client=client, today=TODAY))

    assert "REFUSED" in out
    assert "1 sentence withheld: uncited" in out


def test_the_checker_is_direct_over_constructed_citables():
    """The checker alone, no database: pure function, constructed record.
    An empty sentence counts too — "never silently dropped" includes a
    sentence with no words."""
    from corridor.briefing import Citable, Sentence, _check

    by_ref = {
        "E1": Citable(
            ref="E1", kind="evidence", text="q", quote="on the page",
            page_text="exactly on the page", text_source="text_layer",
        ),
        "X1": Citable(ref="X1", kind="exception", text="OVERDUE"),
    }
    kept, withheld = _check(
        [
            Sentence(text="Good.", cites=("X1",)),
            Sentence(text="Backed.", cites=("E1",)),
            Sentence(text="", cites=("X1",)),
            Sentence(text="Uncited.", cites=()),
            Sentence(text="Invented.", cites=("E9",)),
        ],
        by_ref,
    )

    assert [s.text for s in kept] == ["Good.", "Backed."]
    assert withheld == {"empty": 1, "uncited": 1, "unknown citation": 1}


# ---------------------------------------- one narrative per project (#119)


@pytest.fixture
def second_dependency(session, project, dependency):
    """A second record, so the project floor spans records."""
    dep = Dependency(
        project_id=project.id,
        ref_code="DEP-00002",
        dep_type="utility_relocation",
        title="Gas — Atmos",
        resolution_strategy=None,
        internal_owner="Bryce",
    )
    session.add(dep)
    session.flush()
    return dep


def project_floor(session, project):
    """The project's rule facets, in the bucket refs the component assigns."""
    from corridor.dependency_events import published_dependency_statements
    from corridor.exceptions import evaluate_project

    dependencies = list(
        session.scalars(
            select(Dependency)
            .where(
                Dependency.project_id == project.id,
                Dependency.dismissed_at.is_(None),
            )
            .order_by(Dependency.ref_code)
        )
    )
    publication = published_dependency_statements(
        session,
        (dependency.id for dependency in dependencies),
        project_id=project.id,
    )
    evaluation = evaluate_project(
        session,
        project.id,
        today=TODAY,
        statement_publication=publication,
    )
    return [f"XB{i + 1}" for i in range(len(evaluation.facets()))]


def test_a_project_briefing_accepts_one_floor_citation_per_exception_bucket(
    session, project, dependency, second_dependency
):
    """At project scope the floor is the rule facets, while each bucket
    still names the individual Exception refs it places underneath."""
    from corridor.briefing import brief_project
    from corridor.dependency_events import published_dependency_statements
    from corridor.exceptions import evaluate_project

    publication = published_dependency_statements(
        session,
        (dependency.id, second_dependency.id),
        project_id=project.id,
    )
    facets = evaluate_project(
        session,
        project.id,
        today=TODAY,
        statement_publication=publication,
    ).facets()
    bucket_refs = [f"XB{i + 1}" for i in range(len(facets))]
    covering = [(f"Bucket {ref} holds.", [ref]) for ref in bucket_refs]

    briefing = brief_project(
        session,
        project.id,
        client=stub_client([drafted(*covering)]),
        today=TODAY,
    )

    assert not briefing.refused
    buckets = [
        citable
        for citable in briefing.citables
        if citable.kind == "exception_bucket"
    ]
    assert briefing.floor == tuple(bucket_refs), (
        [facet.rule for facet in facets],
        [bucket.text for bucket in buckets],
    )
    assert [bucket.ref for bucket in buckets] == bucket_refs
    assert [bucket.count for bucket in buckets] == [facet.count for facet in facets]
    assert all(bucket.covers for bucket in buckets)
    assert {covered for bucket in buckets for covered in bucket.covers} == {
        citable.ref
        for citable in briefing.citables
        if citable.kind == "exception"
    }


def test_the_project_prompt_requires_buckets_and_keeps_instances_optional(
    session, project, dependency, second_dependency
):
    from corridor.briefing import brief_project
    from corridor.dependency_events import published_dependency_statements
    from corridor.exceptions import evaluate_project

    publication = published_dependency_statements(
        session,
        (dependency.id, second_dependency.id),
        project_id=project.id,
    )
    facet_count = len(
        evaluate_project(
            session,
            project.id,
            today=TODAY,
            statement_publication=publication,
        ).facets()
    )
    bucket_refs = [f"XB{i + 1}" for i in range(facet_count)]
    client = stub_client(
        [drafted(*[(f"Bucket {ref} holds.", [ref]) for ref in bucket_refs])]
    )

    briefing = brief_project(session, project.id, client=client, today=TODAY)

    floor_line = next(
        line
        for line in client.calls[0].user.splitlines()
        if line.startswith("Every one of these")
    )
    assert all(ref in floor_line for ref in briefing.floor)
    assert all(ref.startswith("XB") for ref in briefing.floor)
    assert not any(
        citable.ref in floor_line
        for citable in briefing.citables
        if citable.kind == "exception"
    )


def test_a_project_briefing_floors_every_records_exceptions(
    session, project, dependency, second_dependency
):
    """A draft that omits any project rule bucket is refused whole."""
    from corridor.briefing import brief_project

    refs = project_floor(session, project)
    assert len(refs) > 1

    covering = [(f"Fact {ref} holds.", [ref]) for ref in refs]
    briefing = brief_project(
        session, project.id, client=stub_client([drafted(*covering)]), today=TODAY
    )
    assert not briefing.refused
    assert len(briefing.sentences) == len(refs)

    partial = covering[:-1]
    refused = brief_project(
        session, project.id, client=stub_client([drafted(*partial)]), today=TODAY
    )
    assert refused.refused
    assert "floor" in refused.refusal_reason


def test_project_citables_attribute_their_record(
    session, project, dependency, second_dependency
):
    """Refs are unique across the project and each citable names the
    record it belongs to — the model cannot attribute one record's fact
    to another without the reader seeing the ref resolve elsewhere."""
    from corridor.briefing import brief_project

    refs = project_floor(session, project)
    covering = [(f"Fact {ref} holds.", [ref]) for ref in refs]
    client = stub_client([drafted(*covering)])

    briefing = brief_project(session, project.id, client=client, today=TODAY)

    user = client.calls[0].user
    assert "DEP-00001" in user
    assert "DEP-00002" in user
    assert len({c.ref for c in briefing.citables}) == len(briefing.citables)
    assert any("DEP-00002" in c.text for c in briefing.citables if c.kind == "exception")
    assert briefing.floor == tuple(refs)


def test_a_project_briefing_carries_the_same_stamps(
    session, project, dependency, second_dependency
):
    from corridor.briefing import brief_project

    refs = project_floor(session, project)
    covering = [(f"Fact {ref} holds.", [ref]) for ref in refs]

    briefing = brief_project(
        session, project.id, client=stub_client([drafted(*covering)]), today=TODAY
    )

    assert briefing.prompt_version == PROMPT_VERSION
    assert briefing.evaluated_at == TODAY
    assert briefing.ruleset_version == RULESET_VERSION
    assert project.slug in briefing.ref_code


def test_a_project_briefing_is_one_model_call(
    session, project, dependency, second_dependency
):
    """Cost stays one read of one project's record per invocation — no
    per-record fan-out, no batch path."""
    from corridor.briefing import brief_project

    refs = project_floor(session, project)
    covering = [(f"Fact {ref} holds.", [ref]) for ref in refs]
    client = stub_client([drafted(*covering)])

    brief_project(session, project.id, client=client, today=TODAY)

    assert len(client.calls) == 1


def test_an_empty_scope_briefs_empty_without_a_model_call(session, project):
    """Nothing to cite means nothing a sentence could stand on. The
    honest briefing is empty — and free, because drafting prose only to
    withhold all of it would burn a call on nothing."""
    from corridor.briefing import brief_project

    client = stub_client([])

    briefing = brief_project(session, project.id, client=client, today=TODAY)

    assert not briefing.refused
    assert briefing.sentences == ()
    assert briefing.citables == ()
    assert client.calls == []


def test_a_dismissed_record_is_not_narrated(session, project, dependency):
    """A briefing narrates the working list; a dismissed record left it,
    with its reason on file (ADR-0032). Narrating it — or refusing the
    whole draft because its exceptions went uncited — would resurrect a
    record a reviewer threw out."""
    from corridor.adjudicate import dismiss_dependency
    from corridor.briefing import brief_project
    from corridor.principals import HumanPrincipal

    dismiss_dependency(
        session,
        dependency,
        "not-a-conflict",
        principal=HumanPrincipal("local:briefing-tester"),
    )

    client = stub_client([])
    briefing = brief_project(session, project.id, client=client, today=TODAY)

    assert "0 records" in briefing.ref_code
    assert briefing.sentences == ()
    assert client.calls == []


def test_live_nhhip_project_floor_is_bounded_by_buckets(
    shared_source_database_url,
):
    """The populated project is the scale regression from #127.

    Fresh databases skip because they deliberately carry no production corpus;
    the shared corpus proves every live Exception remains under a bounded facet
    floor without making a model call in the test suite.
    """
    from corridor.briefing import brief_project

    shared_engine = create_engine(shared_source_database_url)
    connection = shared_engine.connect()
    shared_session = Session(bind=connection)
    try:
        project = shared_session.scalars(
            select(Project).where(Project.slug == "nhhip-3c2")
        ).first()
        if project is None:
            pytest.skip("the shared NHHIP corpus is not present")
        from corridor.project_reading import freeze_project_reading

        # Guard on the same scope the briefing itself reads: a freshly
        # re-ingested project can exist — even hold raw rows mid-processing —
        # while the coherent project reading is still empty. That is the same
        # "no production corpus" state the docstring already promises to
        # skip on.
        if not freeze_project_reading(shared_session, project.id).rows:
            pytest.skip("the shared NHHIP corpus carries no readable records yet")

        def floor_covering_answer(call):
            """Cite exactly the floor references the prompt named."""
            floor_line = next(
                line
                for line in call.user.splitlines()
                if line.startswith("Every one of these")
            )
            refs = floor_line.rsplit(":", 1)[1].strip().removesuffix(".").split(", ")
            return drafted(*[(f"Bucket {ref} holds.", [ref]) for ref in refs])

        briefing = brief_project(
            shared_session,
            project.id,
            client=FakeModelClient(
                floor_covering_answer,
                configuration=RequestConfiguration(model="scripted-floor-coverer"),
            ),
            today=TODAY,
        )

        assert not briefing.refused
        assert briefing.floor
        assert all(ref.startswith("XB") for ref in briefing.floor)
        assert len(
            [
                citable
                for citable in briefing.citables
                if citable.kind == "exception"
            ]
        ) > len(briefing.floor)
        assert {sentence.cites[0] for sentence in briefing.sentences} == set(
            briefing.floor
        )
    finally:
        shared_session.close()
        connection.close()
        shared_engine.dispose()
