"""The statement coordination screens' reading, and their written interface.

`statement_coordinate.html` used to require thirty names that nothing declared,
assembled as a thirty-one key dict literal at the end of a two-hundred-and-ten
line handler body. The environment's `Undefined` renders a missing scalar or a
missing sequence as nothing, so a dropped key produced a blank section and no
error, and every proof of this screen went through `TestClient` plus
PostgreSQL. The tests here construct the reading directly and render the
screens from it, with no database and no HTTP, and state mechanically that
what each template binds is a field the reading actually declares.
"""

import jinja2
import pytest
from jinja2 import meta, nodes

from corridor.models import (
    Candidate,
    ExternalOrg,
    Milestone,
    Project,
    ProjectRosterEntry,
)
from corridor.presentation import read_guided_save_offer
from corridor.statement_coordination import (
    AdmittedStatementCoordination,
    STATEMENT_NEXT_ACTION_CHOICES,
)
from corridor.web.app import TEMPLATES
from corridor.web.statement_view import (
    ADMITTED_TEMPLATE,
    PROPOSED_TEMPLATE,
    StatementCoordinationView,
)
from corridor.web.statement_forms import CandidateStatementEvidenceView

# The three names the handler passes beside the reading: the project it is
# scoped to, the reading itself, and the refusal sentence this route's own
# write path just raised.
HANDLER_CONTEXT = {"project", "reading", "error"}


def _reading(**overrides) -> StatementCoordinationView:
    """The proposed-statement screen's reading, stated entirely in this test."""
    defaults = dict(
        project=Project(id=1, slug="acme", name="Acme Interchange"),
        candidate=Candidate(id=77, kind="event", state="pending"),
        template=PROPOSED_TEMPLATE,
        candidate_fields={
            "description": "Relocation of the 12-inch main completes in March.",
            "event_date": "2026-02-04",
        },
        candidate_party="Northern Gas",
        candidate_affected_party_id=9,
        candidate_stated_party="Northern Gas",
        candidate_stated_party_id=9,
        candidate_description="Relocation of the 12-inch main completes in March.",
        candidate_event_date="2026-02-04",
        candidate_timing={
            "available": True,
            "text": "March 2026",
            "precision": "month",
            "start_date": "2026-03-01",
            "end_date": "2026-03-31",
        },
        guided_save_available=True,
        guided_save_refusal=None,
        next_action_choices=STATEMENT_NEXT_ACTION_CHOICES,
        candidate_evidence=(
            CandidateStatementEvidenceView(
                document_id=3,
                page_no=12,
                quote="the 12-inch main will be relocated by March 2026",
                filename="northern-gas-letter.pdf",
                page_text="Full registered page text.",
                page_text_source="text",
                has_page_image=True,
                supporting_quote_available=True,
            ),
        ),
        candidate_evidence_available=True,
        dependencies=(
            {
                "id": 51,
                "ref_code": "UC-014",
                "source_ref": "SUE-014",
                "title": "12-inch gas main",
                "location_desc": "north verge",
                "station_from": "10+00",
                "station_to": "12+50",
                "external_org_id": 9,
                "external_org_name": "Northern Gas",
            },
        ),
        roster=(ProjectRosterEntry(id=4, display_name="Dana Reyes"),),
        parties=(ExternalOrg(id=9, name="Northern Gas"),),
        milestones=(Milestone(id=2, code="M3", name="Substantial completion"),),
        history=(
            {"label": "Saved statement and Follow-up plan", "detail": "grouping receipt 8"},
        ),
    )
    return StatementCoordinationView(**{**defaults, **overrides})


def _render(reading: StatementCoordinationView, *, error: str | None = None) -> str:
    return TEMPLATES.env.get_template(reading.template).render(
        project=reading.project, reading=reading, error=error
    )


def _bound_fields(template_name: str, source_name: str) -> set[str]:
    """The fields a template's prelude binds off one reading object."""
    environment = jinja2.Environment(autoescape=True)
    tree = environment.parse(TEMPLATES.env.loader.get_source(
        TEMPLATES.env, template_name
    )[0])
    return {
        node.attr
        for node in tree.find_all(nodes.Getattr)
        if isinstance(node.node, nodes.Name) and node.node.name == source_name
    }


