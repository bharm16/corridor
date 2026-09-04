"""The change summary and the weekly Coordination Report, from one frozen revision.

ADR-0086 makes one authorized package the external issue unit and gives every
artifact in it the same five inputs: one accepted Project Record revision, zero
or one previous approved issue, one source cutoff, one declared coverage state,
and one approved template and mapping set.  #495 renders the customer's native
workbook from those inputs and #425 the chase list; this module renders the
other two, the **Change Summary** and the weekly **Coordination Report**.

**Why the two are in one module.**  They are the pair most able to contradict
each other.  A change summary that says a Promised For moved to 15 December and
a weekly report that still prints 1 November are, to the person reading them,
Corridor being wrong about their project — and that happens for no reason more
exotic than reading "the current record" twice while a coordinator resolves a
delta between the two reads.  So there is exactly one way in.
``bind_issue_reading`` reads the accepted projection **once**, holds it on a
frozen ``BoundIssueReading``, and both readings carry that same object; a test
asserts they are the same instance, and the rendered artifacts both print the
revision they came from.  Nothing in this module reads "current" state a second
time.

**Why the frozen revision comes from #488's reading and is not recomputed.**
``report_preparation`` already resolves the week as a half-open range of
append-only identifiers — ``(previous, current]`` on ``proposed_deltas.id`` and
``delta_dispositions.id`` — and states its counts against one accepted
revision.  It exists in that shape because its first version compared a
PostgreSQL-assigned ``created_at`` against a caller-supplied logical time and
silently miscounted.  Recomputing any of that here would reintroduce exactly
the divergence #488 removed, so the preparation reading is a required input:
its ``accepted_revision_id`` **is** the frozen revision, and binding verifies
that pinned identifier exists for this project rather than asking the database
again which revision is newest.

**Why the comparison window is a revision watermark and not a date.**  ADR-0086
fixes the baseline at the last approved package, so the change summary covers
``(previous approved issue's revision, frozen revision]`` on
``project_record_revisions.id``.  Prepared candidates, internal snapshots, and
weekly readings never move it, a missed week simply widens it, and no clock
participates.  Before a project's first approved issue there is no predecessor
at all: the configured first-issue behavior either states that plainly or
summarizes current accepted state, and neither invents a prior occurrence.

**What is excluded, and why it still has to be visible.**  Only ``accept`` and
``edit`` change the accepted record (ADR-0084).  A rejected delta records that
the accepted value stands, a deferral is Work List scheduling, a superseded
delta was replaced by a newer source revision, and a stale one was raised
against an accepted value that has since moved.  None of those are changes, so
none reach an accepted-change section — but a coordinator still has to be able
to see them, so the structured reading keeps every one of them with its state
in ``unaccepted_deltas``.  Whether a deferral is still running is judged
against the declared source cutoff, never the wall clock, so the same bound
reading answers the same way whenever it is rendered.

**Provenance follows the value class** (ADR-0082).  An accepted field value is
source-backed and needs its typed locator, at least one named Support
Assessment, and its decision lineage; a count is a Derivation and needs its
rule identity, version, input record identities, and the moment it was
evaluated as of; a Follow-up Plan line is a Coordination Decision and needs its
responsible person, subject, and decision time.  Rendering refuses a value
whose class-complete provenance is missing, the way ``report`` refuses a bare
cell.  Support is always read from the assessment relation: this module never
reads locator validation, because a passed Source Passage Check says a
quotation is where it was cited and never that the source supports the value.

**The accepted record's checks are its own** (ADR-0090).  The weekly report
runs a declared check set, ``accepted_record_checks_v2``, and says so.  Five
of the twelve released Constraint Alert rules are in it: the three date
checks derivable from accepted values, plus ``MISSING_EVIDENCE`` re-based on
the Support Assessment relation rather than the Source Passage Check it used
to mean, and ``SUPERSEDED_CITATION`` with ADR-0016's predicate carried over —
the accepted value still depends on a superseded revision and nothing current
has replaced it, never "some document has a successor".  Three of the
remaining seven are raised where a coordinator can act on them (a
contradiction is a Proposed Delta and a focused question; due and overdue
follow-up is the Follow-up section), and four are not checked at all because
they only ever said an administrative field was empty.  An adopted-baseline
project's alerts therefore do **not** match a legacy project's, and the
report declares the difference instead of leaving a customer to notice that
findings stopped appearing.  A check set change is a change in the rules and
never a change in the project, which is the distinction ``changes`` draws for
the legacy path; the change summary states which set each issue was built
under and no set change ever reaches an accepted-change section.

**Adopted projects only.**  ADR-0084 §3 names the change summary and weekly
report as spine-native surfaces for adopted-baseline projects, reading the
current and as-of projections.  A legacy project keeps ``corridor.report``,
which already renders its weekly view from the legacy readers, so binding
refuses a legacy project instead of extending a legacy reader or requiring a
whole-model dual write.

This module writes nothing and needs no table of its own.  Everything it says
is derived from records that already exist, and #529 consumes the structured
readings rather than reparsing the rendered bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.current_record import (
    CurrentRecordValue,
    read_project_record_as_of_revision,
)
from corridor.models import (
    DeltaDecisionSupport,
    DeltaDeferral,
    DeltaDisposition,
    DeltaRecordDecision,
    DeltaSupersession,
    Document,
    FactDecision,
    FactSource,
    ProjectRecordRevision,
    ProposedDelta,
    SourceSegment,
    SupportAssessment,
    SupportAssessmentSource,
)
from corridor.operating_mode import ADOPTED_BASELINE, project_operating_mode
from corridor.presentation import accepted_record_exception_name, field_label
from corridor.review_packet_reading import is_stale
from corridor.support_assessments import FactProposition, current_support_assessments


# The two behaviors a project may configure for the issue that has no
# predecessor.  ADR-0086 permits either and forbids inventing a third.
CURRENT_STATE_SUMMARY = "current_state_summary"
NO_PRIOR_COMPARISON_STATEMENT = "no_prior_comparison_statement"
FIRST_ISSUE_BEHAVIORS = (CURRENT_STATE_SUMMARY, NO_PRIOR_COMPARISON_STATEMENT)
# What the released contract does for a project's first issue. A worker may
# never choose between the two (#690): if `current_state_summary` is ever
# wanted for another renderer or partner it becomes an explicit versioned
# project or renderer configuration, and the supervisor reads that. Until then
# there is one released answer and this is it.
RELEASED_FIRST_ISSUE_BEHAVIOR = NO_PRIOR_COMPARISON_STATEMENT

# The report regions a partner template may declare.  The set is closed
# because a template that named an unknown region would silently render
# nothing there, and a section a customer expected to see would be missing
# with no failure anywhere.
SECTION_CONSTRAINT_ALERTS = "constraint_alerts"
SECTION_COMMITMENTS = "commitments"
SECTION_KEY_DATES = "key_dates"
SECTION_FOLLOW_UP_PLANS = "follow_up_plans"
SECTION_PENDING_COORDINATION = "pending_coordination"
SUPPORTED_SECTIONS = (
    SECTION_CONSTRAINT_ALERTS,
    SECTION_COMMITMENTS,
    SECTION_KEY_DATES,
    SECTION_FOLLOW_UP_PLANS,
    SECTION_PENDING_COORDINATION,
)

# The released renderer contract (#690). The sections and their headings are a
# property of the deployed renderer this version of the product ships, exactly
# as `SUPPORTED_SECTIONS` above is; what a *project* configures is which
# artifacts participate (ADR-0091), never what the weekly report's regions are
# called. Naming them here is what lets a background supervisor assemble a
# `TemplateBinding` from registered contracts rather than from a test
# constant: the identities and versions come from the project's registered
# output-template and field-mapping registrations, and the regions come from
# here. A partner template that declares a different set is a second contract
# version, not a value a worker chooses per run.
RENDERER_CONTRACT_VERSION = "issue-renderer-contract-v1"

# The declared coverage vocabulary.  "Read" and "late" are not the same
# statement and a customer reading a bounded coverage sentence has to be able
# to tell a required source that failed from an optional one that arrived
# after the cutoff.
REQUIRED = "required"
OPTIONAL = "optional"
COVERAGE_REQUIREMENTS = (REQUIRED, OPTIONAL)
COVERAGE_READ = "read"
COVERAGE_FAILED = "failed"
COVERAGE_EXCLUDED = "excluded"
COVERAGE_LATE = "late"
COVERAGE_STATES = (COVERAGE_READ, COVERAGE_FAILED, COVERAGE_EXCLUDED, COVERAGE_LATE)

# The value classes ADR-0082 fixed, and what each one needs before a value may
# be published.
SOURCE_BACKED = "source_backed_fact"
RECORDED_VERBAL = "recorded_verbal_statement"
COORDINATION_DECISION = "coordination_decision"
DERIVATION = "derivation"

# The lifecycle states an unaccepted Proposed Delta can be in.  ADR-0084 keeps
# them apart: a deferral is not a decision and a rejection is not a change.
DELTA_OPEN = "open"
DELTA_DEFERRED = "deferred"
DELTA_REJECTED = "rejected"
DELTA_SUPERSEDED = "superseded"
DELTA_STALE = "stale"

# The dispositions that actually moved the accepted record.
ACCEPTED_DISPOSITIONS = ("accept", "edit")

# The identity and version of the derivations this module publishes.  A count
# that changed because the rule changed is not project movement, which is the
# distinction ``changes`` was built to preserve, so each derived figure names
# the rule it came from and that rule's version.
CHANGE_SUMMARY_RULE = "change_summary_from_accepted_revisions"
CHANGE_SUMMARY_RULE_VERSION = "v1"
WEEKLY_REPORT_RULE = "weekly_coordination_report_from_accepted_revision"
WEEKLY_REPORT_RULE_VERSION = "v1"
ACCEPTED_RECORD_CHECK_RULE = "accepted_record_checks"
ACCEPTED_RECORD_CHECK_VERSION = "v2"
# The declared name of the check set, as one string, because that is the unit
# a previous approved issue records and this one compares against.  ADR-0090
# advances it from ``accepted_record_date_checks_v1``: the set is no longer
# only date checks once the two support rules join it, so the name loses
# "date" as well as taking a new version.
ACCEPTED_RECORD_CHECK_SET = (
    f"{ACCEPTED_RECORD_CHECK_RULE}_{ACCEPTED_RECORD_CHECK_VERSION}"
)
FIRST_ACCEPTED_RECORD_CHECK_SET = "accepted_record_date_checks_v1"
# Every released set, oldest first.  A previous approved issue that names one
# of these is comparable; one that names nothing is an unknown boundary, and
# ADR-0044's discipline applies — an unrecorded set is never backfilled from
# the current one.
RELEASED_ACCEPTED_RECORD_CHECK_SETS = (
    FIRST_ACCEPTED_RECORD_CHECK_SET,
    ACCEPTED_RECORD_CHECK_SET,
)

# The five released rules this set runs on the accepted Project Record, and
# the seven it does not.  ADR-0090 dispositioned all twelve: keep three, port
# two, supersede three, retire four.  Both halves are stated as data because
# the report declares its own coverage — an adopted-baseline project's alerts
# differ from a legacy project's, and #596 opened on that difference being
# silent.
ACCEPTED_RECORD_CHECK_RULES = (
    "OVERDUE",
    "DUE_SOON",
    "MISSING_DATE",
    "MISSING_EVIDENCE",
    "SUPERSEDED_CITATION",
)

# Three findings that still reach the coordinator, once, in the surface that
# can act on them.  A second alert saying the same thing would give one
# question two action surfaces, and no rule for which one wins.
CHECKS_RAISED_ELSEWHERE = (
    (
        "CONTRADICTION",
        "a source that disagrees with the accepted record is incoming "
        "evidence rather than a second accepted fact, so it is raised as a "
        "proposed change with a question about that one value",
    ),
    (
        "ACTION_DUE_SOON",
        "a next action that is coming due is stated in the follow-up "
        "section, from the accepted follow-up plan that carries it",
    ),
    (
        "ACTION_OVERDUE",
        "a next action that is past its date is stated in the follow-up "
        "section, from the accepted follow-up plan that carries it",
    ),
)

# Four rules the accepted record does not check at all.  Each of them says
# only that an administrative field is empty, and firing on that asserts that
# every constraint on the project ought to have an internal owner, a task, a
# recent document and a key date link.  Most legitimately have none of those,
# and a project with three thousand records would produce three thousand
# identical findings that mean "nobody typed anything here" (ADR-0010,
# ADR-0090).
CHECKS_NOT_RUN = (
    (
        "MISSING_OWNER",
        "many constraints correctly have no internal owner, so an empty "
        "owner is not a problem to report",
    ),
    (
        "MISSING_ACTION",
        "a constraint whose next move belongs to the utility owner needs no "
        "task of ours, so an empty next action is not a problem to report",
    ),
    (
        "STALE",
        "nobody sending a document is not evidence that anybody failed to "
        "answer; a missing answer is recorded against a request that was "
        "actually sent, and it is reported there",
    ),
    (
        "ORPHAN",
        "whether a constraint needs a key date linked to it is each "
        "project's own decision, not a defect Corridor can assert",
    ),
)

# The Support Assessment outcomes that make a Source Segment Supporting
# Documentation.  "Contradicted", "unclear" and "not assessed" are readings
# too, and none of them is support (ADR-0082).
SUPPORTING_OUTCOMES = ("supported", "partially_supported")

# The horizon the near-date check uses, in days.  It is the same thirty days
# the released ruleset gives the Required By lane; it is stated here because
# this check runs over accepted spine values rather than over the legacy
# Dependency rows, and a shared constant would hide that difference.
DUE_SOON_DAYS = 30

_SCHEMA_VERSION = "report-preparation-result-v1"
_READING_IDENTITY_VERSION = "issue-reading-v1"


class MixedIssueInputs(ValueError):
    """The inputs offered for one issue do not describe one coherent reading."""


class IncompleteProvenance(Exception):
    """A value was about to be published without the provenance its class needs."""


# --- The five bound inputs ------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreviousApprovedIssue:
    """The one predecessor a comparison window may open from (ADR-0086).

    A prepared candidate, an internal snapshot, and the newest render of any
    kind are all excluded by construction: nothing but an approved issue can
    be put here.

    ``check_set`` is the accepted-record check set that issue was rendered
    under.  It is here rather than derived because the comparison it enables
    is exactly the one ``changes`` makes for the legacy path: a finding that
    moved because the rules changed is not the project moving, and the only
    way to tell is to know which rules the predecessor ran.  ``None`` is the
    honest answer for an issue approved before any set was recorded, and it
    is read as unknown rather than backfilled from the current set
    (ADR-0044).
    """

    issue_identity: str
    accepted_revision_id: int
    approved_at: datetime
    check_set: str | None = None


@dataclass(frozen=True, slots=True)
class SourceCoverage:
    """One line of the declared coverage state: what was read, and what was not."""

    source_name: str
    requirement: str
    state: str
    detail: str

    @property
    def is_exception(self) -> bool:
        return self.state != COVERAGE_READ


@dataclass(frozen=True, slots=True)
class ReportSection:
    """One region the partner's own weekly template declares, in its own words."""

    key: str
    heading: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class TemplateBinding:
    """The approved template and field mapping every artifact in one issue uses."""

    template_identity: str
    template_version: str
    mapping_identity: str
    mapping_version: str
    sections: tuple[ReportSection, ...] = ()

    def section(self, key: str) -> ReportSection | None:
        for candidate in self.sections:
            if candidate.key == key:
                return candidate
        return None


