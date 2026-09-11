"""Permanent database baseline and supported-upgrade contract.

The fresh-schema proof builds from an empty database. Supported-transition
cases clone one genuinely migrated predecessor template per process, seed
their own historical rows, and execute every tested upgrade or downgrade on
that independent clone. Rebuilding the same predecessor for each case adds
setup cost without adding another transition proof.
"""

from __future__ import annotations

import ast
from datetime import date, datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from corridor.config import settings
from corridor.db_roles import RECORD_DECISION_ROLE
from corridor.facts import _fact_digest, _statement_timing_structured
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.materializer import (
    PDF_MARKED_RESOLUTION_TRANSFORMATION,
    PDF_TEXT_TRANSFORMATION,
    materialize_pdf_marked_resolution,
    materialize_pdf_segment_value,
    materialize_quoted_statement_wording,
    materialize_typed_satellite,
    replay_pdf_materialized_value,
)
from corridor.models import SourceSegment
from corridor.source_append import append_fact
from corridor.statement_values import StatementTiming
from alembic.config import Config
from alembic.script import ScriptDirectory

from corridor.migrations import policy
from corridor.product_proving_database import fingerprint_database_url
from corridor.report_release import retrieve_released_external_report
from harness_support import as_role
from ratchet_support import assert_ratchet


ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "src" / "corridor" / "migrations" / "baseline_versions"
# The two revisions this file drives its upgrades between. Read from the
# policy rather than retyped: a retyped copy that a revision consolidation
# missed would silently prove the transition of a revision nothing installs.
SUPPORTED_HEAD = policy.SUPPORTED_FROM_REVISION
CURRENT_HEAD = policy.CURRENT_HEAD
EXPECTED_SCHEMA_SHA256 = (
    # One `policy_activations` relation replaces the four per-family ADR-0050
    # activation ledgers (-4 tables and -4 sequences, +1 of each), and its
    # `ck_policy_activation_event_admission_reason` check keeps that family's
    # reason inside the 128 characters its restored predecessor column holds.
    # #809 adds `scanned_page_observations` (+1 table, +1 sequence, its own
    # partition policy) and gives `unreadable_cell_resolutions` the three
    # binding columns and `ck_unreadable_cell_resolution_observation`.
    # #811 adds `spend_authorizations` (+1 table, +1 sequence, its immutability
    # guard and partition policy) and moves the nine common columns and their
    # checks off the five assistant configuration relations, which each gain
    # `authorization_id`, `operation` and the composite reference that holds a
    # configuration to a declaration of its own project and operation.
    # #859 makes a Revision Comparison's execution identity unique: one
    # constraint and its index on `revision_comparison_runs`, and no new
    # relation, so the table and sequence counts are unchanged.
    # #823 adds `source_delivery_confirmations` (+1 table, +1 sequence, its
    # immutability guard and partition policy) and replaces
    # `ck_source_delivery_push_credential` with the authentication-mode pair
    # `ck_source_delivery_authentication` and `ck_source_delivery_principal`
    # over a new `source_deliveries.delivered_by_principal` column.
    # #824 partitions the six relations the admitted intake path reads --
    # `processing_artifacts` by its own `project_id`, and `doc_pages`,
    # `document_quarantines`, `extraction_runs`, `page_render_derivatives`
    # and `token_layers` through the `documents` row each names. Row-level
    # security and policies are not schema objects this fingerprint reads
    # (it reads columns, constraints, functions, triggers and indexes), and
    # no relation or sequence is added, so the digest and both counts are
    # unchanged. Recomputed against a fresh disposable database to say so
    # rather than assumed.
    # #825 adds `source_revision_declarations` (+1 table, +1 sequence, its
    # immutability guard and partition policy): what a coordinator declared
    # about one delivery, which the processing pass routes on. Recomputed
    # against a fresh disposable database.
    # #839 adds `enforce_coordination_designation` and the two `before insert`
    # triggers that call it, on `issue_coverage_declarations` and
    # `release_preparation_requests`: the Project Coordination designation,
    # proved on the relation the way #533 proves external release inside
    # `authorize_release_package`. A function and two triggers are schema
    # objects this fingerprint reads, so the digest moves; no relation or
    # sequence is added, so both counts are unchanged. Recomputed against a
    # fresh disposable database.
    # #835 adds `delta_follow_up_plan_closures` (+1 table, +1 sequence) and the
    # `close_delta_follow_up_plan` command: how a Follow-up Plan stops being an
    # outside ask, superseded by a corrected plan or cancelled with a
    # structured reason. It carries the Review Packet family's own
    # record-decision write guard and, because retiring somebody's outside ask
    # is coordination work reachable on its own route, #839's
    # `enforce_coordination_designation` trigger -- the same function, named at
    # its own principal column, rather than a second near-copy. Recomputed
    # against a fresh disposable database.
    # #837 finishes the accepted #652 correspondence contract. `outgoing_requests`
    # loses `follow_up_plan_id` and `sent_bytes` and gains the storage key, the
    # recorder, and its correction pair; `outgoing_request_plans` is the new
    # relation that makes one request advance several Follow-up Plans (+1 table,
    # +1 sequence); `outgoing_request_responses` loses its one-per-request unique
    # and gains completeness, the four evidence columns with the check that makes
    # them mutually exclusive, its own idempotency key and its correction pair.
    # Both append commands change signature. Recomputed against a fresh
    # disposable database.
    # #836 adds `capture_correction_requests` (+1 table, +1 sequence) and the
    # `report_capture_correction` command: ADR-0100's ancillary action, where a
    # coordinator reports that one capture is wrong about its source. It
    # carries the Review Packet family's own record-decision write guard and no
    # designation trigger -- it writes no accepted authority and makes nothing
    # effective -- and its composite foreign keys name the Fact through
    # `(project_id, document_id, fact_id)` and both passage columns through
    # that same `document_id`, so a selected passage outside the capture's own
    # source is unrepresentable. Recomputed against a fresh disposable database
    # with template reuse off.
    # #827 adds the limited onboarding authorization ADR-0099 decides: the
    # grant a restricted operations actor records in the customer environment,
    # the append-only events that say what happened to it, the retained
    # preview an approval request verifies without opening a workbook, and the
    # retained proof that a permitted act committed while the grant was valid.
    # Four relations and four sequences, their five commands, the guard that
    # refuses every other write, and #839's designation trigger on the act.
    # Recomputed against a fresh disposable database with template reuse off.
    # #886 adds the versioned set of authorized source bindings a project may
    # take delivery on: `project_source_authorizations`, one recorded version
    # with the governing customer authorization it was issued under and the
    # digest the command derives from its bindings, and
    # `project_source_authorization_bindings`, the set itself -- channel,
    # configuration identity and version, permitted source classes and
    # authentication mode, several rows free to name one channel. Two relations
    # and two sequences, their two commands, the guard that refuses every other
    # write, and a partition policy each. The activation configuration's single
    # `source_channel` / `source_configuration` pair is gone, which is why a
    # product upload was refused on a mailbox-activated deployment. Recomputed
    # against a fresh disposable database with template reuse off.
    # #919 types the hold relation `document_quarantines` rather than adding a
    # second list of blocked documents. It gains a surrogate `id` (+1 sequence,
    # and the primary key moves off `document_id`, so one document may carry
    # several independent restrictions), the prohibited stage, the
    # machine-readable reason code, the authority and rule or actor that
    # imposed it, the supporting evidence, and the four columns one
    # attributable release writes. A partial unique index holds one open
    # restriction per document and reason; `enforce_document_hold_write` and
    # its two triggers refuse every delete, every truncate and every update but
    # that one release. No relation is added, so the table count is unchanged
    # and the sequence count rises by one. Recomputed against a fresh
    # disposable database with template reuse off.
    # ADR-0101 adds the correction lifecycle the `withdraw_for_no_change`
    # refusal was holding open: `capture_correction_results`, the whole proof
    # one source-grounded correction rests on, and `delta_capture_corrections`,
    # the append-only relationship that retires the obsolete proposal, with one
    # row per delta so a retry is the same act and two competing retirements
    # serialise on the index. Two relations and two sequences, their command,
    # the guard that refuses every other write, and a partition policy each.
    # Two further functions, called by commands created earlier in the
    # revision: `lock_proposed_delta_terminal`, the advisory lock every
    # terminal writer takes so a competing pair cannot both commit, and
    # `proposed_delta_capture_correction`, the one predicate they all ask
    # instead of four copies. `resolve_proposed_delta_decision`,
    # `defer_proposed_delta` and `record_delta_follow_up_plan` each gain that
    # lock and that refusal, and the two bulk supersession sweeps skip a
    # retired delta. Recomputed against a fresh disposable database with
    # template reuse off.
    # #933 adds `project_accepted_record_decision_count`, one `SECURITY
    # DEFINER` reading owned by the record-decision role and granted to both
    # runtime capabilities: the count of accepted record decisions Adopt
    # Baseline refuses to overwrite, on both halves -- the spine's effective
    # decisions and the legacy Constraint Records. The web capability holds no
    # privilege on `dependencies`, so the Python guard in front of the command
    # answered `permission denied` on an enforcing deployment; asking the role
    # that already holds the read keeps both halves and leaves the boundary
    # where it was. A function is a schema object this fingerprint reads, so
    # the digest moves; no relation or sequence is added, so both counts are
    # unchanged. Recomputed against a fresh disposable database with template
    # reuse off.
    # #903 replaces the Work List deferral's identity: `request_identity`, what
    # the caller says its request is, joins the delta in
    # `uq_delta_deferrals_request` and the instant leaves it, and
    # `supersedes_deferral_id` records the schedule each act replaced, unique
    # because one schedule is replaced at most once. Two columns, two
    # constraints, a check that a request identity is not blank, and a
    # `defer_proposed_delta` that takes two more arguments -- all of them on
    # relations and commands that already exist, so the digest moves and
    # neither count does. Recomputed against a fresh disposable database with
    # template reuse off.
    # #937 bounds the web half of `project_accepted_record_decision_count` to
    # the partition the database sealed on this transaction, and refuses
    # anything else with `insufficient_privilege`. A `SECURITY DEFINER` command
    # steps outside row-level security by construction, so #933's reading --
    # correct about the privilege it needed -- answered about any project the
    # caller named, membership or no membership; "only a number" is still that
    # project's number. The unpartitioned worker reading is unchanged, because
    # a background run carries no person's authorization to bound (ADR-0079).
    # The body is rewritten, so the digest moves; no relation, sequence,
    # privilege or grant changes, so both counts are unchanged. Recomputed
    # against a fresh disposable database with template reuse off.
    "1334b7abe9d9fefced5df35247e0eb8b3b8933992b33b2df1d3ad11ec3e1b4ca"
)

pytestmark = [pytest.mark.slow, pytest.mark.migration]


def test_the_executable_migration_window_matches_the_recorded_policy():
    """Assert the revision graph, not a list of filenames.

    The guard this replaces enumerated the permitted filenames, so every
    migration-bearing change added its name and passed. It could not fail as
    the chain grew, which is how 2 executable revisions became 23 after #423
    bounded them.
    """

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", "src/corridor/migrations"
    )
    script = ScriptDirectory.from_config(config)

    heads = script.get_heads()
    assert list(heads) == [policy.CURRENT_HEAD], (
        "the executable graph must have exactly one head, and it must be the "
        f"revision the policy records: {heads}"
    )

    revisions = list(script.walk_revisions())
    by_id = {revision.revision: revision for revision in revisions}
    assert policy.SCHEMA_BUILDER in by_id, "the fresh-install builder must exist"
    assert policy.SUPPORTED_FROM_REVISION in by_id, (
        "the supported revision must exist in the executable graph"
    )

    # With the chain consolidated the supported revision *is* the head, so
    # there is nothing after it to walk; `iterate_revisions` excludes its
    # lower bound and returns an empty set.
    reachable = {
        revision.revision
        for revision in script.iterate_revisions(
            policy.CURRENT_HEAD, policy.SUPPORTED_FROM_REVISION
        )
    }
    if policy.SUPPORTED_FROM_REVISION != policy.CURRENT_HEAD:
        assert policy.CURRENT_HEAD in reachable

    # No executable revision sits outside the builder's line: history before
    # the supported revision is source bytes, not an upgrade path.
    unreachable = {
        revision.revision
        for revision in revisions
        if revision.revision not in reachable
        and revision.revision
        not in {policy.SCHEMA_BUILDER, policy.SUPPORTED_FROM_REVISION}
        and revision.revision != policy.COMPATIBILITY_MARKER
    }
    assert unreachable == set(), (
        f"unrelated executable history remains: {sorted(unreachable)}"
    )

    # The ratchet. `UNRELEASED_EDGES` may fall and must never rise -- against
    # the number the merge base records, so bumping it here buys nothing. A change
    # needing another revision folds into the current unreleased transition,
    # or consolidates the chain and lowers the recorded number.
    # `iterate_revisions` excludes its lower bound, so the reachable set is
    # exactly the transitions after the supported revision.
    edges = len(reachable)
    assert edges <= max(policy.UNRELEASED_EDGES, policy.UNRELEASED_EDGE_TARGET), (
        f"the executable chain grew to {edges} transitions after "
        f"{policy.SUPPORTED_FROM_REVISION}, above the permitted "
        f"{max(policy.UNRELEASED_EDGES, policy.UNRELEASED_EDGE_TARGET)}. Fold "
        "the change into the current unreleased transition, or consolidate "
        "and lower the policy."
    )
    assert edges >= policy.UNRELEASED_EDGES, (
        f"the chain is down to {edges} transitions; lower "
        f"UNRELEASED_EDGES in the migration policy to hold the gain."
    )
    assert policy.UNRELEASED_EDGE_TARGET == 1, (
        "ADR-0065's window is one supported transition; the target does not move"
    )
    assert_ratchet(
        "src/corridor/migrations/policy.py:UNRELEASED_EDGES",
        measured=edges,
        recorded=policy.UNRELEASED_EDGES,
    )


