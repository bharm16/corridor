"""The shared coordinator-screen primitives, and the two screens using them.

Nothing here reads a clock. Every date is stated in the test, because a
lateness rule proved against `date.today()` proves only that the machine
agreed with itself on the day it ran.
"""

import json
from datetime import date
from hashlib import sha256
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
    record_external_party_statement,
)
from corridor.models import (
    Dependency,
    DocPage,
    Document,
    ExternalOrg,
    Milestone,
    Project,
)
from corridor.presentation import exception_name, label
from corridor.principals import HumanPrincipal
from corridor.web import ui_primitives
from corridor.web.app import TEMPLATES, app, get_human_principal, get_session
from corridor.web.ui_primitives import (
    FOCUS_IDS,
    MARKS,
    NEXT_ACTION_OVERDUE,
    PROMISED_FOR_AFTER_REQUIRED_BY,
    FieldError,
    StateLabel,
    focus_target,
    next_action_overdue,
    promised_after_required,
)

from access_support import seed_membership

TEST_PRINCIPAL = HumanPrincipal("local:test-reviewer")


# --------------------------------------------------------------- the words


def test_a_state_label_carries_its_own_words():
    """A tone with no words is exactly the defect this module exists to end."""
    with pytest.raises(ValueError):
        StateLabel("attention", "   ")


def test_a_state_label_refuses_an_unknown_tone():
    with pytest.raises(ValueError):
        StateLabel("scary", "something happened")


def test_every_tone_prints_a_mark_beside_its_words():
    for tone in MARKS:
        assert StateLabel(tone, "words").mark.strip()


def test_the_consequence_words_come_from_the_adopted_vocabulary():
    """No new customer term: the alert column already prints these words."""
    assert NEXT_ACTION_OVERDUE.text == exception_name("ACTION_OVERDUE")
    assert label("promised_for") in PROMISED_FOR_AFTER_REQUIRED_BY.text
    assert label("required_by") in PROMISED_FOR_AFTER_REQUIRED_BY.text


# ------------------------------------------------------ the lateness rules


def test_a_next_action_due_today_is_not_yet_overdue():
    assert next_action_overdue(date(2026, 3, 4), today=date(2026, 3, 4)) is None


def test_a_next_action_with_no_date_is_not_overdue():
    assert next_action_overdue(None, today=date(2026, 3, 4)) is None


def test_a_next_action_past_its_date_is_overdue():
    assert (
        next_action_overdue(date(2026, 3, 3), today=date(2026, 3, 4))
        is NEXT_ACTION_OVERDUE
    )


def test_a_promise_on_the_required_date_is_not_late():
    assert promised_after_required(date(2026, 9, 1), date(2026, 9, 1)) is None


def test_a_promise_without_a_required_date_says_nothing():
    assert promised_after_required(date(2026, 9, 1), None) is None


def test_a_promise_after_the_required_date_is_named():
    assert (
        promised_after_required(date(2026, 9, 2), date(2026, 9, 1))
        is PROMISED_FOR_AFTER_REQUIRED_BY
    )


# --------------------------------------------------------- where focus goes


def test_a_plain_reading_focuses_the_item():
    assert focus_target() == FOCUS_IDS["item"]


def test_a_completed_save_focuses_its_outcome():
    assert focus_target(saved=True) == FOCUS_IDS["outcome"]


def test_refused_fields_outrank_a_completed_save():
    assert (
        focus_target(saved=True, errors=[FieldError("owner", "Choose a person")])
        == FOCUS_IDS["errors"]
    )


def test_a_refusal_outranks_everything_else():
    assert (
        focus_target(
            refused=True,
            saved=True,
            errors=[FieldError("owner", "Choose a person")],
        )
        == FOCUS_IDS["refusal"]
    )


# ------------------------------------------------------------- the markup