# The regions the released renderer contract declares, in the order a reader
# meets them. See `RENDERER_CONTRACT_VERSION` above for why they live here.
RELEASED_TEMPLATE_SECTIONS: tuple[ReportSection, ...] = (
    ReportSection(key=SECTION_CONSTRAINT_ALERTS, heading="Items needing attention"),
    ReportSection(key=SECTION_COMMITMENTS, heading="Utility commitments"),
    ReportSection(key=SECTION_KEY_DATES, heading="Dates we need work by"),
    ReportSection(key=SECTION_FOLLOW_UP_PLANS, heading="Our next steps"),
    ReportSection(
        key=SECTION_PENDING_COORDINATION, heading="Open questions with owners"
    ),
)


@dataclass(frozen=True, slots=True)
class AcceptedFollowUpPlan:
    """One accepted Follow-up Plan the report may state (ADR-0038, ADR-0084).

    ``open_question`` carries the exact unresolved question a person recorded.
    It deliberately has nowhere to put a proposed value: the accepted position
    beside it is always composed here, from the frozen projection, so an
    unaccepted incoming value cannot reach a customer artifact through this
    field.
    """

    plan_identity: str
    subject_identity: str
    assigned_to: str
    next_action: str
    recorded_by: str
    recorded_at: datetime
    field: str | None = None
    action_due_date: date | None = None
    open_question: str | None = None


@dataclass(frozen=True)
class BoundIssueReading:
    """One issue's inputs, read once, shared by every renderer that follows."""

    project_id: int
    accepted_revision_id: int
    accepted_values: tuple[CurrentRecordValue, ...]
    previous_issue: PreviousApprovedIssue | None
    source_cutoff: datetime
    coverage: tuple[SourceCoverage, ...]
    templates: TemplateBinding
    first_issue_behavior: str
    follow_up_plans: tuple[AcceptedFollowUpPlan, ...]
    preparation: Mapping[str, Any]
    prepared_at: datetime
    reading_identity: str

    @property
    def previous_revision_id(self) -> int | None:
        return (
            None if self.previous_issue is None
            else self.previous_issue.accepted_revision_id
        )

    @property
    def cutoff_date(self) -> date:
        return self.source_cutoff.date()

    @property
    def coverage_complete(self) -> bool:
        return not any(
            line.requirement == REQUIRED and line.is_exception
            for line in self.coverage
        )