# The families b2d5f8a1c4e7 composes, in the order it composes them, and the
# order it takes them apart in. Both are stated here because order is the whole
# of a migration's meaning: a block moved earlier grants on a relation that does
# not exist yet, and a reversal moved later drops the parent of the row it was
# about to refuse for. The revision used to be one 11,966-line module in which
# that order was implicit in a single 830-line function.
COMPOSED_UPGRADE = (
    "operating_mode",
    "baseline_record",
    "push_intake",
    "resolve_delta",
    "review_packets",
    "recorded_verbal",
    "delta_deduplication",
    "unified_delivery",
    "baseline_format_manifest",
    "report_revision_binding",
    "extractor_configuration",
    "report_reading_payload",
    "project_partition",
    "issue_profile",
    "release_candidate",
    "release_package",
    "partition_declaration",
    "partition_seal",
    "coverage_preparation",
    "replay_gate",
    "web_capability",
    "preparation_supervisor",
    "native_segments",
    "environment_binding",
    "outgoing_requests",
    "scanned_observations",
    "spend_authorization",
    "product_upload_delivery",
    "source_revision_declaration",
    "follow_up_plan_closure",
    "capture_correction",
    "onboarding_authorization",
    "source_authorization",
    "processing_holds",
    "capture_correction_retirement",
    # The sibling transitions this revision has always carried at the end, and
    # the PUBLIC sweep that runs last of all because it reads the catalog every
    # block above has finished writing.
    "email_spine",
    "project_contacts",
    "minutes_spine",
    "impact_derivations",
    "retirement_watermark",
    "shadow_schema",
    "legacy_history",
    "coordination_history",
    "support_history",
    "public_privileges",
)
COMPOSED_DOWNGRADE = (
    "support_history",
    "coordination_history",
    "legacy_history",
    "shadow_schema",
    "retirement_watermark",
    "impact_derivations",
    "minutes_spine",
    "project_contacts",
    "email_spine",
    "capture_correction_retirement",
    "processing_holds",
    "source_authorization",
    "onboarding_authorization",
    "capture_correction",
    "follow_up_plan_closure",
    "source_revision_declaration",
    "product_upload_delivery",
    "spend_authorization",
    "scanned_observations",
    "environment_binding",
    "native_segments",
    "public_privileges",
    "outgoing_requests",
    "preparation_supervisor",
    "web_capability",
    "replay_gate",
    "coverage_preparation",
    "partition_seal",
    "partition_declaration",
    "release_package",
    "release_candidate",
    "issue_profile",
    "project_partition",
    "report_reading_payload",
    "extractor_configuration",
    "report_revision_binding",
    "baseline_format_manifest",
    "unified_delivery",
    "delta_deduplication",
    "recorded_verbal",
    "review_packets",
    "push_intake",
    "resolve_delta",
    "baseline_record",
    "operating_mode",
)
# What the revision's own module keeps: its append commands (#492) and the two
# relations they were built for (#518, #530), plus the Fact identity recipe the
# Recorded Verbal backfill replays, which is frozen at this revision.
SOURCE_APPEND_FAMILIES = frozenset(COMPOSED_DOWNGRADE) - {
    "email_spine",
    "project_contacts",
    "minutes_spine",
    "impact_derivations",
    "retirement_watermark",
    "shadow_schema",
    "legacy_history",
    "coordination_history",
    "support_history",
}


def _composed_families(function_name: str) -> tuple[str, ...]:
    """The module each `<family>.upgrade(op)` call in source order names."""

    revision = ast.parse((VERSIONS / f"{CURRENT_HEAD}_source_append_commands.py").read_text())
    function = next(
        node
        for node in revision.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    calls = [
        (node.lineno, node.func.value.id)
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.attr in {"upgrade", "downgrade", "install", "uninstall"}
    ]
    return tuple(module for _, module in sorted(calls))


def test_the_revision_composes_its_families_in_one_declared_order():
    """The order is asserted, not left to review of a 12,000-line module.

    Splitting the folded transition into one module per family moved 214 names
    out of the revision and left the composition behind. The composition is
    the part that can silently change meaning, so both directions are stated
    here as lists; the schema fingerprint above proves what each family then
    executes against a real database.
    """

    assert _composed_families("upgrade") == COMPOSED_UPGRADE
    assert _composed_families("downgrade") == COMPOSED_DOWNGRADE

    package = ROOT / "src" / "corridor" / "migrations" / "source_append_commands"
    modules = {path.stem for path in package.glob("*.py")}
    assert modules == SOURCE_APPEND_FAMILIES | {"__init__", "roles"}, (
        "a family module that nothing composes is dead migration source, and a "
        "composed family with no module cannot be imported"
    )


def test_the_family_package_is_not_executable_migration_history():
    """Alembic must keep seeing exactly two revision files.

    `version_locations` lists `*.py` in one directory and does not descend into
    subdirectories, which is the only reason the families may live in a package
    at all. If that ever changed, twenty-five modules with no revision
    identifier would become candidate revisions.
    """

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", "src/corridor/migrations")
    script = ScriptDirectory.from_config(config)

    paths = {Path(revision.path) for revision in script.walk_revisions()}
    assert {path.parent.name for path in paths} == {"baseline_versions"}
    assert not any("source_append_commands" in path.parts for path in paths)


def test_fresh_database_matches_the_released_schema_exactly():
    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        label="baseline_fresh",
        reuse_migrated_template=False,
    ) as database:
        database_url = configured.set(database=database.name).render_as_string(
            hide_password=False
        )
        fingerprint = fingerprint_database_url(database_url)
        security = _statement_retirement_security(database.session_factory)
        source_append = _source_append_security(database.session_factory)

    assert database.migration_head == CURRENT_HEAD
    assert fingerprint.schema_sha256 == EXPECTED_SCHEMA_SHA256
    assert source_append == SOURCE_APPEND_SECURED
    assert security == {
        "can_login": False,
        "inherits": False,
        "function_owner": "corridor_statement_retirement",
        "function_public_execute": False,
        "tables": {
            "commitment_lineages": ("DELETE", "SELECT"),
            "dependency_event_evidence": ("DELETE", "SELECT"),
            "dependency_event_scope_decisions": ("DELETE", "SELECT"),
            "dependency_event_scopes": ("DELETE", "SELECT"),
            "dependency_event_timings": ("DELETE", "SELECT"),
            "dependency_events": ("DELETE", "SELECT"),
            "evidence_links": ("DELETE", "SELECT"),
            "legacy_ledger_archives": ("DELETE", "SELECT"),
            "projects": ("DELETE", "SELECT"),
            "work_decisions": ("SELECT",),
        },
    }


def test_the_supported_database_upgrades_to_the_current_head_and_back(tmp_path):
    """The one supported transition, proved on its exact transformed rows.

    b2d5f8a1c4e7 transforms privileges and adds relations, not data: it
    takes the raw source-table writes back from the runtime capabilities and
    hands the source-append role its commands (#492), it creates the
    Support Assessment tables that only the fifth command writes (#530), and
    it establishes the baseline/delta operating mode with its immutable
    adoption receipt and the guards that refuse a legacy accepted-value write
    for an adopted project (#520).  A database standing at the supported
    revision must cross that transition in both directions.
    """

    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        label="baseline_transition",
        migration_revision=SUPPORTED_HEAD,
        reuse_migrated_template=True,
    ) as database:
        database_url = configured.set(database=database.name)
        assert database.migration_head == SUPPORTED_HEAD
        assert _source_append_security(database.session_factory) == SOURCE_APPEND_OPEN

        before_public = _public_relation_grants(database.session_factory)
        with database.session_factory() as session, session.begin():
            historical_project = session.scalar(text("insert into projects(slug,name,is_synthetic) values('retirement-transition','Historical retirement',true) returning id"))
            historical_report = session.scalar(text("insert into report_runs(project_id,ts,ruleset_version,snapshot_json) values(:p,'2099-01-01T00:00:00Z','v0.3','{}') returning id"), {"p": historical_project})
            historical_archive = session.scalar(text("""insert into legacy_ledger_archives(project_id,format_version,content_json,content_sha256,
                dependency_count,assertion_count,evidence_link_count,audit_log_count,ref_code_high_watermark,retired_by,retired_at)
                values(:p,'legacy-ledger-v1','{}',:digest,0,0,0,0,0,'system:historical','2000-01-01T00:00:00Z') returning id"""),
                {"p": historical_project, "digest": "0" * 64})
            project_id = session.scalar(text(
                "insert into projects (slug, name, is_synthetic) "
                "values ('native-reading-transition', 'Native reading transition', true) returning id"
            ))
            document_id = session.scalar(text(
                "insert into documents (project_id, sha256, filename, doc_type) "
                "values (:project_id, :digest, 'historical.pdf', 'minutes') returning id"
            ), {"project_id": project_id, "digest": "d" * 64})
            wording = "Historical exact wording with soft\u00adhyphen."
            segment_id = session.scalar(text(
                "insert into source_segments (project_id, document_id, kind, exact_text, "
                "content_sha256, ordinal, page_no, start_offset, end_offset) values "
                "(:project_id, :document_id, 'prose_span', :wording, :digest, 7, 3, 42, :end) returning id"
            ), {"project_id": project_id, "document_id": document_id,
                "wording": wording, "digest": sha256(wording.encode()).hexdigest(), "end": 42 + len(wording)})
            old_segment = tuple(session.execute(text(
                "select id, project_id, document_id, kind, exact_text, content_sha256, "
                "ordinal, page_no, start_offset, end_offset, created_at "
                "from source_segments where id = :id"
            ), {"id": segment_id}).one())
            historical_fact = _seed_identified_fact(
                session, slug="pdf-fact-predecessor",
                digest=sha256(b"pdf-fact-preserved-predecessor").hexdigest(),
            )
            old_fact_and_authority = _fact_and_revision_bytes(session, historical_fact)

            # #919 classifies the holds a retained database already carries,
            # from retained rows and never from the free-text reason. Two
            # documents, one of each reachable outcome: the first has the
            # producer evidence -- an Extraction Run the reader recorded
            # `quarantined`, which only a reader that had already opened the
            # document can write -- and the second has none at all.
            extraction_held_document = session.scalar(text(
                "insert into documents (project_id, sha256, filename, doc_type, "
                "parse_status) values (:project_id, :digest, 'schedule.xlsx', "
                "'schedule', 'parsed') returning id"
            ), {"project_id": project_id, "digest": "e" * 64})
            held_run_id = session.scalar(text(
                "insert into extraction_runs (document_id, prompt_version, "
                "candidate_count, page_errors, outcome) values "
                "(:document_id, 'historical-v1', 0, 1, 'quarantined') returning id"
            ), {"document_id": extraction_held_document})
            session.execute(text(
                "insert into document_quarantines (document_id, reason) "
                "values (:document_id, :reason)"
            ), {"document_id": extraction_held_document,
                "reason": "document-asserted work sequencing is not modeled (#149)"})
            unclassified_document = session.scalar(text(
                "insert into documents (project_id, sha256, filename, doc_type, "
                "parse_status) values (:project_id, :digest, 'unknown.xlsx', "
                "'schedule', 'pending') returning id"
            ), {"project_id": project_id, "digest": "f" * 64})
            session.execute(text(
                "insert into document_quarantines (document_id, reason) "
                "values (:document_id, :reason)"
            ), {"document_id": unclassified_document,
                "reason": "held by an operator whose rule nothing retained"})

        upgraded = _alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr
        assert _migration_head(database.session_factory) == CURRENT_HEAD
        assert _source_append_security(database.session_factory) == SOURCE_APPEND_SECURED
        upgraded_public = _public_relation_grants(database.session_factory)
        with database.session_factory() as session:
            # #656 adds only the local identity attestation here. The control
            # plane is a separately installed database, never a customer table.
            assert session.scalar(text("select count(*) from public.customer_environment_binding")) == 0
            assert session.scalar(text("select to_regnamespace('control_plane')")) is None
            for role in ("corridor_web", "corridor_worker"):
                assert session.scalar(text("select has_table_privilege(:role, 'public.customer_environment_binding', 'SELECT')"), {"role": role}) is True
                assert session.scalar(text("select has_table_privilege(:role, 'public.customer_environment_binding', 'INSERT')"), {"role": role}) is False
            row = session.get(SourceSegment, segment_id)
            assert (row.id, row.project_id, row.document_id, row.kind, row.exact_text,
                    row.content_sha256, row.ordinal, row.page_no, row.start_offset,
                    row.end_offset, row.created_at) == old_segment
            assert row.reading_sha256 is None and row.reader_identity is None
            assert _fact_and_revision_bytes(session, historical_fact) == old_fact_and_authority
            assert session.scalar(text("select retirement_report_run_watermark_id from legacy_ledger_archives where id=:id"), {"id": historical_archive}) is None
            assert session.scalar(text("select retirement_archive_id from report_runs where id=:id"), {"id": historical_report}) is None
            assert session.scalar(text("select count(*) from proposed_delta_impact_derivations")) == 0
            # The three classification outcomes #919's rule declares, on the
            # exact transformed rows. Two are reachable on this transition: the
            # delivery ledger the security branch reads is born in this very
            # revision, so no historical hold can carry a quarantined delivery
            # and that branch is a no-op here by construction.
            classified = session.execute(text(
                "select document_id, prohibited_stage, reason_code, "
                "imposed_by_authority, imposed_by, evidence, reason, released_at "
                "from document_quarantines order by document_id"
            )).all()
            assert [tuple(row) for row in classified] == [
                (
                    extraction_held_document,
                    "semantic_extraction",
                    "extraction_refused_by_rule",
                    "migration",
                    "migration:b2d5f8a1c4e7:quarantined_extraction_run",
                    f"extraction_runs.id={held_run_id} outcome=quarantined",
                    "document-asserted work sequencing is not modeled (#149)",
                    None,
                ),
                (
                    unclassified_document,
                    "document_reading",
                    "unclassified_historical_hold",
                    "migration",
                    "migration:b2d5f8a1c4e7:unclassified",
                    "no retained delivery disposition or extraction run "
                    "establishes what this hold prohibited",
                    "held by an operator whose rule nothing retained",
                    None,
                ),
            ]
            # A recorded hold is append-only: the release columns are the only
            # thing that may move, and a delete is refused outright.
            with pytest.raises(DBAPIError, match="document_hold:immutable"):
                with session.begin_nested():
                    session.execute(text(
                        "update document_quarantines set reason = 'rewritten' "
                        "where document_id = :id"
                    ), {"id": unclassified_document})
            with pytest.raises(DBAPIError, match="document_hold:immutable"):
                with session.begin_nested():
                    session.execute(text(
                        "delete from document_quarantines where document_id = :id"
                    ), {"id": unclassified_document})
            for role in ("corridor_web", "corridor_worker"):
                assert session.scalar(text("select has_table_privilege(:role, 'proposed_delta_impact_derivations', 'SELECT')"), {"role": role})
                assert not session.scalar(text("select has_table_privilege(:role, 'proposed_delta_impact_derivations', 'INSERT')"), {"role": role})
            for table in ("pipeline_observations", "pipeline_comparisons", "pipeline_qualifications",
                          "pipeline_acceptances", "pipeline_selections"):
                assert session.scalar(text(f"select count(*) from public.{table}")) == 0
            for role in ("corridor_web", "corridor_worker", "corridor_source_append"):
                for table in ("pipeline_qualification_policies", "pipeline_comparisons", "pipeline_qualifications",
                              "pipeline_acceptances", "pipeline_selections"):
                    assert session.scalar(text("select has_table_privilege(:role, :table, 'INSERT')"),
                                          {"role": role, "table": table}) is False

        downgraded = _alembic(database_url, "downgrade", SUPPORTED_HEAD)
        assert downgraded.returncode == 0, downgraded.stderr
        assert _migration_head(database.session_factory) == SUPPORTED_HEAD
        assert _source_append_security(database.session_factory) == SOURCE_APPEND_OPEN
        restored_public = _public_relation_grants(database.session_factory)
        with database.session_factory() as session:
            restored = session.execute(text(
                "select id, project_id, document_id, kind, exact_text, content_sha256, "
                "ordinal, page_no, start_offset, end_offset, created_at "
                "from source_segments where id = :id"
            ), {"id": segment_id}).one()
            assert tuple(restored) == old_segment
            assert _fact_and_revision_bytes(session, historical_fact) == old_fact_and_authority
            # Every hold crosses back with the words it was written with. One
            # reason per document is exactly what the predecessor can hold, so
            # this seeding is representable; two open reasons on one document,
            # or any released one, is what the downgrade refuses.
            assert session.execute(text(
                "select document_id, reason from document_quarantines "
                "order by document_id"
            )).all() == [
                (extraction_held_document,
                 "document-asserted work sequencing is not modeled (#149)"),
                (unclassified_document,
                 "held by an operator whose rule nothing retained"),
            ]

        # A recorded native reading cannot be discarded to make a downgrade
        # succeed. Exercise the same supported transition, with actual source
        # bytes and the normal SECURITY DEFINER append command.
        assert _alembic(database_url, "upgrade", "head").returncode == 0
        _assert_history_downgrade_refuses_data_on_this_transition(database.session_factory, project_id)
        from corridor.models import Document, ExtractionRun
        from corridor.reader_segments import append_native_segments, read_native_pdf
        from pdf_fixture_support import PdfFixture

        fixture = PdfFixture()
        page = fixture.add_page()
        for x in (40, 180, 320):
            page.line((x, 40), (x, 140))
        for y in (40, 90, 140):
            page.line((40, y), (320, y))
        page.text((50, 70), "Relocate")
        page.text((190, 70), "Protect")
        page.text((50, 120), "X")
        page.text((190, 120), "X")
        page.text((40, 220), "Native Source Company")
        source = fixture.save(tmp_path / "native-transition.pdf")
        digest = sha256(source.read_bytes()).hexdigest()
        reading = read_native_pdf(source, source_sha256=digest)
        with database.session_factory() as session, session.begin():
            document = Document(project_id=project_id, sha256=digest,
                                filename=source.name, doc_type="matrix")
            session.add(document)
            session.flush()
            native = append_native_segments(session, document, reading)
            native_id = native[0].id
            expected_text = native[0].exact_text
            run = ExtractionRun(
                document_id=document.id, prompt_version="native-migration-fixture", candidate_count=0,
            )
            session.add(run)
            session.flush()
            headers = sorted(
                (row for row in native if row.kind == "pdf_cell" and row.cell_row == 0),
                key=lambda row: row.cell_column,
            )
            marks = sorted(
                (row for row in native if row.kind == "pdf_cell" and row.cell_row == 1),
                key=lambda row: row.cell_column,
            )
            organization = next(row for row in native if row.kind == "pdf_span" and row.exact_text == "Native Source Company")
            accepted_before = session.execute(text(
                "select (select count(*) from fact_decisions), "
                "(select count(*) from project_record_revisions)"
            )).one()
            native_facts = []
            for value in (
                materialize_pdf_segment_value(session, "external_org", organization),
                materialize_pdf_marked_resolution(headers, marks),
            ):
                native_facts.append(append_fact(
                    session, project_id=project_id, document_id=document.id,
                    extraction_run_id=run.id, subject_kind="source_row", subject_key="p1:t0:r1",
                    recorded_by="local:migration-proof",
                    content_sha256=_fact_digest(
                        run_identity={"document_id": document.id, "extraction_run_id": run.id},
                        subject_kind="source_row", subject_key="p1:t0:r1", value=value,
                    ),
                    value=value,
                ))
            assert [(fact.text_value, fact.transformation) for fact in native_facts] == [
                ("Native Source Company", PDF_TEXT_TRANSFORMATION),
                ("Relocate; Protect", PDF_MARKED_RESOLUTION_TRANSFORMATION),
            ]
            resolution = native_facts[1]
            stored_sources = session.execute(text(
                "select role, ordinal, source_segment_id from fact_sources "
                "where fact_id = :id order by id"
            ), {"id": resolution.id}).all()
            assert stored_sources == [
                ("value_source", 1, headers[0].id), ("value_source", 2, headers[1].id),
                ("context", 1, marks[0].id), ("context", 2, marks[1].id),
            ]
            from corridor.source_segments import dereference_source_segment

            for segment in (*headers, *marks):
                dereference_source_segment(document, segment, source)
            assert replay_pdf_materialized_value(
                session, resolution.fact_type, resolution.transformation, headers, marks,
            ).text_value == resolution.text_value
            assert session.execute(text(
                "select (select count(*) from fact_decisions), "
                "(select count(*) from project_record_revisions)"
            )).one() == accepted_before
            assert _fact_and_revision_bytes(session, historical_fact) == old_fact_and_authority
        before_refusal = fingerprint_database_url(database_url.render_as_string(hide_password=False))
        refused = _alembic(database_url, "downgrade", SUPPORTED_HEAD)
        assert refused.returncode != 0 and "native PDF source segments cannot be represented" in refused.stderr
        assert _migration_head(database.session_factory) == CURRENT_HEAD
        assert fingerprint_database_url(database_url.render_as_string(hide_password=False)) == before_refusal
        with database.session_factory() as session:
            assert session.get(SourceSegment, native_id).exact_text == expected_text
            assert _fact_and_revision_bytes(session, historical_fact) == old_fact_and_authority

        # A permanent pipeline identity is also outside the supported
        # predecessor. Prove refusal on this same seeded upgrade, preserving
        # the original Fact/authority bytes and the newly appended identity.
        from corridor.native_pipeline import register_pipeline_configuration
        with database.session_factory() as session, session.begin():
            configuration = register_pipeline_configuration(session, {"fixture": "pipeline migration preservation"})
            configuration_sha = configuration.configuration_sha256
        before_pipeline_refusal = fingerprint_database_url(database_url.render_as_string(hide_password=False))
        refused = _alembic(database_url, "downgrade", SUPPORTED_HEAD)
        assert refused.returncode != 0 and "permanent pipeline evidence cannot be represented" in refused.stderr
        assert fingerprint_database_url(database_url.render_as_string(hide_password=False)) == before_pipeline_refusal
        with database.session_factory() as session:
            assert session.scalar(text("select configuration_sha256 from pipeline_configurations")) == configuration_sha
            assert _fact_and_revision_bytes(session, historical_fact) == old_fact_and_authority

    # #693: the supported revision carries three undocumented grants to
    # PUBLIC, the transition removes all three, and the downgrade returns
    # exactly them — including the grantor, because a re-grant made by a
    # different role is a different ACL entry even when it reads the same.
    assert before_public == PUBLIC_GRANTS_BEFORE_693
    assert upgraded_public == []
    assert restored_public == before_public