class _Markup(HTMLParser):
    """The few structural facts these assertions need from rendered HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str]]] = []
        self.text_of: list[tuple[str, dict[str, str], list[str]]] = []
        self._open: list[list[str]] = []

    def handle_starttag(self, tag, attrs):
        attributes = {name: (value or "") for name, value in attrs}
        self.elements.append((tag, attributes))
        collected: list[str] = []
        self.text_of.append((tag, attributes, collected))
        self._open.append(collected)

    def handle_endtag(self, tag):
        if self._open:
            self._open.pop()

    def handle_data(self, data):
        for collected in self._open:
            collected.append(data)

    def by_tag(self, tag):
        return [attributes for name, attributes in self.elements if name == tag]

    def texts(self, tag):
        return [
            " ".join("".join(parts).split())
            for name, _, parts in self.text_of
            if name == tag
        ]

    def with_attribute(self, name):
        return [
            attributes for _, attributes in self.elements if name in attributes
        ]


def _parse(markup: str) -> _Markup:
    parser = _Markup()
    parser.feed(markup)
    return parser


def _render(source: str, **context) -> str:
    return TEMPLATES.env.from_string(
        '{% import "_primitives.html" as ui %}' + source
    ).render(**context)


def test_a_state_prints_its_words_and_leaves_the_mark_to_the_eye():
    markup = _render(
        "{{ ui.state(label) }}", label=StateLabel("attention", "sources disagree")
    )
    parsed = _parse(markup)

    assert "sources disagree" in markup
    outer = parsed.by_tag("span")[0]
    assert "state-attention" in outer["class"]
    assert parsed.by_tag("span")[1]["aria-hidden"] == "true"
    # The words survive the class being ignored, which is what a screen
    # reader, a printout, and a greyscale projector all do.
    assert "sources disagree" in parsed.texts("span")[0]


def test_an_absent_state_renders_nothing():
    assert _render("{{ ui.state(label) }}", label=None).strip() == ""


def test_before_and_after_values_are_read_in_order_beside_their_field():
    rows = [
        {"field": "Promised for", "before": "2026-04-01", "after": "2026-06-15"},
        {"field": "Assigned to", "before": None, "after": "Dana Fields"},
    ]
    markup = _render(
        "{{ ui.before_after('What this source changes', rows) }}", rows=rows
    )
    parsed = _parse(markup)

    assert parsed.texts("caption") == ["What this source changes"]
    assert [header["scope"] for header in parsed.by_tag("th")] == [
        "col",
        "col",
        "col",
        "row",
        "row",
    ]
    assert parsed.texts("th")[:3] == [
        "Field",
        "Current accepted value",
        "Incoming value",
    ]
    assert parsed.texts("th")[3:] == ["Promised for", "Assigned to"]
    assert parsed.texts("td") == [
        "2026-04-01",
        "2026-06-15",
        ui_primitives.NOT_RECORDED,
        "Dana Fields",
    ]
    # A struck-through value is a difference only the eye can see.
    assert "line-through" not in markup


def test_a_field_label_is_visible_and_bound_to_its_control():
    markup = _render(
        "{% call ui.field('owner', 'Assigned to', hint='A project-team member',"
        " required=true) %}"
        '<select id="owner" aria-describedby="owner-hint"></select>'
        "{% endcall %}"
    )
    parsed = _parse(markup)

    assert parsed.by_tag("label")[0]["for"] == "owner"
    assert "Assigned to" in parsed.texts("label")[0]
    assert "(required)" in parsed.texts("label")[0]
    assert parsed.by_tag("p")[0]["id"] == "owner-hint"
    assert parsed.by_tag("select")[0]["aria-describedby"] == "owner-hint"


def test_the_error_summary_takes_focus_and_links_each_field():
    errors = [
        FieldError("owner", "Choose the project-team member"),
        FieldError("next-action", "Choose the project response"),
    ]
    markup = _render(
        "{{ ui.error_summary(errors, focus) }}",
        errors=errors,
        focus=focus_target(errors=errors),
    )
    parsed = _parse(markup)

    summary = parsed.by_tag("div")[0]
    assert summary["id"] == FOCUS_IDS["errors"]
    assert summary["role"] == "alert"
    assert summary["tabindex"] == "-1"
    assert "autofocus" in summary
    assert "2 fields need attention" in parsed.texts("h2")[0]
    assert [link["href"] for link in parsed.by_tag("a")] == ["#owner", "#next-action"]


def test_no_error_summary_is_rendered_without_errors():
    assert _render("{{ ui.error_summary([], focus) }}", focus="").strip() == ""


def test_child_selection_is_complete_from_the_keyboard():
    children = [
        {"id": 11, "text": "U-042 — Promised for", "selected": True, "held_out": ""},
        {
            "id": 12,
            "text": "U-043 — Utility Owner",
            "selected": False,
            "held_out": "the source removes a value",
        },
    ]
    markup = _render(
        "{{ ui.child_selection('child_id', 'Changes ready for decision', children) }}",
        children=children,
    )
    parsed = _parse(markup)

    boxes = parsed.by_tag("input")
    assert [box["type"] for box in boxes] == ["checkbox", "checkbox"]
    # Every child is reached by Tab and toggled by Space, and every one has a
    # label bound to it rather than adjacent text.
    assert [box["id"] for box in boxes] == ["child_id-11", "child_id-12"]
    assert [entry["for"] for entry in parsed.by_tag("label")] == [
        "child_id-11",
        "child_id-12",
    ]
    assert "checked" in boxes[0]
    assert "disabled" in boxes[1]
    assert boxes[1]["aria-describedby"] == "child_id-12-reason"
    assert "the source removes a value" in markup
    assert parsed.texts("legend") == ["Changes ready for decision"]


def test_known_facts_read_as_terms_and_values():
    markup = _render(
        "{{ ui.fact_list(items) }}",
        items=[("Promised for", "2026-06-15"), ("Assigned to", None)],
    )
    parsed = _parse(markup)

    assert parsed.texts("dt") == ["Promised for", "Assigned to"]
    assert parsed.texts("dd") == ["2026-06-15", ui_primitives.NOT_RECORDED]


def test_a_record_table_carries_a_caption_and_column_headers():
    markup = _render(
        "{% call ui.record_table('Children of this revision',"
        " ['Utility Conflict', 'Field']) %}"
        "<tr><td>U-042</td><td>Promised for</td></tr>{% endcall %}"
    )
    parsed = _parse(markup)

    assert parsed.texts("caption") == ["Children of this revision"]
    assert [header["scope"] for header in parsed.by_tag("th")] == ["col", "col"]
    assert parsed.texts("th") == ["Utility Conflict", "Field"]


def test_the_reason_an_item_is_presented_comes_first_as_a_heading():
    markup = _render(
        "{{ ui.reason_presented('One revision of the UCM workbook',"
        " 'Thirty-eight exact changes are ready for decision.') }}"
    )
    parsed = _parse(markup)

    assert parsed.texts("h2") == ["One revision of the UCM workbook"]
    assert "Thirty-eight exact changes" in markup


def test_sticky_actions_are_named_as_one_group():
    markup = _render(
        "{% call ui.sticky_actions() %}<button>Save</button>{% endcall %}"
    )
    parsed = _parse(markup)

    group = [
        attributes
        for attributes in parsed.by_tag("div")
        if attributes.get("role") == "group"
    ]
    assert group and group[0]["aria-label"] == "Actions for this item"
    assert parsed.texts("button") == ["Save"]


def test_a_region_is_reachable_by_its_own_heading():
    markup = _render(
        "{% call ui.region('packet', 'One revision of the UCM workbook') %}"
        "<p>body</p>{% endcall %}"
    )
    parsed = _parse(markup)

    assert parsed.by_tag("section")[0]["aria-labelledby"] == "packet-heading"
    assert parsed.by_tag("h2")[0]["id"] == "packet-heading"


def test_a_keyboard_hint_spells_its_key_out_for_a_screen_reader():
    markup = _render("{{ ui.shortcut('a', 'Add constraint') }}")
    parsed = _parse(markup)

    assert parsed.by_tag("kbd")[0]["aria-hidden"] == "true"
    assert "keyboard shortcut: a" in markup
    assert "Add constraint" in markup


def test_an_evidence_reference_names_its_source_in_words():
    markup = _render(
        "{{ ui.evidence_reference('ucm-rev-c.pdf', 14, quote='Relocation by June 15') }}"
    )

    assert "ucm-rev-c.pdf, page 14" in markup
    assert label("cited_passage") in markup
    assert "Relocation by June 15" in markup


# ------------------------------------------------------ the screens using it


@pytest.fixture
def client(session):
    """The app shares the test's transaction, so nothing is committed."""
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: TEST_PRINCIPAL
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def project(session):
    p = Project(slug="ui-primitives", name="UI Primitives", is_synthetic=True)
    session.add(p)
    session.flush()
    seed_membership(session, p, TEST_PRINCIPAL)
    return p