# --- Provenance -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValueProvenance:
    """Everything one published value's class requires, and what is missing."""

    value_class: str
    missing: tuple[str, ...] = ()
    source_segment_ids: tuple[int, ...] = ()
    support_assessment_ids: tuple[int, ...] = ()
    decision_id: int | None = None
    revision_id: int | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    subject_identity: str | None = None
    rule_identity: str | None = None
    rule_version: str | None = None
    input_record_ids: tuple[int, ...] = ()
    # What the inputs were, where counting them would misdescribe them.  The
    # weekly standing figures cover every proposed change and decision up to
    # two append-only watermarks rather than a list of records, and saying
    # "from 2 accepted records" would name the watermarks as though they were
    # the population.
    input_description: str | None = None
    evaluated_as_of: date | None = None

    @property
    def complete(self) -> bool:
        return not self.missing

    def sentence(self) -> str:
        """The lineage sentence ADR-0082 gives this value's class."""

        if self.value_class in (SOURCE_BACKED, RECORDED_VERBAL):
            segments = _numbered("segment", self.source_segment_ids)
            assessments = _numbered(
                "support assessment", self.support_assessment_ids
            )
            origin = (
                f"A person recorded this as a verbal statement in {segments}"
                if self.value_class == RECORDED_VERBAL
                else f"The source says this in {segments}"
            )
            return (
                f"{origin}; a person judged that it supports this value in "
                f"{assessments}; {self.decided_by} decided it on "
                f"{_day(self.decided_at)}; the project record has shown it "
                f"since revision {self.revision_id}."
            )
        if self.value_class == COORDINATION_DECISION:
            return (
                f"{self.decided_by} recorded this for {self.subject_identity} on "
                f"{_day(self.decided_at)}."
            )
        if self.input_description is not None:
            inputs = self.input_description
        else:
            count = len(self.input_record_ids)
            inputs = f"{count} accepted record{'' if count == 1 else 's'}"
        return (
            f"Computed by {self.rule_identity} version {self.rule_version} "
            f"from {inputs}, as of {self.evaluated_as_of}."
        )


# --- The change summary reading -------------------------------------------


@dataclass(frozen=True, slots=True)
class AcceptedChange:
    """One accepted Project Record decision inside this issue's window."""

    decision_id: int
    revision_id: int
    delta_id: int
    subject_identity: str
    field: str | None
    prior_value: str | None
    current_value: str | None
    disposition: str
    effect_kind: str
    decided_at: datetime
    decided_by: str
    provenance: ValueProvenance


@dataclass(frozen=True, slots=True)
class UnacceptedDelta:
    """One Proposed Delta that is not an accepted change, and why not."""

    delta_id: int
    subject_identity: str
    field: str | None
    state: str


@dataclass(frozen=True, slots=True)
class AcceptedValueLine:
    """One accepted value, for the first issue's current-state summary."""

    subject_identity: str
    field: str
    value: str | None
    provenance: ValueProvenance


@dataclass(frozen=True)
class ChangeSummaryReading:
    """The structured change summary, ready for #529 without reparsing bytes."""

    bound: BoundIssueReading
    changes: tuple[AcceptedChange, ...]
    unaccepted_deltas: tuple[UnacceptedDelta, ...]
    current_state: tuple[AcceptedValueLine, ...]
    count_provenance: ValueProvenance
    check_set_change: CheckSetChange

    @property
    def reading_identity(self) -> str:
        return self.bound.reading_identity

    @property
    def accepted_revision_id(self) -> int:
        return self.bound.accepted_revision_id

    @property
    def previous_issue_identity(self) -> str | None:
        return (
            None if self.bound.previous_issue is None
            else self.bound.previous_issue.issue_identity
        )

    @property
    def previous_revision_id(self) -> int | None:
        return self.bound.previous_revision_id

    @property
    def source_cutoff(self) -> datetime:
        return self.bound.source_cutoff

    @property
    def template_identity(self) -> str:
        return self.bound.templates.template_identity

    @property
    def mapping_identity(self) -> str:
        return self.bound.templates.mapping_identity


# --- What the checks cover, and whether they changed -----------------------


@dataclass(frozen=True, slots=True)
class CheckCoverage:
    """Which released checks this set runs on the accepted record, and which not.

    ADR-0090 settled that an adopted-baseline project's alerts will **not**
    match a legacy project's, because four rules are retired and three are
    raised somewhere a coordinator can act on them.  The difference is not a
    gap to close; it is a decision, and a decision a customer reads alert
    counts under has to be able to see.  So the report declares it rather
    than leaving the reader to notice that some findings stopped appearing.
    """

    check_set: str
    rules_run: tuple[str, ...]
    rules_raised_elsewhere: tuple[tuple[str, str], ...]
    rules_not_run: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class CheckSetChange:
    """Whether the checks themselves changed across this issue's window.

    ``changes`` draws exactly this line for the legacy path: an alert count
    that moved because the ruleset or a threshold moved is a change in the
    calculation, and reporting it as project movement tells a customer their
    project did something it did not do.  The same distinction has to survive
    here, so the change summary states which set each issue was built under
    and never lets a set change reach an accepted-change section.
    """

    current: str
    previous: str | None
    has_previous_issue: bool

    @property
    def changed(self) -> bool:
        return (
            self.has_previous_issue
            and self.previous is not None
            and self.previous != self.current
        )

    @property
    def unknown(self) -> bool:
        """The predecessor recorded no set, so no comparison can be made."""

        return self.has_previous_issue and self.previous is None


# --- The weekly report reading --------------------------------------------


@dataclass(frozen=True, slots=True)
class ReportLine:
    """One statement in one section of the weekly report, with its provenance."""

    section_key: str
    subject_identity: str
    statement: str
    provenance: ValueProvenance
    rule: str | None = None
    quantity_days: int | None = None


@dataclass(frozen=True)
class WeeklyReportReading:
    """The structured weekly Coordination Report, in the partner's own sections."""

    bound: BoundIssueReading
    lines: tuple[ReportLine, ...]
    standing_provenance: ValueProvenance
    check_coverage: CheckCoverage

    @property
    def reading_identity(self) -> str:
        return self.bound.reading_identity

    @property
    def accepted_revision_id(self) -> int:
        return self.bound.accepted_revision_id

    @property
    def coverage_complete(self) -> bool:
        return self.bound.coverage_complete

    @property
    def open_actionable(self) -> int:
        return int(self.bound.preparation.get("open_actionable") or 0)

    @property
    def open_deferred(self) -> int:
        return int(self.bound.preparation.get("open_deferred") or 0)

    # ADR-0084 keeps three counts apart, and nothing here adds them up.  A
    # deferral is scheduling, so a deferred delta is neither resolved nor
    # actionable; an acceptance and an edit both moved the record but not in
    # the same way; and a rejection decided that the accepted value stands.
    @property
    def resolved_accepted(self) -> int:
        return int(self.bound.preparation.get("resolved_accepted") or 0)

    @property
    def resolved_edited(self) -> int:
        return int(self.bound.preparation.get("resolved_edited") or 0)

    @property
    def resolved_rejected(self) -> int:
        return int(self.bound.preparation.get("resolved_rejected") or 0)

    def of_section(self, key: str) -> tuple[ReportLine, ...]:
        return tuple(line for line in self.lines if line.section_key == key)


@dataclass(frozen=True)
class IssueArtifacts:
    """Both structured readings, taken from one bound reading in one call."""

    bound: BoundIssueReading
    change_summary: ChangeSummaryReading
    weekly_report: WeeklyReportReading

    @property
    def reading_identity(self) -> str:
        return self.bound.reading_identity

    @property
    def accepted_revision_id(self) -> int:
        return self.bound.accepted_revision_id


@dataclass(frozen=True, slots=True)
class RenderedArtifact:
    """One rendered artifact, with its container metadata kept out of the body.

    ADR-0086 requires a re-render of the same bound reading to say the same
    thing.  The observation timestamp is the one honest exception, so it lives
    in ``container`` and never in ``body``; a difference there is a difference
    in when the file was made, never a change in the project.
    """

    kind: str
    reading_identity: str
    accepted_revision_id: int
    container: tuple[str, ...]
    body: str

    @property
    def text(self) -> str:
        return "\n".join(self.container) + "\n\n" + self.body


# --- Binding --------------------------------------------------------------