def test_downgrade_across_the_consolidated_baseline_is_unsupported():
    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        label="baseline_downgrade",
        migration_revision=SUPPORTED_HEAD,
        reuse_migrated_template=True,
    ) as database:
        assert _migration_head(database.session_factory) == SUPPORTED_HEAD
        database_url = configured.set(database=database.name)
        # This case owns baseline -> base refusal. The separate fresh-head and
        # supported-transition cases already prove the successor in both
        # directions; rebuilding it here only repeats that setup and rollback.
        completed = _alembic(database_url, "downgrade", "base")

    assert completed.returncode != 0
    assert (
        "consolidated schema baseline downgrade is unsupported"
        in completed.stderr
    )


def _fact_and_revision_bytes(session, identity: dict) -> tuple:
    """All original Fact, source-link and accepted-authority fields, without rewriting."""

    return tuple(session.execute(text(
        "select (select to_jsonb(f) from facts f where id = :fact_id), "
        "(select jsonb_agg(to_jsonb(s) order by id) from fact_sources s where fact_id = :fact_id), "
        "(select to_jsonb(r) from project_record_revisions r where id = :revision_id)"
    ), identity).one())


def _project_row(session_factory):
    with session_factory() as session:
        return session.execute(
            text(
                "select slug, name, is_synthetic from projects "
                "where slug = 'baseline-bridge'"
            )
        ).one()


def _seed_legacy_release_and_artifact(session, project_id: int) -> None:
    pdf_bytes = b"%PDF-1.7\nlegacy release\n%%EOF"
    digest = sha256(pdf_bytes).hexdigest()
    artifact_id = session.scalar(
        text(
            "insert into external_report_artifacts ("
            "project_id, artifact_name, format, pdf_bytes, pdf_sha256, "
            "evaluated_on, ruleset_version, evaluation_context_json, "
            "provenance_mode, record_context_json"
            ") values ("
            ":project_id, 'artifact-backed.pdf', 'pdf', :pdf_bytes, :digest, "
            "date '2026-08-31', 'v0.4', '{}'::jsonb, "
            "'all-supported-sources', '{}'::jsonb"
            ") returning id"
        ),
        {"project_id": project_id, "pdf_bytes": pdf_bytes, "digest": digest},
    )
    assert artifact_id is not None
    session.execute(
        text(
            "insert into external_report_releases ("
            "project_id, artifact_name, format, pdf_bytes, pdf_sha256, "
            "evaluated_on, ruleset_version, evaluation_context_json, "
            "provenance_mode, record_context_json, released_by, released_by_display"
            ") values ("
            ":project_id, 'legacy-release.pdf', 'pdf', :pdf_bytes, :digest, "
            "date '2026-08-31', 'v0.4', '{}'::jsonb, "
            "'all-supported-sources', '{\"dependencies\": []}'::jsonb, "
            "'local:legacy', 'Legacy Releaser'"
            ")"
        ),
        {"project_id": project_id, "pdf_bytes": pdf_bytes, "digest": digest},
    )


def _legacy_release_row(session_factory):
    with session_factory() as session:
        return session.execute(
            text(
                "select artifact_id, artifact_name, pdf_bytes, pdf_sha256, "
                "evaluation_context_json, record_context_json, released_by "
                "from external_report_releases "
                "where artifact_name = 'legacy-release.pdf'"
            )
        ).one()


def _assert_legacy_release_reader(session_factory, project_id: int) -> None:
    with session_factory() as session:
        release_id = session.scalar(
            text(
                "select id from external_report_releases "
                "where artifact_name = 'legacy-release.pdf'"
            )
        )
        release = retrieve_released_external_report(
            session, project_id, int(release_id)
        )
        assert release.content_storage == "legacy"
        assert release.artifact_id is None
        assert release.pdf_bytes == b"%PDF-1.7\nlegacy release\n%%EOF"
        assert release.record_context_json == {"dependencies": []}
        assert release.digest_is_valid is True


def _insert_artifact_reference_release(session_factory) -> None:
    with session_factory.begin() as session:
        project_id = session.scalar(
            text("select id from projects where slug = 'baseline-bridge'")
        )
        artifact_id = session.scalar(
            text(
                "select id from external_report_artifacts "
                "where artifact_name = 'artifact-backed.pdf'"
            )
        )
        artifact_digest = session.scalar(
            text(
                "select pdf_sha256 from external_report_artifacts where id = :artifact_id"
            ),
            {"artifact_id": artifact_id},
        )
        release_id = session.scalar(
            text(
                "insert into external_report_releases ("
                "project_id, artifact_id, artifact_name, format, pdf_sha256, "
                "evaluated_on, ruleset_version, provenance_mode, "
                "released_by, released_by_display"
                ") values ("
                ":project_id, :artifact_id, 'artifact-backed.pdf', 'pdf', :digest, "
                "date '2026-08-31', 'v0.4', 'all-supported-sources', "
                "'local:new', 'New Releaser'"
                ") returning id"
            ),
            {
                "project_id": project_id,
                "artifact_id": artifact_id,
                "digest": artifact_digest,
            },
        )
        stored = session.execute(
            text(
                "select pdf_bytes, evaluation_context_json, record_context_json "
                "from external_report_releases where id = :release_id"
            ),
            {"release_id": release_id},
        ).one()
        assert tuple(stored) == (None, None, None)
        duplicated_artifact_id = session.scalar(
            text(
                "insert into external_report_artifacts ("
                "project_id, artifact_name, format, pdf_bytes, pdf_sha256, "
                "evaluated_on, ruleset_version, evaluation_context_json, "
                "provenance_mode, record_context_json"
                ") select project_id, 'duplicate-owner.pdf', format, pdf_bytes, "
                "pdf_sha256, evaluated_on, ruleset_version, evaluation_context_json, "
                "provenance_mode, record_context_json "
                "from external_report_artifacts where id = :artifact_id "
                "returning id"
            ),
            {"artifact_id": artifact_id},
        )
        with pytest.raises(Exception, match="content owner"):
            with session.begin_nested():
                session.execute(
                    text(
                        "insert into external_report_releases ("
                        "project_id, artifact_id, artifact_name, format, pdf_bytes, "
                        "pdf_sha256, evaluated_on, ruleset_version, "
                        "evaluation_context_json, provenance_mode, "
                        "record_context_json, released_by, released_by_display"
                        ") select :project_id, :artifact_id, 'duplicate-owner.pdf', "
                        "format, pdf_bytes, pdf_sha256, evaluated_on, ruleset_version, "
                        "evaluation_context_json, provenance_mode, record_context_json, "
                        "'local:invalid-copy', 'Invalid Copy' "
                        "from external_report_artifacts where id = :artifact_id"
                    ),
                    {
                        "project_id": project_id,
                        "artifact_id": duplicated_artifact_id,
                    },
                )
        with pytest.raises(Exception, match="content_owner"):
            with session.begin_nested():
                session.execute(
                    text(
                        "insert into external_report_releases ("
                        "project_id, artifact_name, format, pdf_sha256, "
                        "evaluated_on, ruleset_version, provenance_mode, "
                        "released_by, released_by_display"
                        ") values ("
                        ":project_id, 'missing-content-owner.pdf', 'pdf', :digest, "
                        "date '2026-08-31', 'v0.4', 'all-supported-sources', "
                        "'local:invalid', 'Invalid Releaser'"
                        ")"
                    ),
                    {"project_id": project_id, "digest": artifact_digest},
                )


