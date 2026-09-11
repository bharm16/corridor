"""The shared issue-content seam, and the three levels derived from it (#641).

Two things are under test and they are deliberately in one file, because the
whole point of #641 is that they are one seam and its first consumer rather
than two independent readings of the same configuration.

``issue_content`` resolves what a project is configured to issue into the
renderer contracts, coverage evaluators and executable customer policies a
consumer needs; ``consequence_levels`` projects one proposed difference onto
that, producing ADR-0085's three accepted headings. #529 is queued to consume
the first without changing it, so every test below that fixes the seam's shape
also fixes what #529 will get.

Nothing here reads a clock. Every cutoff, effective instant and decision
instant is declared, exactly as #640's own seams require.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from corridor import access
from corridor.analytics import EventFamily, capture_events
from corridor.consequence_levels import (
    AFFECTS_ISSUE,
    CAN_WAIT,
    CONSEQUENCE_RULE_VERSION,
    LEVEL_HEADINGS,
    MUST_HANDLE,
    consequence_level,
    headline_level,
)
from corridor.issue_content import (
    CHANGE_SUMMARY_IDENTITY,
    CHANGE_SUMMARY_VERSION,
    CHASE_LIST_IDENTITY,
    CHASE_LIST_VERSION,
    NO_ISSUE_PROFILE,
    SELECT_ALL_ISSUE_AFFECTING,
    SELECT_DIFFERENCE,
    SELECT_FIELD,
    SELECT_REASON,
    UCM_RENDERER_IDENTITY,
    UCM_RENDERER_VERSION,
    UNREGISTERED_RENDERER,
    UNRESOLVED_FIELD_MAPPING,
    UNSUPPORTED_COVERAGE_EVALUATOR,
    UNSUPPORTED_DECISION_POLICY,
    UNSUPPORTED_DECISION_SELECTOR,
    WEEKLY_REPORT_IDENTITY,
    WEEKLY_REPORT_VERSION,
    ATTENTION_REASONS,
    CANONICAL_FIELDS,
    DIFFERENCE_KINDS,
    SELECTOR_KINDS,
    ChangeFacts,
    DecisionSelector,
    UnsupportedSelector,
    _PRESENTATION_LABELS,
    _fields_shown_as,
    _selector_guidance,
    decision_selector,
    effective_issue_content,
    parse_selector,
    registered_contract,
)
from corridor.issue_profile import (
    ArtifactEntry,
    CoverageRequirement,
    DecisionBlockingPolicy,
    RendererRevision,
    UPDATED_UCM,
    effective_issue_inventory,
    issue_profile_history,
)
from corridor.presentation import field_label
from corridor.models import Project
from corridor.operating_mode import adopt_project_baseline
from corridor.packet_review import emit_packet_surfacing, read_review_items
from corridor.principals import HumanPrincipal
from corridor.project_workflow import read_project_workflow
from corridor.web.app import (
    app,
    get_human_principal,
    get_review_clock,
    get_session,
)

from fastapi.testclient import TestClient

from access_support import seed_membership
from packet_review_support import (
    CHASE_RENDERER,
    REPORT_RENDERER,
    SUMMARY_RENDERER,
    UCM_RENDERER,
    Rendition,
    accept_baseline_fact,
    append_deltas,
    configure_issue,
    field_mapping,
    modify,
    register_baseline,
    register_field_mapping,
    register_output_template,
    register_source_row,
    subject,
    support,
)


COORDINATOR = HumanPrincipal("local:coordinator")

# Three declared instants: the profile takes effect in January, every reading
# is taken in September, and a changed profile takes effect in between.
JANUARY = datetime(2026, 1, 5, tzinfo=timezone.utc)
JUNE = datetime(2026, 6, 1, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
MAY_CUTOFF = datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)

# Two accepted dates and two proposed ones, all after the cutoff, so no delta
# is in the past-due band and the batch keys stay predictable.
PROMISED = "2026-11-01"
PROMISED_NOW = "2026-12-15"
REQUIRED_BY = "2027-02-01"
REQUIRED_BY_NOW = "2027-03-01"

PROMISED_FOR_FIELD = "committed_date"
REQUIRED_BY_FIELD = "need_date"

BLOCK_PROMISED = DecisionBlockingPolicy(
    policy="resolve_before_issue:v1",
    required_decision=f"field:{PROMISED_FOR_FIELD}",
    statement="Promised For changes must be decided before this issue.",
)

# The same policy waiting on the *second* of the two changes. Every test about
# a packet's headline uses this one, so the headline can never be right by
# accident: the first child is at a lower level than the packet, and a surface
# that read the first child instead of deriving the highest would print the
# wrong heading.
BLOCK_REQUIRED_BY = DecisionBlockingPolicy(
    policy="resolve_before_issue:v1",
    required_decision=f"field:{REQUIRED_BY_FIELD}",
    statement="Required By changes must be decided before this issue.",
)

# #640's own fixture wording, kept verbatim. It is a sentence a person reads,
# and a deterministic reader must never match it against a proposed change.
PROSE_POLICY = DecisionBlockingPolicy(
    policy="owner_sign_off",
    required_decision="Utility owner sign-off recorded",
    statement="This client will not accept an issue before sign-off.",
)

# The same rule a coordinator hand-authored against the label their screen
# prints. It selects nothing and is reported as a configuration problem; #670
# only decides what that problem is allowed to *say*.
LABEL_POLICY = DecisionBlockingPolicy(
    policy="resolve_before_issue:v1",
    required_decision=f"{SELECT_FIELD}:promised_for",
    statement="Promised For changes must be decided before this issue.",
)


@pytest.fixture
def project(session: Session) -> Project:
    row = Project(
        slug=f"consequence-{uuid4().hex[:8]}",
        name="Consequence Levels",
        is_synthetic=True,
    )
    session.add(row)
    session.flush()
    seed_membership(session, row, COORDINATOR, designations=[access.COORDINATION])
    return row


@pytest.fixture
def client(session):
    """The app shares the test's transaction and the test's declared instant."""

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
    app.dependency_overrides[get_review_clock] = lambda: (lambda: CUTOFF)
    with TestClient(app) as made:
        yield made
    app.dependency_overrides.clear()


class Issued:
    """One adopted project, its registered formats, and what it issues.

    The two proposed changes are both date changes of one source revision, so
    the partition puts them on **one** item: whatever their levels turn out to
    be, a packet is never split because its children differ (ADR-0085).
    """

    def __init__(self, session: Session, project: Project):
        self.session = session
        self.project = project
        self.adopted = Rendition(session, project, "ucm-2026-08.xlsx")
        self.incoming = Rendition(session, project, "ucm-2026-09.xlsx")
        self.revision_of: dict[tuple[str, str], int] = {}
        first: int | None = None
        for number, (field_name, value) in (
            (1, (PROMISED_FOR_FIELD, PROMISED)),
            (2, (REQUIRED_BY_FIELD, REQUIRED_BY)),
        ):
            fact, _ = self.adopted.capture(
                fact_type=field_name,
                value=value,
                subject_key=subject(number),
            )
            revision = accept_baseline_fact(session, project, fact)
            first = first or revision
            self.revision_of[(subject(number), field_name)] = revision
        assert first is not None
        self.baseline = register_baseline(
            session, project, self.adopted.document, first
        )
        for number in (1, 2):
            register_source_row(
                session,
                project,
                self.baseline,
                row_number=number,
                business_identity=f"UC-{number:03d}",
            )
        register_output_template(
            session, project, identity="district-ucm-template", version="v3"
        )

    def adopt(self) -> "Issued":
        """The one receipt an adopted-baseline project's whole week reads from."""

        adopt_project_baseline(
            self.session,
            project_id=self.project.id,
            adopted_by_principal="local:adopter",
            baseline_source_sha256=self.adopted.document.sha256,
            importer_identity="consequence_levels_fixture",
            importer_version="v1",
            idempotency_key=f"adopt:{uuid4().hex[:10]}",
        )
        self.session.expire_all()
        return self

    def maps(self, *fields: str) -> "Issued":
        register_field_mapping(self.session, self.project, field_mapping(*fields))
        return self

    def issues(self, **kwargs) -> "Issued":
        configure_issue(
            self.session,
            self.project,
            principal=COORDINATOR,
            effective_from=kwargs.pop("effective_from", JANUARY),
            **kwargs,
        )
        return self

    def proposes(self) -> "Issued":
        """One source revision proposing a new Promised For and Required By."""

        values = []
        for number, (field_name, was, now) in (
            (1, (PROMISED_FOR_FIELD, PROMISED, PROMISED_NOW)),
            (2, (REQUIRED_BY_FIELD, REQUIRED_BY, REQUIRED_BY_NOW)),
        ):
            fact, segment = self.incoming.capture(
                fact_type=field_name,
                value=now,
                subject_key=subject(number),
            )
            support(self.session, self.project, fact, segment)
            values.append(
                modify(
                    subject_key=subject(number),
                    field_name=field_name,
                    accepted_value=was,
                    proposed_value=now,
                    baseline_revision=self.revision_of[(subject(number), field_name)],
                )
            )
        append_deltas(
            self.session,
            self.project,
            self.incoming,
            source_revision="2026-09",
            values=values,
        )
        return self


