# Legacy history inventory and bounded convergence batches (#513, #458)

This is the inventory prerequisite requested by #513's September 4 amendment.
It records supported migration software and remaining conversion work. It does
not declare ADR-0081 stages 2–6 complete or supersede ADR-0074.

## Decomposition for the issue tracker

1. **Inventory and retained-history custody (#783)**: explicit per-class ownership,
   complete original rows, native lineage, original actors and recorded times,
   run executor/code revision kept separately, counts/digest, exact replay and
   append-only reversal. Implemented by this batch.
2. **Bounded exact Evidence Link citation migration (#784)**: migrate a quote only when
   one existing located Source Segment on the same document/page has exactly
   those whole words and a verified digest. Preserve every original quote.
   Missing/ambiguous matches remain permanent historical quote text. Implemented
   by this batch, with its own per-link outcome receipts.
3. **Native Coordination Decisions (#785)**: implemented as a second bounded batch.
   Independent native subject identity, typed decisions bound to the existing
   ProjectRecordRevision family, original actor/time/predecessor, grouped plan
   Save/Undo, and native current/as-of readings. Existing plan writers dual-write
   migrated subjects; single/batched plan and milestone-impact reads use native
   decisions through an explicit identity adapter. Unattributed/conflicting actor
   history remains compatibility-only; its exact IDs and original actors are
   reported, never relabeled. Remaining conversion includes readiness/support
   role/scope, unresolved legacy assertion values, and complete statement/source
   reader integration. Keep #513 open.
4. **Independent native readers and field/record coverage (#786)**: complete all seven
   surfaces below, then bind their successful evidence to writer-switch commands.
   The generic semantic comparator is implemented, and the old misleading gate
   is corrected. The actual legacy readers remain unconverted; keep #458 open.
5. **Writer switch and retirement**: install and exercise application/database
   refusal of copied-current-field writes only after native coverage passes;
   remove compatibility writes after the declared observed window. A build-only
   window evaluator exists; no live switch or retirement is implemented here.

All schema objects fold into the one existing successor; no migration edge is
added. The inventory and batches are published as #513's linked sub-issues,
with native dependency edges from the migration batches to the inventory.

## Exact history custody

`legacy_history_batches` stores a database-generated canonical history manifest,
not caller-authored values. Capture requires the digest reviewed during inventory. The complete class
manifest is one UNION/aggregate statement and therefore one MVCC snapshot. The command itself rereads
every declared ownership slice and rejects drift. Project/run-key retries return
the same batch; a different digest, executor or code revision is refused. The
receipt's executor never replaces a row's actor, decision type, source identity
or time. Full native revisions/Facts/sources are retained as original lineage;
no new accepted value or artificial source is created by custody capture.

`legacy_history_reversals` withdraws routing by appending an attributable act.
All archived rows and migration outcomes stay readable. A reversed batch cannot
silently reactivate. For migrated Evidence Links the quotation reader returns to
the original quote after reversal; genuinely pre-existing citations are unaffected.
Rehearsing later in a new batch can reference the same retained citation without
duplicating it.

`capture`, `backfill-evidence` and `reverse` are migration-only SECURITY DEFINER
commands. Runtime logins can inventory/read their permitted project but cannot
execute migration writes. Direct insert/update/delete of receipts is refused by
command-owner triggers. No accepted-record decision command or authority is bypassed.

## Per-class inventory

Every listed table retains every column, including empty classes in the manifest.
`native_lineage` means original native rows are retained, not that a new native
reader was proved. `compatibility` means active routing cannot retire yet.

| Physical class | Treatment | Expiry criterion |
| --- | --- | --- |
| `dependencies` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `dependency_events` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `commitment_lineages` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `candidates` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `assertions` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `dependency_dismissals` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `dispute_settlements` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `dispute_history_resolutions` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `operative_support` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `dependency_evidence_sufficiencies` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `documentation_field_confirmations` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `condition_resolutions` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `retired_dependency_statuses` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `follow_up_plan_receipts` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `reconfirmation_receipts` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `evidence_links` | retained_quote | Retain original quote permanently as historical text when no exact source locator can be proven; never manufacture a Source Segment. Customer-wide disposition still applies. |
| `dependency_event_scope_decisions` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `dependency_event_scopes` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `dependency_event_timings` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `dependency_event_evidence` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `dependency_event_migration_receipts` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `candidate_dispositions` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `statement_coordination_receipts` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `statement_coordination_reversals` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `work_decisions` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `work_decision_milestone_impacts` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `statement_coordination_reversal_effects` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `follow_up_plan_reversals` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `dependency_admission_outcomes` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `event_admission_outcomes` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `automatic_carry_forward_receipts` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `automatic_carry_forward_outcomes` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `schedule_link_receipts` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `milestones` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `schedule_governing_derivations` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `organization_identity_receipts` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `milestone_registrations` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |
| `documents` | native_lineage | Keep original lineage for the lifetime of the referencing record and released artifacts. |
| `source_segments` | native_lineage | Keep original lineage for the lifetime of the referencing record and released artifacts. |
| `facts` | native_lineage | Keep original lineage for the lifetime of the referencing record and released artifacts. |
| `fact_decisions` | native_lineage | Keep original lineage for the lifetime of the referencing record and released artifacts. |
| `project_record_revisions` | native_lineage | Keep original lineage for the lifetime of the referencing record and released artifacts. |
| `recorded_verbal_origins` | native_lineage | Keep original lineage for the lifetime of the referencing record and released artifacts. |
| `recorded_verbal_origin_statements` | native_lineage | Keep original lineage for the lifetime of the referencing record and released artifacts. |
| `evidence_link_sources` | native_lineage | Keep original lineage for the lifetime of the referencing record and released artifacts. |
| `extracted_proposals` | native_lineage | Keep original lineage for the lifetime of the referencing record and released artifacts. |
| `support_assessments` | native_lineage | Keep original lineage for the lifetime of the referencing record and released artifacts. |
| `fact_sources` | native_lineage | Keep original lineage for the lifetime of the referencing record and released artifacts. |
| `fact_statement_timings` | native_lineage | Keep original lineage for the lifetime of the referencing record and released artifacts. |
| `fact_applies_to` | native_lineage | Keep original lineage for the lifetime of the referencing record and released artifacts. |
| `fact_closure_results` | native_lineage | Keep original lineage for the lifetime of the referencing record and released artifacts. |
| `fact_closure_sources` | native_lineage | Keep original lineage for the lifetime of the referencing record and released artifacts. |
| `support_assessment_sources` | native_lineage | Keep original lineage for the lifetime of the referencing record and released artifacts. |
| `audit_log` | compatibility | Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy. |

| `policy_approvals` | compatibility | Retain the exact authorizing human, policy version and digest with every policy-authored historical act. |
| `policy_runs` | compatibility | Retain original run identity and counts with the policy outcomes they authorize. |
| `recorded_verbal_origin_backfill_receipts` | native_lineage | Retain original executor/origin/statement mapping for the life of its referencing history. |
| `recorded_verbal_origin_fact_digests` | native_lineage | Retain old and replacement Fact digests; never lose the original source identity. |

## What the compatibility reader proves

- Native FactDecision readings at a retained project revision reconstruct original
  facts, source identities, authority and supersession, including Do Not Add.
- Coordination Decision chains use original recorded_at, predecessor and grouped
  reversal history; migration time is never their effective time.
- Statement correction reads preserve source kind, original recorder, wording,
  timing precision, scope decisions/membership, and recorded-verbal origin linkage.
- Do Not Add and restoration replay at the original act's timestamp. Restoration
  contributes no acceptance.
- Discrepancy resolution preserves null conclusions and original settler; a later
  assertion beyond covers_assertion_id reopens the field.
- Current support designations retain original role, field, scope, document and
  evidence identity at capture.

**Historical support limitation:** operative_support was mutated in place,
including designated_by/designated_at. A previous designation can only be
reconstructed when a retained audit/transfer/decision receipt contains it. The
current support API therefore names its supported capture instant; it does not
invent arbitrary earlier readings. Generic mutable Dependency as-of reconstruction
is also not certified by this batch. Existing preserved receipts remain available
for subsequent class-specific conversion, and absent original evidence is an
explicit coverage failure.

## EvidenceLink.quote writers

`adjudicate._evidence_link` and the EvidenceLink construction in
`operative_support.transfer_operative_scopes_under_lock` remain compatibility
writers. Quote-carrying support read models in operative_support remain consumers
of the historical quotation. Their rows are classified by exact retained evidence,
not by deleting the known carrier or claiming every matrix is segmented. An exact
whole-page-located segment can receive a citation; otherwise the original quote
remains permanently readable subject to customer-wide lawful disposition.

## Reader and rollback gates

`reader_equivalence.py` now owns the former rendering gate; `current_record.py`
keeps lazy compatibility exports and plain projection/timing reads. Equal rendered
outputs are reported separately from `passed`. The old station-only overlay cannot
pass reader coverage. `reader_coverage.py` compares a declared population and
exact field set for all seven ADR-0081 surfaces, refusing missing/extra/duplicate
records, missing classes, semantic changes and compatibility-carried fields.
Workbook/customer-format and release output identities must also match because
these contracts intentionally retain output shape. Field-origin declarations are
comparison inputs; they are not independent proof of a native production read
path. Actual reader integration and architecture enforcement remain required.

`legacy_cutover.assess_cutover` evaluates declared start/end, cohort, traffic
owner, decision authority, zero-divergence threshold and maximum observation gap.
Missing cohort samples, duplicate/out-of-window observations and divergence fail
closed. Elapsed time alone cannot establish a completed shadow window. Retirement
also needs the named decision, removed compatibility writes, readable history,
no hold, verified expired backups and an external disposition receipt. It changes
no traffic or database writer permissions; real observations and the owner's live
decision remain customer-specific gates.

## Operator entry point and validation

The Make target is `legacy-history ARGS="..."`, dispatching
`python -m corridor.legacy_history_cli`. Inventory writes a reviewable manifest of
counts, class treatments and digest. Capture requires that digest, run key,
executor and code revision. `backfill-evidence --batch ...` records exact-source
outcomes. `reverse --batch ... --executor ... --reason ...` appends the reversal.
`export --batch ...` writes the database's exact canonical history string and its
SHA-256, allowing offline verification without reproducing JSONB key order.
Every action requires `--project` and `--output`; exports contain source/history
content and belong in the customer's retained custody namespace.

Validation seams are `test_legacy_history.py`, `test_legacy_history_readings.py`,
`test_reader_coverage.py`, `test_legacy_cutover.py`, `test_current_record.py`, and
`test_evidence_citations.py`, followed by CI migration/authority/release gates.
The coordinator alone runs them. Storage reduction is **not claimed**: this
rehearsal retains originals and custody payloads, increasing retained bytes until
approved retirement and backup expiry. No table has been dropped.

## Native coordination batch

Run `legacy-history ARGS="migrate-coordination --project ... --batch ... --output ..."`
with the migration database principal after custody capture. The command verifies
that coordination decisions and grouping/reversal receipts still match the
reviewed batch before enabling any subject's native route. All original members
of a subject's chains must have attributable, compatible authorship. A subject
with an unattributed actor remains on the legacy route and appears in the explicit
compatibility-gap report.

Native subjects are UUIDs. Original Dependency, Commitment Lineage and Work
Decision IDs exist only in `coordination_subject_lineage` and
`coordination_decision_lineage`; they are not native subject/decision authority.
`coordination_record_decisions` stores typed ownership, next action/due date,
milestone impact/members, deferral/reasons, original actor/time and native
predecessor. It binds the existing `project_record_revisions` family directly.
No Source Fact or Support Assessment is fabricated for a Coordination Decision.

The current native view does not read the legacy value tables. Historical native
reads use original recorded_at and native supersession/reversal chains. Grouped
Constraint Follow-up Plan Save/Undo each share one native coordination revision;
existing single-field commands nest under the outer operation identity. Cited
Statement Save still has older per-fact source revisions before its coordination
portion; consolidating that complete source/coordination act remains required
before claiming the entire grouped statement flow shares one revision.

Backfill rereads are idempotent and preserve the original actors and event times.
Migration executor/code identity remains on the custody batch. Live migrated plan
writes append the native decision in the existing command transaction; they still
maintain the explicitly temporary legacy compatibility projection. Batch reversal
withdraws native current routing and keeps original/native history readable.
Application/database writer cutover for all legacy value classes remains #458.

Additional validation: `tests/test_coordination_history.py`, existing
`tests/test_work_decisions.py`, `tests/test_follow_up_plans.py`,
`tests/test_statement_coordination.py`, and `tests/test_work_list.py`.

The support-history reader now distinguishes a namespaced human designation from
an exact automatic-carry-forward receipt and its matching PolicyApproval family,
version and digest. Known system actors are never classified as humans merely
because their labels contain a colon. Missing or ambiguous policy lineage stays
explicitly unknown. This provenance reconstruction does not convert support into
Ready or authorize a new support designation.


The CLI uses only `CORRIDOR_HISTORY_OPERATIONS_DATABASE_URL`, an explicitly
provisioned login that inherits the non-login `corridor_history_operations`
capability. That capability can read retained migration results and execute the
reviewed capture/backfill/reversal commands; it has no raw accepted-table writes,
no schema ownership, and no membership in the record-decision writer role.
Schema-owner, superuser, role/database administrator and application logins are
refused. The operator's `--executor` must match the authenticated database login;
a free-text actor does not authenticate a migration. Login provisioning is an
operations prerequisite; the migration creates the capability role, never a login
or password. Web callers still require their sealed project partition.


Native coordination migration requires READ COMMITTED and takes the shared
project-row lock before inspecting source decisions. A REPEATABLE READ snapshot
can predate the lock acquisition and miss a Save that committed while migration
waited; that isolation level is explicitly refused for this command. With READ
COMMITTED, its post-lock reads see that Save and either match the reviewed batch
or refuse drift. The custody manifest itself is one SQL statement, so READ
COMMITTED does not split it across several source snapshots.


The supporting-document Fact contract now names the exact receipted transfer
exception from ADR-0083 as well as Human Record Decisions. Its automatic segment
kind set remains empty: finding a cell, passage or document never authorizes
inclusion. Only the support-history command's original transfer receipt,
matching scope/audit identity, and exact approval or managed-policy run/outcome
can take the policy branch. Locator checks still establish no readiness or
semantic-support conclusion.