def _migration_head(session_factory) -> str:
    with session_factory() as session:
        return str(session.scalar(text("select version_num from alembic_version")))


def _source_segment_rows(session_factory) -> list[tuple]:
    with session_factory() as session:
        return list(
            session.execute(
                text(
                    "select id, project_id, document_id, kind, exact_text, cell_range "
                    "from source_segments order by id"
                )
            ).all()
        )


def _fact_rows(session_factory) -> list[tuple]:
    with session_factory() as session:
        return list(
            session.execute(
                text(
                    "select id, document_id, extraction_run_id, fact_type, "
                    "subject_key, text_value from facts order by id"
                )
            ).all()
        )


SOURCE_TABLES = (
    "source_segments",
    "facts",
    "fact_sources",
    "fact_applies_to",
    "fact_closure_results",
    "fact_closure_sources",
    "fact_statement_timings",
    "extracted_proposals",
    "extracted_proposal_facts",
    "source_fact_append_receipts",
)
# Born in b2d5f8a1c4e7 (#530): absent before it, appended only through the
# command after it.
SUPPORT_TABLES = (
    "support_assessments",
    "support_assessment_sources",
)
SOURCE_APPEND_COMMANDS = (
    "append_source_segments",
    "append_fact",
    "append_extracted_proposal",
    "append_source_fact_receipt",
    "append_support_assessment",
)
# Before b2d5f8a1c4e7: the runtime capabilities write the source tables raw,
# no append command exists, and the Support Assessment relation does not
# exist (None, as distinct from a table nobody may write).
SOURCE_APPEND_OPEN = {
    "runtime_insert": {
        **{table: ("corridor_web", "corridor_worker") for table in SOURCE_TABLES},
        **{table: None for table in SUPPORT_TABLES},
    },
    "runtime_select": {
        **{table: ("corridor_web", "corridor_worker") for table in SOURCE_TABLES},
        **{table: None for table in SUPPORT_TABLES},
    },
    "commands": {},
}
# After it: nobody writes them raw, both capabilities read them all, and
# every command is owned by the source-append role, hidden from PUBLIC, and
# callable by both capabilities.
SOURCE_APPEND_SECURED = {
    "runtime_insert": {table: () for table in SOURCE_TABLES + SUPPORT_TABLES},
    "runtime_select": {
        table: ("corridor_web", "corridor_worker")
        for table in SOURCE_TABLES + SUPPORT_TABLES
    },
    "commands": {
        command: {
            "owner": "corridor_source_append",
            "public_execute": False,
            "execute": ("corridor_web", "corridor_worker"),
        }
        for command in SOURCE_APPEND_COMMANDS
    },
}


def _runtime_table_privilege(session, table: str, privilege: str):
    """Which runtime capabilities hold one privilege, or None if no such table."""

    if session.scalar(text("select to_regclass(:table)"), {"table": table}) is None:
        return None
    return tuple(
        session.scalars(
            text(
                "select r from unnest(array['corridor_web', 'corridor_worker']) as r "
                "where has_table_privilege(r, :table, :privilege) order by r"
            ),
            {"table": table, "privilege": privilege},
        ).all()
    )


def _source_append_security(session_factory) -> dict:
    with session_factory() as session:
        runtime_insert = {
            table: _runtime_table_privilege(session, table, "insert")
            for table in SOURCE_TABLES + SUPPORT_TABLES
        }
        runtime_select = {
            table: _runtime_table_privilege(session, table, "select")
            for table in SOURCE_TABLES + SUPPORT_TABLES
        }
        commands = {}
        for name in SOURCE_APPEND_COMMANDS:
            row = session.execute(
                text(
                    "select owner.rolname, "
                    "has_function_privilege('public', function.oid, 'execute'), "
                    "has_function_privilege('corridor_web', function.oid, 'execute'), "
                    "has_function_privilege('corridor_worker', function.oid, 'execute') "
                    "from pg_proc function "
                    "join pg_namespace namespace on namespace.oid = function.pronamespace "
                    "join pg_roles owner on owner.oid = function.proowner "
                    "where namespace.nspname = 'public' and function.proname = :name"
                ),
                {"name": name},
            ).first()
            if row is None:
                continue
            commands[name] = {
                "owner": str(row[0]),
                "public_execute": bool(row[1]),
                "execute": tuple(
                    role
                    for role, granted in (
                        ("corridor_web", row[2]),
                        ("corridor_worker", row[3]),
                    )
                    if granted
                ),
            }
    return {
        "runtime_insert": runtime_insert,
        "runtime_select": runtime_select,
        "commands": commands,
    }


def _statement_retirement_security(session_factory) -> dict:
    with session_factory() as session:
        can_login, inherits = session.execute(
            text(
                "select rolcanlogin, rolinherit from pg_roles "
                "where rolname = 'corridor_statement_retirement'"
            )
        ).one()
        function_owner = session.scalar(
            text(
                "select owner.rolname from pg_proc function "
                "join pg_namespace namespace on namespace.oid = function.pronamespace "
                "join pg_roles owner on owner.oid = function.proowner "
                "where namespace.nspname = 'public' "
                "and function.proname = 'purge_external_party_statement_rows'"
            )
        )
        public_execute = session.scalar(
            text(
                "select has_function_privilege("
                "'public', "
                "'public.purge_external_party_statement_rows(bigint,text)', "
                "'execute')"
            )
        )
        rows = session.execute(
            text(
                "select table_name, privilege_type "
                "from information_schema.role_table_grants "
                "where table_schema = 'public' "
                "and grantee = 'corridor_statement_retirement' "
                "order by table_name, privilege_type"
            )
        ).all()
    tables: dict[str, list[str]] = {}
    for table_name, privilege in rows:
        tables.setdefault(str(table_name), []).append(str(privilege))
    return {
        "can_login": bool(can_login),
        "inherits": bool(inherits),
        "function_owner": str(function_owner),
        "function_public_execute": bool(public_execute),
        "tables": {name: tuple(values) for name, values in tables.items()},
    }


# --- #693 The PUBLIC grants the transition removes, and hands back ---------
#
# #680's revoke reported success on a relation that stayed readable, because
# `REVOKE ... FROM corridor_web` does not touch a grant to PUBLIC. #693 sweeps
# them out of the catalog. The downgrade has to hand back the exact shape it
# found — same relation, same privilege, same grantor — so this reads the
# effective ACL on both sides of a real `alembic downgrade` rather than
# trusting that a re-grant is symmetrical.

_PUBLIC_RELATION_GRANTS = text(
    "select c.relname, a.privilege_type, pg_get_userbyid(a.grantor) as grantor, "
    "       a.is_grantable "
    "  from pg_class c "
    "  join pg_namespace n on n.oid = c.relnamespace "
    "  cross join lateral aclexplode(c.relacl) a "
    " where n.nspname = 'public' "
    "   and c.relkind in ('r', 'p', 'v', 'm', 'f') "
    "   and a.grantee = 0 "
    " union all "
    "select c.relname || '.' || att.attname, a.privilege_type, "
    "       pg_get_userbyid(a.grantor), a.is_grantable "
    "  from pg_class c "
    "  join pg_namespace n on n.oid = c.relnamespace "
    "  join pg_attribute att "
    "    on att.attrelid = c.oid and att.attnum > 0 and not att.attisdropped "
    "  cross join lateral aclexplode(att.attacl) a "
    " where n.nspname = 'public' and a.grantee = 0 "
    " order by 1, 2"
)

PUBLIC_GRANTS_BEFORE_693 = [
    ("fact_decisions", "SELECT", "corridor_fact_decision_writer", False),
    ("project_record_revisions", "SELECT", "corridor_fact_decision_writer", False),
    ("subject_resolution_decisions", "SELECT", "corridor_fact_decision_writer", False),
]


def _public_relation_grants(session_factory):
    with session_factory() as session:
        return [tuple(row) for row in session.execute(_PUBLIC_RELATION_GRANTS)]


