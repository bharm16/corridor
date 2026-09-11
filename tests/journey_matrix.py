"""Who may do what, in which state, on which route, with what result (#848).

Three of the things the customer-journey audit asks to be guaranteed cannot be
found by searching the code, because each of them is an absence:

- every state a person is shown as actionable has a permitted action, or an
  accountable handoff to somebody who can act;
- every workflow the product claims has a production writer *and* a production
  reader, rather than a writer with no caller;
- every output the product calls approved can be fetched back through the
  product.

A grep proves none of those. "There is no way to finish this" is not a string,
a writer with no caller looks exactly like a writer, and bytes that cannot be
retrieved are bytes that exist. So the inventory is declared here and the
scenarios in ``tests/test_core_journey_acceptance.py`` are run against it: each
row names the step that exercises it, and the checks below refuse a row that
claims a route nothing serves, an actionable state with neither an action nor a
handoff, an approved output nothing retrieves, or a workflow with only half a
production path.

**The words are the product's own.** Roles are the four designations
``corridor.access`` declares, spelled as the product spells them; actions are
the labels on the controls; states are the sentences the sections already
print. Nothing here coins a customer-facing term: a term this inventory needed
and did not have would be a question for the maintainer under the terminology
procedure in ``docs/agents/domain.md``, not a name invented in a test.

**A row with no route is a row for a step the product cannot do yet**, and it
says which ticket owes it. That is the point of committing the inventory before
the tickets land: the empty cells are the work, and they are countable.
"""

from __future__ import annotations

from dataclasses import dataclass


# --- The four designations, in the words the product uses -------------------

COORDINATION = "Project Coordination"
EXTERNAL_RELEASE = "External Release"
TECHNICAL_OPERATIONS = "Technical Operations"
DOCUMENTATION_REVIEW = "Documentation Review"
#: Somebody with no session at all. Not a designation: the state of a person
#: who has not yet signed in, which is where the supported journey starts.
NOT_SIGNED_IN = "Not signed in"
#: Enrolled on the project's roster, holding none of the four designations.
ENROLLED_ONLY = "Enrolled member, no designation"


@dataclass(frozen=True)
class JourneyRow:
    """One role, in one workflow state, doing one thing, with one result.

    ``route`` is ``METHOD /path`` for a route the application serves, and the
    empty string for a step the product cannot perform at all yet -- in which
    case ``owner`` names the ticket that owes it and the scenario step is
    marked expected-to-fail. ``handoff_to`` is for a state where this role may
    not act: it names who can, and ``route`` is the route *they* use.
    """

    role: str
    state: str
    action: str
    route: str
    result: str
    scenario: str
    owner: str
    handoff_to: str = ""
    #: The output this row produces, for the "approved output is retrievable"
    #: question. A produced output must be retrieved by some other row.
    produces: str = ""
    #: The output this row retrieves.
    retrieves: str = ""