def _levels(session: Session, project: Project, *, as_of=CUTOFF) -> dict[str, str | None]:
    """Each proposed change's own level, keyed by the field it changes."""

    reading = read_review_items(session, project_id=project.id, as_of=as_of)
    return {
        child.field: (
            child.consequence.name if child.consequence is not None else None
        )
        for item in reading.items
        for child in item.children
    }


def _content(session: Session, project: Project, *, as_of=CUTOFF):
    return effective_issue_content(
        session, effective_issue_inventory(session, project.id, as_of)
    )


# --- the registry is the only authority on what a renderer publishes -------


def test_every_registered_contract_names_the_module_that_produces_it():
    """The registry's identities are the producing modules' own constants.

    ``issue_content`` spells them rather than importing them, because importing
    ``follow_up_bundles`` would close an import cycle back through
    ``project_workflow`` and ``packet_review``. This is the assertion that
    keeps them one authority: a renamed or re-versioned renderer fails here
    rather than silently becoming an unregistered contract in production.
    """

    from corridor.follow_up_bundles import FOLLOW_UP_RULE, FOLLOW_UP_RULE_VERSION
    from corridor.issue_rendering import (
        CHANGE_SUMMARY_RULE,
        CHANGE_SUMMARY_RULE_VERSION,
        WEEKLY_REPORT_RULE,
        WEEKLY_REPORT_RULE_VERSION,
    )
    from corridor.workbook_render import RENDERER_VERSION

    assert UCM_RENDERER_VERSION == RENDERER_VERSION
    assert (CHANGE_SUMMARY_IDENTITY, CHANGE_SUMMARY_VERSION) == (
        CHANGE_SUMMARY_RULE,
        CHANGE_SUMMARY_RULE_VERSION,
    )
    assert (WEEKLY_REPORT_IDENTITY, WEEKLY_REPORT_VERSION) == (
        WEEKLY_REPORT_RULE,
        WEEKLY_REPORT_RULE_VERSION,
    )
    assert (CHASE_LIST_IDENTITY, CHASE_LIST_VERSION) == (
        FOLLOW_UP_RULE,
        FOLLOW_UP_RULE_VERSION,
    )