def _alembic(database_url, command: str, target: str):
    environment = {
        **os.environ,
        "DATABASE_URL": database_url.render_as_string(hide_password=False),
    }
    return subprocess.run(
        ["uv", "run", "alembic", command, target],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


# --- The #512 recorded-verbal backfill, on its exact transformed rows -------

VERBAL_WORDS = "Equistar will submit the signed exhibit by March 2025."
VERBAL_RECORDER = "local:dana-fields"
# Supplied logical times. The recorded time this transition must preserve is
# written explicitly, so the assertion is exact and reads no wall clock.
VERBAL_RECORDED_AT = datetime(2025, 3, 3, 14, 30, tzinfo=timezone.utc)
VERBAL_CONVERSATION_DATE = date(2025, 3, 3)
SECOND_VERBAL_WORDS = "Oncor will set the pole the week of April 7, 2025."
SECOND_VERBAL_RECORDED_AT = datetime(2025, 4, 1, 9, 15, tzinfo=timezone.utc)
SECOND_VERBAL_CONVERSATION_DATE = date(2025, 4, 1)


def _seed_dual_written_verbal(
    session,
    *,
    slug: str,
    words: str,
    recorded_at: datetime,
    conversation_date: date,
    dependency_count: int,
    unreconcilable_fact_type: str | None = None,
) -> dict:
    """Write one verbal exactly as the #451 dual-write left it at the released head."""

    project_id = session.scalar(
        text(
            "insert into projects (slug, name, is_synthetic) "
            "values (:slug, :name, true) returning id"
        ),
        {"slug": slug, "name": slug},
    )
    dependency_ids = [
        session.scalar(
            text(
                "insert into dependencies (project_id, ref_code, dep_type, title) "
                "values (:project_id, :ref_code, 'utility_relocation', :title) "
                "returning id"
            ),
            {
                "project_id": project_id,
                "ref_code": f"DEP-{index + 1}",
                "title": f"{slug} constraint {index + 1}",
            },
        )
        for index in range(dependency_count)
    ]
    org_id = session.scalar(
        text(
            "insert into external_orgs (name, org_type) "
            "values (:name, 'utility') returning id"
        ),
        {"name": f"{slug} utility"},
    )
    lineage_id = session.scalar(
        text(
            "insert into commitment_lineages (project_id) values (:project_id) "
            "returning id"
        ),
        {"project_id": project_id},
    )
    statement_id = session.scalar(
        text(
            "insert into dependency_events ("
            "project_id, commitment_lineage_id, event_type, source_kind, "
            "affected_external_org_id, stated_external_org_id, attribution_state, "
            "stated_party, description, created_by, event_date, created_at"
            ") values ("
            ":project_id, :lineage_id, 'commitment', 'verbal', :org_id, :org_id, "
            "'resolved', :party, :words, :recorder, :conversation_date, "
            ":recorded_at) returning id"
        ),
        {
            "project_id": project_id,
            "lineage_id": lineage_id,
            "org_id": org_id,
            "party": f"{slug} utility",
            "words": words,
            "recorder": VERBAL_RECORDER,
            "conversation_date": conversation_date,
            "recorded_at": recorded_at,
        },
    )
    session.execute(
        text(
            "insert into dependency_event_timings ("
            "event_id, kind, text, precision, start_date, end_date"
            ") values (:event_id, 'new', 'March 3, 2025', 'day', "
            "date '2025-03-03', date '2025-03-03')"
        ),
        {"event_id": statement_id},
    )
    segment_id = session.scalar(
        text(
            "insert into source_segments ("
            "project_id, statement_id, kind, exact_text, content_sha256, ordinal"
            ") values ("
            ":project_id, :statement_id, 'recorded_verbal_statement', :words, "
            ":digest, 1) returning id"
        ),
        {
            "project_id": project_id,
            "statement_id": statement_id,
            "words": words,
            "digest": sha256(words.encode("utf-8")).hexdigest(),
        },
    )
    subject_key = f"lineage:{lineage_id}"
    timings = (("new", StatementTiming.day("March 3, 2025", date(2025, 3, 3))),)
    expected = _verbal_fact_digests(
        segment_id=segment_id,
        project_id=project_id,
        words=words,
        subject_key=subject_key,
        run_identity={"statement_id": statement_id},
        timings=timings,
        dependency_ids=tuple(dependency_ids),
    )
    if unreconcilable_fact_type is not None:
        # A stored identity the recipe cannot reproduce, written as it would
        # have to arrive: `facts` refuses every later UPDATE.
        expected = {**expected, unreconcilable_fact_type: "0" * 64}

    fact_ids = {}
    fact_ids["statement_wording"] = session.scalar(
        text(
            "insert into facts ("
            "project_id, fact_type, subject_kind, subject_key, text_value, "
            "transformation, recorded_by, content_sha256"
            ") values ("
            ":project_id, 'statement_wording', 'statement_candidate', :subject_key, "
            ":words, 'exact_prose_span_v1', :recorder, :digest) returning id"
        ),
        {
            "project_id": project_id,
            "subject_key": subject_key,
            "words": words,
            "recorder": VERBAL_RECORDER,
            "digest": expected["statement_wording"],
        },
    )
    for role in ("value_source", "attribution_source"):
        session.execute(
            text(
                "insert into fact_sources ("
                "project_id, fact_id, source_segment_id, role, ordinal"
                ") values (:project_id, :fact_id, :segment_id, :role, 1)"
            ),
            {
                "project_id": project_id,
                "fact_id": fact_ids["statement_wording"],
                "segment_id": segment_id,
                "role": role,
            },
        )

    fact_ids["statement_timing"] = session.scalar(
        text(
            "insert into facts ("
            "project_id, fact_type, subject_kind, subject_key, transformation, "
            "recorded_by, content_sha256"
            ") values ("
            ":project_id, 'statement_timing', 'statement_candidate', :subject_key, "
            "'typed_statement_timing_v1', :recorder, :digest) returning id"
        ),
        {
            "project_id": project_id,
            "subject_key": subject_key,
            "recorder": VERBAL_RECORDER,
            "digest": expected["statement_timing"],
        },
    )
    session.execute(
        text(
            "insert into fact_sources ("
            "project_id, fact_id, source_segment_id, role, ordinal"
            ") values (:project_id, :fact_id, :segment_id, 'value_source', 1)"
        ),
        {
            "project_id": project_id,
            "fact_id": fact_ids["statement_timing"],
            "segment_id": segment_id,
        },
    )
    for role, timing in timings:
        session.execute(
            text(
                "insert into fact_statement_timings ("
                "project_id, fact_id, timing_role, text, precision, "
                "start_date, end_date"
                ") values ("
                ":project_id, :fact_id, :role, :timing_text, :precision, "
                ":start_date, :end_date)"
            ),
            {
                "project_id": project_id,
                "fact_id": fact_ids["statement_timing"],
                "role": role,
                "timing_text": timing.text,
                "precision": timing.precision,
                "start_date": timing.start_date,
                "end_date": timing.end_date,
            },
        )

    fact_ids["applies_to"] = session.scalar(
        text(
            "insert into facts ("
            "project_id, fact_type, subject_kind, subject_key, transformation, "
            "recorded_by, content_sha256"
            ") values ("
            ":project_id, 'applies_to', 'statement_candidate', :subject_key, "
            "'structured_reference_set_v1', :recorder, :digest) returning id"
        ),
        {
            "project_id": project_id,
            "subject_key": subject_key,
            "recorder": VERBAL_RECORDER,
            "digest": expected["applies_to"],
        },
    )
    session.execute(
        text(
            "insert into fact_sources ("
            "project_id, fact_id, source_segment_id, role, ordinal"
            ") values (:project_id, :fact_id, :segment_id, 'value_source', 1)"
        ),
        {
            "project_id": project_id,
            "fact_id": fact_ids["applies_to"],
            "segment_id": segment_id,
        },
    )
    for slot, dependency_id in enumerate(dependency_ids, start=1):
        session.execute(
            text(
                "insert into fact_applies_to ("
                "project_id, fact_id, dependency_id, ordinal"
                ") values (:project_id, :fact_id, :dependency_id, :ordinal)"
            ),
            {
                "project_id": project_id,
                "fact_id": fact_ids["applies_to"],
                "dependency_id": dependency_id,
                "ordinal": slot,
            },
        )

    return {
        "project_id": project_id,
        "statement_id": statement_id,
        "segment_id": segment_id,
        "subject_key": subject_key,
        "words": words,
        "recorded_at": recorded_at,
        "conversation_date": conversation_date,
        "timings": timings,
        "dependency_ids": tuple(dependency_ids),
        "fact_ids": fact_ids,
        "prior_digests": expected,
    }


def _verbal_fact_digests(
    *,
    segment_id: int,
    project_id: int,
    words: str,
    subject_key: str,
    run_identity: dict,
    timings,
    dependency_ids,
) -> dict[str, str]:
    """The three Fact identity digests, from the application's own recipe.

    The migration carries a frozen copy of that recipe; computing the expected
    values here proves the copy still agrees with `corridor.facts`, on both the
    digest the backfill must reproduce and the one it must write.
    """

    segment = SourceSegment(
        project_id=project_id,
        document_id=None,
        recorded_verbal_origin_id=0,
        kind="recorded_verbal_statement",
        exact_text=words,
        content_sha256=sha256(words.encode("utf-8")).hexdigest(),
        ordinal=1,
    )
    segment.id = segment_id
    return {
        "statement_wording": _fact_digest(
            run_identity=run_identity,
            subject_kind="statement_candidate",
            subject_key=subject_key,
            value=materialize_quoted_statement_wording(segment, words),
        ),
        "statement_timing": _fact_digest(
            run_identity=run_identity,
            subject_kind="statement_candidate",
            subject_key=subject_key,
            value=materialize_typed_satellite("statement_timing", segment),
            structured_value=_statement_timing_structured(timings),
        ),
        "applies_to": _fact_digest(
            run_identity=run_identity,
            subject_kind="statement_candidate",
            subject_key=subject_key,
            value=materialize_typed_satellite("applies_to", segment),
            structured_value={"dependency_ids": list(dependency_ids)},
        ),
    }


def test_the_recorded_verbal_backfill_reconciles_one_to_one(tmp_path):
    """ADR-0081 stage 1, proved on the exact rows the transition transformed.

    Two dual-written verbals cross the transition. Each must gain exactly one
    origin carrying the recorder and the original recorded time, exactly one
    compatibility mapping, exactly one receipt, and three Fact identity digests
    replaced from a receipt that names both values. Nothing else moves, and the
    downgrade puts every one of them back.
    """

    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        label="baseline_verbal",
        migration_revision=SUPPORTED_HEAD,
        reuse_migrated_template=True,
    ) as database:
        database_url = configured.set(database=database.name)
        with database.session_factory.begin() as session:
            first = _seed_dual_written_verbal(
                session,
                slug="verbal-backfill-one",
                words=VERBAL_WORDS,
                recorded_at=VERBAL_RECORDED_AT,
                conversation_date=VERBAL_CONVERSATION_DATE,
                dependency_count=2,
            )
            second = _seed_dual_written_verbal(
                session,
                slug="verbal-backfill-two",
                words=SECOND_VERBAL_WORDS,
                recorded_at=SECOND_VERBAL_RECORDED_AT,
                conversation_date=SECOND_VERBAL_CONVERSATION_DATE,
                dependency_count=0,
            )

        upgraded = _alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr

        with database.session_factory() as session:
            receipts = session.execute(
                text(
                    "select receipt.legacy_statement_id, receipt.source_segment_id, "
                    "       receipt.origin_id, receipt.project_id, "
                    "       receipt.fact_count, receipt.migration_revision, "
                    "       receipt.executed_by, "
                    "       origin.recorded_by, origin.recorded_at, "
                    "       origin.conversation_date, origin.exact_text, "
                    "       origin.content_sha256, origin.corrects_origin_id, "
                    "       mapping.statement_id as mapped_statement_id, "
                    "       segment.recorded_verbal_origin_id as segment_origin_id "
                    "  from recorded_verbal_origin_backfill_receipts receipt "
                    "  join recorded_verbal_origins origin "
                    "    on origin.id = receipt.origin_id "
                    "  join recorded_verbal_origin_statements mapping "
                    "    on mapping.origin_id = receipt.origin_id "
                    "  join source_segments segment "
                    "    on segment.id = receipt.source_segment_id "
                    " order by receipt.legacy_statement_id"
                )
            ).all()

            assert len(receipts) == 2
            for receipt, seeded in zip(receipts, (first, second)):
                assert receipt.legacy_statement_id == seeded["statement_id"]
                assert receipt.source_segment_id == seeded["segment_id"]
                assert receipt.mapped_statement_id == seeded["statement_id"]
                assert receipt.segment_origin_id == receipt.origin_id
                assert receipt.project_id == seeded["project_id"]
                assert receipt.fact_count == 3
                assert receipt.migration_revision == CURRENT_HEAD
                assert receipt.executed_by == "migration:b2d5f8a1c4e7/512"
                # The recorder attested; the migration only moved the row.
                assert receipt.recorded_by == VERBAL_RECORDER
                assert receipt.recorded_at == seeded["recorded_at"]
                assert receipt.conversation_date == seeded["conversation_date"]
                assert receipt.exact_text == seeded["words"]
                assert receipt.content_sha256 == sha256(
                    seeded["words"].encode("utf-8")
                ).hexdigest()
                assert receipt.corrects_origin_id is None

            # One origin, one mapping, one receipt per verbal; nothing else.
            counts = session.execute(
                text(
                    "select (select count(*) from recorded_verbal_origins) as origins, "
                    "  (select count(*) from recorded_verbal_origin_statements) "
                    "    as mappings, "
                    "  (select count(*) from "
                    "    recorded_verbal_origin_backfill_receipts) as receipts, "
                    "  (select count(*) from recorded_verbal_origin_fact_digests) "
                    "    as digests, "
                    "  (select count(*) from source_segments "
                    "    where kind = 'recorded_verbal_statement' "
                    "      and recorded_verbal_origin_id is null) as unpointed"
                )
            ).one()
            assert (
                counts.origins,
                counts.mappings,
                counts.receipts,
                counts.digests,
                counts.unpointed,
            ) == (2, 2, 2, 6, 0)

            for receipt, seeded in zip(receipts, (first, second)):
                after = _verbal_fact_digests(
                    segment_id=seeded["segment_id"],
                    project_id=seeded["project_id"],
                    words=seeded["words"],
                    subject_key=seeded["subject_key"],
                    run_identity={"recorded_verbal_origin_id": receipt.origin_id},
                    timings=seeded["timings"],
                    dependency_ids=seeded["dependency_ids"],
                )
                for fact_type, fact_id in seeded["fact_ids"].items():
                    stored = session.execute(
                        text(
                            "select fact.content_sha256 as digest, "
                            "       change.prior_content_sha256 as prior, "
                            "       change.content_sha256 as recorded, "
                            "       change.receipt_id as receipt_id, "
                            "       change.project_id as project_id "
                            "  from facts fact "
                            "  join recorded_verbal_origin_fact_digests change "
                            "    on change.fact_id = fact.id "
                            " where fact.id = :fact_id"
                        ),
                        {"fact_id": fact_id},
                    ).one()
                    assert stored.digest == after[fact_type]
                    assert stored.recorded == after[fact_type]
                    assert stored.prior == seeded["prior_digests"][fact_type]
                    assert stored.project_id == seeded["project_id"]

        downgraded = _alembic(database_url, "downgrade", SUPPORTED_HEAD)
        assert downgraded.returncode == 0, downgraded.stderr

        with database.session_factory() as session:
            for seeded in (first, second):
                restored = session.execute(
                    text(
                        "select statement_id, kind, exact_text "
                        "  from source_segments where id = :segment_id"
                    ),
                    {"segment_id": seeded["segment_id"]},
                ).one()
                assert restored.statement_id == seeded["statement_id"]
                assert restored.kind == "recorded_verbal_statement"
                assert restored.exact_text == seeded["words"]
                for fact_type, fact_id in seeded["fact_ids"].items():
                    digest = session.scalar(
                        text("select content_sha256 from facts where id = :fact_id"),
                        {"fact_id": fact_id},
                    )
                    assert digest == seeded["prior_digests"][fact_type]
            assert session.scalar(
                text(
                    "select count(*) from information_schema.tables "
                    " where table_schema = 'public' "
                    "   and table_name like 'recorded_verbal%'"
                )
            ) == 0


def test_the_recorded_verbal_backfill_refuses_an_unreconcilable_fact(tmp_path):
    """A Fact whose stored identity cannot be reproduced aborts the transition.

    The backfill derives the replacement digest from the same reconstruction
    that must first reproduce the stored one. A row it cannot reproduce is a
    row it does not understand, so it refuses rather than writing a digest that
    would silently redefine the Fact.
    """

    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        label="baseline_verbal_bad",
        migration_revision=SUPPORTED_HEAD,
        reuse_migrated_template=True,
    ) as database:
        database_url = configured.set(database=database.name)
        with database.session_factory.begin() as session:
            seeded = _seed_dual_written_verbal(
                session,
                slug="verbal-backfill-broken",
                words=VERBAL_WORDS,
                recorded_at=VERBAL_RECORDED_AT,
                conversation_date=VERBAL_CONVERSATION_DATE,
                dependency_count=1,
                unreconcilable_fact_type="statement_wording",
            )

        completed = _alembic(database_url, "upgrade", "head")

        assert completed.returncode != 0
        assert "#512 backfill refuses" in completed.stderr
        assert _migration_head(database.session_factory) == SUPPORTED_HEAD
        with database.session_factory() as session:
            assert session.scalar(
                text(
                    "select statement_id from source_segments where id = :segment_id"
                ),
                {"segment_id": seeded["segment_id"]},
            ) == seeded["statement_id"]
            assert session.scalar(
                text(
                    "select count(*) from information_schema.tables "
                    " where table_schema = 'public' "
                    "   and table_name like 'recorded_verbal%'"
                )
            ) == 0


DEDUPLICATION_WORDS = "Oncor will relocate the pole before 3 March 2025."