def _autofocused(markup: str) -> list[dict[str, str]]:
    return _parse(markup).with_attribute("autofocus")


class _LedgerRows(HTMLParser):
    """The Constraint log's rows, as the text of each cell."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "td" and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def row(self, ref_code: str) -> list[str]:
        for row in self.rows:
            if row and row[0] == ref_code:
                return row
        raise AssertionError(f"no ledger row for {ref_code}")


def _promise(session, project, dependency, *, promised_for: date) -> None:
    """One published Promised For for this Constraint, from a cited source."""
    quote = "The organization stated when it would deliver."
    document = Document(
        project_id=project.id,
        sha256=sha256(f"{project.id}:{dependency.ref_code}".encode()).hexdigest(),
        filename="promise.pdf",
        doc_type="minutes",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    session.add(
        DocPage(document_id=document.id, page_no=1, text=quote, image_path=None)
    )
    session.flush()
    record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=dependency.external_org_id,
        stated_party=session.get(ExternalOrg, dependency.external_org_id).name,
        stated_external_org_id=dependency.external_org_id,
        source_kind="cited",
        event_date=date(2026, 1, 5),
        description=quote,
        new_timing=StatementTiming.day(promised_for.isoformat(), promised_for),
        scope=StatementScope.selected((dependency.id,)),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(document.id, 1, quote),
    )


def _dependency(session, project, ref_code: str, **columns) -> Dependency:
    org = ExternalOrg(name=f"{ref_code} organization")
    session.add(org)
    session.flush()
    dependency = Dependency(
        project_id=project.id,
        ref_code=ref_code,
        dep_type="utility_relocation",
        title=f"Telecom — {ref_code}",
        external_org_id=org.id,
        internal_owner="Dana Fields",
        **columns,
    )
    session.add(dependency)
    session.flush()
    return dependency


def test_a_next_action_past_its_date_says_so_in_the_same_cell(
    client, session, project
):
    """The defect this ticket names: the date was red and bold and silent."""
    _dependency(
        session,
        project,
        "DEP-LATE-ACTION",
        next_action="Confirm the relocation date",
        # Before Corridor existed, so no run of this test can call it current.
        action_due_date=date(2000, 1, 1),
    )

    page = client.get(f"/ledger/{project.slug}")
    rows = _LedgerRows()
    rows.feed(page.text)
    next_action_cell = rows.row("DEP-LATE-ACTION")[7]

    assert "by 2000-01-01" in next_action_cell
    assert exception_name("ACTION_OVERDUE") in next_action_cell
    assert 'class="late"' not in page.text


def test_a_next_action_still_ahead_of_its_date_says_nothing(
    client, session, project
):
    _dependency(
        session,
        project,
        "DEP-EARLY-ACTION",
        next_action="Confirm the relocation date",
        action_due_date=date(2099, 1, 1),
    )

    page = client.get(f"/ledger/{project.slug}")
    rows = _LedgerRows()
    rows.feed(page.text)

    assert exception_name("ACTION_OVERDUE") not in rows.row("DEP-EARLY-ACTION")[7]


def test_a_promise_after_the_required_date_says_so_in_the_same_cell(
    client, session, project
):
    dependency = _dependency(
        session, project, "DEP-LATE-PROMISE", need_date=date(2030, 1, 1)
    )
    _promise(session, project, dependency, promised_for=date(2030, 6, 1))

    page = client.get(f"/ledger/{project.slug}")
    rows = _LedgerRows()
    rows.feed(page.text)
    promised_cell = rows.row("DEP-LATE-PROMISE")[8]

    assert "2030-06-01" in promised_cell
    assert PROMISED_FOR_AFTER_REQUIRED_BY.text in promised_cell


def test_a_promise_before_the_required_date_says_nothing(client, session, project):
    dependency = _dependency(
        session, project, "DEP-EARLY-PROMISE", need_date=date(2030, 6, 1)
    )
    _promise(session, project, dependency, promised_for=date(2030, 1, 1))

    page = client.get(f"/ledger/{project.slug}")
    rows = _LedgerRows()
    rows.feed(page.text)

    assert (
        PROMISED_FOR_AFTER_REQUIRED_BY.text
        not in rows.row("DEP-EARLY-PROMISE")[8]
    )


# --- The focus contract on a screen that saves, refuses, and refreshes ------


KEY_DATE_CSV = "code,name,need_date\nUTIL-CLEAR,Utility clearance,2026-11-01\n"


def _preview(client, project, content=KEY_DATE_CSV):
    return client.post(
        f"/key-dates/{project.slug}/preview",
        data={"source_name": "typed.csv", "content": content},
    )


def _confirm_fields(session, project, content=KEY_DATE_CSV):
    codes = [
        row.split(",")[0]
        for row in content.strip().splitlines()[1:]
        if row.strip()
    ]
    predecessors = {code: None for code in codes}
    for milestone in session.scalars(
        select(Milestone).where(Milestone.project_id == project.id)
    ):
        if milestone.code in predecessors:
            predecessors[milestone.code] = milestone.id
    return {
        "source_name": "typed.csv",
        "expected_sha256": sha256(content.encode()).hexdigest(),
        "expected_predecessors": json.dumps(predecessors),
        "content": content,
    }


def test_a_plain_reading_puts_the_keyboard_on_the_item(client, project):
    page = client.get(f"/key-dates/{project.slug}")
    focused = _autofocused(page.text)

    assert len(focused) == 1
    assert focused[0]["id"] == FOCUS_IDS["item"]
    assert focused[0]["tabindex"] == "-1"


def test_a_completed_import_puts_the_keyboard_on_its_outcome(client, project):
    page = client.get(f"/key-dates/{project.slug}?imported=1%20added")
    focused = _autofocused(page.text)

    assert len(focused) == 1
    assert focused[0]["id"] == FOCUS_IDS["outcome"]
    assert focused[0]["tabindex"] == "-1"
    # Announced without stealing the reading order from a screen reader.
    assert focused[0]["role"] == "status"
    assert focused[0]["aria-live"] == "polite"
    assert "Imported: 1 added." in page.text


def test_a_stale_refusal_puts_the_keyboard_on_the_refusal(
    client, session, project
):
    """The Save was refused, so the refusal is what is announced and focused."""
    _preview(client, project)
    fields = _confirm_fields(session, project)
    fields["content"] = KEY_DATE_CSV.replace("2026-11-01", "2026-12-15")

    refused = client.post(
        f"/key-dates/{project.slug}/confirm", data=fields, follow_redirects=False
    )
    focused = _autofocused(refused.text)

    assert refused.status_code == 409
    assert len(focused) == 1
    assert focused[0]["id"] == FOCUS_IDS["refusal"]
    assert focused[0]["tabindex"] == "-1"
    assert focused[0]["role"] == "alert"
    assert focused[0]["aria-live"] == "assertive"
    assert "Refused" in refused.text
    assert "Nothing was imported." in refused.text


def test_a_refresh_after_a_refusal_returns_the_keyboard_to_the_item(
    client, session, project
):
    _preview(client, project)
    fields = _confirm_fields(session, project)
    fields["content"] = KEY_DATE_CSV.replace("2026-11-01", "2026-12-15")
    client.post(
        f"/key-dates/{project.slug}/confirm", data=fields, follow_redirects=False
    )

    refreshed = client.get(f"/key-dates/{project.slug}")
    focused = _autofocused(refreshed.text)

    assert len(focused) == 1
    assert focused[0]["id"] == FOCUS_IDS["item"]