def bind_issue_reading(
    session: Session,
    *,
    project_id: int,
    preparation: Mapping[str, Any],
    source_cutoff: datetime,
    coverage: Sequence[SourceCoverage],
    templates: TemplateBinding,
    first_issue_behavior: str,
    prepared_at: datetime,
    previous_issue: PreviousApprovedIssue | None = None,
    follow_up_plans: Sequence[AcceptedFollowUpPlan] = (),
) -> BoundIssueReading:
    """Bind one issue's five inputs, refusing anything that does not agree.

    The frozen revision is the one #488's reading already stated its counts
    against.  It is verified to exist for this project and then pinned; no
    reader in this module asks the database which revision is newest, because
    two such questions asked a second apart are exactly how a summary and a
    report come to disagree.
    """

    if preparation.get("schema_version") != _SCHEMA_VERSION:
        raise MixedIssueInputs(
            "the weekly reading is not the report-preparation result this "
            "renderer reads"
        )
    if int(preparation.get("project_id") or 0) != project_id:
        raise MixedIssueInputs("the weekly reading belongs to another project")
    if preparation.get("health") != "healthy":
        raise MixedIssueInputs(
            "the weekly reading found no accepted record to state changes "
            "against; the next step is Adopt Baseline, not an issue"
        )
    accepted_revision_id = int(preparation.get("accepted_revision_id") or 0)
    if accepted_revision_id <= 0:
        raise MixedIssueInputs("the weekly reading names no accepted revision")
    if first_issue_behavior not in FIRST_ISSUE_BEHAVIORS:
        raise MixedIssueInputs(
            "a project configures one first-issue behavior: "
            f"{' or '.join(FIRST_ISSUE_BEHAVIORS)}"
        )
    if source_cutoff.tzinfo is None:
        raise MixedIssueInputs("the source cutoff is an exact moment with a zone")
    if prepared_at.tzinfo is None:
        raise MixedIssueInputs("the observation time is an exact moment with a zone")

    mode = project_operating_mode(session, project_id)
    if mode != ADOPTED_BASELINE:
        raise MixedIssueInputs(
            "the change summary and weekly Coordination Report are spine-native "
            "surfaces for an adopted-baseline project; a legacy project keeps "
            "its own reader in corridor.report (ADR-0084)"
        )

    _validate_templates(templates)
    coverage = tuple(coverage)
    _validate_coverage(coverage)
    follow_up_plans = tuple(follow_up_plans)
    _validate_follow_up_plans(follow_up_plans)

    if not _revision_exists(session, project_id, accepted_revision_id):
        raise MixedIssueInputs(
            f"accepted revision {accepted_revision_id} is not a revision of "
            "this project"
        )
    if previous_issue is not None:
        if previous_issue.accepted_revision_id > accepted_revision_id:
            raise MixedIssueInputs(
                "the previous approved issue stands on a later accepted "
                "revision than this one, so there is no window between them"
            )
        if not _revision_exists(
            session, project_id, previous_issue.accepted_revision_id
        ):
            raise MixedIssueInputs(
                "the previous approved issue names a revision this project "
                "never had"
            )
        if (
            previous_issue.check_set is not None
            and previous_issue.check_set not in RELEASED_ACCEPTED_RECORD_CHECK_SETS
        ):
            # An unrecognized name would be reported to a customer as "the
            # checks changed", which is a claim about why their alert counts
            # moved.  Not recording a set at all is a different and honest
            # answer, and it is the one that stays available.
            raise MixedIssueInputs(
                "the previous approved issue names a check set that was "
                f"never released: {previous_issue.check_set}"
            )

    accepted_values = tuple(
        sorted(
            read_project_record_as_of_revision(
                session, project_id, accepted_revision_id
            ),
            key=lambda value: (value.subject_key, value.fact_type, value.fact_id),
        )
    )
    return BoundIssueReading(
        project_id=project_id,
        accepted_revision_id=accepted_revision_id,
        accepted_values=accepted_values,
        previous_issue=previous_issue,
        source_cutoff=source_cutoff,
        coverage=coverage,
        templates=templates,
        first_issue_behavior=first_issue_behavior,
        follow_up_plans=follow_up_plans,
        preparation=dict(preparation),
        prepared_at=prepared_at,
        reading_identity=_reading_identity(
            project_id=project_id,
            accepted_revision_id=accepted_revision_id,
            previous_issue=previous_issue,
            source_cutoff=source_cutoff,
            coverage=coverage,
            templates=templates,
            first_issue_behavior=first_issue_behavior,
            follow_up_plans=follow_up_plans,
            preparation=preparation,
        ),
    )


def _validate_templates(templates: TemplateBinding) -> None:
    identities = (
        templates.template_identity,
        templates.template_version,
        templates.mapping_identity,
        templates.mapping_version,
    )
    if any(not str(one).strip() for one in identities):
        raise MixedIssueInputs(
            "an issue names the approved template and field mapping it was "
            "rendered through"
        )
    seen: set[str] = set()
    for section in templates.sections:
        if section.key not in SUPPORTED_SECTIONS:
            raise MixedIssueInputs(
                f"the template declares a section this renderer does not "
                f"support: {section.key}"
            )
        if section.key in seen:
            raise MixedIssueInputs(
                f"the template declares {section.key} twice, so the report "
                "would say the same thing in two places"
            )
        if not section.heading.strip():
            raise MixedIssueInputs("every declared section carries its own heading")
        seen.add(section.key)


def _validate_coverage(coverage: tuple[SourceCoverage, ...]) -> None:
    for line in coverage:
        if line.requirement not in COVERAGE_REQUIREMENTS:
            raise MixedIssueInputs(
                f"a source is required or optional, not {line.requirement}"
            )
        if line.state not in COVERAGE_STATES:
            raise MixedIssueInputs(
                f"a declared coverage state is one of "
                f"{', '.join(COVERAGE_STATES)}, not {line.state}"
            )
        if not line.source_name.strip() or not line.detail.strip():
            raise MixedIssueInputs(
                "a coverage statement names the source and says what happened "
                "to it"
            )


def _validate_follow_up_plans(plans: tuple[AcceptedFollowUpPlan, ...]) -> None:
    for plan in plans:
        if not plan.assigned_to.strip() or not plan.next_action.strip():
            raise MixedIssueInputs(
                "an accepted Follow-up Plan names who is doing what"
            )
        if plan.open_question is None:
            continue
        if not plan.open_question.strip().endswith("?"):
            raise MixedIssueInputs(
                "a pending coordination line asks a question; a sentence that "
                "asserts a value would put an unaccepted value in front of the "
                "customer as though the record held it"
            )


def _revision_exists(session: Session, project_id: int, revision_id: int) -> bool:
    return bool(
        session.scalar(
            select(ProjectRecordRevision.id).where(
                ProjectRecordRevision.id == revision_id,
                ProjectRecordRevision.project_id == project_id,
            )
        )
    )