def _seed_identified_fact(session, *, slug: str, digest: str | None) -> dict:
    """One project-scoped Fact at the supported head, with or without identity.

    ``facts.content_sha256`` is nullable there behind a partial unique index,
    which is exactly what #457 makes permanent state: a Fact with no digest is
    a Fact with no identity, and two of them are indistinguishable.
    """

    project_id = session.scalar(
        text(
            "insert into projects (slug, name, is_synthetic) "
            "values (:slug, :name, true) returning id"
        ),
        {"slug": slug, "name": slug},
    )
    document_id = session.scalar(
        text(
            "insert into documents (project_id, sha256, filename, doc_type) "
            "values (:project_id, :digest, 'minutes.pdf', 'minutes') returning id"
        ),
        {
            "project_id": project_id,
            "digest": sha256(slug.encode("utf-8")).hexdigest(),
        },
    )
    segment_id = session.scalar(
        text(
            "insert into source_segments ("
            "project_id, document_id, kind, exact_text, content_sha256, ordinal, "
            "page_no, start_offset, end_offset"
            ") values ("
            ":project_id, :document_id, 'prose_span', :words, :digest, 1, "
            "1, 0, :end_offset) returning id"
        ),
        {
            "project_id": project_id,
            "document_id": document_id,
            "words": DEDUPLICATION_WORDS,
            "digest": sha256(DEDUPLICATION_WORDS.encode("utf-8")).hexdigest(),
            "end_offset": len(DEDUPLICATION_WORDS),
        },
    )
    subject_key = f"candidate:{segment_id}"
    fact_id = session.scalar(
        text(
            "insert into facts ("
            "project_id, fact_type, subject_kind, subject_key, text_value, "
            "transformation, recorded_by, content_sha256"
            ") values ("
            ":project_id, 'statement_wording', 'statement_candidate', "
            ":subject_key, :words, 'exact_prose_span_v1', 'local:test', :digest"
            ") returning id"
        ),
        {
            "project_id": project_id,
            "subject_key": subject_key,
            "words": DEDUPLICATION_WORDS,
            "digest": digest,
        },
    )
    session.execute(
        text(
            "insert into fact_sources ("
            "project_id, fact_id, source_segment_id, role, ordinal"
            ") values (:project_id, :fact_id, :segment_id, 'value_source', 1)"
        ),
        {"project_id": project_id, "fact_id": fact_id, "segment_id": segment_id},
    )
    # A revision is written only by the role that owns accepted authority; a
    # guard trigger refuses every other writer, this seed included.
    with as_role(session, RECORD_DECISION_ROLE):
        revision_id = session.scalar(
            text(
                "insert into project_record_revisions ("
                "project_id, command_type, human_principal, released_policy, "
                "idempotency_key"
                ") values ("
                ":project_id, 'record_human_fact_decision', 'local:coordinator', "
                "null, :key) returning id"
            ),
            {"project_id": project_id, "key": f"decide:{fact_id}"},
        )
    return {
        "project_id": project_id,
        "document_id": document_id,
        "segment_id": segment_id,
        "subject_key": subject_key,
        "fact_id": fact_id,
        "revision_id": revision_id,
        "digest": digest,
        "idempotency_key": f"decide:{fact_id}",
    }


def test_the_deduplication_transition_carries_identified_rows_across_unchanged():
    """#457, proved on the exact rows the transition constrains.

    The block writes no row: it turns each family's derived identity into a
    constraint. So the proof is that an identified Fact and its revision cross
    unchanged, that the identity is afterwards a property of the table rather
    than of the command — an exact copy is refused — and that the downgrade
    leaves both rows exactly as they were seeded.
    """

    configured = make_url(settings.database_url)
    digest = sha256(f"fact:{DEDUPLICATION_WORDS}".encode("utf-8")).hexdigest()
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        label="baseline_dedup",
        migration_revision=SUPPORTED_HEAD,
        reuse_migrated_template=True,
    ) as database:
        database_url = configured.set(database=database.name)
        with database.session_factory.begin() as session:
            seeded = _seed_identified_fact(
                session, slug="dedup-identified", digest=digest
            )

        upgraded = _alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr

        with database.session_factory() as session:
            fact = session.execute(
                text(
                    "select project_id, fact_type, subject_kind, subject_key, "
                    "       text_value, transformation, recorded_by, content_sha256 "
                    "  from facts where id = :fact_id"
                ),
                {"fact_id": seeded["fact_id"]},
            ).one()
            assert fact.project_id == seeded["project_id"]
            assert fact.fact_type == "statement_wording"
            assert fact.subject_kind == "statement_candidate"
            assert fact.subject_key == seeded["subject_key"]
            assert fact.text_value == DEDUPLICATION_WORDS
            assert fact.transformation == "exact_prose_span_v1"
            assert fact.recorded_by == "local:test"
            assert fact.content_sha256 == digest

            revision = session.execute(
                text(
                    "select project_id, command_type, human_principal, "
                    "       released_policy, idempotency_key "
                    "  from project_record_revisions where id = :revision_id"
                ),
                {"revision_id": seeded["revision_id"]},
            ).one()
            assert revision.project_id == seeded["project_id"]
            assert revision.command_type == "record_human_fact_decision"
            assert revision.human_principal == "local:coordinator"
            assert revision.released_policy is None
            assert revision.idempotency_key == seeded["idempotency_key"]

            # The identity is now the table's, not the command's.
            assert session.execute(
                text(
                    "select conname, contype from pg_constraint "
                    " where conname in ('uq_facts_content_sha256', "
                    "   'ck_project_record_revisions_idempotency_key', "
                    "   'uq_delta_groups_source_change', "
                    "   'uq_fact_decisions_revision_fact', "
                    "   'uq_delta_deferrals_request', "
                    "   'uq_push_deliveries_envelope', "
                    "   'uq_source_deliveries_observation') order by conname"
                )
            ).all() == [
                ("ck_project_record_revisions_idempotency_key", "c"),
                ("uq_delta_deferrals_request", "u"),
                ("uq_delta_groups_source_change", "u"),
                ("uq_fact_decisions_revision_fact", "u"),
                ("uq_facts_content_sha256", "u"),
                # The connector-delivery identity #457 established, carried
                # onto the family both transports share and widened by the
                # disposition (#599, ADR-0089).
                ("uq_source_deliveries_observation", "u"),
            ]
            assert session.scalar(
                text(
                    "select attnotnull from pg_attribute "
                    " where attrelid = 'public.facts'::regclass "
                    "   and attname = 'content_sha256'"
                )
            ) is True

        with database.session_factory() as session:
            with pytest.raises(DBAPIError, match="uq_facts_content_sha256"):
                session.execute(
                    text(
                        "insert into facts (project_id, fact_type, subject_kind, "
                        "  subject_key, text_value, transformation, recorded_by, "
                        "  content_sha256) "
                        "select project_id, fact_type, subject_kind, subject_key, "
                        "  text_value, transformation, recorded_by, content_sha256 "
                        "  from facts where id = :fact_id"
                    ),
                    {"fact_id": seeded["fact_id"]},
                )

        downgraded = _alembic(database_url, "downgrade", SUPPORTED_HEAD)
        assert downgraded.returncode == 0, downgraded.stderr

        with database.session_factory() as session:
            restored = session.execute(
                text(
                    "select content_sha256, subject_key, text_value "
                    "  from facts where id = :fact_id"
                ),
                {"fact_id": seeded["fact_id"]},
            ).one()
            assert restored.content_sha256 == digest
            assert restored.subject_key == seeded["subject_key"]
            assert restored.text_value == DEDUPLICATION_WORDS
            assert session.scalar(
                text(
                    "select count(*) from pg_constraint "
                    " where conname in ('uq_facts_content_sha256', "
                    "   'ck_project_record_revisions_idempotency_key', "
                    "   'uq_delta_deferrals_request', "
                    "   'uq_push_deliveries_envelope', "
                    "   'uq_source_deliveries_observation')"
                )
            ) == 0
            assert session.scalar(
                text(
                    "select indexdef from pg_indexes "
                    " where indexname = 'uq_facts_content_sha256'"
                )
            ) == (
                "CREATE UNIQUE INDEX uq_facts_content_sha256 ON public.facts "
                "USING btree (content_sha256) WHERE (content_sha256 IS NOT NULL)"
            )


def test_the_deduplication_transition_refuses_a_fact_with_no_identity():
    """A row the new identity cannot represent aborts the transition.

    Which of two indistinguishable rows is the record is a semantic question,
    and no migration has the authority to answer it: the transition counts what
    it cannot represent, names the family, and leaves every row alone.
    """

    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        label="baseline_dedup_bad",
        migration_revision=SUPPORTED_HEAD,
        reuse_migrated_template=True,
    ) as database:
        database_url = configured.set(database=database.name)
        with database.session_factory.begin() as session:
            seeded = _seed_identified_fact(
                session, slug="dedup-unidentified", digest=None
            )

        completed = _alembic(database_url, "upgrade", "head")

        assert completed.returncode != 0
        assert "#457 de-duplication refuses" in completed.stderr
        assert "1 Source Facts with no identity digest" in completed.stderr
        assert "Nothing is merged or dropped here" in completed.stderr
        assert _migration_head(database.session_factory) == SUPPORTED_HEAD
        with database.session_factory() as session:
            assert session.scalar(
                text("select content_sha256 from facts where id = :fact_id"),
                {"fact_id": seeded["fact_id"]},
            ) is None
            assert session.scalar(
                text(
                    "select count(*) from pg_constraint "
                    " where conname = 'uq_source_deliveries_observation'"
                )
            ) == 0


DELIVERY_BYTES = b"%PDF-1.7\nrelocation exhibit\n%%EOF"


def _seed_pulled_delivery(session, *, slug: str) -> dict:
    """One pulled delivery and one push delivery, on the unified family.

    Seeded after the upgrade rather than before it, because the push ledger
    this family is made from is itself created by this same transition: a
    database standing at the supported head has no delivery table at all.
    """

    project_id = session.scalar(
        text(
            "insert into projects (slug, name, is_synthetic) "
            "values (:slug, 'Delivery Family', true) returning id"
        ),
        {"slug": slug},
    )
    digest = sha256(DELIVERY_BYTES).hexdigest()
    identity = sha256(
        f"acme-utilities:{slug}:shared-files:item-a:v1".encode("utf-8")
    ).hexdigest()
    key = sha256(f"{identity}:{digest}".encode("utf-8")).hexdigest()
    delivery_id = session.scalar(
        text(
            "insert into source_deliveries ("
            "customer, project_id, transport, channel, configuration_identity, "
            "configuration_version, external_identity, external_version, "
            "content_sha256, bytes_reference, delivery_identity, "
            "idempotency_key, service_identity, run_identity, disposition"
            ") values ("
            "'acme-utilities', :project_id, 'pull', 'shared-files', "
            "'txdot-rid-box-v1', 'connector-polling-v1', 'item-a', 'v1', "
            ":digest, :reference, :identity, :key, "
            "'corridor.connector_polling', 'due-attempt:seed', 'stored'"
            ") returning id"
        ),
        {
            "project_id": project_id,
            "digest": digest,
            "reference": f"{digest[:2]}/{digest}.pdf",
            "identity": identity,
            "key": key,
        },
    )
    return {
        "project_id": project_id,
        "delivery_id": delivery_id,
        "digest": digest,
        "delivery_identity": identity,
        "idempotency_key": key,
    }


def test_the_delivery_family_transition_refuses_a_downgrade_that_would_lose_a_pull():
    """#599, proved on the exact rows the push-only ledger cannot hold.

    The upgrade renames rather than copies, so there is nothing to reconcile on
    a fresh database — the push ledger is born in this same transition. What
    the transition still owes is the other direction: a pulled delivery, and a
    refused one, have no representation in the shape the downgrade restores,
    and dropping them is exactly the loss ADR-0089 set out to remove. The
    transition counts what it cannot carry back, names it, and leaves every row
    exactly as it was.
    """

    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        label="baseline_delivery",
        migration_revision=SUPPORTED_HEAD,
        reuse_migrated_template=True,
    ) as database:
        database_url = configured.set(database=database.name)
        upgraded = _alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr

        with database.session_factory.begin() as session:
            seeded = _seed_pulled_delivery(session, slug="delivery-family")

        refused = _alembic(database_url, "downgrade", SUPPORTED_HEAD)

        assert refused.returncode != 0
        assert "#599 downgrade refuses" in refused.stderr
        assert "1 delivery row(s) are pulled, refused, or failed" in refused.stderr
        assert "Nothing is merged or dropped here" in refused.stderr
        assert _migration_head(database.session_factory) == CURRENT_HEAD

        with database.session_factory() as session:
            row = session.execute(
                text(
                    "select customer, project_id, transport, channel, "
                    "       configuration_identity, configuration_version, "
                    "       external_identity, external_version, content_sha256, "
                    "       bytes_reference, delivery_identity, idempotency_key, "
                    "       service_identity, run_identity, disposition, "
                    "       refusal_reason, credential_id "
                    "  from source_deliveries where id = :delivery_id"
                ),
                {"delivery_id": seeded["delivery_id"]},
            ).one()
            assert row.customer == "acme-utilities"
            assert row.project_id == seeded["project_id"]
            assert row.transport == "pull"
            assert row.channel == "shared-files"
            assert row.configuration_identity == "txdot-rid-box-v1"
            assert row.configuration_version == "connector-polling-v1"
            assert row.external_identity == "item-a"
            assert row.external_version == "v1"
            assert row.content_sha256 == seeded["digest"]
            assert row.bytes_reference == (
                f"{seeded['digest'][:2]}/{seeded['digest']}.pdf"
            )
            assert row.delivery_identity == seeded["delivery_identity"]
            assert row.idempotency_key == seeded["idempotency_key"]
            assert row.service_identity == "corridor.connector_polling"
            assert row.run_identity == "due-attempt:seed"
            assert row.disposition == "stored"
            assert row.refusal_reason is None
            assert row.credential_id is None

        # A push-only ledger downgrades: every row it holds is representable.
        with database.session_factory.begin() as session:
            session.execute(
                text("delete from source_deliveries where id = :delivery_id"),
                {"delivery_id": seeded["delivery_id"]},
            )
        downgraded = _alembic(database_url, "downgrade", SUPPORTED_HEAD)
        assert downgraded.returncode == 0, downgraded.stderr
        assert _migration_head(database.session_factory) == SUPPORTED_HEAD


# The declaration a registered mapping revision stores (#610). It is a literal
# rather than a constructed manifest because the transition holds it as exact
# bytes and checks nothing but their digest: what this test proves is that the
# bytes survive, unchanged, or the downgrade stops.
STORED_DECLARATION = (
    '{"external_references":[],"identity":"ucm-published-column-headings",'
    '"mappings":[{"cardinality":"1 → 1","composition":'
    '"one_value_per_column_v1","source_columns":["Start Station"],'
    '"target_fields":["station_from"]}],'
    '"schema_version":"field-mapping-manifest-v1","version":"v1"}'
)