def test_an_unknown_renderer_version_is_never_resolved_to_a_known_one():
    """A version decides what the artifact carries, so it is not a decoration."""

    assert registered_contract(
        UPDATED_UCM, RendererRevision(UCM_RENDERER_IDENTITY, UCM_RENDERER_VERSION)
    ) is not None
    assert (
        registered_contract(
            UPDATED_UCM,
            RendererRevision(UCM_RENDERER_IDENTITY, "workbook_render_unknown"),
        )
        is None
    )
    assert (
        registered_contract(
            UPDATED_UCM, RendererRevision("some-other-renderer", UCM_RENDERER_VERSION)
        )
        is None
    )


def test_a_sidecar_has_no_registered_contract_and_is_not_assumed_empty():
    """ADR-0086 permits a sidecar; nothing in this release declares one."""

    assert (
        registered_contract(
            "provenance_sidecar", RendererRevision("provenance-sidecar", "v1")
        )
        is None
    )


# --- a statement is never parsed for behaviour -----------------------------


def test_prose_is_not_a_decision_selector_however_it_is_spelled():
    """#640's own fixture wording must select nothing, by any matching rule."""

    assert parse_selector(PROSE_POLICY.required_decision) is None
    assert parse_selector("") is None
    assert parse_selector("Promised For") is None
    assert parse_selector("field:") is None


def test_a_selector_names_a_canonical_field_and_never_a_printed_label():
    """``promised_for`` is presentation wording for ``committed_date``.

    Accepting the label as an alias would be prose parsing with a lookup table
    in front of it, so the label selects nothing and the stored field does.
    """

    assert parse_selector("field:promised_for") is None
    assert parse_selector(f"field:{PROMISED_FOR_FIELD}") is not None


def test_each_typed_selector_matches_only_what_it_names():
    change = ChangeFacts(
        field=PROMISED_FOR_FIELD,
        change_type="modify",
        attention_reasons=("promised_timing_change",),
    )
    matches = [
        (f"field:{PROMISED_FOR_FIELD}", True),
        (f"field:{REQUIRED_BY_FIELD}", False),
        ("difference:modify", True),
        ("difference:apparent_removal", False),
        ("reason:promised_timing_change", True),
        ("reason:apparent_removal", False),
    ]
    for token, expected in matches:
        selector = parse_selector(token)
        assert selector is not None, token
        assert selector.selects(change, issue_affecting=False) is expected, token
    every = parse_selector("all_issue_affecting")
    assert every is not None
    assert every.selects(change, issue_affecting=True) is True
    assert every.selects(change, issue_affecting=False) is False


def test_a_selector_naming_a_field_that_does_not_exist_is_refused():
    """A vocabulary check, so a typo cannot silently select nothing forever."""

    assert parse_selector("field:promissed_for") is None
    assert parse_selector("difference:removal") is None
    assert parse_selector("reason:very_urgent") is None


# --- the type itself refuses an unbacked pair (#682) -----------------------


def test_an_unsupported_selector_object_cannot_be_constructed():
    """The invariant belongs to the type, not to the functions beside it.

    ``DecisionSelector("field", "promised_for")`` used to build cleanly and
    would then have matched a delta whose field was literally ``promised_for``
    — #670's alias, reached by skipping the two doors that check. The
    constructor is now the runtime authority, so the object cannot exist.
    """

    with pytest.raises(UnsupportedSelector):
        DecisionSelector(SELECT_FIELD, "promised_for")
    for kind, value in (
        (SELECT_FIELD, None),
        (SELECT_DIFFERENCE, "unknown"),
        (SELECT_REASON, ""),
        (SELECT_ALL_ISSUE_AFFECTING, "extra"),
        ("unknown", None),
    ):
        with pytest.raises(UnsupportedSelector):
            DecisionSelector(kind, value)