#: The supported journey, role by state by action by route by result.
CORE_JOURNEY: tuple[JourneyRow, ...] = (
    JourneyRow(
        role=NOT_SIGNED_IN,
        state="Has an enrolled address and no session",
        action="Request a sign-in link",
        route="POST /sign-in/request",
        result="A single-use link is sent to the enrolled address; the page "
        "says so whether or not the address is enrolled",
        scenario="sign_in",
        owner="#331",
    ),
    JourneyRow(
        role=NOT_SIGNED_IN,
        state="Holds an unconsumed sign-in link",
        action="Open the link",
        route="GET /sign-in/consume",
        result="A session cookie and its request-forgery token are established "
        "and the link cannot be consumed again",
        scenario="sign_in",
        owner="#331",
    ),
    JourneyRow(
        role=ENROLLED_ONLY,
        state="Signed in",
        action="Read the projects this person is enrolled on",
        route="GET /",
        result="Every project on this person's roster, and nothing else",
        scenario="find_the_project",
        owner="#331",
    ),
    JourneyRow(
        role=COORDINATION,
        state="Signed in, coordinating one or more adopted projects",
        action="Read what every coordinated project needs this week",
        route="GET /portfolio",
        result="One derived weekly reading per adopted project",
        scenario="find_the_project",
        owner="#537",
    ),
    JourneyRow(
        role=COORDINATION,
        state="Project provisioned, baseline not adopted",
        action="Read what Corridor needs next to start this project",
        route="",
        result="The onboarding state and the next act, in the product",
        scenario="read_onboarding_state",
        owner="#827",
    ),
    JourneyRow(
        role=COORDINATION,
        state="Project provisioned, baseline not adopted",
        action="Supply the customer's UCM workbook",
        route="POST /projects/{slug}/sources/upload",
        result="One Source Delivery with a receipt, inside the enforced "
        "boundary",
        scenario="supply_the_baseline",
        owner="#823, #824",
    ),
    JourneyRow(
        role=TECHNICAL_OPERATIONS,
        state="A delivery Corridor could not read or could not place",
        action="Repair the mechanical problem under policy",
        route="",
        result="The delivery proceeds, and the repair leaves a receipt the "
        "source register shows",
        scenario="operations_resolves_mechanics",
        owner="#842",
    ),
    JourneyRow(
        role=COORDINATION,
        state="A delivery Corridor could not read or could not place",
        action="None: this is a mechanical failure, not a record decision",
        route="",
        result="The blocked row names Technical Operations as the owner and "
        "the next action",
        scenario="operations_resolves_mechanics",
        owner="#842",
        handoff_to=TECHNICAL_OPERATIONS,
    ),
    JourneyRow(
        role=COORDINATION,
        state="Baseline previewed, material questions open",
        action="Answer the material questions and adopt the baseline",
        route="",
        result="One Adopt Baseline receipt, and the project moves to adopted "
        "baseline operating mode in the same transaction",
        scenario="coordinator_answers_and_adopts",
        owner="#827",
    ),
    JourneyRow(
        role=COORDINATION,
        state="Adopted, issue set not yet approved",
        action="Review and approve the set of artifacts this project issues",
        route="",
        result="One issue profile version, attributable to this person",
        scenario="approve_the_issue_configuration",
        owner="#828",
    ),
    JourneyRow(
        role=COORDINATION,
        state="Adopted, a later UCM revision to submit",
        action="Supply the later revision",
        route="POST /projects/{slug}/sources/upload",
        result="One Source Delivery, captured as a later revision against the "
        "adopted baseline",
        scenario="submit_a_later_revision",
        owner="#823, #824, #825",
    ),
    JourneyRow(
        role=COORDINATION,
        state="A delivery has been received",
        action="Read its receipt and processing state",
        route="GET /projects/{slug}/sources",
        result="Every delivery of every transport, with what processing "
        "produced and what is blocked",
        scenario="see_receipt_and_processing_state",
        owner="#824, #841",
    ),
    JourneyRow(
        role=COORDINATION,
        state="A proposed change cites a source passage",
        action="Read the exact wording at its place in the source",
        route="GET /review/{slug}/source",
        result="The cited passage, in the source, at the locator recorded for "
        "it",
        scenario="inspect_exact_source_context",
        owner="#824, #831",
    ),
    JourneyRow(
        role=COORDINATION,
        state="Review packets open for this reporting cutoff",
        action="Apply, Keep current or Defer the selected changes",
        route="POST /review/{slug}",
        result="One atomic Project Record Revision per act, or a refusal that "
        "wrote nothing",
        scenario="review_routine_changes",
        owner="#526, #528",
    ),
    JourneyRow(
        role=COORDINATION,
        state="A cross-source question is open",
        action="Save the answers for these sources",
        route="POST /review/{slug}/answers",
        result="One answer per source, recorded against the question",
        scenario="review_routine_changes",
        owner="#528",
    ),
    JourneyRow(
        role=COORDINATION,
        state="A capture is wrong at the source",
        action="Report the extraction error",
        route="",
        result="The corrected capture returns through Review, bound to the "
        "source passage that proves it",
        scenario="report_an_extraction_error",
        owner="#832, #836",
    ),
    JourneyRow(
        role=COORDINATION,
        state="A decision was just recorded",
        action="Undo it",
        route="",
        result="The packet receipt names what was decided, and the undo "
        "reverses exactly that act",
        scenario="undo_one_decision",
        owner="#834",
    ),
    JourneyRow(
        role=COORDINATION,
        state="No issue has been prepared for this project yet",
        action="Confirm coverage and prepare issue",
        route="POST /work/{slug}/issue/prepare",
        result="One Release Preparation Request over the reading the page "
        "showed, published for the worker",
        scenario="prepare_the_issue",
        owner="#675, #821",
    ),
    JourneyRow(
        role=COORDINATION,
        state="Preparing this issue",
        action="None: a worker holds the request",
        route="GET /work/{slug}",
        result="The section says a worker holds it, and offers neither a "
        "second preparation nor an approval",
        scenario="worker_prepares_the_candidate",
        owner="#690",
        handoff_to="the Due Work runtime",
    ),
    JourneyRow(
        role=COORDINATION,
        state="Ready for your approval",
        action="Inspect the exact artifacts this candidate holds",
        route="",
        result="The actual bytes of each configured artifact, before approving "
        "them",
        scenario="inspect_the_actual_artifacts",
        owner="#830",
    ),
    JourneyRow(
        role=COORDINATION,
        state="Ready for your approval",
        action="None: preparing an issue is not releasing one",
        route="POST /work/{slug}/issue/authorize",
        result="Refused: the page names External Release as the designation "
        "that approves",
        scenario="approve_as_the_designated_releaser",
        owner="#533, #839",
        handoff_to=EXTERNAL_RELEASE,
    ),
    JourneyRow(
        role=EXTERNAL_RELEASE,
        state="Ready for your approval",
        action="Approve this issue for sharing",
        route="POST /work/{slug}/issue/authorize",
        result="One immutable Release Package bound to the accepted revision "
        "and its predecessor",
        scenario="approve_as_the_designated_releaser",
        owner="#533, #821",
        produces="the approved Release Package",
    ),
    JourneyRow(
        role=COORDINATION,
        state="Approved and sent as this issue",
        action="Download the approved package",
        route="",
        result="Exactly the approved bytes, and nothing that would send them "
        "again",
        scenario="download_the_approved_package",
        owner="#830",
        retrieves="the approved Release Package",
    ),
    JourneyRow(
        role=COORDINATION,
        state="Approved, and a later revision has since arrived",
        action="Retrieve the earlier package",
        route="",
        result="The earlier package unchanged, with its package history in the "
        "Record view",
        scenario="retrieve_the_earlier_package_unchanged",
        owner="#830",
        retrieves="the approved Release Package",
    ),
    JourneyRow(
        role=COORDINATION,
        state="Adopted, reading the record behind a decision",
        action="Read what the record says, what it said before, and who "
        "changed it",
        route="GET /record/{slug}",
        result="The accepted values, the settled proposals and the audit "
        "trail, one step away from the week",
        scenario="review_routine_changes",
        owner="#642",
    ),
    JourneyRow(
        role=ENROLLED_ONLY,
        state="Not a member of the project named in the request",
        action="Request any project-scoped route",
        route="GET /work/{slug}",
        result="Answered exactly as a missing project, so membership cannot be "
        "probed",
        scenario="find_the_project",
        owner="#331",
    ),
)