def _reading_identity(**parts: Any) -> str:
    """A digest over the bound inputs, and deliberately not over the clock.

    Two preparations of the same revision, the same predecessor, the same
    cutoff, the same coverage, and the same templates are the same reading
    however far apart they were taken, so the observation time is not in here.
    """

    previous = parts["previous_issue"]
    payload = {
        "version": _READING_IDENTITY_VERSION,
        "project_id": parts["project_id"],
        "accepted_revision_id": parts["accepted_revision_id"],
        "previous_issue": (
            None
            if previous is None
            else {
                "identity": previous.issue_identity,
                "accepted_revision_id": previous.accepted_revision_id,
                "approved_at": previous.approved_at.isoformat(),
                "check_set": previous.check_set,
            }
        ),
        # The set of checks the accepted record is read under is part of what
        # this reading says, not part of when it was taken: the same revision
        # read under a different set is a different reading, and two artifacts
        # that disagree about which checks ran are exactly what one identity
        # exists to prevent.
        "check_set": ACCEPTED_RECORD_CHECK_SET,
        "source_cutoff": parts["source_cutoff"].isoformat(),
        "coverage": [
            [line.source_name, line.requirement, line.state, line.detail]
            for line in parts["coverage"]
        ],
        "template": [
            parts["templates"].template_identity,
            parts["templates"].template_version,
            parts["templates"].mapping_identity,
            parts["templates"].mapping_version,
            [[one.key, one.heading, one.required] for one in parts["templates"].sections],
        ],
        "first_issue_behavior": parts["first_issue_behavior"],
        "follow_up_plans": [
            [
                plan.plan_identity,
                plan.subject_identity,
                plan.field,
                plan.assigned_to,
                plan.next_action,
                None if plan.action_due_date is None else plan.action_due_date.isoformat(),
                plan.open_question,
            ]
            for plan in parts["follow_up_plans"]
        ],
        "watermarks": [
            int(parts["preparation"].get("through_delta_id") or 0),
            int(parts["preparation"].get("through_disposition_id") or 0),
        ],
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# --- Reading --------------------------------------------------------------


def read_issue_artifacts(
    session: Session, reading: BoundIssueReading
) -> IssueArtifacts:
    """Both structured readings, from one bound reading, in one call.

    #529 calls this rather than the two readers, so the pair cannot be built
    from two different bindings by accident.
    """

    return IssueArtifacts(
        bound=reading,
        change_summary=read_change_summary(session, reading),
        weekly_report=read_weekly_report(session, reading),
    )


def read_change_summary(
    session: Session, reading: BoundIssueReading
) -> ChangeSummaryReading:
    """What the accepted record decided since the previous approved issue."""

    changes = (
        ()
        if reading.previous_issue is None
        else _accepted_changes(session, reading)
    )
    current_state = (
        _current_state(session, reading)
        if reading.previous_issue is None
        and reading.first_issue_behavior == CURRENT_STATE_SUMMARY
        else ()
    )
    return ChangeSummaryReading(
        bound=reading,
        changes=changes,
        unaccepted_deltas=_unaccepted_deltas(session, reading),
        current_state=current_state,
        check_set_change=CheckSetChange(
            current=ACCEPTED_RECORD_CHECK_SET,
            previous=(
                None
                if reading.previous_issue is None
                else reading.previous_issue.check_set
            ),
            has_previous_issue=reading.previous_issue is not None,
        ),
        count_provenance=ValueProvenance(
            value_class=DERIVATION,
            rule_identity=CHANGE_SUMMARY_RULE,
            rule_version=CHANGE_SUMMARY_RULE_VERSION,
            input_record_ids=(
                tuple(change.decision_id for change in changes)
                or tuple(
                    line.provenance.decision_id
                    for line in current_state
                    if line.provenance.decision_id is not None
                )
            ),
            evaluated_as_of=reading.cutoff_date,
        ),
    )


def _accepted_changes(
    session: Session, reading: BoundIssueReading
) -> tuple[AcceptedChange, ...]:
    rows = session.execute(
        select(DeltaRecordDecision, ProposedDelta, DeltaDisposition)
        .join(
            ProposedDelta,
            ProposedDelta.id == DeltaRecordDecision.delta_id,
        )
        .join(
            DeltaDisposition,
            DeltaDisposition.id == DeltaRecordDecision.disposition_id,
        )
        .where(
            DeltaRecordDecision.project_id == reading.project_id,
            DeltaRecordDecision.disposition.in_(ACCEPTED_DISPOSITIONS),
            DeltaRecordDecision.revision_id > (reading.previous_revision_id or 0),
            DeltaRecordDecision.revision_id <= reading.accepted_revision_id,
        )
        .order_by(DeltaRecordDecision.revision_id, DeltaRecordDecision.id)
    ).all()

    changes: list[AcceptedChange] = []
    for decision, delta, disposition in rows:
        effective = (
            disposition.effective_value
            if disposition.effective_value is not None
            else delta.proposed_value
        )
        changes.append(
            AcceptedChange(
                decision_id=decision.id,
                revision_id=decision.revision_id,
                delta_id=delta.id,
                subject_identity=delta.target_subject_identity,
                field=delta.target_field,
                prior_value=_value_text(delta.accepted_value),
                current_value=_value_text(effective),
                disposition=decision.disposition,
                effect_kind=decision.effect_kind,
                decided_at=decision.decided_at,
                decided_by=decision.decided_by_principal,
                provenance=_decision_provenance(session, decision, delta),
            )
        )
    return tuple(changes)


def _decision_provenance(
    session: Session, decision: DeltaRecordDecision, delta: ProposedDelta
) -> ValueProvenance:
    fact_ids = tuple(
        session.scalars(
            select(FactDecision.fact_id).where(
                FactDecision.project_id == decision.project_id,
                FactDecision.revision_id == decision.revision_id,
                FactDecision.subject_key == delta.target_subject_identity,
                FactDecision.disposition == "include",
            )
        ).all()
    )
    segment_ids, kinds = _segments_of(session, fact_ids)
    support_ids = tuple(
        session.scalars(
            select(DeltaDecisionSupport.support_assessment_id)
            .where(DeltaDecisionSupport.decision_id == decision.id)
            .order_by(DeltaDecisionSupport.ordinal)
        ).all()
    )
    value_class = (
        RECORDED_VERBAL
        if kinds and all(kind == "recorded_verbal_statement" for kind in kinds)
        else SOURCE_BACKED
    )
    missing: list[str] = []
    if value_class == SOURCE_BACKED and not segment_ids:
        missing.append("a typed source locator")
    if not support_ids:
        missing.append("at least one named support assessment")
    return ValueProvenance(
        value_class=value_class,
        missing=tuple(missing),
        source_segment_ids=segment_ids,
        support_assessment_ids=support_ids,
        decision_id=decision.id,
        revision_id=decision.revision_id,
        decided_by=decision.decided_by_principal,
        decided_at=decision.decided_at,
        subject_identity=delta.target_subject_identity,
    )


def _segments_of(
    session: Session, fact_ids: Sequence[int]
) -> tuple[tuple[int, ...], tuple[str, ...]]:
    if not fact_ids:
        return (), ()
    rows = session.execute(
        select(FactSource.source_segment_id, SourceSegment.kind)
        .join(SourceSegment, SourceSegment.id == FactSource.source_segment_id)
        .where(FactSource.fact_id.in_(tuple(fact_ids)))
        .order_by(FactSource.source_segment_id)
    ).all()
    return tuple(row[0] for row in rows), tuple(row[1] for row in rows)


def _revision_authorities(
    session: Session, reading: BoundIssueReading
) -> dict[int, tuple[str | None, datetime | None]]:
    """Who decided each revision this reading projects, and when they said so.

    The decision's own ``decided_at`` is preferred over the revision's
    insertion time wherever a Resolve Delta wrote one.  Otherwise the change
    summary would date an acceptance by the moment the person made it and the
    weekly report would date the same act by the moment PostgreSQL inserted
    the row, and the two artifacts would disagree about a date in the issue
    they were prepared for.
    """

    revision_ids = {value.revision_id for value in reading.accepted_values}
    if not revision_ids:
        return {}
    authorities: dict[int, tuple[str | None, datetime | None]] = {}
    for revision in session.scalars(
        select(ProjectRecordRevision).where(
            ProjectRecordRevision.id.in_(tuple(revision_ids))
        )
    ):
        authorities[revision.id] = (
            revision.human_principal or revision.released_policy,
            revision.recorded_at,
        )
    for decision in session.scalars(
        select(DeltaRecordDecision).where(
            DeltaRecordDecision.project_id == reading.project_id,
            DeltaRecordDecision.revision_id.in_(tuple(revision_ids)),
        )
    ):
        authorities[decision.revision_id] = (
            decision.decided_by_principal,
            decision.decided_at,
        )
    return authorities


def _accepted_value_provenance(
    session: Session,
    reading: BoundIssueReading,
    value: CurrentRecordValue,
    authorities: Mapping[int, tuple[str | None, datetime | None]],
) -> ValueProvenance:
    segment_ids, kinds = _segments_of(session, (value.fact_id,))
    support_ids = tuple(
        assessment.id
        for assessment in current_support_assessments(
            session, reading.project_id, FactProposition(value.fact_id)
        )
        if assessment.assessment in SUPPORTING_OUTCOMES
    )
    decided_by, decided_at = authorities.get(value.revision_id, (None, None))
    value_class = (
        RECORDED_VERBAL
        if kinds and all(kind == "recorded_verbal_statement" for kind in kinds)
        else SOURCE_BACKED
    )
    missing: list[str] = []
    if value_class == SOURCE_BACKED and not segment_ids:
        missing.append("a typed source locator")
    if not support_ids:
        missing.append("at least one named support assessment")
    if decided_by is None:
        missing.append("the authority of the revision that accepted it")
    return ValueProvenance(
        value_class=value_class,
        missing=tuple(missing),
        source_segment_ids=segment_ids,
        support_assessment_ids=support_ids,
        decision_id=value.decision_id,
        revision_id=value.revision_id,
        decided_by=decided_by,
        decided_at=decided_at,
        subject_identity=value.subject_key,
    )


def _current_state(
    session: Session, reading: BoundIssueReading
) -> tuple[AcceptedValueLine, ...]:
    authorities = _revision_authorities(session, reading)
    return tuple(
        AcceptedValueLine(
            subject_identity=value.subject_key,
            field=value.fact_type,
            value=_projected_text(value),
            provenance=_accepted_value_provenance(
                session, reading, value, authorities
            ),
        )
        for value in reading.accepted_values
    )


def _unaccepted_deltas(
    session: Session, reading: BoundIssueReading
) -> tuple[UnacceptedDelta, ...]:
    accepted = select(DeltaDisposition.delta_id).where(
        DeltaDisposition.project_id == reading.project_id,
        DeltaDisposition.disposition.in_(ACCEPTED_DISPOSITIONS),
    )
    ceiling = int(reading.preparation.get("through_delta_id") or 0)
    deltas = tuple(
        session.scalars(
            select(ProposedDelta)
            .where(
                ProposedDelta.project_id == reading.project_id,
                ProposedDelta.id <= ceiling,
                ProposedDelta.id.not_in(accepted),
            )
            .order_by(ProposedDelta.id)
        ).all()
    )
    if not deltas:
        return ()
    ids = tuple(delta.id for delta in deltas)
    rejected = set(
        session.scalars(
            select(DeltaDisposition.delta_id).where(
                DeltaDisposition.project_id == reading.project_id,
                DeltaDisposition.delta_id.in_(ids),
            )
        ).all()
    )
    superseded = set(
        session.scalars(
            select(DeltaSupersession.prior_delta_id).where(
                DeltaSupersession.project_id == reading.project_id,
                DeltaSupersession.prior_delta_id.in_(ids),
            )
        ).all()
    )
    # A deferral is running when it has no return date or its return date is
    # still ahead of the declared cutoff.  Both sides are values a person
    # chose or an issue declared; no wall clock takes part.
    deferred = set(
        session.scalars(
            select(DeltaDeferral.delta_id).where(
                DeltaDeferral.project_id == reading.project_id,
                DeltaDeferral.delta_id.in_(ids),
                (DeltaDeferral.deferred_until.is_(None))
                | (DeltaDeferral.deferred_until > reading.source_cutoff),
            )
        ).all()
    )
    standing = _standing_revisions(session, reading)

    lines: list[UnacceptedDelta] = []
    for delta in deltas:
        if delta.id in rejected:
            state = DELTA_REJECTED
        elif delta.id in superseded:
            state = DELTA_SUPERSEDED
        elif delta.id in deferred:
            state = DELTA_DEFERRED
        elif is_stale(delta, standing):
            state = DELTA_STALE
        else:
            state = DELTA_OPEN
        lines.append(
            UnacceptedDelta(
                delta_id=delta.id,
                subject_identity=delta.target_subject_identity,
                field=delta.target_field,
                state=state,
            )
        )
    return tuple(lines)


def _standing_revisions(
    session: Session, reading: BoundIssueReading
) -> dict[tuple[str, str], int]:
    """The revision each accepted subject and field stands on, at the freeze."""

    return {
        (value.subject_key, value.fact_type): value.revision_id
        for value in reading.accepted_values
    }


def read_weekly_report(
    session: Session, reading: BoundIssueReading
) -> WeeklyReportReading:
    """The accepted reading, arranged into the sections the template declares."""

    authorities = _revision_authorities(session, reading)
    lines: list[ReportLine] = []
    for section in reading.templates.sections:
        if section.key == SECTION_CONSTRAINT_ALERTS:
            lines.extend(_alert_lines(session, reading))
        elif section.key == SECTION_COMMITMENTS:
            lines.extend(
                _value_lines(session, reading, "committed_date", section.key, authorities)
            )
        elif section.key == SECTION_KEY_DATES:
            lines.extend(
                _value_lines(session, reading, "need_date", section.key, authorities)
            )
        elif section.key == SECTION_FOLLOW_UP_PLANS:
            lines.extend(_follow_up_lines(reading))
        elif section.key == SECTION_PENDING_COORDINATION:
            lines.extend(_pending_lines(reading))
    return WeeklyReportReading(
        bound=reading,
        lines=tuple(lines),
        check_coverage=CheckCoverage(
            check_set=ACCEPTED_RECORD_CHECK_SET,
            rules_run=ACCEPTED_RECORD_CHECK_RULES,
            rules_raised_elsewhere=CHECKS_RAISED_ELSEWHERE,
            rules_not_run=CHECKS_NOT_RUN,
        ),
        standing_provenance=ValueProvenance(
            value_class=DERIVATION,
            rule_identity=WEEKLY_REPORT_RULE,
            rule_version=WEEKLY_REPORT_RULE_VERSION,
            input_record_ids=(
                int(reading.preparation.get("through_delta_id") or 0),
                int(reading.preparation.get("through_disposition_id") or 0),
            ),
            input_description=(
                "every proposed change up to number "
                f"{int(reading.preparation.get('through_delta_id') or 0)} and "
                "every decision up to number "
                f"{int(reading.preparation.get('through_disposition_id') or 0)}"
            ),
            evaluated_as_of=reading.cutoff_date,
        ),
    )


def _value_lines(
    session: Session,
    reading: BoundIssueReading,
    fact_type: str,
    section_key: str,
    authorities: Mapping[int, tuple[str | None, datetime | None]],
) -> list[ReportLine]:
    lines = []
    for value in reading.accepted_values:
        if value.fact_type != fact_type:
            continue
        lines.append(
            ReportLine(
                section_key=section_key,
                subject_identity=value.subject_key,
                statement=(
                    f"{value.subject_key}: {field_label(fact_type)} is "
                    f"{_projected_text(value) or 'not recorded'}."
                ),
                provenance=_accepted_value_provenance(
                    session, reading, value, authorities
                ),
            )
        )
    return lines


@dataclass(frozen=True, slots=True)
class _SupportInUse:
    """What one accepted value's Supporting Documentation in Use rests on.

    ``stands_on_current`` is true as soon as one segment in use sits in a
    Document Revision that has not been replaced.  A Recorded Verbal
    Statement is not a Document Revision at all, so it can never be
    superseded and counts here.  ``replaced_on`` is the authority's own
    replacement date for the earliest superseded revision in use, and is
    ``None`` when none is in use — or, for a pre-constraint row, when the
    registry never recorded one; a document, retrieval, or ingestion date is
    never substituted for it (ADR-0016).
    """

    assessment_ids: tuple[int, ...]
    stands_on_current: bool
    replaced_on: date | None

    @property
    def depends_on_superseded(self) -> bool:
        return not self.stands_on_current


def _support_in_use(
    session: Session, reading: BoundIssueReading
) -> dict[int, _SupportInUse]:
    """The Supporting Documentation in Use for every accepted value, once.

    ADR-0017 requires one resolver for "what does this record stand on", and
    both support checks below read this and nothing else.  Support is the
    Support Assessment relation: an effective assessment whose outcome is
    supported or partially supported.  A passed Source Passage Check is never
    consulted, because a passage being where it was cited says nothing about
    whether it supports the value beside it (ADR-0082).

    An accepted value with no entry here has no Supporting Documentation in
    use at all, which is the ported ``MISSING_EVIDENCE``.  That is why the
    absence is expressed by the key being missing rather than by an empty
    record: the two checks below then cannot both fire on one value.

    ``stands_on_current`` is a real question even though today's database
    answers it one way.  ``append_support_assessment`` requires every named
    segment to sit in the proposition's own rendition, so one accepted value's
    support cannot currently span a replaced revision and its successor at
    once, and the record moves onto the successor by accepting that revision's
    own statement instead.  The predicate is written as ADR-0090 states it —
    a superseded revision in use **and** no current support beside it —
    rather than as "this value's document was replaced", because the second
    would become the wrong rule the moment a proposition may name more than
    one rendition.
    """

    fact_ids = tuple({value.fact_id for value in reading.accepted_values})
    if not fact_ids:
        return {}
    rows = session.execute(
        select(
            SupportAssessment.fact_id,
            SupportAssessment.id,
            Document.superseded_by,
            Document.superseded_on,
        )
        .join(
            SupportAssessmentSource,
            SupportAssessmentSource.support_assessment_id == SupportAssessment.id,
        )
        .join(
            SourceSegment,
            SourceSegment.id == SupportAssessmentSource.source_segment_id,
        )
        .outerjoin(Document, Document.id == SourceSegment.document_id)
        .where(
            SupportAssessment.project_id == reading.project_id,
            SupportAssessment.proposition_kind == "source_fact",
            SupportAssessment.fact_id.in_(fact_ids),
            SupportAssessment.superseded_by.is_(None),
            SupportAssessment.assessment.in_(SUPPORTING_OUTCOMES),
        )
    ).all()

    assessments: dict[int, set[int]] = {}
    current: set[int] = set()
    replaced: dict[int, date] = {}
    for fact_id, assessment_id, superseded_by, superseded_on in rows:
        assessments.setdefault(fact_id, set()).add(assessment_id)
        if superseded_by is None:
            current.add(fact_id)
        elif superseded_on is not None:
            held = replaced.get(fact_id)
            if held is None or superseded_on < held:
                replaced[fact_id] = superseded_on
    return {
        fact_id: _SupportInUse(
            assessment_ids=tuple(sorted(found)),
            stands_on_current=fact_id in current,
            replaced_on=replaced.get(fact_id),
        )
        for fact_id, found in assessments.items()
    }


def _alert_lines(session: Session, reading: BoundIssueReading) -> list[ReportLine]:
    """The accepted record's own check set, ``accepted_record_checks_v2``.

    The rule names and their customer labels are the released ones, so an
    alert here reads exactly as the same finding reads everywhere else.  The
    version is this module's own because the inputs are accepted spine values
    rather than the legacy Dependency rows, and a shared version string would
    hide that difference the next time a count moves.

    Five of the twelve released rules run here (ADR-0090).  Three are the
    date checks derivable from accepted values alone.  The other two are the
    ones that say whether the accepted record stands on anything, and they
    read the Support Assessment relation: ``MISSING_EVIDENCE`` re-based off
    the Source Passage Check it used to mean, and ``SUPERSEDED_CITATION``
    with ADR-0016's predicate carried over exactly — never "a document has a
    successor", only "the accepted value still depends on the superseded
    revision and nothing current has replaced it".  The other seven are
    raised where they can be acted on, or are not Corridor's to assert; the
    report declares which, so the difference from a legacy project's alerts
    is stated rather than silent.
    """

    cutoff = reading.cutoff_date
    promised: dict[str, CurrentRecordValue] = {}
    required: dict[str, CurrentRecordValue] = {}
    for value in reading.accepted_values:
        if value.fact_type == "committed_date":
            promised[value.subject_key] = value
        elif value.fact_type == "need_date":
            required[value.subject_key] = value

    lines: list[ReportLine] = []
    for subject in sorted(set(promised) | set(required)):
        promise = promised.get(subject)
        need = required.get(subject)
        if promise is not None and promise.date_value is not None:
            elapsed = (cutoff - promise.date_value).days
            if elapsed > 0:
                lines.append(
                    _alert(reading, subject, "OVERDUE", elapsed, (promise.fact_id,))
                )
        elif need is not None:
            lines.append(
                _alert(reading, subject, "MISSING_DATE", None, (need.fact_id,))
            )
        if need is not None and need.date_value is not None:
            remaining = (need.date_value - cutoff).days
            if 0 <= remaining <= DUE_SOON_DAYS:
                lines.append(
                    _alert(reading, subject, "DUE_SOON", remaining, (need.fact_id,))
                )

    lines.extend(_support_alert_lines(session, reading))
    return lines


def _support_alert_lines(
    session: Session, reading: BoundIssueReading
) -> list[ReportLine]:
    """The two ported rules, one accepted value at a time (ADR-0090).

    They are evaluated per accepted proposition rather than per subject
    because that is the unit a Support Assessment is recorded against, and
    because "this promised date stands on nothing" is a different finding
    from "this required-by date stands on nothing".  Each line therefore
    names the field it is about, so no two of them are the identical row
    ADR-0010 found unreadable.

    The two are mutually exclusive by construction.  A value with no
    Supporting Documentation in Use cannot also be depending on a superseded
    revision, so a coordinator never reads two alerts about one absence.
    """

    standing = _support_in_use(session, reading)
    lines: list[ReportLine] = []
    for value in reading.accepted_values:
        in_use = standing.get(value.fact_id)
        if in_use is None:
            lines.append(
                _alert(
                    reading,
                    value.subject_key,
                    "MISSING_EVIDENCE",
                    None,
                    (value.fact_id,),
                    field=value.fact_type,
                )
            )
            continue
        if not in_use.depends_on_superseded:
            # A current revision is in use for this value, so an older one
            # beside it is history doing no work.  ADR-0016 refused the "any
            # old link exists" reading precisely here: an alert on it could
            # never clear without deleting provenance, which is pressure to
            # erase exactly what makes the record checkable.
            continue
        lines.append(
            _alert(
                reading,
                value.subject_key,
                "SUPERSEDED_CITATION",
                None
                if in_use.replaced_on is None
                else (reading.cutoff_date - in_use.replaced_on).days,
                in_use.assessment_ids,
                field=value.fact_type,
            )
        )
    return lines


def _alert(
    reading: BoundIssueReading,
    subject: str,
    rule: str,
    quantity_days: int | None,
    input_record_ids: tuple[int, ...],
    field: str | None = None,
) -> ReportLine:
    days = "" if quantity_days is None else f" ({quantity_days} days)"
    about = "" if field is None else f" — {field_label(field)}"
    return ReportLine(
        section_key=SECTION_CONSTRAINT_ALERTS,
        subject_identity=subject,
        statement=(
            f"{subject}{about}: {accepted_record_exception_name(rule)}{days}."
        ),
        provenance=ValueProvenance(
            value_class=DERIVATION,
            rule_identity=ACCEPTED_RECORD_CHECK_RULE,
            rule_version=ACCEPTED_RECORD_CHECK_VERSION,
            input_record_ids=input_record_ids,
            evaluated_as_of=reading.cutoff_date,
            subject_identity=subject,
        ),
        rule=rule,
        quantity_days=quantity_days,
    )


def _plan_provenance(plan: AcceptedFollowUpPlan) -> ValueProvenance:
    return ValueProvenance(
        value_class=COORDINATION_DECISION,
        decided_by=plan.recorded_by,
        decided_at=plan.recorded_at,
        subject_identity=plan.subject_identity,
    )


def _follow_up_lines(reading: BoundIssueReading) -> list[ReportLine]:
    lines = []
    for plan in reading.follow_up_plans:
        due = (
            "no date is set for it yet"
            if plan.action_due_date is None
            else f"it is due on {plan.action_due_date.isoformat()}"
        )
        lines.append(
            ReportLine(
                section_key=SECTION_FOLLOW_UP_PLANS,
                subject_identity=plan.subject_identity,
                statement=(
                    f"{plan.subject_identity}: {plan.assigned_to} will "
                    f"{plan.next_action[0].lower()}{plan.next_action[1:]}, and "
                    f"{due}."
                ),
                provenance=_plan_provenance(plan),
            )
        )
    return lines


def _pending_lines(reading: BoundIssueReading) -> list[ReportLine]:
    """The open questions, each beside the position the record actually holds.

    The question is the one a person recorded on an accepted Follow-up Plan.
    The position beside it is composed here from the frozen projection, so no
    incoming value that has not been accepted can appear in this section.
    """

    accepted = {
        (value.subject_key, value.fact_type): value
        for value in reading.accepted_values
    }
    lines = []
    for plan in reading.follow_up_plans:
        if plan.open_question is None:
            continue
        held = accepted.get((plan.subject_identity, plan.field or ""))
        if plan.field is None:
            position = (
                "Nothing about this has been accepted into the project record "
                "yet."
            )
        elif held is None:
            position = (
                f"The project record holds no {field_label(plan.field)} for "
                "this item, and no change to that has been accepted."
            )
        else:
            position = (
                f"The project record shows {field_label(plan.field)} "
                f"{_projected_text(held)}, and no change to that has been "
                "accepted."
            )
        lines.append(
            ReportLine(
                section_key=SECTION_PENDING_COORDINATION,
                subject_identity=plan.subject_identity,
                statement=(
                    f"{plan.subject_identity}: {plan.open_question.strip()} "
                    f"{position}"
                ),
                provenance=_plan_provenance(plan),
            )
        )
    return lines


# --- Rendering ------------------------------------------------------------


def render_change_summary(reading: ChangeSummaryReading) -> RenderedArtifact:
    """The customer's change summary, in sentences rather than a table of codes."""

    bound = reading.bound
    paragraphs: list[str] = []
    if bound.previous_issue is not None:
        paragraphs.append(
            "This summary covers every change the project record accepted "
            f"between the revision your last approved issue was built from "
            f"(revision {bound.previous_revision_id}) and the revision this "
            f"issue is built from (revision {bound.accepted_revision_id}). "
            f"{_cutoff_sentence(bound)}"
        )
        if reading.changes:
            paragraphs.append(_change_count_sentence(reading))
            for position, change in enumerate(reading.changes, start=1):
                paragraphs.append(_change_paragraph(position, change))
        else:
            paragraphs.append(
                "Nothing was accepted into the project record in that window. "
                "The record still says what it said when your last issue was "
                "approved."
            )
    else:
        paragraphs.append(
            "This is the first issue prepared for this project, so there is "
            "no earlier approved issue to compare it with. Corridor has not "
            "invented one: there is no earlier date window here and nothing "
            "below is described as a change."
        )
        if bound.first_issue_behavior == CURRENT_STATE_SUMMARY:
            paragraphs.append(
                "Instead, this states what the accepted project record holds "
                f"at revision {bound.accepted_revision_id}. "
                f"{_cutoff_sentence(bound)}"
            )
            for line in reading.current_state:
                _require_provenance(line.provenance, line.subject_identity)
                paragraphs.append(
                    f"{line.subject_identity} — {field_label(line.field)}: "
                    f"{line.value or 'not recorded'}. {line.provenance.sentence()}"
                )
        else:
            paragraphs.append(
                "The next issue will compare against this one."
            )

    paragraphs.append(_unaccepted_paragraph(reading))
    paragraphs.append(_check_set_paragraph(reading.check_set_change))
    paragraphs.extend(_coverage_paragraphs(bound))
    return RenderedArtifact(
        kind="change_summary",
        reading_identity=bound.reading_identity,
        accepted_revision_id=bound.accepted_revision_id,
        container=_container(bound, "What changed since the last issue"),
        body="\n\n".join(paragraphs),
    )


def _check_set_paragraph(change: CheckSetChange) -> str:
    """Say whether the checks moved, so a moved alert is not read as movement.

    ``changes`` makes this distinction for the legacy path and states why: a
    report's most-read section is the one saying what moved, and it is only
    trustworthy if a reader can tell the project changing from the rules
    changing, because those call for opposite responses.  Nothing about a
    check set ever enters an accepted-change section here — a change to the
    accepted record is a Human Record Decision, and a check set is not one.
    """

    if not change.has_previous_issue:
        return (
            "The checks Corridor runs on your accepted project record are "
            f"the set called {change.current}. This is the first issue, so "
            "there is no earlier set to compare them with."
        )
    if change.unknown:
        return (
            "Your last approved issue did not record which set of checks it "
            "was built under, and Corridor will not assume it was this one. "
            f"This issue was built under {change.current}. Because the "
            "earlier set is unknown, an alert that is present in one issue "
            "and absent in the other cannot be attributed either to your "
            "project or to a change in the checks."
        )
    if change.changed:
        return (
            "The set of checks Corridor runs on your accepted project "
            "record changed between the two issues: your last one was "
            f"built under {change.previous} and this one is built under "
            f"{change.current}. An alert that appears or disappears for that "
            "reason is a change in the checks and not a change in your "
            "project, so nothing above describes it as something that "
            "happened on the project. The report lists the checks this set "
            "runs and the ones it deliberately does not."
        )
    return (
        "Both issues were built under the same set of checks, "
        f"{change.current}, so an alert that appeared or disappeared "
        "between them reflects your project rather than a change in what "
        "Corridor checks."
    )


def _change_count_sentence(reading: ChangeSummaryReading) -> str:
    count = len(reading.changes)
    noun = "change was" if count == 1 else "changes were"
    return (
        f"{count} {noun} accepted. {reading.count_provenance.sentence()}"
    )


def _change_paragraph(position: int, change: AcceptedChange) -> str:
    _require_provenance(change.provenance, change.subject_identity)
    name = field_label(change.field) if change.field else "This item"
    edited = (
        " The value accepted was edited from the one the source proposed."
        if change.disposition == "edit"
        else ""
    )
    return (
        f"{position}. {change.subject_identity} — {name}. The project record "
        f"showed {change.prior_value or 'nothing recorded'}; it now shows "
        f"{change.current_value or 'nothing recorded'}. "
        f"{change.provenance.sentence()}{edited}"
    )


def _unaccepted_paragraph(reading: ChangeSummaryReading) -> str:
    counted: dict[str, int] = {}
    for line in reading.unaccepted_deltas:
        counted[line.state] = counted.get(line.state, 0) + 1
    if not counted:
        return (
            "Every proposed change Corridor has raised for this project has "
            "been decided, so nothing is waiting."
        )
    parts = [
        f"{_things(count)} {_state_words(state, count)}"
        for state, count in sorted(counted.items())
    ]
    return (
        "Some proposed changes are not changes to the project record, so none "
        "of them appear above: " + _join(parts) + ". They stay on the work "
        "list with their own history, and this issue does not treat any of "
        "them as an accepted value."
    )


def _things(count: int) -> str:
    return "1 proposed change" if count == 1 else f"{count} proposed changes"


def _state_words(state: str, count: int) -> str:
    singular = count == 1
    return {
        DELTA_OPEN: "is still open" if singular else "are still open",
        DELTA_DEFERRED: (
            "was deferred to a later date"
            if singular
            else "were deferred to a later date"
        ),
        DELTA_REJECTED: (
            "was decided by keeping the current value"
            if singular
            else "were decided by keeping the current value"
        ),
        DELTA_SUPERSEDED: (
            "was replaced by a newer source revision"
            if singular
            else "were replaced by a newer source revision"
        ),
        DELTA_STALE: (
            "was raised against a value that has since moved"
            if singular
            else "were raised against a value that has since moved"
        ),
    }[state]


def render_weekly_report(reading: WeeklyReportReading) -> RenderedArtifact:
    """The weekly Coordination Report, in the partner's own template sections."""

    bound = reading.bound
    paragraphs: list[str] = [
        f"This report is built from accepted project record revision "
        f"{bound.accepted_revision_id}, using the template "
        f"{bound.templates.template_identity} version "
        f"{bound.templates.template_version} and the field mapping "
        f"{bound.templates.mapping_identity} version "
        f"{bound.templates.mapping_version}. The checks below are the set "
        f"called {reading.check_coverage.check_set}. {_cutoff_sentence(bound)}"
    ]
    for section in bound.templates.sections:
        lines = reading.of_section(section.key)
        paragraphs.append(section.heading)
        if not lines:
            paragraphs.append(_empty_section_sentence(section.key))
        else:
            for line in lines:
                _require_provenance(line.provenance, line.subject_identity)
                paragraphs.append(f"{line.statement} {line.provenance.sentence()}")
        if section.key == SECTION_CONSTRAINT_ALERTS:
            paragraphs.extend(_check_coverage_paragraphs(reading.check_coverage))

    paragraphs.append(
        f"{_open_work_paragraph(reading)} "
        f"{reading.standing_provenance.sentence()}"
    )
    paragraphs.extend(_coverage_paragraphs(bound))
    return RenderedArtifact(
        kind="weekly_coordination_report",
        reading_identity=bound.reading_identity,
        accepted_revision_id=bound.accepted_revision_id,
        container=_container(bound, "Constraint status report"),
        body="\n\n".join(paragraphs),
    )


def _check_coverage_paragraphs(coverage: CheckCoverage) -> list[str]:
    """State which released checks ran here, and which did not, and why.

    An adopted-baseline project's alerts are not the same as a legacy
    project's and are not going to become the same: four of the twelve
    released rules are retired and three are raised where they can be acted
    on (ADR-0090).  A customer comparing two projects, or one project across
    the change, has to be told that in the report rather than left to notice
    that findings stopped appearing.  So the difference is declared here, in
    the same words the decision was made in.
    """

    ran = _join(
        [accepted_record_exception_name(rule) for rule in coverage.rules_run]
    )
    paragraphs = [
        f"These are the checks Corridor ran on your accepted project record, "
        f"as the set called {coverage.check_set}: {ran}. Each finding above "
        "names the check it came from and the accepted records it was "
        "computed from."
    ]
    if coverage.rules_raised_elsewhere:
        elsewhere = " ".join(
            f'"{accepted_record_exception_name(rule)}" is not repeated here, '
            f"because {why}."
            for rule, why in coverage.rules_raised_elsewhere
        )
        paragraphs.append(
            "Three things Corridor used to report as alerts now reach you "
            "somewhere you can act on them, and are deliberately not shown "
            f"twice. {elsewhere}"
        )
    if coverage.rules_not_run:
        retired = " ".join(
            f'It does not report "{accepted_record_exception_name(rule)}", '
            f"because {why}."
            for rule, why in coverage.rules_not_run
        )
        paragraphs.append(
            "Corridor also stopped making four checks that only said an "
            "administrative field was empty, because a list where every row "
            f"means the same thing is a list nobody can read. {retired}"
        )
    return paragraphs


def _empty_section_sentence(section_key: str) -> str:
    return {
        SECTION_CONSTRAINT_ALERTS: (
            "No check on the accepted record found anything needing attention "
            "this week."
        ),
        SECTION_COMMITMENTS: (
            "The project record holds no promised dates for this project yet."
        ),
        SECTION_KEY_DATES: (
            "The project record holds no required-by dates for this project yet."
        ),
        SECTION_FOLLOW_UP_PLANS: (
            "No follow-up plan has been recorded for this project yet."
        ),
        SECTION_PENDING_COORDINATION: (
            "No open question has been recorded on a follow-up plan, so there "
            "is nothing waiting on an answer here."
        ),
    }[section_key]


def _open_work_paragraph(reading: WeeklyReportReading) -> str:
    """The week's decided and undecided work, in ADR-0084's separate counts.

    A deferral is Work List scheduling, not a decision, so a deferred proposed
    change is counted on its own and never added to either the decided or the
    waiting figure.  Folding the three together is how a report comes to say a
    week's work is finished when a coordinator has only postponed it.
    """

    decided = []
    if reading.resolved_accepted:
        decided.append(
            f"{_things(reading.resolved_accepted)} accepted as the source "
            "stated them"
        )
    if reading.resolved_edited:
        decided.append(
            f"{_things(reading.resolved_edited)} accepted with an edited value"
        )
    if reading.resolved_rejected:
        decided.append(
            f"{_things(reading.resolved_rejected)} decided by keeping the "
            "value the project record already held"
        )
    sentences = []
    if decided:
        sentences.append(
            "Since the last weekly reading, " + _join(decided) + "."
        )
    actionable = reading.open_actionable
    deferred = reading.open_deferred
    if not actionable and not deferred:
        sentences.append(
            "There are no proposed changes waiting on a decision, so "
            "everything above is the settled record."
        )
    else:
        waiting = []
        if actionable:
            waiting.append(f"{_things(actionable)} waiting on a decision")
        if deferred:
            waiting.append(
                f"{_things(deferred)} deferred to a later date, which is a "
                "decision about when to look at it and not about what the "
                "record says"
            )
        sentences.append(
            "Corridor is holding " + _join(waiting) + ". None of them has "
            "changed the project record, and nothing above states them as "
            "though it had."
        )
    return " ".join(sentences)


def _coverage_paragraphs(bound: BoundIssueReading) -> list[str]:
    paragraphs = ["What was read for this issue"]
    if not bound.coverage:
        paragraphs.append(
            "No source coverage was declared for this issue, so this report "
            "cannot say what was and was not read."
        )
        return paragraphs
    for line in bound.coverage:
        if line.state == COVERAGE_READ:
            paragraphs.append(
                f"{line.source_name} was read: {line.detail}."
            )
        elif line.state == COVERAGE_LATE:
            paragraphs.append(
                f"{line.source_name} was not read for this issue because "
                f"{line.detail}. It is an optional source, so this issue is "
                "complete without it, and nothing in it has been treated as "
                "an accepted value."
            )
        else:
            necessity = (
                "It is a required source"
                if line.requirement == REQUIRED
                else "It is an optional source"
            )
            paragraphs.append(
                f"{line.source_name} was not read for this issue: "
                f"{line.detail}. {necessity}, so anything it would have said "
                "is missing from everything above."
            )
    if not bound.coverage_complete:
        paragraphs.append(
            "Because a required source was not read, this issue does not "
            "cover the whole project record."
        )
    return paragraphs


def _container(bound: BoundIssueReading, title: str) -> tuple[str, ...]:
    """The heading block: what this is, which revision, and when it was made.

    The revision belongs here as well as in the body because a reader holding
    the file has to be able to name the exact revision it came from without
    reading it through.  It is stable for one bound reading; only the last
    line moves with the clock.
    """

    return (
        title,
        f"Built from accepted project record revision "
        f"{bound.accepted_revision_id}.",
        f"Prepared {bound.prepared_at.isoformat()}.",
    )


def _cutoff_sentence(bound: BoundIssueReading) -> str:
    return (
        "Anything a source delivered after "
        f"{bound.cutoff_date.isoformat()} is not in it."
    )


def _require_provenance(provenance: ValueProvenance, subject: str) -> None:
    if provenance.complete:
        return
    raise IncompleteProvenance(
        f"{subject} would be published without the provenance a "
        f"{provenance.value_class} needs: {_join(list(provenance.missing))} is "
        "missing. A passed source passage check is not support (ADR-0082)."
    )


def _projected_text(value: CurrentRecordValue) -> str | None:
    if value.date_value is not None:
        return value.date_value.isoformat()
    if value.date_range_start is not None and value.date_range_end is not None:
        return f"{value.date_range_start.isoformat()} to {value.date_range_end.isoformat()}"
    return value.text_value


def _value_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _day(moment: datetime | None) -> str:
    if moment is None:
        return "a date that was not recorded"
    return _aware(moment).date().isoformat()


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _numbered(noun: str, identifiers: Sequence[int]) -> str:
    if not identifiers:
        return f"no {noun}"
    listed = ", ".join(str(one) for one in identifiers)
    return f"{noun} {listed}" if len(identifiers) == 1 else f"{noun}s {listed}"


def _join(parts: list[str]) -> str:
    if not parts:
        return "nothing"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]