def test_every_form_the_vocabulary_backs_still_constructs():
    """The guard refuses what is unbacked and nothing else.

    Each kind is swept over its whole vocabulary rather than one example of
    it, because a validator that accepted only the value a test happened to
    name would pass a single-example check and fail a customer.
    """

    assert (
        DecisionSelector(SELECT_FIELD, PROMISED_FOR_FIELD).token
        == f"field:{PROMISED_FOR_FIELD}"
    )
    assert DecisionSelector(SELECT_DIFFERENCE, "modify").token == "difference:modify"
    assert (
        DecisionSelector(SELECT_REASON, "promised_timing_change").token
        == "reason:promised_timing_change"
    )
    assert DecisionSelector(SELECT_ALL_ISSUE_AFFECTING).value is None
    assert (
        DecisionSelector(SELECT_ALL_ISSUE_AFFECTING).token
        == SELECT_ALL_ISSUE_AFFECTING
    )
    for kind, vocabulary in (
        (SELECT_FIELD, CANONICAL_FIELDS),
        (SELECT_DIFFERENCE, DIFFERENCE_KINDS),
        (SELECT_REASON, ATTENTION_REASONS),
    ):
        assert vocabulary
        for value in sorted(vocabulary):
            assert DecisionSelector(kind, value).token == f"{kind}:{value}"


def test_every_selector_kind_has_matching_semantics():
    """A validated kind may not select nothing (#682).

    Each kind is asked twice, about a difference it must select and one it
    must not, so a kind whose rule went missing — and fell to the fallback —
    cannot pass by answering the same way to both.
    """

    selected = ChangeFacts(
        field=PROMISED_FOR_FIELD,
        change_type="modify",
        attention_reasons=("promised_timing_change",),
    )
    passed_over = ChangeFacts(
        field=REQUIRED_BY_FIELD,
        change_type="apparent_removal",
        attention_reasons=("apparent_removal",),
    )
    by_kind = {
        SELECT_FIELD: DecisionSelector(SELECT_FIELD, PROMISED_FOR_FIELD),
        SELECT_DIFFERENCE: DecisionSelector(SELECT_DIFFERENCE, "modify"),
        SELECT_REASON: DecisionSelector(SELECT_REASON, "promised_timing_change"),
        SELECT_ALL_ISSUE_AFFECTING: DecisionSelector(SELECT_ALL_ISSUE_AFFECTING),
    }

    assert sorted(by_kind) == sorted(SELECTOR_KINDS)
    for kind, selector in by_kind.items():
        assert selector.selects(selected, issue_affecting=True) is True, kind
        assert selector.selects(passed_over, issue_affecting=False) is False, kind


def test_the_composition_helper_still_names_the_field_and_its_label():
    """#670's diagnostic survives the constructor becoming the authority.

    ``decision_selector`` composes a token and calls ``parse_selector``, which
    now constructs; the typed refusal still arrives as ``None`` there, so the
    helper still raises its own sentence naming the canonical field and the
    label a coordinator reads. Nothing is accepted or rewritten.
    """

    with pytest.raises(UnsupportedSelector) as refused:
        decision_selector(SELECT_FIELD, "promised_for")

    message = str(refused.value)
    assert repr("field:promised_for") in message
    assert f"use {SELECT_FIELD}:{PROMISED_FOR_FIELD}" in message
    assert f"shown as {field_label(PROMISED_FOR_FIELD)}" in message
    assert parse_selector(f"{SELECT_FIELD}:promised_for") is None


# --- naming the canonical field is display, never behaviour (#670) ---------


def test_a_typed_helper_composes_the_selector_a_caller_would_otherwise_spell():
    """Configuration gets the canonical token from the vocabulary, not a guess.

    The helper is the evaluator run over the composed parts, so what a
    configuration screen may store and what a policy may match are one
    decision. A screen shows ``field_label`` beside the token it stores; the
    label never becomes the token.
    """

    assert (
        decision_selector(SELECT_FIELD, PROMISED_FOR_FIELD).token
        == f"field:{PROMISED_FOR_FIELD}"
    )
    assert decision_selector(SELECT_DIFFERENCE, "modify").token == "difference:modify"
    assert (
        decision_selector(SELECT_REASON, "promised_timing_change").token
        == "reason:promised_timing_change"
    )
    assert (
        decision_selector(SELECT_ALL_ISSUE_AFFECTING).token == SELECT_ALL_ISSUE_AFFECTING
    )
    for composed in (
        decision_selector(SELECT_FIELD, PROMISED_FOR_FIELD),
        decision_selector(SELECT_ALL_ISSUE_AFFECTING),
    ):
        assert parse_selector(composed.token) == composed
    assert field_label(PROMISED_FOR_FIELD) == "Promised for"