#: Every workflow the product claims, with the production code that writes it
#: and the production surface that reads it. A workflow with a writer and no
#: reader is the audit's "correspondence writers have no production callers".
@dataclass(frozen=True)
class WorkflowRow:
    """One claimed workflow, its production writer and its production reader."""

    workflow: str
    producer: str
    consumer: str
    scenario: str
    owner: str


CLAIMED_WORKFLOWS: tuple[WorkflowRow, ...] = (
    WorkflowRow(
        workflow="A person signs in",
        producer="corridor.web.auth, from POST /sign-in/request",
        consumer="GET /sign-in/consume, and every route's session gate",
        scenario="sign_in",
        owner="#331",
    ),
    WorkflowRow(
        workflow="A delivery is received and processed",
        producer="",
        consumer="GET /projects/{slug}/sources",
        scenario="see_receipt_and_processing_state",
        owner="#823, #841",
    ),
    WorkflowRow(
        workflow="The baseline is adopted",
        producer="",
        consumer="GET /work/{slug}",
        scenario="coordinator_answers_and_adopts",
        owner="#827",
    ),
    WorkflowRow(
        workflow="A review packet is resolved",
        producer="POST /review/{slug}, through the atomic packet transaction",
        consumer="GET /review/{slug} and GET /record/{slug}",
        scenario="review_routine_changes",
        owner="#526",
    ),
    WorkflowRow(
        workflow="An issue is prepared",
        producer="corridor.release_preparation_supervisor, claimed through the "
        "Due Work handler registry",
        consumer="GET /work/{slug}, the Issue section",
        scenario="worker_prepares_the_candidate",
        owner="#690",
    ),
    WorkflowRow(
        workflow="An issue is approved for sharing",
        producer="POST /work/{slug}/issue/authorize",
        consumer="GET /work/{slug}, the Issue section",
        scenario="approve_as_the_designated_releaser",
        owner="#533",
    ),
    WorkflowRow(
        workflow="The approved package is delivered to the person who asked "
        "for it",
        producer="POST /work/{slug}/issue/authorize",
        consumer="",
        scenario="download_the_approved_package",
        owner="#830",
    ),
)


