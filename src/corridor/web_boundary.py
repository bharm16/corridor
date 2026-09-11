"""The live-pilot web capability boundary: which routes run, and on what (#680).

#531 made the project a data partition for four spine relations and #657
finished the *inventory*: every one of the 191 relations ``corridor_web`` can
read carries a recorded answer to "how does the partition cover this", and a
ratchet refuses a new relation that carries none.  What #657 explicitly did
not do is turn that inventory into a boundary.  130 relations stayed
classified ``NOT_YET_PARTITIONED`` and stayed directly selectable, so
``select * from work_decisions where id = 41`` still answered with whatever
project owned row 41.

The rejected fix was a loop putting row-level security on all 127
``project_id`` relations.  It would break machine ingress, customer-wide
registries, authorization inputs, legacy compatibility routes and parentless
child relations, and it would break them *silently*, because every web test
overrides the session with the schema owner's, and the schema owner bypasses
row-level security.  A green suite would have proved nothing.

So the boundary is drawn where the product actually is.  The live pilot
enables the routes below and nothing else.  The relations those routes read
are either partitioned, an input the partition itself is derived from,
answered by a partitioned relation underneath, or genuinely the whole
customer database's.  **Every other unpartitioned relation is revoked from
``corridor_web`` outright** — not filtered, not policed, revoked — so a route
that quietly starts reading one fails loudly in production instead of
returning another customer's rows.

Three consequences are deliberate and worth stating out loud.

- **The revoke is unconditional; the route refusal is declared.**  The
  database half ships in the migration for every deployment, because a
  capability that can still read a relation is not denied it whatever the
  application believes.  The route half is
  ``settings.live_pilot_web_boundary``, because a legacy development
  deployment still runs the frozen surfaces (ADR-0081) and reaches them by
  pointing ``WEB_DATABASE_URL`` at the opt-in ``corridor_legacy_dev`` login,
  which keeps the blanket read the schema dump grants it.  The two halves are
  bound by ``tests/test_architecture.py``: a route may be enabled only if
  every relation recorded for it survives the revoke.  #694 finished the
  declared half: "declared" is now a three-valued state rather than a
  boolean, so the deployment that has the revoke and not the flag refuses
  before the handler instead of letting PostgreSQL answer.  See the second
  block below.

- **Machine traffic is not human traffic.**  ``/intake/inbound`` and
  ``/health`` carry no person, no membership and therefore no partition.
  They now take the operations capability's session rather than the human
  web role's, which is what makes it safe to partition ``documents`` and
  ``source_deliveries`` at last — #657 recorded that partitioning them would
  refuse a working ingress, and this removes that objection rather than
  overruling it.

- **A legacy project is refused by the enabled route, not served badly by
  it.**  ``/work/{slug}`` renders ADR-0035's item-per-record Work List for a
  project that has not adopted a baseline, and that list reads
  ``dependencies``, ``work_decisions``, ``evidence_links`` and the dispute
  tables — every one of them revoked.  Under the boundary the route refuses
  the legacy project by name instead of failing halfway through a template
  with a permission error.

#693 closed the last hole in that claim.  A revoke aimed at
``corridor_web`` leaves a grant to PUBLIC standing, so three relations stayed
readable by every role in the database — present and future — while every
capability list said otherwise.  The rule is now general and lives below as
``PUBLIC_RELATION_PRIVILEGES``: no application table or view carries a
privilege granted to PUBLIC, the allowlist of intentional ones is empty, and
the migration sweeps the catalog rather than naming relations.

``PILOT_ROUTE_RELATIONS`` was not written from memory.  It was recorded from
an instrumented run of the route tests, keyed on the ``FROM``/``JOIN``/
``INTO``/``UPDATE`` targets of every statement issued while each request was
in flight, with the legacy-project cases excluded from ``/work/{slug}``.  It
is a *record* of what was observed, so the live proof is still the real-login
test that drives these routes as ``corridor_web`` itself.

#824 admitted the deterministic intake path -- upload, preview, confirmation,
the source register and the Review source link -- and recorded its relations
the same way, from an instrumented run of both the workbook and the PDF case,
the refusal and the quarantine.  That reading is what found the six relations
this change had to partition rather than assume: confirmation registered *and
parsed* the file inside the request, so the pages, the token layers, the
render derivatives and the Class B receipts a parse writes were the
confirmation's relations too, and the register derives its honest processing
outcome from the extraction runs and the quarantine.  None of the six carries
a ``project_id``, except ``processing_artifacts``; the other five are
partitioned through the ``documents`` row each of them names.

#893 removed the reason four of those six were the confirmation's relations at
all.  The read left the request for the standing project-processing pass, and
a fresh instrumented walk of the intake path names no ``doc_pages``, no
``token_layers``, no ``page_render_derivatives``, no ``processing_artifacts``
and no ``source_segments``.  The partition stays on all of them -- a relation
does not stop belonging to one project because one route stopped writing it --
but the writes go, through ``WRITE_DENIED_RELATIONS`` below.  That is the one
revoke in this boundary aimed at a privilege rather than a relation, and the
reason it is narrower is written there.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from sqlalchemy.engine import make_url

from corridor.config import settings
from corridor.db_roles import LEGACY_DEV_ROLE, WEB_CAPABILITY_LOGIN
from corridor import access


@dataclass(frozen=True)
class PilotRoute:
    """One enabled route, and the relations it was observed to reach."""

    why: str
    relations: frozenset[str]


PILOT_ROUTES: dict[tuple[str, str], PilotRoute] = {
    ("POST", "/projects/{slug}/contacts/{contact_id}/correct"): PilotRoute(
        why="#562 an authenticated, project-scoped onboarding correction",
        relations=frozenset({"projects", "project_roster_entries", "project_contacts", "project_contact_imports",
                             "current_project_record", "external_orgs"}),
    ),
    ("GET", "/readyz"): PilotRoute(
        why=(
            "the load balancer's readiness probe. It resolves no principal "
            "and names no project: `serving_report` reaches the database "
            "only through `select 1` and the object store through its own "
            "probe, so it needs no relation the revoke takes away. It has "
            "to be enabled -- an enforced deployment answers 404 on an "
            "unlisted route, and a 404 from the target group deregisters "
            "every web task and takes the environment down"
        ),
        relations=frozenset(),
    ),
    ("GET", "/health"): PilotRoute(
        why=(
            "the platform probe. It resolves no principal and names no "
            "project, and #680 moved it onto the operations capability, "
            "so the human web role holds none of the due-work relations "
            "it reads"
        ),
        relations=frozenset(),
    ),
    ("GET", "/"): PilotRoute(
        why=(
            "the signed-in person's own project list, read inside the "
            "member partition"
        ),
        relations=frozenset(
            {
                "project_roster_entries",
                "projects",
                "web_sessions",
            }
        ),
    ),
    ("GET", "/sign-in"): PilotRoute(
        why=(
            "the sign-in form; identity relations only"
        ),
        relations=frozenset(
            {
                "web_sessions",
            }
        ),
    ),
    ("POST", "/sign-in/request"): PilotRoute(
        why=(
            "mints a magic link; identity relations only"
        ),
        relations=frozenset(
            {
                "person_identities",
                "sign_in_attempts",
                "sign_in_tokens",
            }
        ),
    ),
    ("GET", "/sign-in/consume"): PilotRoute(
        why=(
            "spends a magic link; identity relations only"
        ),
        relations=frozenset(
            {
                "audit_log",
                "person_identities",
                "sign_in_attempts",
                "sign_in_tokens",
                "web_sessions",
            }
        ),
    ),
    ("POST", "/sign-out"): PilotRoute(
        why=(
            "revokes this session; identity relations only"
        ),
        relations=frozenset(
            {
                "audit_log",
                "person_identities",
                "web_sessions",
            }
        ),
    ),
    ("GET", "/portfolio"): PilotRoute(
        why=(
            "#537 the coordinator's projects, in the member partition"
        ),
        relations=frozenset(
            {
                "delta_deferrals",
                "delta_dispositions",
                "delta_follow_up_plans",
                "delta_review_packet_children",
                "delta_review_packet_reversals",
                "delta_supersessions",
                "documents",
                "fact_decisions",
                "project_baseline_adoptions",
                "project_baseline_format_manifests",
                "project_baseline_formats",
                "project_baseline_source_rows",
                "project_issue_profile_artifacts",
                "project_issue_profiles",
                "project_record_revisions",
                "project_roster_entries",
                "projects",
                "proposed_deltas",
                "release_candidates",
                "release_packages",
                "release_preparation_requests",
            }
        ),
    ),
    ("GET", "/work/{slug}"): PilotRoute(
        why=(
            "#536 one adopted project's ordered week"
        ),
        relations=frozenset(
            {
                "audit_log",
                "candidates",
                "delta_deferrals",
                "delta_dispositions",
                "delta_follow_up_plan_closures",
                "delta_follow_up_plan_evidence",
                "delta_follow_up_plans",
                "delta_groups",
                "delta_review_packet_children",
                "delta_review_packet_reversals",
                "delta_supersessions",
                "dependency_events",
                "documents",
                "extracted_proposals",
                "fact_applies_to",
                "fact_closure_results",
                "fact_closure_sources",
                "fact_decisions",
                "fact_sources",
                "fact_statement_timings",
                "facts",
                "issue_coverage_declarations",
                # #652's retained correspondence, read by the chase list's
                # no-response band, and since #837 by the follow-up section
                # that shows what was sent and what came back.
                "outgoing_request_plans",
                "outgoing_request_responses",
                "outgoing_requests",
                "project_baseline_format_manifests",
                "project_baseline_formats",
                "project_baseline_source_rows",
                "project_issue_profile_artifacts",
                "project_issue_profiles",
                "project_record_revisions",
                "project_roster_entries",
                "projects",
                "proposed_deltas",
                "release_candidate_artifacts",
                "release_candidates",
                "release_packages",
                "release_preparation_attempts",
                "release_preparation_requests",
                "source_deliveries",
                "source_segments",
                # #831 the follow-up bundle's citations, resolved to the exact
                # passages each Support Assessment recorded, so a bundle links
                # to the source rather than printing an assessment id. Read
                # beside `delta_follow_up_plan_evidence`, in the same call.
                "support_assessment_sources",
                "support_assessments",
            }
        ),
    ),
    ("POST", "/work/{slug}/issue/prepare"): PilotRoute(
        why=(
            "#675, #529 prepare the release candidate"
        ),
        relations=frozenset(
            {
                "audit_log",
                "candidates",
                "delta_groups",
                "documents",
                "extracted_proposals",
                "fact_applies_to",
                "fact_closure_results",
                "fact_closure_sources",
                "fact_decisions",
                "fact_statement_timings",
                "facts",
                "issue_coverage_declarations",
                "project_baseline_format_manifests",
                "project_baseline_formats",
                "project_baseline_source_rows",
                "project_issue_profile_artifacts",
                "project_issue_profiles",
                "project_record_revisions",
                "project_roster_entries",
                "projects",
                "proposed_deltas",
                "release_candidates",
                "release_preparation_attempts",
                "release_preparation_requests",
                "source_deliveries",
            }
        ),
    ),
    ("POST", "/work/{slug}/issue/authorize"): PilotRoute(
        why=(
            "#533 authorize the customer issue"
        ),
        relations=frozenset(
            {
                "audit_log",
                "candidates",
                "delta_deferrals",
                "delta_dispositions",
                "delta_follow_up_plans",
                "delta_groups",
                "delta_review_packet_children",
                "delta_review_packet_reversals",
                "delta_supersessions",
                "dependency_events",
                "documents",
                "extracted_proposals",
                "fact_applies_to",
                "fact_closure_results",
                "fact_closure_sources",
                "fact_decisions",
                "fact_sources",
                "fact_statement_timings",
                "facts",
                "issue_coverage_declarations",
                "project_baseline_format_manifests",
                "project_baseline_formats",
                "project_baseline_source_rows",
                "project_issue_profile_artifacts",
                "project_issue_profiles",
                "project_record_revisions",
                "project_roster_entries",
                "projects",
                "proposed_deltas",
                "release_candidate_artifacts",
                "release_candidates",
                "release_packages",
                "release_preparation_requests",
                "source_deliveries",
                "source_segments",
                "support_assessments",
            }
        ),
    ),
    (
        "GET",
        "/work/{slug}/issue/candidates/{candidate_id}/artifacts/{artifact_type}",
    ): PilotRoute(
        why=(
            "#830 read back the exact bytes one prepared candidate retained. "
            "It resolves the candidate within the project and the artifact "
            "within the candidate, so it reaches the candidate's own rows and "
            "the membership gate and nothing else"
        ),
        relations=frozenset(
            {
                "projects",
                "project_roster_entries",
                "release_candidates",
                "release_candidate_artifacts",
            }
        ),
    ),
    (
        "GET",
        "/work/{slug}/issue/packages/{issue_number}/artifacts/{artifact_type}",
    ): PilotRoute(
        why=(
            "#830 read back the exact bytes one approved issue sealed"
        ),
        relations=frozenset(
            {
                "projects",
                "project_roster_entries",
                "release_packages",
                "release_package_artifacts",
            }
        ),
    ),
    ("GET", "/work/{slug}/issue/packages/{issue_number}/bundle"): PilotRoute(
        why=(
            "#830 read back one approved issue as the single set ADR-0086 "
            "makes it"
        ),
        relations=frozenset(
            {
                "projects",
                "project_roster_entries",
                "release_packages",
                "release_package_artifacts",
            }
        ),
    ),
    # --- #824 The deterministic intake path ------------------------------
    #
    # A coordinator opens the source register from the Work or Record page, hands
    # Corridor one file, reads what registering it would record, confirms it,
    # and follows a Review row back to the source it came from. Every one of
    # those routes existed and none was listed, so an enforcing deployment
    # answered 404 to links its own enabled pages printed.
    #
    # What stays outside is the optional model-assisted draft — the draft
    # configuration, the draft request and the draft read-back. They spend
    # model budget under their own declared authority and submitting a UCM
    # needs none of them, so the pilot does not serve them and the preview
    # page does not offer a control this deployment would refuse.
    #
    # #825 added what a later revision of the registered workbook has to
    # declare, so the preview and the confirmation both read the project's
    # operating mode and its registered field mapping — the adoption receipt
    # the mode is derived from, the format registration, and the declaration
    # stored beside it — and the confirmation appends the one declaration row
    # the processing pass later routes on.
    ("GET", "/projects/{slug}/sources/upload"): PilotRoute(
        why=(
            "#349 the upload fallback's form (ADR-0058). Authorization: an "
            "authenticated person holding this project's coordination "
            "designation, proved by `_project` before the page is drawn; it "
            "reads the registry and the roster and nothing else"
        ),
        relations=frozenset(
            {
                "project_roster_entries",
                "projects",
            }
        ),
    ),
    ("POST", "/projects/{slug}/sources/upload"): PilotRoute(
        why=(
            "#349, #823 takes delivery of the bytes and draws the read-only "
            "preview. Authorization: the same coordination designation, and "
            "the delivery it appends names the authenticated person as the "
            "transport's own authentication. It asks after the optional "
            "model-assisted draft only where the deployment serves that "
            "route, so on an enforcing one it never reads "
            "`source_intake_draft_configurations` -- which this boundary "
            "revokes"
        ),
        relations=frozenset(
            {
                "customer_environment_binding",
                "documents",
                "project_baseline_adoptions",
                "project_baseline_format_manifests",
                "project_baseline_formats",
                "project_roster_entries",
                "projects",
                "source_deliveries",
            }
        ),
    ),
    ("POST", "/projects/{slug}/sources/confirm"): PilotRoute(
        why=(
            "#349, #823 registers the previewed source and records the one "
            "attributable confirmation. Authorization: the same coordination "
            "designation, re-proved against the delivery, these bytes and "
            "this project before anything is written. #824 measured it also "
            "*reading* the file here -- 19 seconds for a forty-page PDF -- so "
            "the page family a read writes was recorded as this route's too. "
            "#893 moved that read to the standing project-processing pass, "
            "and this set is the instrumented walk of the route after it: no "
            "`doc_pages`, no `token_layers`, no `page_render_derivatives`, no "
            "`processing_artifacts` and no `source_segments`. "
            "`document_quarantines` stays because a `schedule` upload is "
            "registered deliberately unread (#149) in the request that "
            "registers it"
        ),
        relations=frozenset(
            {
                "audit_log",
                "customer_environment_binding",
                "document_quarantines",
                "documents",
                "project_roster_entries",
                "projects",
                "project_baseline_adoptions",
                "project_baseline_format_manifests",
                "project_baseline_formats",
                "source_deliveries",
                "source_delivery_confirmations",
                "source_revision_declarations",
            }
        ),
    ),
    ("GET", "/projects/{slug}/sources"): PilotRoute(
        why=(
            "#349, #841 the source register: every delivery this project has "
            "received, whatever transport carried it, and what became of it. "
            "Authorization: the same coordination designation. Its spine is "
            "the ADR-0089 ledger and the admissions recorded against it, so a "
            "refused or unconfirmed delivery is a row rather than an absence; "
            "the processing state is derived from the extraction runs and the "
            "quarantine rather than stored; and what a reading produced is "
            "counted from the Source Facts and the Proposed Deltas its "
            "document's delta groups carry, with the open ones read through "
            "the same supersession and disposition partition the Review "
            "screen reads. A parsed source with no run is only *waiting* for "
            "a pass that would take it, so `extractable_document` is asked of "
            "the document, and the operating mode it consults for a minutes "
            "source is why `project_baseline_adoptions` is read. #841 had it "
            "read no `audit_log`, because the confirmation it used to scope "
            "itself by is now the delivery's own; #842 gave it one reading "
            "there again and a different one -- the repair receipt operations "
            "leaves against a blocked source, selected by this project's own "
            "document ids, so a row that names an owner also says what has "
            "been done about it"
        ),
        relations=frozenset(
            {
                "audit_log",
                "delta_dispositions",
                "delta_groups",
                "delta_supersessions",
                "document_quarantines",
                "documents",
                "extraction_runs",
                "facts",
                "project_baseline_adoptions",
                "project_roster_entries",
                "projects",
                "proposed_deltas",
                "source_deliveries",
                "source_delivery_confirmations",
            }
        ),
    ),
    ("GET", "/review/{slug}/source"): PilotRoute(
        why=(
            "#528 the source link the authorized packet reading already "
            "offered. Authorization: an active member of this project; no "
            "designation, because the reading it follows needs none. No URL "
            "comes from the query -- the retained child and its immutable "
            "baseline source row are resolved inside the project partition, "
            "and an off-partition id answers the same 404 a missing one does"
        ),
        relations=frozenset(
            {
                "customer_environment_binding",
                "project_baseline_source_rows",
                "project_roster_entries",
                "projects",
                "proposed_deltas",
            }
        ),
    ),
    ("GET", "/record/{slug}"): PilotRoute(
        why=(
            "#642 the read-only record and history investigation"
        ),
        relations=frozenset(
            {
                "current_coordination_record",
                "coordination_record_subjects",
                "coordination_subject_lineage",
                "coordination_record_decisions",
                "coordination_record_reversals",
                "coordination_history_activations",
                "coordination_decision_lineage",
                "legacy_history_batches",
                "legacy_history_reversals",
                "support_scope_lineage",
                "support_history_receipts",
                "audit_log",
                "candidates",
                "current_project_record",
                "delta_deferrals",
                "delta_dispositions",
                "delta_record_decisions",
                "delta_review_packet_children",
                "delta_review_packet_receipts",
                "delta_review_packet_reversals",
                "delta_supersessions",
                "documents",
                "external_report_artifacts",
                "external_report_releases",
                "extracted_proposals",
                "fact_applies_to",
                "fact_closure_results",
                "fact_closure_sources",
                "fact_decisions",
                "fact_sources",
                "fact_statement_timings",
                "facts",
                "project_baseline_source_rows",
                "project_record_revisions",
                "project_roster_entries",
                "projects",
                "proposed_deltas",
                # #830: the approved packages the same view now lists beside
                # the legacy report releases above.
                "release_package_artifacts",
                "release_packages",
                "source_segments",
                "support_assessment_sources",
                "support_assessments",
            }
        ),
    ),
    ("GET", "/review/{slug}"): PilotRoute(
        why=(
            "#527, #528 the Review Packet reading"
        ),
        relations=frozenset(
            {
                "delta_deferrals",
                "delta_dispositions",
                "delta_groups",
                "delta_review_packet_children",
                "delta_review_packet_reversals",
                "delta_supersessions",
                "dependency_events",
                "documents",
                "fact_decisions",
                "fact_sources",
                "facts",
                "issue_coverage_declarations",
                "project_baseline_format_manifests",
                "project_baseline_formats",
                "project_baseline_source_rows",
                "project_issue_profile_artifacts",
                "project_issue_profiles",
                "project_record_revisions",
                "project_roster_entries",
                "projects",
                "proposed_deltas",
                "source_segments",
                "support_assessments",
            }
        ),
    ),
    ("POST", "/review/{slug}"): PilotRoute(
        why=(
            "#526 resolve one Proposed Delta"
        ),
        relations=frozenset(
            {
                "delta_deferrals",
                "delta_dispositions",
                "delta_groups",
                "delta_review_packet_children",
                "delta_review_packet_reversals",
                "delta_supersessions",
                "dependency_events",
                "documents",
                "fact_decisions",
                "fact_sources",
                "facts",
                "issue_coverage_declarations",
                "project_baseline_formats",
                "project_baseline_source_rows",
                "project_issue_profiles",
                "project_record_revisions",
                "project_roster_entries",
                "projects",
                "proposed_deltas",
                "source_segments",
                "support_assessments",
            }
        ),
    ),
    ("GET", "/review/{slug}/packet/{receipt_id}"): PilotRoute(
        why=(
            "#834 read back one recorded Review Packet act and what each of "
            "its children became"
        ),
        relations=frozenset(
            {
                "delta_review_packet_children",
                "delta_review_packet_receipts",
                "delta_review_packet_reversals",
                "project_baseline_source_rows",
                "project_roster_entries",
                "projects",
                "proposed_deltas",
            }
        ),
    ),
    ("POST", "/review/{slug}/packet/{receipt_id}/undo"): PilotRoute(
        why=(
            "#834 compensate for one recorded Review Packet act through #526's "
            "own reversal command. The compensating revision and its decisions "
            "are written by `reverse_review_packet` as its own owner (#492), "
            "so the human web capability reaches the same relations here as "
            "the reading beside it and no revision relation of its own"
        ),
        relations=frozenset(
            {
                "delta_review_packet_children",
                "delta_review_packet_receipts",
                "delta_review_packet_reversals",
                "project_baseline_source_rows",
                "project_roster_entries",
                "projects",
                "proposed_deltas",
            }
        ),
    ),
    ("GET", "/sources/{slug}/passage/{segment_id}"): PilotRoute(
        why=(
            "#831 the exact-source view every citation opens: one project's "
            "cited passage, its Document Revision and rendition, and the "
            "neighbouring Source Segments that are its nearby context. It "
            "reads no `doc_pages` and no `page_render_derivatives` — both are "
            "revoked — so the context is the spine's own segments, which is "
            "also what makes a boundary-admitted reading possible"
        ),
        relations=frozenset(
            {
                "documents",
                "project_roster_entries",
                "projects",
                "source_segments",
            }
        ),
    ),
    ("GET", "/sources/{slug}/document/{document_id}/original"): PilotRoute(
        why=(
            "#831 the registered original behind that view, served to a "
            "member of the owning project. The legacy `/page-image` route "
            "stays out: it serves a derived rendition addressed by document "
            "id alone and no pilot template uses it"
        ),
        relations=frozenset(
            {
                "documents",
                "project_roster_entries",
                "projects",
            }
        ),
    ),
    ("GET", "/issue-configuration/{slug}"): PilotRoute(
        why=(
            "#828 read what this project is configured to externally issue, "
            "its version history, and the change a person is preparing"
        ),
        relations=frozenset(
            {
                "project_baseline_format_manifests",
                "project_baseline_formats",
                "project_issue_profile_artifacts",
                "project_issue_profiles",
                "project_record_revisions",
                "project_roster_entries",
                "projects",
                "release_candidates",
                "release_packages",
                "web_sessions",
            }
        ),
    ),
    ("POST", "/issue-configuration/{slug}/approve"): PilotRoute(
        why=(
            "#828 approve the next version of the configured issue set; the "
            "write itself is the record-decision command's, not this role's"
        ),
        relations=frozenset(
            {
                "project_baseline_format_manifests",
                "project_baseline_formats",
                "project_issue_profile_artifacts",
                "project_issue_profiles",
                "project_record_revisions",
                "project_roster_entries",
                "projects",
                "release_candidates",
                "release_packages",
                "web_sessions",
            }
        ),
    ),
    ("GET", "/template-and-mapping/{slug}"): PilotRoute(
        why=(
            "#829 read the output template and field mapping this project "
            "renders through, every registration it has made, and a "
            "replacement somebody prepared -- which arrives as a link, "
            "because the offered bytes are content-addressed and no schema "
            "holds a proposal"
        ),
        relations=frozenset(
            {
                "project_baseline_format_manifests",
                "project_baseline_formats",
                "project_issue_profile_artifacts",
                "project_issue_profiles",
                "project_record_revisions",
                "project_roster_entries",
                "projects",
                "release_candidates",
                "release_packages",
                "web_sessions",
            }
        ),
    ),
    ("POST", "/template-and-mapping/{slug}/validate"): PilotRoute(
        why=(
            "#829 the Corridor operations reading of an offered replacement: "
            "importer mechanics, and whether the file is the mapping revision "
            "the project registered. It registers nothing and the only thing "
            "it writes is the offered bytes, retained under their own digest"
        ),
        relations=frozenset(
            {
                "project_baseline_format_manifests",
                "project_baseline_formats",
                "project_issue_profile_artifacts",
                "project_issue_profiles",
                "project_record_revisions",
                "project_roster_entries",
                "projects",
                "release_candidates",
                "release_packages",
                "web_sessions",
            }
        ),
    ),
    ("POST", "/template-and-mapping/{slug}/register"): PilotRoute(
        why=(
            "#829 register the replacement output template or mapping "
            "revision; the write itself is the record-decision command's, not "
            "this role's, and no accepted value moves"
        ),
        relations=frozenset(
            {
                "project_baseline_format_manifests",
                "project_baseline_format_objects",
                "project_baseline_formats",
                "project_issue_profile_artifacts",
                "project_issue_profiles",
                "project_record_revisions",
                "project_roster_entries",
                "projects",
                "release_candidates",
                "release_packages",
                "web_sessions",
            }
        ),
    ),
    ("POST", "/review/{slug}/answers"): PilotRoute(
        why=(
            "#526 record the Review Packet answers"
        ),
        relations=frozenset(
            {
                "delta_deferrals",
                "delta_dispositions",
                "delta_groups",
                "delta_review_packet_children",
                "delta_review_packet_reversals",
                "delta_supersessions",
                "dependency_events",
                "documents",
                "fact_decisions",
                "fact_sources",
                "facts",
                "issue_coverage_declarations",
                "project_baseline_formats",
                "project_baseline_source_rows",
                "project_issue_profiles",
                "project_record_revisions",
                "project_roster_entries",
                "projects",
                "proposed_deltas",
                "source_segments",
                "support_assessments",
            }
        ),
    ),
}


# The relations the human web capability keeps: everything the classification
# answers with something other than "not yet partitioned". After #680 that is
# exactly the set the pilot routes can reach, because every remaining
# unpartitioned relation is revoked.
PROTECTED_RELATIONS: frozenset[str] = frozenset(
    set(access.PARTITIONED_RELATIONS)
    | set(access.AUTHORIZATION_INPUT_RELATIONS)
    | set(access.PROTECTED_RELATIONS)
    | set(access.CUSTOMER_WIDE_RELATIONS)
)

# What the migration takes away from ``corridor_web``. Derived rather than
# listed a second time: one list of holes, and the boundary is that the web
# capability holds no privilege on any of them.
DENIED_RELATIONS: frozenset[str] = frozenset(access.NOT_YET_PARTITIONED_RELATIONS)

# The narrower revoke, and the only one in this boundary that takes a
# privilege rather than a relation (#893).
#
# #824 partitioned these four and left the schema owner's default grant --
# select, insert, update and delete -- standing on all of them, because the
# confirmation route *wrote* every one: it rendered and parsed the uploaded
# file inside the web request. #893 moved that read to the standing pass, so
# the route writes none of them and the recorded set above no longer names
# them. What that leaves is a human web capability that may still insert a
# page's text, update a Class B receipt or delete a token layer on its own
# project's sources -- privileges nothing asks for, on exactly the rows a
# citation is later replayed against. Row-level security answers *whose* rows
# a capability reaches; it does not answer what a capability should be able to
# do to them, and #492 already drew that second line for the source tables.
#
# The reading stays. Each is partitioned, a human review surface reads a page
# and a render, and taking SELECT from these four alone would state a rule
# this boundary does not hold about the ninety-odd other partitioned relations
# no enabled route reads.
WRITE_DENIED_RELATIONS: frozenset[str] = frozenset(
    {
        "doc_pages",
        "page_render_derivatives",
        "processing_artifacts",
        "token_layers",
    }
)


# --- #693 Nothing is readable by decision of nobody ------------------------
#
# #680's revoke was aimed at one named login, and for one relation it reported
# success while the relation stayed readable.  `subject_resolution_decisions`
# also carried `GRANT SELECT ... TO PUBLIC`, left behind by the command role
# that created it.  A `REVOKE ... FROM corridor_web` does not touch a PUBLIC
# grant, and `has_table_privilege('corridor_web', ...)` — which is what every
# list-versus-catalog check above asks — answers *true* through PUBLIC without
# distinguishing where the privilege came from.  So the boundary read as
# complete and was not.
#
# That is why the assertion that matters is `select * from <relation>` as a
# real login, and why the lists below exist as well: a static reading of the
# explicit `GRANT ... TO corridor_web` statements is exactly the check that
# missed this.
#
# **The general rule, replacing the one-off list.**  No application table or
# view in `public` carries any privilege granted to PUBLIC.  The migration
# does not name relations to fix; it sweeps the catalog and revokes whatever
# it finds outside this allowlist, so `subject_resolution_decisions`,
# `fact_decisions` and `project_record_revisions` are governed by the same
# rule as a relation added next year rather than by a hand list somebody has
# to remember to extend.
#
# **What is not in scope here, said out loud.**  PostgreSQL grants `USAGE` on
# the `public` *schema* to PUBLIC by default, and grants `EXECUTE` on a
# function to PUBLIC unless the function's ACL says otherwise.  The schema
# grant is what makes a named table grant reachable at all and is not a DML
# privilege on a relation.  The function default is real, but every
# `SECURITY DEFINER` command in this database already carries an explicit ACL
# with no PUBLIC entry — the narrow execution grants #492 and #531 wrote — and
# the remaining functions run with the caller's own rights.  Changing either
# would be a decision this boundary has not been asked to make.

#: Relation-level privileges granted to PUBLIC that a decision intends, keyed
#: by ``(relation, privilege)`` and valued by the written reason for it.
#:
#: **Empty, deliberately.**  Neither relation #693 found was intentional; both
#: were incidental to the command role that created the table.  An entry here
#: means somebody decided that every role in the customer database, including
#: every role added later, should hold that privilege — so an entry needs a
#: reason, and the reason is the value.
PUBLIC_RELATION_PRIVILEGES: dict[tuple[str, str], str] = {}


def undocumented_public_privileges(
    observed: Iterable[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """The observed PUBLIC grants no decision above accounts for, in order.

    ``observed`` is ``(relation, privilege)`` pairs read from the catalog, and
    the privilege is compared case-insensitively because PostgreSQL reports
    ``SELECT`` while a grant is written ``select``.
    """

    allowed = {
        (relation, privilege.upper())
        for relation, privilege in PUBLIC_RELATION_PRIVILEGES
    }
    return tuple(
        sorted(
            (relation, privilege)
            for relation, privilege in observed
            if (relation, privilege.upper()) not in allowed
        )
    )


# These routes consume the shared native review reading (#456), and the
# project workflow also consumes source-backed recipient resolution (#562).
for _key in (("GET", "/work/{slug}"), ("POST", "/work/{slug}/issue/prepare"),
             ("POST", "/work/{slug}/issue/authorize"), ("GET", "/review/{slug}"),
             ("POST", "/review/{slug}"), ("POST", "/review/{slug}/answers")):
    _route = PILOT_ROUTES[_key]
    _relations = _route.relations | {"minutes_captures", "source_segments", "documents"}
    if _key[1].startswith("/work/"):
        _relations |= {"project_contact_imports", "project_contacts", "external_orgs", "current_project_record"}
    PILOT_ROUTES[_key] = PilotRoute(_route.why, frozenset(_relations))


# #835's scheduling act renders the week it just changed, so every relation it
# reaches is one `/work/{slug}` already reaches: the same reading, the same
# chase list, the same Issue section, plus the one `delta_deferrals` receipt
# the command appends -- which that page already reads to decide what is
# deferred. It is derived from that entry rather than copied out of it, because
# a copy is what drifts when the week learns to read one more thing.
# #835's plan lifecycle renders the same week for the same reason, and the one
# relation it adds is the closure the week now reads to decide which asks still
# stand. Derived from the week's entry for the reason above.
PILOT_ROUTES[("POST", "/work/{slug}/follow-up/close")] = PilotRoute(
    why=(
        "#835 correct a recorded Follow-up Plan, or cancel it with a "
        "structured reason. Attributable, and neither writes a Project "
        "Record revision (ADR-0084)"
    ),
    relations=PILOT_ROUTES[("GET", "/work/{slug}")].relations,
)


PILOT_ROUTES[("POST", "/work/{slug}/schedule")] = PilotRoute(
    why=(
        "#835 move a deferred Proposed Delta's return date, or bring it back "
        "now. ADR-0084 Work List scheduling: the delta stays open and no "
        "Project Record revision is written"
    ),
    relations=PILOT_ROUTES[("GET", "/work/{slug}")].relations,
)

# #837's two recording acts. Each writes through the record-decision command
# and then re-renders the week around its outcome, so each reaches exactly what
# the week reaches and nothing more. The set is *derived* from the week's
# rather than copied beside it: a relation the week gains later cannot leave
# these two behind, which is how the copied list would have failed.
for _key, _why in (
    (
        ("POST", "/work/{slug}/follow-up/sent"),
        "#837 record one message a person sent from their own mail client, "
        "against the Follow-up Plans it advanced. Corridor sends nothing; this "
        "is the recording of a send that already happened",
    ),
    (
        ("POST", "/work/{slug}/follow-up/response"),
        "#837 record that something came back, and which Document, Source "
        "Delivery, Source Segment or attributable manual observation it was "
        "read from. It stops the no-response finding and settles nothing",
    ),
    (
        ("GET", "/work/{slug}/follow-up/sent/{request_id}"),
        "#837 read back the exact message that was sent. A digest cannot show "
        "a coordinator what was asked, so the content is retained and served "
        "here rather than rendered into the week, which keeps a store that "
        "will not answer out of the whole page",
    ),
):
    PILOT_ROUTES[_key] = PilotRoute(
        _why, PILOT_ROUTES[("GET", "/work/{slug}")].relations
    )


def route_is_enabled(method: str, template: str) -> bool:
    """Whether the live pilot serves this route at all."""

    return (method.upper(), template) in PILOT_ROUTES


def pilot_relations() -> frozenset[str]:
    """Every relation the enabled routes were observed to reach."""

    return frozenset().union(
        *(route.relations for route in PILOT_ROUTES.values())
    ) if PILOT_ROUTES else frozenset()


def unprotected_route_relations() -> tuple[str, ...]:
    """Relations an enabled route needs that the revoke takes away, in order.

    Non-empty means the two halves of the boundary disagree: the application
    would serve a route whose data the database no longer hands it. That is a
    build failure, not a runtime surprise.
    """

    return tuple(sorted(pilot_relations() - PROTECTED_RELATIONS))


# --- #694 What the deployment does when the boundary is not enforced -------
#
# #680 shipped the route half as a declared flag defaulting off, and left the
# flag-off behaviour as a stated partial: the route reached its handler, the
# handler queried a revoked relation, and PostgreSQL answered ``permission
# denied for table ...``. The database refusal is defence in depth and stays.
# It must not be the route-selection mechanism, and its message must never be
# what a person reads.
#
# So the deployment has a *state*, not a boolean, and it is derived rather
# than asserted, because the flag alone cannot tell the two flag-off
# deployments apart:
#
# - a legacy development clone points ``WEB_DATABASE_URL`` at the opt-in
#   ``corridor_legacy_dev`` login, which kept the blanket read (ADR-0081), so
#   nothing is revoked and nothing may be refused; and
# - a live-pilot deployment that forgot the flag runs the same routes on
#   ``corridor_web``, which holds nothing on 124 relations.
#
# Which of the two it is, is answered by the login the request's own reads run
# as. That is the same fact the revoke was aimed at, so the two cannot drift,
# and it is read off the session's bind rather than queried, so deciding it
# issues no statement of any kind.
#
# #822 fixed the default that question carried. There is a third answer — the
# reader could not inspect the bind at all, or inspected it and found a login
# this build cannot name — and #694 filed all three under "not ``corridor_web``,
# therefore the legacy clone". A deployment nobody can identify is not
# evidence that nothing was taken away from it, so the legacy answer is now a
# named list (``legacy_capabilities``) and everything outside it is
# inconsistent.

# The login the migration revoked. A deployment reading as this one has the
# database half of the boundary applied to it whatever the flag says.
LIVE_PILOT_WEB_CAPABILITY = WEB_CAPABILITY_LOGIN


def legacy_capabilities() -> frozenset[str]:
    """The logins that may still run the frozen legacy surfaces, by name (#822).

    #694 asked one question of the reading login — "is this ``corridor_web``?"
    — and treated every other answer as the legacy development deployment.
    Every other answer includes the two the reader cannot vouch for: a bind it
    could not inspect, and a login this build has never heard of. Reading
    either as "nothing was taken away" picks the more permissive of the two
    deployments it might be, which is the one that serves another customer's
    rows.

    So the permission is a list rather than a fallback. The revoke names
    ``corridor_web`` alone, and exactly two capabilities are left holding the
    blanket read: the opt-in login ADR-0081 keeps for a legacy development
    deployment, and the schema owner that migrations, the test harness and
    local tooling connect as. Both are deployment configuration a person
    selected; neither is inferred from a failure to look. Anything else is an
    inconsistent configuration and refuses.

    The owner's name is the deployment's own to choose, so unlike the two
    capability logins it cannot be a constant; it is read off the configured
    schema-owner URL here rather than imported from ``corridor.db``, which
    would close an import cycle ``tests/test_architecture.py`` rejects. An
    owner URL carrying no username contributes no name, so an unreadable
    capability -- which answers with the same empty string -- cannot match it.
    """

    owner = make_url(settings.database_url).username or ""
    return frozenset({LEGACY_DEV_ROLE, owner}) - {""}


# The stable internal reason, carried by both the refusal and the readiness
# probe so an operator greps one string. It names no relation and no
# credential; it is the deployment's configuration, not the request's fault.
BOUNDARY_DISABLED_REASON = "live_pilot_web_boundary_disabled"

# What an unapproved route says when the boundary *is* enforced. A disabled
# surface should not advertise that it exists, so this is the same answer a
# missing project gets.
ROUTE_NOT_FOUND_DETAIL = "not found"

# The readiness component name. `/health` is itself a pilot route reaching no
# relation, so it stays served in every state and can report the state.
HEALTH_COMPONENT = "live_pilot_web_boundary"


class BoundaryState(Enum):
    """Whether this deployment can serve the routes outside the pilot set."""

    #: The flag is declared. The pilot set is the surface; everything else is
    #: refused as missing.
    ENFORCED = "enforced"
    #: The flag is off and this deployment's web login is one of the named
    #: capabilities that kept the blanket read. Nothing was taken away, so
    #: nothing is refused: the frozen legacy surfaces run as they always have.
    NOT_DECLARED = "not_declared"
    #: The two halves disagree — the flag is off while the reads run as the
    #: revoked live-pilot capability, or as a capability this build cannot
    #: name at all, or an enabled route needs a relation the revoke takes
    #: away. Refuse rather than let PostgreSQL answer.
    INCONSISTENT = "inconsistent"


@dataclass(frozen=True)
class RouteRefusal:
    """A controlled answer, decided before the handler and before any query."""

    status_code: int
    detail: str


def boundary_state(*, declared: bool, web_capability: str) -> BoundaryState:
    """The deployment's state, from the declared flag and the reading login.

    ``declared`` has to be the boolean itself, not merely something truthy: a
    boundary configured with a value nobody parsed is a configuration nobody
    declared, and ``"false"`` is truthy (#822). The settings field is typed
    ``bool``, so a malformed value is rejected before a process starts; this
    is what the state says when it arrives from somewhere else anyway.
    """

    if unprotected_route_relations():
        # An enabled route needs a relation the revoke takes away. `make check`
        # fails on this, so reaching it at runtime means the build guard was
        # bypassed; refuse anyway rather than serve half a page.
        return BoundaryState.INCONSISTENT
    if declared is True:
        return BoundaryState.ENFORCED
    if web_capability in legacy_capabilities():
        return BoundaryState.NOT_DECLARED
    return BoundaryState.INCONSISTENT


def health_detail(state: BoundaryState) -> str:
    """The bounded reason code the readiness probe carries for this state."""

    if state is BoundaryState.INCONSISTENT:
        return BOUNDARY_DISABLED_REASON
    return state.value


def route_refusal(
    state: BoundaryState, method: str, template: str
) -> RouteRefusal | None:
    """How this deployment answers one route, or None to run its handler.

    The invariant, stated once: a route this deployment cannot serve never
    enters a handler that depends on a revoked relation. The status code
    distinguishes whose problem it is — an enforced boundary is serving its
    declared surface and the route is simply not part of it, while an
    inconsistent one is misconfigured and says so.
    """

    if state is BoundaryState.NOT_DECLARED:
        return None
    if route_is_enabled(method, template):
        return None
    if state is BoundaryState.ENFORCED:
        return RouteRefusal(404, ROUTE_NOT_FOUND_DETAIL)
    return RouteRefusal(503, BOUNDARY_DISABLED_REASON)