def test_the_typed_helper_refuses_the_label_the_evaluator_refuses():
    """The one composition path may not be the alias door (#670).

    A helper that accepted the label would put the alias table back with an
    extra step in front of it: the token would be stored, digested, and matched
    forever after, and no diagnostic would ever be printed.
    """

    for kind, value in (
        (SELECT_FIELD, "promised_for"),
        (SELECT_FIELD, "Promised for"),
        (SELECT_FIELD, "promissed_for"),
        (SELECT_DIFFERENCE, "removal"),
        (SELECT_REASON, "very_urgent"),
        ("selector", "anything"),
    ):
        with pytest.raises(UnsupportedSelector):
            decision_selector(kind, value)
    with pytest.raises(UnsupportedSelector):
        decision_selector("Utility owner sign-off recorded")


def test_the_guidance_names_one_canonical_field_only_for_an_exact_label():
    """Exact equality on a normalized spelling, and nothing that resembles.

    Underscores, case and whitespace are the three ways one label is written
    down. There is deliberately no edit distance behind this: ``promissed_for``
    is a typo for a canonical field and gets the vocabulary, not a repair.
    """

    for written in ("promised_for", "Promised For", "promised for"):
        exact = _selector_guidance(f"{SELECT_FIELD}:{written}")
        # The offending value is echoed as written, and the field named once.
        assert repr(written) in exact
        assert f"use {SELECT_FIELD}:{PROMISED_FOR_FIELD}" in exact
        assert f"shown as {field_label(PROMISED_FOR_FIELD)}" in exact
    for silent in (
        f"{SELECT_FIELD}:promissed_for",
        f"{SELECT_FIELD}:{PROMISED_FOR_FIELD}",
        f"{SELECT_FIELD}:",
        "Utility owner sign-off recorded",
        "difference:removal",
        "",
    ):
        assert _selector_guidance(silent) == ""


def test_a_label_shown_for_two_canonical_fields_names_neither():
    """Ambiguity lists the supported selectors; it never picks a candidate.

    Nothing in this release prints one label for two comparable fields, so the
    rule is proved over a supplied label table rather than by waiting for a
    presentation change to introduce the collision silently.
    """

    assert _fields_shown_as("promised for", {"committed_date": "Promised for"}) == (
        "committed_date",
    )
    ambiguous = {"committed_date": "Promised for", "need_date": "promised_for"}
    assert _fields_shown_as("promised for", ambiguous) == (
        "committed_date",
        "need_date",
    )
    # The live table is unambiguous, which is what lets the sentence name one.
    collisions = [
        label
        for label in {field_label(field) for field in sorted(CANONICAL_FIELDS)}
        if len(_fields_shown_as(label, _PRESENTATION_LABELS)) > 1
    ]
    assert collisions == []


def test_a_label_selector_is_a_configuration_problem_that_names_the_field(
    session: Session, project: Project
):
    """#670: the coordinator is told what to configure, and nothing is executed.

    The label appears in a sentence. It does not become a policy, it does not
    select the field it looks like, and the profile keeps waiting on exactly
    the string it was registered with.
    """

    Issued(session, project).maps(PROMISED_FOR_FIELD).issues(policies=(LABEL_POLICY,))

    content = _content(session, project)

    assert [problem.code for problem in content.problems] == [
        UNSUPPORTED_DECISION_SELECTOR
    ]
    sentence = content.problems[0].sentence
    assert f"{SELECT_FIELD}:promised_for" in sentence
    assert f"use {SELECT_FIELD}:{PROMISED_FOR_FIELD}" in sentence
    assert f"shown as {field_label(PROMISED_FOR_FIELD)}" in sentence
    assert not content.supported


def test_the_named_field_never_reaches_the_evaluator_or_the_stored_digest(
    session: Session, project: Project
):
    """The suggestion is text, and text is all that leaves this seam (#670).

    Naming ``committed_date`` in a sentence must not normalize the declaration,
    re-digest it, or give ``parse_selector`` a second answer. If any of those
    happened the alias table would be back, one step further from the reader
    who would have to find it.
    """

    Issued(session, project).maps(PROMISED_FOR_FIELD).issues(policies=(LABEL_POLICY,))

    content = _content(session, project)

    inventory = effective_issue_inventory(session, project.id, CUTOFF)
    assert inventory is not None
    stored = inventory.decision_blocking_policies[0].required_decision
    # Nothing executes: naming the field does not resurrect the policy.
    assert content.blocking_policies == ()
    assert (
        content.blocking_policies_for(
            ChangeFacts(field=PROMISED_FOR_FIELD, change_type="modify")
        )
        == ()
    )
    # Nothing is rewritten: the declaration and its digest are as registered.
    assert stored == LABEL_POLICY.required_decision
    assert content.content_sha256 == inventory.content_sha256
    declaration = issue_profile_history(session, project.id)[-1].declaration
    assert f"{SELECT_FIELD}:promised_for" in declaration
    assert f"{SELECT_FIELD}:{PROMISED_FOR_FIELD}" not in declaration
    # The two public doors into the vocabulary, asked the same question.
    assert parse_selector(stored) is None
    with pytest.raises(UnsupportedSelector):
        decision_selector(SELECT_FIELD, "promised_for")