def _seed_stored_mapping_revision(session, *, slug: str) -> dict:
    """One registered mapping revision that stores its full declaration.

    Written with the append-only guards lifted, the way the committed-scenario
    cleanups do: the registration family is written only by the record-decision
    role's commands, and reaching those would mean adopting a whole baseline to
    prove a property of the transition rather than of the adoption.
    """

    project_id = session.scalar(
        text(
            "insert into projects (slug, name, is_synthetic) "
            "values (:slug, 'Stored Mapping Revision', true) returning id"
        ),
        {"slug": slug},
    )
    digest = sha256(STORED_DECLARATION.encode("utf-8")).hexdigest()
    session.execute(text("set local session_replication_role = replica"))
    format_id = session.scalar(
        text(
            "insert into project_baseline_formats ("
            "project_id, format_kind, format_identity, format_version, "
            "content_sha256, registered_by_principal, idempotency_key"
            ") values ("
            ":project_id, 'field_mapping', 'ucm-published-column-headings', "
            "'v1', :digest, 'local:coordinator', 'seed-stored-revision'"
            ") returning id"
        ),
        {"project_id": project_id, "digest": digest},
    )
    session.execute(
        text(
            "insert into project_baseline_format_manifests ("
            "format_id, project_id, format_identity, format_version, "
            "content_sha256, manifest_schema_version, declaration"
            ") values ("
            ":format_id, :project_id, 'ucm-published-column-headings', 'v1', "
            ":digest, 'field-mapping-manifest-v1', :declaration"
            ")"
        ),
        {
            "format_id": format_id,
            "project_id": project_id,
            "digest": digest,
            "declaration": STORED_DECLARATION,
        },
    )
    session.execute(text("set local session_replication_role = origin"))
    return {"project_id": project_id, "format_id": format_id, "digest": digest}


def test_the_stored_mapping_revision_refuses_a_downgrade_that_would_lose_it():
    """#610, proved on the exact row the digest-only registration cannot hold.

    The registration this downgrade restores carries a mapping revision's
    identity, version and digest and nothing that resolves them, which is the
    unresolvable digest #610 exists to remove. Dropping the declaration to get
    back there would recreate it silently, so the transition counts what it
    cannot carry, names it, and leaves the row exactly as it was.
    """

    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        label="baseline_mapping",
        migration_revision=SUPPORTED_HEAD,
        reuse_migrated_template=True,
    ) as database:
        database_url = configured.set(database=database.name)
        upgraded = _alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr

        with database.session_factory.begin() as session:
            seeded = _seed_stored_mapping_revision(
                session, slug="stored-mapping-revision"
            )

        refused = _alembic(database_url, "downgrade", SUPPORTED_HEAD)

        assert refused.returncode != 0
        assert "#610 downgrade refuses" in refused.stderr
        assert "1 registered mapping revision(s)" in refused.stderr
        assert "Nothing is dropped here" in refused.stderr
        assert _migration_head(database.session_factory) == CURRENT_HEAD

        with database.session_factory() as session:
            row = session.execute(
                text(
                    "select format_id, project_id, format_identity, "
                    "       format_version, content_sha256, "
                    "       manifest_schema_version, declaration "
                    "  from project_baseline_format_manifests "
                    " where format_id = :format_id"
                ),
                {"format_id": seeded["format_id"]},
            ).one()
            assert row.project_id == seeded["project_id"]
            assert row.format_identity == "ucm-published-column-headings"
            assert row.format_version == "v1"
            assert row.content_sha256 == seeded["digest"]
            assert row.manifest_schema_version == "field-mapping-manifest-v1"
            assert row.declaration == STORED_DECLARATION

        # A registration that stores no declaration loses nothing, so the
        # transition crosses back.
        with database.session_factory.begin() as session:
            session.execute(text("set local session_replication_role = replica"))
            session.execute(
                text(
                    "delete from project_baseline_format_manifests "
                    " where format_id = :format_id"
                ),
                {"format_id": seeded["format_id"]},
            )
            session.execute(text("set local session_replication_role = origin"))
        downgraded = _alembic(database_url, "downgrade", SUPPORTED_HEAD)
        assert downgraded.returncode == 0, downgraded.stderr
        assert _migration_head(database.session_factory) == SUPPORTED_HEAD


def _seed_bound_report_reading(session, *, slug: str) -> dict:
    """One Report Run naming the accepted revision it was produced against.

    Written with the append-only guards lifted, the way the committed-scenario
    cleanups do: a Project Record revision is written only by the
    record-decision role's commands, and reaching one would mean adopting a
    whole baseline to prove a property of the transition rather than of the
    adoption.
    """

    project_id = session.scalar(
        text(
            "insert into projects (slug, name, is_synthetic) "
            "values (:slug, 'Bound Report Reading', true) returning id"
        ),
        {"slug": slug},
    )
    session.execute(text("set local session_replication_role = replica"))
    revision_id = session.scalar(
        text(
            "insert into project_record_revisions ("
            "project_id, command_type, human_principal, idempotency_key"
            ") values ("
            ":project_id, 'record_verbal_statement', 'local:coordinator', "
            "'seed-bound-report-reading'"
            ") returning id"
        ),
        {"project_id": project_id},
    )
    run_id = session.scalar(
        text(
            "insert into report_runs ("
            "project_id, revision_id, ruleset_version, snapshot_json, "
            "document_only"
            ") values ("
            ":project_id, :revision_id, 'v0.4', "
            "'{\"dependencies\": {}}'::jsonb, false"
            ") returning id"
        ),
        {"project_id": project_id, "revision_id": revision_id},
    )
    session.execute(text("set local session_replication_role = origin"))
    return {
        "project_id": project_id,
        "revision_id": revision_id,
        "run_id": run_id,
    }


def test_the_report_revision_binding_refuses_a_downgrade_that_would_lose_it():
    """#602, proved on the exact row the snapshot-only shape cannot hold.

    The shape this downgrade restores is a ``snapshot_json`` copy and nothing
    saying which accepted revision it was taken against, which is the
    unreferenced copy #602 removes. Dropping the column to get back there would
    recreate it silently, for every reading already bound, so the transition
    counts what it cannot carry, names it, and leaves the row exactly as it
    was.
    """

    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        label="baseline_binding",
        migration_revision=SUPPORTED_HEAD,
        reuse_migrated_template=True,
    ) as database:
        database_url = configured.set(database=database.name)
        upgraded = _alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr

        with database.session_factory.begin() as session:
            seeded = _seed_bound_report_reading(session, slug="bound-report-reading")

        refused = _alembic(database_url, "downgrade", SUPPORTED_HEAD)

        assert refused.returncode != 0
        assert "#602 downgrade refuses" in refused.stderr
        assert "1 report reading(s)" in refused.stderr
        assert "Nothing is dropped here" in refused.stderr
        assert _migration_head(database.session_factory) == CURRENT_HEAD

        with database.session_factory() as session:
            row = session.execute(
                text(
                    "select project_id, revision_id, ruleset_version, "
                    "       snapshot_json, document_only "
                    "  from report_runs where id = :run_id"
                ),
                {"run_id": seeded["run_id"]},
            ).one()
            assert row.project_id == seeded["project_id"]
            assert row.revision_id == seeded["revision_id"]
            assert row.ruleset_version == "v0.4"
            assert row.snapshot_json == {"dependencies": {}}
            assert row.document_only is False

        # A reading that names no revision loses nothing, so the transition
        # crosses back.
        with database.session_factory.begin() as session:
            session.execute(text("set local session_replication_role = replica"))
            session.execute(
                text("update report_runs set revision_id = null where id = :run_id"),
                {"run_id": seeded["run_id"]},
            )
            session.execute(text("set local session_replication_role = origin"))
        downgraded = _alembic(database_url, "downgrade", SUPPORTED_HEAD)
        assert downgraded.returncode == 0, downgraded.stderr
        assert _migration_head(database.session_factory) == SUPPORTED_HEAD