# --------------------------------------------- the interface is written down


@pytest.mark.parametrize("template", [PROPOSED_TEMPLATE, ADMITTED_TEMPLATE])
def test_a_statement_screen_requires_only_the_reading_and_the_handlers_own_names(
    template,
):
    """What the screen needs is the reading, not a dict a route assembled.

    `statement_coordinate.html` required thirty undeclared names and
    `statement_admitted_coordinate.html` twenty-two. Anything left here beyond
    the handler's three is a name the template reads off whatever the caller
    happened to put in the context.
    """
    environment = jinja2.Environment(autoescape=True)
    source = TEMPLATES.env.loader.get_source(TEMPLATES.env, template)[0]
    undeclared = meta.find_undeclared_variables(environment.parse(source))
    # `request` is Starlette's own, and the template's own macros are reported
    # by `meta` as if they were free names.
    macros = {
        node.name
        for node in environment.parse(source).find_all(nodes.Macro)
    }

    assert undeclared - set(TEMPLATES.env.globals) - {"request"} - macros == (
        HANDLER_CONTEXT
    )


def test_every_name_the_proposed_screen_binds_is_a_field_of_the_reading():
    """A prelude may only bind what the reading declares.

    Jinja answers a missing attribute with `Undefined`, which prints as the
    empty string, so a field renamed or dropped from the reading would leave
    this screen rendering blank sections. The field list is the interface, and
    this is what makes that true rather than intended.
    """
    declared = set(StatementCoordinationView.__dataclass_fields__)

    assert _bound_fields(PROPOSED_TEMPLATE, "reading") <= declared


def test_every_name_the_admitted_screen_binds_is_declared_by_its_two_readings():
    """The residual screen binds the accepted Commitment's own reading.

    `AdmittedStatementCoordination` already declares those facts, so the
    template reads them straight off it instead of through a dict that
    restated their names. The reason sets the plan controls offer belong to
    the screen's reading beside it.
    """
    admitted_names = set(AdmittedStatementCoordination.__dataclass_fields__) | {
        name
        for name in dir(AdmittedStatementCoordination)
        if isinstance(getattr(AdmittedStatementCoordination, name, None), property)
    }

    assert _bound_fields(ADMITTED_TEMPLATE, "admitted") <= admitted_names
    assert _bound_fields(ADMITTED_TEMPLATE, "reading") <= set(
        StatementCoordinationView.__dataclass_fields__
    )


# ------------------------------------------ the screen renders from a reading


def test_the_proposed_statement_screen_renders_from_its_reading_alone():
    """No database and no HTTP: the reading is the whole of what it needs."""
    markup = _render(_reading())

    assert "Acme Interchange" in markup
    assert "Northern Gas" in markup
    # The cited passage and its registered page, beside the claim.
    assert "the 12-inch main will be relocated by March 2026" in markup
    assert "northern-gas-letter.pdf" in markup
    # The Constraint choices, the roster, the key dates and the Next Actions.
    assert "SUE-014" in markup and "12-inch gas main" in markup
    assert "Dana Reyes" in markup
    assert "M3 — Substantial completion" in markup
    for choice in STATEMENT_NEXT_ACTION_CHOICES:
        assert choice in markup


def test_the_offer_the_reading_refuses_is_the_sentence_the_screen_prints():
    """One value for the offer (ADR-0039); the screen adds no condition."""
    offer = read_guided_save_offer(
        evidence_available=False, timing_available=True, roster_available=True
    )
    assert offer.refusal is not None

    markup = _render(
        _reading(guided_save_available=offer.available, guided_save_refusal=offer.refusal)
    )

    assert offer.refusal in markup
    assert "disabled>Save statement and Follow-up plan" in markup


def test_a_statement_with_no_registered_source_context_says_so():
    """The evidence the reading carries is what the screen reports on."""
    markup = _render(_reading(candidate_evidence=()))

    assert (
        "This proposed statement has no complete registered source context"
        in markup
    )


def test_the_routes_refusal_sentence_is_the_one_thing_the_handler_adds():
    markup = _render(_reading(), error="the statement moved on while you read it")

    assert "the statement moved on while you read it" in markup