# --- resolving one project's configuration ---------------------------------


def test_a_supported_profile_resolves_every_artifact_to_its_content_contract(
    session: Session, project: Project
):
    Issued(session, project).maps(PROMISED_FOR_FIELD).issues(
        artifacts=(
            ArtifactEntry("accepted_change_summary", SUMMARY_RENDERER),
            ArtifactEntry("weekly_coordination_report", REPORT_RENDERER),
            ArtifactEntry("chase_list", CHASE_RENDERER),
        )
    )

    content = _content(session, project)

    assert content.supported
    assert content.artifact_types == (
        UPDATED_UCM,
        "accepted_change_summary",
        "chase_list",
        "weekly_coordination_report",
    )
    # The updated UCM's contract defers to the registered mapping revision, so
    # its fields are this project's, not the renderer's.
    assert content.artifact(UPDATED_UCM).accepted_record_fields == frozenset(
        {PROMISED_FOR_FIELD}
    )
    assert content.artifact("chase_list").accepted_record_fields == frozenset()


def test_no_effective_profile_is_a_configuration_problem_not_an_empty_issue(
    session: Session, project: Project
):
    """ADR-0091 makes the UCM mandatory, so "issues nothing" is not a state."""

    Issued(session, project).maps(PROMISED_FOR_FIELD)

    content = _content(session, project)

    assert not content.supported
    assert [problem.code for problem in content.problems] == [NO_ISSUE_PROFILE]
    assert content.artifacts == ()


def test_an_unsupported_renderer_version_is_stated_not_resolved(
    session: Session, project: Project
):
    Issued(session, project).maps(PROMISED_FOR_FIELD).issues(
        ucm=RendererRevision(UCM_RENDERER_IDENTITY, "workbook_render_v9")
    )

    content = _content(session, project)

    assert not content.supported
    assert [problem.code for problem in content.problems] == [UNREGISTERED_RENDERER]
    assert "workbook_render_v9" in content.problems[0].sentence
    assert content.artifact(UPDATED_UCM) is None


def test_an_unknown_evaluator_and_a_prose_policy_are_configuration_problems(
    session: Session, project: Project
):
    """#640's complete-profile fixture, read by a machine for the first time."""

    Issued(session, project).maps(PROMISED_FOR_FIELD).issues(
        coverage=(
            CoverageRequirement(
                requirement="all_delivered_sources_read",
                statement="Every source delivered before the cutoff has been read.",
            ),
        ),
        policies=(PROSE_POLICY,),
    )

    content = _content(session, project)

    assert not content.supported
    assert {problem.code for problem in content.problems} == {
        UNSUPPORTED_COVERAGE_EVALUATOR,
        UNSUPPORTED_DECISION_POLICY,
    }
    assert content.blocking_policies == ()
    assert content.coverage_requirements == ()


def test_a_supported_policy_with_a_prose_selector_is_a_selector_problem(
    session: Session, project: Project
):
    """The evaluator is one we run; what it waits on is still a sentence."""

    Issued(session, project).maps(PROMISED_FOR_FIELD).issues(
        policies=(
            DecisionBlockingPolicy(
                policy="resolve_before_issue:v1",
                required_decision="Utility owner sign-off recorded",
                statement="This client will not accept an issue before sign-off.",
            ),
        )
    )

    content = _content(session, project)

    assert [problem.code for problem in content.problems] == [
        UNSUPPORTED_DECISION_SELECTOR
    ]
    assert content.blocking_policies == ()


def test_a_mapping_whose_declaration_was_never_stored_is_not_read_as_empty(
    session: Session, project: Project
):
    """The workbook's fields are the registered mapping's, or they are unknown.

    A registration written before #610 stored declarations names the approved
    revision without saying what it targets. Reading that as "targets nothing"
    would classify every proposed change "Can wait" for the one artifact
    ADR-0091 makes mandatory.
    """

    issued = Issued(session, project)
    register_field_mapping(
        session, project, field_mapping(PROMISED_FOR_FIELD), store_declaration=False
    )
    issued.issues()

    content = _content(session, project)

    assert [problem.code for problem in content.problems] == [
        UNRESOLVED_FIELD_MAPPING
    ]
    assert content.artifacts == ()