def _assert_history_downgrade_refuses_data_on_this_transition(session_factory, project_id):
    """Exercise data-loss guards in the existing supported-transition clone.

    The owning test already proves empty downgrade/upgrade. These synthetic
    rows live in one rollback scope, so checking loss refusal does not create
    another blank database or erase retained rows to continue the rehearsal.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from corridor.coordination_history import migrate_coordination_history
    from corridor.legacy_history import capture_history, inventory_history, read_history
    from corridor.migrations import coordination_history, legacy_history, support_history
    from corridor.models import Dependency, Document, EvidenceLink, SourceSegment
    from corridor.operative_support import designate_publication_support
    from corridor.principals import HumanPrincipal
    from corridor.support_history import migrate_support_history
    from corridor.work_decisions import assign_internal_owner

    with session_factory() as session:
        rollback_scope = session.begin_nested()
        try:
            dependency = Dependency(project_id=project_id, ref_code="DEP-HISTORY-GUARD",
                                    dep_type="utility_relocation", title="History loss guard")
            document = Document(project_id=project_id, sha256="c" * 64, filename="history-guard.pdf",
                                doc_type="minutes", parse_status="parsed", pages=1)
            session.add_all([dependency, document])
            session.flush()
            words = "Retained source history."
            segment = SourceSegment(project_id=project_id, document_id=document.id, kind="prose_span",
                exact_text=words, content_sha256=sha256(words.encode()).hexdigest(), ordinal=1,
                page_no=1, start_offset=0, end_offset=len(words))
            evidence = EvidenceLink(dependency_id=dependency.id, document_id=document.id, page_no=1,
                                    quote=words, verified=True)
            session.add_all([segment, evidence])
            session.flush()
            principal = HumanPrincipal("local:history-loss-guard")
            assign_internal_owner(session, dependency.id, "Original owner", principal=principal)
            designate_publication_support(session, dependency.id, evidence.id, principal=principal)
            batch = capture_history(session, inventory_history(session, project_id), run_key="loss-guard",
                                    executor=session.scalar(text("select session_user")), code_revision="a" * 40)
            migrate_coordination_history(session, batch)
            migrate_support_history(session, batch)
            operations = Operations(MigrationContext.configure(session.connection()))
            for helper in (support_history, coordination_history, legacy_history):
                with pytest.raises(RuntimeError, match="cannot be discarded"):
                    helper.downgrade(operations)
                assert read_history(session, project_id, batch.id).content_sha256 == batch.content_sha256
        finally:
            rollback_scope.rollback()


def test_a_scanned_reading_binds_to_its_observation_and_an_earlier_one_says_it_cannot(tmp_path):
    """#809, on exact rows: the binding, the declared unknown, and the refusals.

    A scanned reading #804 wrote before the binding existed comes through the
    transition marked ``predates_observation_binding`` and otherwise untouched;
    a human decision beside it, which no observation read, carries no reason. A
    new scanned reading is refused unless it names an observation or the one
    reason; an observation is one row per identity; a reason beside a binding
    is refused. With no observation recorded the downgrade hands the earlier
    rows back exactly, without the three columns and without the relation; the
    relation is append-only, so the round trip is proved before an observation
    is written, and the downgrade refuses once one is.
    """

    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        label="baseline_observation",
        migration_revision=SUPPORTED_HEAD,
        reuse_migrated_template=True,
    ) as database:
        database_url = configured.set(database=database.name)
        with database.session_factory.begin() as session:
            project_id = session.scalar(text(
                "insert into projects (slug, name, is_synthetic) values "
                "('observation-binding', 'Observation binding', true) returning id"
            ))
            document_id = session.scalar(text(
                "insert into documents (project_id, sha256, filename, doc_type, parse_status) "
                "values (:project_id, :digest, 'matrix/scan.pdf', 'matrix', 'parsed') returning id"
            ), {"project_id": project_id, "digest": "e" * 64})
            legacy_id = session.scalar(text(
                "insert into unreadable_cell_resolutions (project_id, document_id, page_no, "
                " cell_key, state, value, origin, policy_version) values "
                "(:project_id, :document_id, 1, 'scan:p1:t0:r1:c1', 'unconfirmed', 'SCANNED', "
                " 'harness', 'scanned-textract-reading-v1') returning id"
            ), {"project_id": project_id, "document_id": document_id})
            human_id = session.scalar(text(
                "insert into unreadable_cell_resolutions (project_id, document_id, page_no, "
                " cell_key, state, value, origin, recorded_by) values "
                "(:project_id, :document_id, 1, 'scan:p1:t0:r1:c1', 'corroborated', 'SCANNED', "
                " 'human_decision', 'local:person') returning id"
            ), {"project_id": project_id, "document_id": document_id})

        upgraded = _alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr
        assert _migration_head(database.session_factory) == CURRENT_HEAD

        with database.session_factory() as session:
            carried = session.execute(text(
                "select id, run_id, observation_id, source_region_id, "
                "observation_unbound_reason from unreadable_cell_resolutions order by id"
            )).all()
        assert [tuple(row) for row in carried] == [
            (legacy_id, None, None, None, "predates_observation_binding"),
            (human_id, None, None, None, None),
        ]

        downgraded = _alembic(database_url, "downgrade", SUPPORTED_HEAD)
        assert downgraded.returncode == 0, downgraded.stderr
        assert _migration_head(database.session_factory) == SUPPORTED_HEAD
        with database.session_factory() as session:
            restored = session.execute(text(
                "select id, project_id, document_id, page_no, cell_key, state, value, "
                "origin, policy_version, recorded_by from unreadable_cell_resolutions order by id"
            )).all()
            columns = set(session.scalars(text(
                "select column_name from information_schema.columns "
                "where table_schema = 'public' and table_name = 'unreadable_cell_resolutions'"
            )).all())
            relation = session.scalar(text(
                "select to_regclass('public.scanned_page_observations')"
            ))
        assert [tuple(row) for row in restored] == [
            (legacy_id, project_id, document_id, 1, "scan:p1:t0:r1:c1", "unconfirmed",
             "SCANNED", "harness", "scanned-textract-reading-v1", None),
            (human_id, project_id, document_id, 1, "scan:p1:t0:r1:c1", "corroborated",
             "SCANNED", "human_decision", None, "local:person"),
        ]
        assert not columns & {"observation_id", "source_region_id", "observation_unbound_reason"}
        assert relation is None

        again = _alembic(database_url, "upgrade", "head")
        assert again.returncode == 0, again.stderr
        scanned_row = (
            "insert into unreadable_cell_resolutions (project_id, document_id, page_no, "
            " cell_key, state, value, origin, policy_version, observation_id, "
            " source_region_id, observation_unbound_reason) values "
            "(:project_id, :document_id, 1, 'scan:p1:t0:r1:c1', 'unconfirmed', 'SCANNED', "
            " 'harness', 'scanned-textract-reading-v1', :observation_id, :region, :reason) "
            "returning id"
        )
        cell = {"project_id": project_id, "document_id": document_id}
        with database.session_factory() as session:
            with pytest.raises(DBAPIError) as unbound:
                with session.begin():
                    session.execute(text(scanned_row), {
                        **cell, "observation_id": None, "region": None, "reason": None,
                    })
        assert "ck_unreadable_cell_resolution_observation" in str(unbound.value)

        observation = (
            "insert into scanned_page_observations (project_id, document_id, "
            " rendition_sha256, page_no, authorization_record_id, scope_digest, "
            " raster_sha256, raw_response_sha256, reading_sha256, "
            " provider_model_version, provider_request_id, observed_at) values "
            "(:project_id, :document_id, :rendition, 1, 'exp-0001', :scope, :raster, "
            " :response, :reading, '1.0', 'req-1', '2026-09-10T12:00:00Z') returning id"
        )
        identity = {
            **cell, "rendition": "e" * 64, "scope": "1" * 64, "raster": "2" * 64,
            "response": "3" * 64, "reading": "4" * 64,
        }
        with database.session_factory.begin() as session:
            observation_id = session.scalar(text(observation), identity)
            bound_id = session.scalar(text(scanned_row), {
                **cell, "observation_id": observation_id, "region": "image-1", "reason": None,
            })
        with database.session_factory() as session:
            with pytest.raises(DBAPIError) as duplicate:
                with session.begin():
                    session.execute(text(observation), identity)
        assert "uq_scanned_page_observation_identity" in str(duplicate.value)
        with database.session_factory() as session:
            with pytest.raises(DBAPIError) as contradictory:
                with session.begin():
                    session.execute(text(scanned_row), {
                        **cell, "observation_id": observation_id, "region": "image-1",
                        "reason": "predates_observation_binding",
                    })
        assert "ck_unreadable_cell_resolution_observation" in str(contradictory.value)
        with database.session_factory() as session:
            bound = session.execute(text(
                "select observation_id, source_region_id, observation_unbound_reason, run_id "
                "from unreadable_cell_resolutions where id = :id"
            ), {"id": bound_id}).one()
        assert tuple(bound) == (observation_id, "image-1", None, None)

        refused = _alembic(database_url, "downgrade", SUPPORTED_HEAD)
        assert refused.returncode != 0
        assert "scanned page observations cannot be represented" in refused.stderr
        assert _migration_head(database.session_factory) == CURRENT_HEAD


_CONFIGURATION_COMMON = (
    "model, max_input_tokens, max_output_tokens, timeout_seconds, max_requests, "
    "retry_policy, retention_policy, observation_context"
)


def test_each_configuration_becomes_one_declaration_its_own_actor_made(tmp_path):
    """#811, on exact rows: the migration carries declarations, it makes none.

    Three configuration rows at the supported revision -- two Coordination
    Summary declarations by different people, one of them the pre-#355
    indefinite class, and one intake draft -- come through the transition as
    three ``spend_authorizations`` rows carrying each row's own ``created_by``
    as the declaring actor and its own ``created_at`` as the moment it took
    effect, one per configuration and bound by id; no synthetic actor and no
    migration-time timestamp appears. The family rows keep only what is theirs
    and name their declaration. A declaration made for one operation cannot
    then back a configuration of another. The downgrade hands back the nine
    columns exactly as they were and drops the relation; a declaration no
    configuration references refuses it, because the supported predecessor has
    nowhere to hold one person's declaration without dropping it.
    """

    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        label="baseline_spend",
        migration_revision=SUPPORTED_HEAD,
        reuse_migrated_template=True,
    ) as database:
        database_url = configured.set(database=database.name)
        summary_columns = (
            f"id, project_id, source_scope, prompt_version, {_CONFIGURATION_COMMON}, "
            "created_by, created_at"
        )
        draft_columns = (
            f"id, project_id, prompt_version, {_CONFIGURATION_COMMON}, created_by, created_at"
        )
        with database.session_factory.begin() as session:
            project_id = session.scalar(text(
                "insert into projects (slug, name, is_synthetic) values "
                "('spend-authorization', 'Spend authorization', true) returning id"
            ))
            first_id = session.scalar(text(
                "insert into coordination_summary_configurations (project_id, source_scope, "
                " model, prompt_version, max_input_tokens, max_output_tokens, timeout_seconds, "
                " max_requests, retry_policy, retention_policy, observation_context, "
                " created_by, created_at) values (:project_id, 'all_sources', 'model-a', "
                " 'briefing_v2', 8000, 500, 30, 1, 'none', 'retained_indefinitely', "
                " 'internal_working_view', 'local:first-declarer', '2026-01-05T09:30:00Z') "
                "returning id"
            ), {"project_id": project_id})
            second_id = session.scalar(text(
                "insert into coordination_summary_configurations (project_id, source_scope, "
                " model, prompt_version, max_input_tokens, max_output_tokens, timeout_seconds, "
                " max_requests, retry_policy, retention_policy, observation_context, "
                " created_by, created_at) values (:project_id, 'documents_only', 'model-b', "
                " 'briefing_v2', 8000, 500, 30, 1, 'none', 'class_b_30_days', "
                " 'internal_working_view', 'local:second-declarer', '2026-03-17T16:45:00Z') "
                "returning id"
            ), {"project_id": project_id})
            draft_id = session.scalar(text(
                "insert into source_intake_draft_configurations (project_id, model, "
                " prompt_version, max_input_tokens, max_output_tokens, timeout_seconds, "
                " max_requests, retry_policy, retention_policy, observation_context, "
                " created_by, created_at) values (:project_id, 'model-c', "
                " 'source_intake_draft_v1', 50000, 2000, 30, 1, 'none', 'class_b_30_days', "
                " 'internal_working_view', 'local:curator', '2026-06-01T08:00:00Z') "
                "returning id"
            ), {"project_id": project_id})
            before_summary = [tuple(row) for row in session.execute(text(
                f"select {summary_columns} from coordination_summary_configurations order by id"
            )).all()]
            before_draft = [tuple(row) for row in session.execute(text(
                f"select {draft_columns} from source_intake_draft_configurations order by id"
            )).all()]

        upgraded = _alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr
        assert _migration_head(database.session_factory) == CURRENT_HEAD

        with database.session_factory() as session:
            declarations = [tuple(row) for row in session.execute(text(
                f"select id, project_id, operation, {_CONFIGURATION_COMMON}, declared_by, "
                "effective_from from spend_authorizations order by id"
            )).all()]
            summaries = [tuple(row) for row in session.execute(text(
                "select id, project_id, authorization_id, operation, source_scope, "
                "prompt_version from coordination_summary_configurations order by id"
            )).all()]
            drafts = [tuple(row) for row in session.execute(text(
                "select id, project_id, authorization_id, operation, prompt_version "
                "from source_intake_draft_configurations order by id"
            )).all()]
            columns = set(session.scalars(text(
                "select column_name from information_schema.columns "
                "where table_schema = 'public' "
                "and table_name = 'coordination_summary_configurations'"
            )).all())
            worker_insert = session.scalar(text(
                "select has_table_privilege('corridor_worker', 'public.spend_authorizations', 'INSERT')"
            ))
            web_insert = session.scalar(text(
                "select has_table_privilege('corridor_web', 'public.spend_authorizations', 'INSERT')"
            ))
        assert declarations == [
            (1, project_id, "coordination_summary", "model-a", 8000, 500, 30, 1, "none",
             "retained_indefinitely", "internal_working_view", "local:first-declarer",
             datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)),
            (2, project_id, "coordination_summary", "model-b", 8000, 500, 30, 1, "none",
             "class_b_30_days", "internal_working_view", "local:second-declarer",
             datetime(2026, 3, 17, 16, 45, tzinfo=timezone.utc)),
            (3, project_id, "source_intake_draft", "model-c", 50000, 2000, 30, 1, "none",
             "class_b_30_days", "internal_working_view", "local:curator",
             datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)),
        ]
        assert summaries == [
            (first_id, project_id, 1, "coordination_summary", "all_sources", "briefing_v2"),
            (second_id, project_id, 2, "coordination_summary", "documents_only", "briefing_v2"),
        ]
        assert drafts == [(draft_id, project_id, 3, "source_intake_draft", "source_intake_draft_v1")]
        assert columns == {
            "id", "project_id", "source_scope", "prompt_version", "authorization_id", "operation",
        }
        assert worker_insert is True and web_insert is False

        # The intake draft's declaration cannot back a Coordination Summary.
        with database.session_factory() as session:
            with pytest.raises(DBAPIError) as crossed:
                with session.begin():
                    session.execute(text(
                        "insert into coordination_summary_configurations (project_id, "
                        " authorization_id, operation, source_scope, prompt_version) values "
                        "(:project_id, 3, 'coordination_summary', 'all_sources', 'briefing_v2')"
                    ), {"project_id": project_id})
        assert "fk_coordination_summary_configurations_authorization" in str(crossed.value)

        downgraded = _alembic(database_url, "downgrade", SUPPORTED_HEAD)
        assert downgraded.returncode == 0, downgraded.stderr
        assert _migration_head(database.session_factory) == SUPPORTED_HEAD
        with database.session_factory() as session:
            restored_summary = [tuple(row) for row in session.execute(text(
                f"select {summary_columns} from coordination_summary_configurations order by id"
            )).all()]
            restored_draft = [tuple(row) for row in session.execute(text(
                f"select {draft_columns} from source_intake_draft_configurations order by id"
            )).all()]
            relation = session.scalar(text("select to_regclass('public.spend_authorizations')"))
            restored_columns = set(session.scalars(text(
                "select column_name from information_schema.columns "
                "where table_schema = 'public' "
                "and table_name = 'coordination_summary_configurations'"
            )).all())
        assert restored_summary == before_summary
        assert restored_draft == before_draft
        assert relation is None
        assert not restored_columns & {"authorization_id", "operation"}

        again = _alembic(database_url, "upgrade", "head")
        assert again.returncode == 0, again.stderr
        with database.session_factory.begin() as session:
            session.execute(text(
                f"insert into spend_authorizations (project_id, operation, {_CONFIGURATION_COMMON}, "
                " declared_by) values (:project_id, 'revision_change_explanation', 'model-d', "
                " 8000, 500, 30, 1, 'none', 'class_b_30_days', 'internal_working_view', "
                " 'local:third-declarer')"
            ), {"project_id": project_id})
        refused = _alembic(database_url, "downgrade", SUPPORTED_HEAD)
        assert refused.returncode != 0
        assert "spend authorization no configuration references" in refused.stderr
        assert _migration_head(database.session_factory) == CURRENT_HEAD


def test_the_activation_ledger_carries_an_event_admission_reason_back_unchanged():
    """The fold widened ``reason`` to 160; the shape it restores holds 128.

    ``event_admission_activations.reason`` is ``character varying(128)`` in the
    released schema, and the consolidated ``policy_activations.reason`` is 160
    because the three fingerprint-bound families used that width. The downgrade
    therefore wrote ``left(reason, 128)``, so a suspension reason an operator
    typed after the fold came back with its tail cut off — a silently edited
    human attribution, in the one family whose docstring promises that "a round
    trip loses no row and no attribution".

    The schema keeps that promise rather than the callers: the relation refuses
    an Event Admission reason wider than the column its own downgrade restores,
    and the downgrade copies the reason exactly.
    """

    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        label="baseline_activation",
        migration_revision=SUPPORTED_HEAD,
        reuse_migrated_template=True,
    ) as database:
        database_url = configured.set(database=database.name)
        upgraded = _alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr

        at_the_limit = "why this admission class was suspended: " + "e" * 88
        assert len(at_the_limit) == 128
        with database.session_factory.begin() as session:
            project_id = session.scalar(text(
                "insert into projects (slug, name, is_synthetic) values "
                "('activation-round-trip', 'Activation round trip', true) returning id"
            ))
            receipt_id = session.scalar(text(
                "insert into event_admission_acceptance_receipts (project_id, status, "
                " source_revision, migration_head, predecessor_policy_version, "
                " policy_version, policy_sha256, reason_version, selection_rule, "
                " receipt_json, receipt_sha256) values "
                "(:project_id, 'passed', 'rev-1', :head, 'event-admission-v1', "
                " 'unknown-scope-v1', :digest, 'reason-v1', 'unknown-scope', "
                " '{}', :digest) returning id"
            ), {"project_id": project_id, "head": CURRENT_HEAD, "digest": "a" * 64}) 
            session.execute(text(
                "insert into policy_activations (project_id, family, action, "
                " policy_version, policy_sha256, replay_case_count, "
                " acceptance_receipt_id, reason, recorded_by) values "
                "(:project_id, 'event_admission', 'suspend', 'unknown-scope-v1', "
                " null, null, :receipt_id, :reason, 'local:operator')"
            ), {"project_id": project_id, "receipt_id": receipt_id, "reason": at_the_limit})
            # The three fingerprint-bound families keep the full 160: their own
            # predecessor columns hold it, and nothing about them truncates.
            session.execute(text(
                "insert into policy_activations (project_id, family, action, "
                " policy_version, policy_sha256, replay_case_count, "
                " acceptance_receipt_id, reason, recorded_by) values "
                "(:project_id, 'schedule_link', 'suspend', 'schedule-link-v1', "
                " :digest, null, null, :reason, 'local:operator')"
            ), {"project_id": project_id, "digest": "b" * 64, "reason": "s" * 160})

        with database.session_factory() as session:
            with pytest.raises(DBAPIError) as refused:
                with session.begin():
                    session.execute(text(
                        "insert into policy_activations (project_id, family, action, "
                        " policy_version, policy_sha256, replay_case_count, "
                        " acceptance_receipt_id, reason, recorded_by) values "
                        "(:project_id, 'event_admission', 'suspend', "
                        " 'unknown-scope-v1', null, null, :receipt_id, :reason, "
                        " 'local:operator')"
                    ), {"project_id": project_id, "receipt_id": receipt_id,
                        "reason": "t" * 129})
        assert "ck_policy_activation_event_admission_reason" in str(refused.value)

        downgraded = _alembic(database_url, "downgrade", SUPPORTED_HEAD)
        assert downgraded.returncode == 0, downgraded.stderr
        assert _migration_head(database.session_factory) == SUPPORTED_HEAD
        with database.session_factory() as session:
            restored = session.execute(text(
                "select reason, recorded_by, acceptance_receipt_id, action "
                "from event_admission_activations"
            )).one()
            assert restored.reason == at_the_limit
            assert restored.recorded_by == "local:operator"
            assert restored.acceptance_receipt_id == receipt_id
            assert restored.action == "suspend"
            assert session.scalar(text(
                "select reason from schedule_link_activations"
            )) == "s" * 160