#: Correspondence recording (#837), the first *selected* capability in this
#: inventory. #652's maintainer decision classifies it exactly that way: it is
#: "required when the partner's configured workflow or measured chase process
#: includes response tracking" and explicitly "not a blocker for UCM
#: compatibility intake, deterministic shadow processing, or a pilot scoped to
#: manually managed correspondence". So it is not a step of the core journey
#: every customer walks -- putting it there would claim every pilot needs it --
#: and it is not omitted either, because a capability nobody exercises is
#: exactly what #848 exists to find. It is a declared extension, exercised by
#: its own scenario over the same harness, and held to the same three
#: questions: every actionable state has an act, every claimed workflow has
#: both halves, and every row names a route the product serves.
SELECTED_CAPABILITIES: tuple[JourneyRow, ...] = (
    JourneyRow(
        role=COORDINATION,
        state="A Follow-up Plan is recorded and the coordinator has written "
        "to the External Organization from their own mail client",
        action="Record what was sent, against the plans it advanced",
        route="POST /work/{slug}/follow-up/sent",
        result="The message, its digest and the plans it named are retained, "
        "with an explicit expected-response date the person confirmed",
        scenario="record_what_was_sent",
        owner="#837",
        produces="retained outgoing request",
    ),
    JourneyRow(
        role=COORDINATION,
        state="A request is retained and somebody asks what was actually sent",
        action="Read the exact message back",
        route="GET /work/{slug}/follow-up/sent/{request_id}",
        result="The retained content, verified against its digest on the way "
        "out; never the digest in place of the message",
        scenario="record_what_was_sent",
        owner="#837",
        retrieves="retained outgoing request",
    ),
    JourneyRow(
        role=COORDINATION,
        state="A retained request is past the expected-response date and "
        "nothing has been recorded back",
        action="None: there is nothing to record until somebody replies",
        route="GET /work/{slug}",
        result="The follow-up carries the no-response finding, which exists "
        "only because a request and a boundary are both retained (ADR-0090)",
        scenario="the_boundary_passes",
        owner="#837",
        handoff_to=COORDINATION,
    ),
    JourneyRow(
        role=COORDINATION,
        state="Something has come back from the External Organization",
        action="Record the response and what it was read from",
        route="POST /work/{slug}/follow-up/response",
        result="The no-response finding stops; the Follow-up Plan, the "
        "Proposed Delta and the accepted record are unchanged",
        scenario="record_what_came_back",
        owner="#837",
    ),
    JourneyRow(
        role=COORDINATION,
        state="A recorded request or response was wrong",
        action="Record a corrected one naming the original and why",
        route="POST /work/{slug}/follow-up/sent",
        result="Both records stay readable and the corrected one is the record "
        "in force; nothing is rewritten",
        scenario="correct_a_mistaken_record",
        owner="#837",
    ),
    JourneyRow(
        role=ENROLLED_ONLY,
        state="A reply is recorded and the record question is still open",
        action="None: recording a reply settles nothing",
        route="POST /review/{slug}/answers",
        result="The question is settled on the review screen, by a decision "
        "that writes a Project Record revision -- not by the reply",
        scenario="record_what_came_back",
        owner="#837, #835",
        handoff_to=COORDINATION,
    ),
)