def test_the_coverage_evaluator_answers_whether_the_requirement_is_met(
    session: Session, project: Project
):
    """The seam answers ADR-0086's coverage gate for #529 without a screen."""

    Issued(session, project).maps(PROMISED_FOR_FIELD).issues(
        coverage=(
            CoverageRequirement(
                requirement="all_required_sources_read:v1",
                statement="Every source delivered before the cutoff has been read.",
            ),
        )
    )

    content = _content(session, project)

    assert content.supported
    assert content.unmet_coverage(unread_source_count=0) == ()
    assert len(content.unmet_coverage(unread_source_count=1)) == 1


# --- the three levels ------------------------------------------------------


def test_a_change_a_customer_policy_waits_on_must_be_handled_first(
    session: Session, project: Project
):
    Issued(session, project).maps(PROMISED_FOR_FIELD, REQUIRED_BY_FIELD).issues(
        policies=(BLOCK_PROMISED,)
    ).proposes()

    levels = _levels(session, project)

    assert levels[PROMISED_FOR_FIELD] == MUST_HANDLE
    assert levels[REQUIRED_BY_FIELD] == AFFECTS_ISSUE


def test_a_change_the_configured_workbook_states_affects_this_issue(
    session: Session, project: Project
):
    """No policy selects it, and accepting it changes what the customer gets."""

    Issued(session, project).maps(PROMISED_FOR_FIELD, REQUIRED_BY_FIELD).issues(
    ).proposes()

    levels = _levels(session, project)

    assert levels[PROMISED_FOR_FIELD] == AFFECTS_ISSUE
    assert levels[REQUIRED_BY_FIELD] == AFFECTS_ISSUE


def test_a_change_no_configured_artifact_states_can_wait(
    session: Session, project: Project
):
    """The customer's form does not carry Required By, so nothing they receive
    changes when it moves. It keeps every Attention Reason it had."""

    Issued(session, project).maps(PROMISED_FOR_FIELD).issues().proposes()

    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    by_field = {
        child.field: child for item in reading.items for child in item.children
    }

    assert by_field[REQUIRED_BY_FIELD].consequence.name == CAN_WAIT
    assert by_field[PROMISED_FOR_FIELD].consequence.name == AFFECTS_ISSUE
    assert by_field[REQUIRED_BY_FIELD].attention_reasons == (
        "promised_timing_change",
    )


def test_an_unresolved_change_alone_never_reaches_the_chase_list(
    session: Session, project: Project
):
    """#425's artifact is derived from accepted authority, never from a delta."""

    Issued(session, project).maps().issues(
        artifacts=(ArtifactEntry("chase_list", CHASE_RENDERER),)
    ).proposes()

    levels = _levels(session, project)

    assert set(levels.values()) == {CAN_WAIT}


def test_the_weekly_report_discloses_an_open_question_so_it_affects_the_issue(
    session: Session, project: Project
):
    """Its pending-coordination region states what is still open at the cutoff."""

    Issued(session, project).maps().issues(
        artifacts=(ArtifactEntry("weekly_coordination_report", REPORT_RENDERER),)
    ).proposes()

    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    child = next(
        child
        for item in reading.items
        for child in item.children
        if child.field == REQUIRED_BY_FIELD
    )

    assert child.consequence.name == AFFECTS_ISSUE
    assert any("discloses" in reason for reason in child.consequence.reasons)


def test_a_packet_headlines_its_highest_child_level_and_is_not_split(
    session: Session, project: Project
):
    """ADR-0085: a packet may headline, and must not split because levels differ.

    The blocking policy deliberately waits on the *second* change, so the
    headline is not the first child's level and cannot be produced by reading
    one child instead of deriving the highest.
    """

    Issued(session, project).maps(PROMISED_FOR_FIELD).issues(
        policies=(BLOCK_REQUIRED_BY,)
    ).proposes()

    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)

    assert len(reading.items) == 1, [item.item_key for item in reading.items]
    item = reading.items[0]
    assert [child.consequence.name for child in item.children] == [
        AFFECTS_ISSUE,
        MUST_HANDLE,
    ]
    assert item.consequence == MUST_HANDLE
    assert item.consequence_heading == "Must handle before this issue"
    assert item.consequence_rule_version == CONSEQUENCE_RULE_VERSION


def test_a_level_changes_when_the_project_changes_what_it_issues(
    session: Session, project: Project
):
    """One cutoff reads January's configuration; a later one reads June's.

    The change is an attributable act with its own effective instant, so the
    earlier cutoff keeps the level it was configured to have.
    """

    issued = Issued(session, project).maps(PROMISED_FOR_FIELD).issues()
    issued.proposes()
    before = _levels(session, project, as_of=MAY_CUTOFF)

    issued.issues(policies=(BLOCK_PROMISED,), effective_from=JUNE)

    assert before[PROMISED_FOR_FIELD] == AFFECTS_ISSUE
    assert _levels(session, project, as_of=MAY_CUTOFF)[PROMISED_FOR_FIELD] == (
        AFFECTS_ISSUE
    )
    assert _levels(session, project)[PROMISED_FOR_FIELD] == MUST_HANDLE


