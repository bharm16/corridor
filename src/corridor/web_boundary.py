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
  every relation recorded for it survives the revoke.

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

``PILOT_ROUTE_RELATIONS`` was not written from memory.  It was recorded from
an instrumented run of the route tests, keyed on the ``FROM``/``JOIN``/
``INTO``/``UPDATE`` targets of every statement issued while each request was
in flight, with the legacy-project cases excluded from ``/work/{slug}``.  It
is a *record* of what was observed, so the live proof is still the real-login
test that drives these routes as ``corridor_web`` itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from corridor import access


@dataclass(frozen=True)
class PilotRoute:
    """One enabled route, and the relations it was observed to reach."""

    why: str
    relations: frozenset[str]


PILOT_ROUTES: dict[tuple[str, str], PilotRoute] = {
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
    ("GET", "/record/{slug}"): PilotRoute(
        why=(
            "#642 the read-only record and history investigation"
        ),
        relations=frozenset(
            {
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