#: The workflows the selected capability claims, held to #848's "a writer with
#: no caller looks exactly like a writer" question. #652 shipped exactly that
#: and this is what closes it, so the row is the point rather than a formality.
SELECTED_CAPABILITY_WORKFLOWS: tuple[WorkflowRow, ...] = (
    WorkflowRow(
        workflow="An outgoing request is retained",
        producer="POST /work/{slug}/follow-up/sent",
        consumer="GET /work/{slug}, the Follow-up section, and "
        "GET /work/{slug}/follow-up/sent/{request_id} for the content",
        scenario="record_what_was_sent",
        owner="#837",
    ),
    WorkflowRow(
        workflow="A received response is recorded",
        producer="POST /work/{slug}/follow-up/response",
        consumer="GET /work/{slug}, the Follow-up section, and the "
        "no-response band that stops firing",
        scenario="record_what_came_back",
        owner="#837",
    ),
)


#: How a row says this role may not act here. The sentence after it is the
#: reason, and the row then has to name who can act instead.
NO_PERMITTED_ACTION = "None:"


def rows_without_an_act(
    rows: tuple[JourneyRow, ...] = CORE_JOURNEY,
) -> tuple[JourneyRow, ...]:
    """Rows whose state offers neither a permitted action nor a handoff.

    An actionable state with nothing behind it is the audit's "some work is
    presented with no way to complete it", and it is the one thing a row can
    say that no amount of reading the code would reveal. A row that begins its
    action with "None:" is declaring that this role may not act here, which is
    allowed only when the row also names the role that can.

    A row with an action and no route yet is *not* stranded: the act exists
    and the product cannot perform it, which is a ticket, and the check that a
    missing route matches a step still waiting on one is separate.
    """

    return tuple(
        row
        for row in rows
        if not row.action.strip()
        or (row.action.strip().startswith(NO_PERMITTED_ACTION) and not row.handoff_to)
    )


def unretrieved_outputs(
    rows: tuple[JourneyRow, ...] = CORE_JOURNEY,
) -> tuple[str, ...]:
    """Outputs some row produces that no row retrieves, in order."""

    produced = {row.produces for row in rows if row.produces}
    retrieved = {row.retrieves for row in rows if row.retrieves}
    return tuple(sorted(produced - retrieved))


def half_built_workflows(
    rows: tuple[WorkflowRow, ...] = CLAIMED_WORKFLOWS,
) -> tuple[tuple[str, str], ...]:
    """Each claimed workflow missing a production writer or reader, and which.

    Returned rather than asserted, because today several of them *are* half
    built and the ticket that finishes each one is on the row. The guard is
    that the set may not grow without the ticket to go with it.
    """

    missing = []
    for row in rows:
        if not row.producer.strip():
            missing.append((row.workflow, "no production writer"))
        if not row.consumer.strip():
            missing.append((row.workflow, "no production reader"))
    return tuple(missing)
