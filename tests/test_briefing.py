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
from sqlalchemy import select

from corridor.briefing import PROMPT_VERSION, brief, render
from corridor.db import Session, engine
from corridor.exceptions import RULESET_VERSION
from corridor.models import (
    Assertion,
    AuditLog,
    Candidate,
    Dependency,
    DocPage,
    Document,
    EvidenceLink,
    Project,
)

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
        status="committed",
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
        satisfies_requirement=False,
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


class StubClient:
    """Recorded responses. CI never calls a model."""

    def __init__(self, responses, model="gpt-5.6-luna"):
        self.responses = list(responses)
        self.model = model
        self.calls = []

    def complete(self, *, system, user, schema, images=(), logprobs=False):
        self.calls.append({"system": system, "user": user, "schema": schema})
        return self.responses.pop(0)


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
    client = StubClient([drafted(*covering_sentences(session, dependency))])

    briefing = brief(session, dependency.id, client=client, today=TODAY)

    assert briefing.prompt_version == PROMPT_VERSION
    assert briefing.model == "gpt-5.6-luna"
    assert briefing.evaluated_at == TODAY
    assert briefing.ruleset_version == RULESET_VERSION


def test_an_uncited_sentence_is_withheld_and_counted(session, dependency):
    """A sentence that cannot cite is a sentence the Briefing may not
    contain — withheld loudly, never shown unverified, never silently
    dropped."""
    client = StubClient([
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
    client = StubClient([
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

    client = StubClient([
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
    client = StubClient([
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
    client = StubClient([drafted(*covering[:-1], *broken)])

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
    client = StubClient([drafted(*covering_sentences(session, dependency))])

    brief(session, dependency.id, client=client, today=TODAY)

    assert counts() == before


def test_the_prompt_supplies_the_citables_by_reference(session, dependency):
    """The model composes over objects the component names — it is never
    asked to invent a citation format. The user message therefore carries
    every ref the checker will accept."""
    client = StubClient([drafted(*covering_sentences(session, dependency))])

    brief(session, dependency.id, client=client, today=TODAY)

    user = client.calls[0]["user"]
    assert "E1" in user
    assert "A1" in user
    for ref in floor_refs(session, dependency):
        assert ref in user


# ------------------------------------------------------------- the render


def test_the_render_shows_sentences_with_their_markers(session, dependency):
    client = StubClient([drafted(*covering_sentences(session, dependency))])

    out = render(brief(session, dependency.id, client=client, today=TODAY))

    assert "DEP-00001" in out
    assert PROMPT_VERSION in out
    assert RULESET_VERSION in out
    assert "[X1" in out


def test_withheld_counts_are_visible_in_the_render(session, dependency):
    client = StubClient([
        drafted(
            ("This claim cites nothing.", []),
            *covering_sentences(session, dependency),
        )
    ])

    out = render(brief(session, dependency.id, client=client, today=TODAY))

    assert "1 sentence withheld: uncited" in out


def test_a_refused_briefing_renders_as_a_refusal(session, dependency):
    refs = floor_refs(session, dependency)
    client = StubClient([drafted(("Only one.", [refs[0]]))])

    out = render(brief(session, dependency.id, client=client, today=TODAY))

    assert "REFUSED" in out
    assert "floor" in out


def test_withheld_counts_show_even_on_a_refusal(session, dependency):
    """A refusal explains itself fully: what was buried AND what was
    withheld on the way. The reader diagnosing a refused draft needs
    both."""
    refs = floor_refs(session, dependency)
    client = StubClient([
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
        status="identified",
        resolution_strategy=None,
        internal_owner="Bryce",
    )
    session.add(dep)
    session.flush()
    return dep


def project_floor(session, project):
    """Every fired Exception across the project, in the refs the component
    assigns — read off the same engine, like the single-record helper."""
    from corridor.exceptions import evaluate

    return [f"X{i + 1}" for i in range(len(evaluate(session, project.id, today=TODAY)))]


def test_a_project_briefing_floors_every_records_exceptions(
    session, project, dependency, second_dependency
):
    """The floor widens to the whole project: a draft covering one
    record's Exceptions while burying another record's is refused whole,
    exactly as on the single path."""
    from corridor.briefing import brief_project

    refs = project_floor(session, project)
    assert len(refs) > len(floor_refs(session, dependency)), (
        "the second record must add fired Exceptions, or this test "
        "proves nothing about widening"
    )

    covering = [(f"Fact {ref} holds.", [ref]) for ref in refs]
    briefing = brief_project(
        session, project.id, client=StubClient([drafted(*covering)]), today=TODAY
    )
    assert not briefing.refused
    assert len(briefing.sentences) == len(refs)

    partial = covering[:-1]
    refused = brief_project(
        session, project.id, client=StubClient([drafted(*partial)]), today=TODAY
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
    client = StubClient([drafted(*covering)])

    briefing = brief_project(session, project.id, client=client, today=TODAY)

    user = client.calls[0]["user"]
    assert "DEP-00001" in user
    assert "DEP-00002" in user
    assert len({c.ref for c in briefing.citables}) == len(briefing.citables)
    by_ref = {c.ref: c for c in briefing.citables}
    assert any("DEP-00002" in c.text for c in briefing.citables if c.kind == "exception")
    assert briefing.floor == tuple(refs)


def test_a_project_briefing_carries_the_same_stamps(
    session, project, dependency, second_dependency
):
    from corridor.briefing import brief_project

    refs = project_floor(session, project)
    covering = [(f"Fact {ref} holds.", [ref]) for ref in refs]

    briefing = brief_project(
        session, project.id, client=StubClient([drafted(*covering)]), today=TODAY
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
    client = StubClient([drafted(*covering)])

    brief_project(session, project.id, client=client, today=TODAY)

    assert len(client.calls) == 1


def test_an_empty_scope_briefs_empty_without_a_model_call(session, project):
    """Nothing to cite means nothing a sentence could stand on. The
    honest briefing is empty — and free, because drafting prose only to
    withhold all of it would burn a call on nothing."""
    from corridor.briefing import brief_project

    client = StubClient([])

    briefing = brief_project(session, project.id, client=client, today=TODAY)

    assert not briefing.refused
    assert briefing.sentences == ()
    assert briefing.citables == ()
    assert client.calls == []