def test_an_unsupported_configuration_derives_no_level_and_blocks_nothing(
    session: Session, project: Project
):
    """Never a silent fallback, and never a blanket "Must handle"."""

    Issued(session, project).maps(PROMISED_FOR_FIELD).issues(
        ucm=RendererRevision(UCM_RENDERER_IDENTITY, "workbook_render_v9")
    ).proposes()

    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)

    assert reading.items
    for item in reading.items:
        assert item.consequence is None
        assert item.consequence_heading is None
        for child in item.children:
            assert child.consequence is None
            assert child.consequence_heading is None


def test_a_settled_decision_a_policy_waits_on_no_longer_blocks_the_issue(
    session: Session, project: Project
):
    """The policy's second limb: it waits only while the decision is open.

    This is asked of the seam directly because every Review child is actionable
    by construction, so the unsettled branch is the only one that surface can
    reach. #529 asks the same question about differences that have been
    decided, which is why the parameter exists at all — and ADR-0084 makes a
    dated Defer scheduling rather than a decision, so a deferred difference is
    still unsettled here.
    """

    Issued(session, project).maps(PROMISED_FOR_FIELD).issues(
        policies=(BLOCK_PROMISED,)
    )
    content = _content(session, project)
    change = ChangeFacts(field=PROMISED_FOR_FIELD, change_type="modify")

    assert consequence_level(content, change).name == MUST_HANDLE
    assert consequence_level(
        content, change, decision_settled=True
    ).name == AFFECTS_ISSUE


def test_the_headline_of_a_packet_with_no_derivable_level_is_absent():
    assert headline_level([]) is None
    assert headline_level([None, None]) is None


# --- where the levels are shown --------------------------------------------


def test_the_review_screen_and_the_project_week_agree_about_one_packet(
    session: Session, project: Project, client
):
    """Neither surface derives a level; both print the one derivation (#641).

    The packet's first child is at "Affects this issue" and the packet is at
    "Must handle before this issue", so a surface that derived a headline of
    its own from one child would print a different heading from the other. The
    week prints the packet's heading and nothing else, which is what makes the
    disagreement detectable at all.
    """

    Issued(session, project).maps(PROMISED_FOR_FIELD).issues(
        policies=(BLOCK_REQUIRED_BY,)
    ).proposes().adopt()
    item = read_review_items(session, project_id=project.id, as_of=CUTOFF).items[0]
    heading = LEVEL_HEADINGS[MUST_HANDLE]

    review = client.get(f"/review/{project.slug}").text
    week = client.get(f"/work/{project.slug}").text

    assert item.consequence_heading == heading
    assert item.children[0].consequence_heading == LEVEL_HEADINGS[AFFECTS_ISSUE]
    assert heading in review
    assert heading in week
    # The week states the packet's own level. Printing a child's instead would
    # be a second derivation, and this is what would catch it.
    assert LEVEL_HEADINGS[AFFECTS_ISSUE] not in week


def test_a_configuration_problem_is_issue_readiness_and_never_a_review_child(
    session: Session, project: Project
):
    """An operations failure is never put in front of a person as a decision."""

    Issued(session, project).maps(PROMISED_FOR_FIELD).issues(
        ucm=RendererRevision(UCM_RENDERER_IDENTITY, "workbook_render_v9")
    ).proposes()

    workflow = read_project_workflow(session, project_id=project.id, as_of=CUTOFF)

    assert UNREGISTERED_RENDERER in {
        problem.code for problem in workflow.readiness
    }
    offered = {
        child.delta_id for item in workflow.review.items for child in item.children
    }
    assert offered == set(workflow.review.reading.actionable_delta_ids)


def test_the_packet_surfacing_event_records_the_profile_the_level_came_from(
    session: Session, project: Project
):
    """One family, more fields — never a second family for the same fact."""

    Issued(session, project).maps(PROMISED_FOR_FIELD).issues(
        policies=(BLOCK_PROMISED,)
    ).proposes()
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = reading.items[0]
    inventory = effective_issue_inventory(session, project.id, CUTOFF)

    with capture_events() as events:
        emit_packet_surfacing(reading, item)

    assert [event.family for event in events.events] == [
        EventFamily.PACKET_SURFACING
    ]
    payload = events.events[0].payload
    assert payload["issue_profile_id"] == inventory.profile_id
    assert payload["issue_profile_identity"] == inventory.profile_identity
    assert payload["issue_profile_version"] == inventory.profile_version
    assert payload["issue_profile_sha256"] == inventory.content_sha256
    assert payload["consequence_rule_version"] == CONSEQUENCE_RULE_VERSION
    assert payload["consequence_level"] == MUST_HANDLE
    assert payload["cutoff"] == CUTOFF.isoformat()
    assert events.events[0].metric_labels["consequence_level"] == MUST_HANDLE
    levels = {
        row["delta_id"]: (row["level"], tuple(row["reasons"]))
        for row in payload["child_consequences"]
    }
    assert {level for level, _ in levels.values()} == {MUST_HANDLE, CAN_WAIT}
    for _, reasons in levels.values():
        assert reasons
