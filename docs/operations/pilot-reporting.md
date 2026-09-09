# Pilot economics and checkpoint reports

`make pilot-report` implements the reusable #424 reader. `make pilot-checkpoint`
implements the reusable #498 written report. Both consume private frozen inputs;
neither contacts a provider, changes the Project Record, authorizes release,
enables a policy, or edits the temporary success contract. Real customer findings
still require the declared live population and the partner's evidence.

The input is the [#532 analytical report](pilot-measurement.md), including its
native receipt references and exact configurations. Delta receipt source classes
come from the related Document's `doc_type`; `source_family` is retained
separately. A workbook-family identity or hashed minutes family is not a source
class and cannot silently create a new reporting stratum.

## Reproduce the software fixture

```bash
make pilot-measurement ARGS="--input tests/fixtures/pilot-measurement.json --output out/pilot-measurement.json"
make pilot-report ARGS="--measurement out/pilot-measurement.json --contract tests/fixtures/pilot-report-contract.json --evidence tests/fixtures/pilot-report-evidence.json --output out/pilot-report.json"
make pilot-checkpoint ARGS="--report out/pilot-report.json --output out/pilot-checkpoint.json"
```

The report retains its exact three input files together in `pilot-report.inputs.json`.
The checkpoint writes both JSON and Markdown, with an input digest and the source
report digest. All outputs are replaced atomically with owner-only permissions.
The fixture supplies neither an approved baseline nor live sampling or commercial
evidence. It produces explicit insufficient-evidence findings, and retained
confirmed failures remain failures. It cannot establish a customer outcome.

For another date range, produce a #532 report with those exact half-open periods,
then assign every period to a cohort in the contract. Do not trim periods after
seeing their performance. Partial weeks are retained but cannot count as complete
project-weeks in weekly time or cost gates.

## Contract file

`schema_version` is `pilot-contract-v1`. `declared_at` precedes every measured
period. `evidence_kind` is `live` only for actual customer observations; fixtures
cannot establish a bound live cohort.

Required declarations are:

- `source_classes`: the exact native source classes evaluated. Recorded Verbal
  Statements are outside the initial population unless expressly selected. Any
  observed class outside the declared population withholds a bound-cohort pass;
  its raw evidence remains visible. The #499 initial material sample supports
  matrix/UCM revisions, email, minutes and schedule exports only; selecting a
  broader class does not make that sample prove it.
- `packet_precision_basis`: `all_surfaced` or `interrupting`, fixed before
  measurement. #424/#498 specify all surfaced packets; the existing success
  document specifies interrupting packets. The software prints **both** and
  refuses to select silently. This explicit choice records the pilot owner's
  resolution of the contract discrepancy; it does not rewrite either document.
- `minimum_stratum_project_weeks`: a positive predeclared evidence minimum for
  each quiet, ordinary and burst stratum. Missing strata stay insufficient.
- `business_calendar`: `timezone`, an explicit `holidays` date list, and
  `rule: elapsed_weekdays_excluding_declared_holidays`. A business day here is
  elapsed local calendar-day time on weekdays excluding the declared holidays.
  The threshold is three such days; it is never approximated as 72 elapsed hours.
- `shape`: `partners`, keyed by partner ID, each with `project_keys` and
  `signed_agreement_reference`, plus `baseline_method` and `comparison_method`.
  A project key joins customer ID, environment, database identity and local
  project ID with `|`. Use exact declarations, not filenames or a customer label.
- `cohorts`: each has a unique `id`, `period_ids`, `live_period_ids`,
  `onboarding_period_ids`, `pilot_week_by_period` and `full_feature_flags`.
  All measured periods belong to exactly one cohort. `live_period_ids` identify
  gated weeks, normally weeks 3–10; onboarding remains measured in the retained
  rows. `pilot_week_by_period` supplies those numeric pilot weeks, which also
  fixes the early exception sample to weeks 3–4 and the late sample to weeks 7–10.
  `full_feature_flags` maps every flag to its explicit boolean state, including
  disabled flags. Enabled values must match the #532 binding.
- `responses`: one predeclared response string per criterion in
  `pilot_checkpoint.CRITERIA`. Missing responses are reporting defects, never
  evidence that the response was taken.

The shape gate checks two signed partners, two projects per partner, two measured
onboarding weeks, eight consecutive complete live weeks per project and forty
captured source arrivals per partner. Every declared partner receives every
finding even if its data is wholly absent. A material configuration change
requires a separate cohort: differing bindings, templates, mappings, issue
profiles, artifact obligations or volume rules cannot be pooled. Preserve the
change's reason and commit in the cohort declaration. A shorter segment remains
insufficient; it cannot borrow weeks from the other side of the boundary.

## Evidence file

Evidence supplements existing instrumentation only where the partner or an
independent reader must provide a fact. Each human assertion names `actor` and
`evidence_reference`. Retain source documents privately with the same access and
retention controls as the #532 export.

`baselines` contains one approved log per `project_key`. Required fields are
`start`, `end`, `adopted_at`, `approved_at`, `approved_by`, `actor`,
`evidence_reference`, `complete_work_log`, `complete_reporting_cycle`,
`ordinary_work`, `no_change_work`, `substantive_revision`, and `work`. Work entries
name `category` (`record_maintenance` or `report_preparation`), `minutes`, `actor`
and `evidence_reference`; report work also names the pre-existing `artifact_type`.
Use `source_class` when the work can be attributed. `extension` retains an
extended baseline's explanation. A `matched_historical_event` can satisfy the
substantive-event requirement only with an actor, evidence reference,
`same_coordinator: true`, `comparable_revision: true`, `justification`, and an
`agreed_at` before adoption. The two-week floor and other conditions still apply.

`period_attestations` is keyed by period ID. Each names its actor and evidence
reference, `complete_time_categories`, `disjoint_time_entries`,
`all_provider_costs_complete`, `false_write_review_complete`, and, when available,
`complete_diagnostics`. Categories are the existing #532 categories. The
attestation establishes that zero-event categories are an observed zero and that
review/repair time is not already included in maintenance time. Without it,
missing data remains unavailable. All coordinator categories count toward the
same-work calculation, including review, repair and reconstruction. Time tagged
to an artifact the partner did not previously produce is reported as new-output
time and earns **no savings**. Unknown source attribution stays `unattributed`;
the reader does not allocate shared time across source classes.

`material_sampling` contains one entry per cohort with `cohort_id`, `declared_at`,
`policy`, `arrivals`, and `inspections`, using #499's `ComparisonPolicy` and
`assess_material_sample` shapes. The policy names fields, material fields, seed
and a minimum of at least thirty material cases. The exact eligible native census
is printed as `native_sampling_populations`; supplied arrivals must match it.
IDs combine the exact project key and native source identity with `|`. Only this
sampling adapter maps native `matrix` to `ucm_revision` and `schedule` to
`schedule_export`; reporting retains native names. It inspects every arrival up
to twenty per partner-week, otherwise twenty seeded draws without replacement.
Every inspection names the reviewer and all resolvers, and the reviewer cannot
be a resolver. A missing selected inspection or insufficient material cases
withholds the miss rate. Confirmed material misses require causes. Working-reference
agreement from #499 is never treated as independently adjudicated truth.

`adoption_audits` contains one audit per exact `project_key`, with actor/reference,
`random_seed`, `total_rows`, the complete `population_row_ids`,
`sampled_row_ids`, `all_material_fields_checked`, `sampled_at`, `adopted_at`,
`checked_at`, `onboarding_end`, `discarded_rows`, `discarded_columns`, and `fields`.
The reader reproduces Python's seeded sample of the sorted row population, using
100 rows or all rows if fewer. Each field names `row_id`, `field`, `material`,
`populated`, `correct`, and `evidence_reference`. Duplicate cells are refused as
valid evidence. **Checked populated material fields** are the accuracy denominator;
rows, empty fields and duplicate cells cannot inflate it. Any discarded structure
fails the criterion even when other evidence is thin.

`diagnostics` contains independent `kind`, `identity`, `period_id`, actor/reference,
`useful`, `triaged_at` and `judged_at` values. Supported kinds are
`child_outcome_identifiability`, `alert_usefulness`, `release_warning_usefulness`
and `exception_usefulness`. Judgment must occur at triage. Child-identifiability
entries additionally name `receipt_id`; the set must reconcile exactly to native
multi-child packet-save receipts. Each kind uses its own unit and completeness
attestation. Nothing adds child, alert or warning counts to the packet denominator.

`commercial` contains written statements with `partner_id`, `cohort_id`, actor,
evidence reference and explicit `will_pay`. `responses_taken` contains criterion,
partner and cohort IDs, actor/reference, `action`, and `next_period_or_extension`.
An unrecorded response is printed as unrecorded. `checkpoint_decision` records
one supported `outcome`, actor and evidence reference. Without that record the
conclusion is explicitly a recommendation. `continue as designed` is rejected
unless every finding passes. The software reports contract closure only after
a human outcome and required responses are evidenced; it never edits the contract.

## Measures and limits

The report preserves every #532 period and observation, including receipt IDs,
provider/model/prompt/policy dimensions, declared configuration, package membership,
repair time, missing or mixed-revision artifacts and stale-candidate failures.
Every rate prints its numerator, denominator and unit. Time distributions print
median and nearest-rank p90. Source-to-delta latency includes native deltas that
were never surfaced as packets. An arrival outside the retained period is not
guessed: its latency remains unavailable. The native export's complete history
can be retained in a larger declared observation window when that evidence is
needed.

The checkpoint evaluates each partner and cohort separately. Relevant time,
latency, precision and reconstruction gates are also evaluated for every volume
stratum. A burst failure overrides a pooled pass; a thin stratum never disappears
into a neighboring one. A confirmed automatic material false write stays failed
for that policy class across later cohorts. New observations can support diagnosis
but cannot erase that failure.

The report gates validated claims, cohort expansion, broader rollout and feature
enablement. Ordinary development continues under the roadmap unless a separate
safety defect blocks it.
