---
status: accepted
domain: migration
scope: current product
amended_by:
  - ADR-0081
migration: Dual-write of every human flow and the statement_id legacy reference remain; exit criteria and stages are in ADR-0081.
---

# Human Record Decisions and Recorded Verbal Statements on the spine

The spine (`facts → fact_decisions → project_record_revisions → current_project_record`, ADR-0067/0071) has one writer today: the automatic structured-cell inclusion policy. Every Human Record Decision and every Recorded Verbal Statement still writes only to legacy tables (`dependency_events`, `work_decisions`, `operative_support`, dispute tables) and never reaches the spine. #451 puts them on it. This ADR records the modeling decisions that migration requires; nothing here changes behavior until the implementation lands.

Guiding constraints, unchanged: facts append and stand; effectiveness lives on decisions; recorded time is the only axis; one revision per atomic change carries a human principal XOR a released policy (ADR-0071). Automatic inclusion follows the segment kind; everything else is a Human Record Decision (ADR-0070). Every human act is attributed, guided, and reversible by a later recorded act, never by editing history (ADR-0035/0039).

## Decisions

**1. A recorded-verbal segment kind, self-certifying.** `source_segments.kind` gains `recorded_verbal_statement` (ADR-0068 already names "the exact words of a Recorded Verbal Statement" as a segment kind). A verbal has no source Document to replay against, so the segment carries a nullable `statement_id` (FK to `dependency_events`) instead of `document_id`/page/offset, and `content_sha256 = sha256(exact_text)`. Its integrity check is self-consistency of that digest plus the recorder attribution on the statement — the recorder attests the words (ADR-0033/0036); there are no external bytes to dereference. The locator CHECK is extended so a verbal segment requires `statement_id` and null document locators, exactly as a spreadsheet cell requires sheet/cell and a prose span requires page/offsets.

**2. A `statement_timing` typed fact carrying precision.** The structured date fact types (`committed_date`, `action_due_date`, `need_date`) are single ISO days and cannot express a verbal's `day | month | approximate` precision. #451 adds a `statement_timing` fact type with a typed satellite `fact_statement_timings (fact_id, timing_role, iso_date, precision)`, mirroring the `applies_to` satellite pattern. It is set-valued (a statement may state several timings), so — like `statement_wording` — it is not in `EFFECTIVE_SINGLE_VALUE_FACT_TYPES`; effectiveness is carried by its human decisions.

**3. One human-authored spine command; the trigger accepts the human branch.** A new `record_human_fact_decision(...)` (Python in `fact_decisions.py`, backed by a `SECURITY DEFINER` SQL function owned by `corridor_fact_decision_writer`) mirrors `include_structured_cell_fact_by_policy` but writes a revision with `human_principal` set and `released_policy` null. `enforce_fact_decision_write` is extended to accept that binding (the revision XOR already anticipates it; the trigger simply did not permit the human branch yet). Every human decision type is one `command_type`: `record_verbal_statement`, `correct_statement_scope`, `correct_statement_facts`, `mark_do_not_add`, `resolve_discrepancy`, `designate_support`.

**4. Explicit stale-write guard for set-valued types.** Single-value fact types are guarded from concurrent supersession by the partial unique index `uq_fact_decision_effective`. Set-valued types (`statement_wording`, `statement_timing`, `applies_to`) are not, so the human command takes an `expected_predecessor` (the decision id the actor saw) and refuses if the current effective decision differs — the spine analogue of `StaleVerbalCorrection`/`_check_expected_predecessors`. This preserves the optimistic predecessor discipline of the guided flows (ADR-0039).

**5. Candidate-less decisions project through their satellites.** The current-record view filters only on `superseded_by IS NULL`; it does not require a candidate, so a recorder-authored decision appears. Its Constraint scoping comes from its `applies_to` satellite (already hydrated by `current_record._attach_structured_values`), not from `candidates.merged_into`. Readers are made tolerant of a null candidate/proposal lineage; the four-surface equivalence gate (`prove_reader_equivalence`) stays green. "Applies To not yet known" is kept as an explicit empty/undetermined reference set, never inferred (CLAUDE.md; Project Record glossary).

**6. Reversal is a compensating decision, never a mutation.** Undo and Do-Not-Add reversal write a new human decision that supersedes the prior effective decision (`superseded_by` append-only), exactly as the automatic path supersedes. Legacy receipts remain readable; new work dual-writes the legacy row and the spine decision inside the one existing atomic transaction, so guided Save stays one attributable, reversible command (ADR-0035/0039) and old rows keep working.

**7. Support designation reuses existing terms.** "Support designation" is not a new concept: it is *Supporting Documentation in Use* / *Human Support Update* (`OperativeSupport`, ADR-0017). On the spine it becomes the `designate_support` human decision; no new domain term is coined (ADR-0048 adoption boundary).

## Sequencing

The migration is additive and staged, each stage its own mergeable change: (1) the segment kind + `statement_timing` fact type + the human command + trigger extension (the rails); (2) Recorded Verbal Statement → recorder segment + wording/timing/Applies-To facts + human decision + revision, reversible; (3) each guided flow (coordinate/correct/Do-Not-Add/discrepancy/support) dual-writes its spine decision; (4) readers/equivalence confirmed. Legacy tables stay readable throughout.

## Consequences

The spine gains its second and general writer — attributable human decisions — behind the same append-only, revision-bound, hold-aware authority as the policy path. Nothing is retracted; the current view stays a plain projection. The guided flows keep their exact guard-test behavior (`test_statement_coordination`, `test_work_list`) and the evidence-bound-fact authority rule.

## #451 Stage 3 decisions

Governing distinction, applied consistently: a **source segment** says where the information came from; a **fact** says what proposition or value was recorded; a **decision** says what the Project Record does with that fact; a **command name** says which human action produced the decision. A source kind supplies provenance; it never determines which Project Record fields are representable, and readers never branch on the command name.

### Cited statement inclusion command

`coordinate_statement` is the human command name for inclusion of a Cited statement. The Recorded Verbal Statement command remains exclusive to capture of a Recorded Verbal Statement. Both commands record the existing typed inclusion decision. The command identifies the human action; the source segment kind identifies provenance. Reusing the verbal command would make the Audit Trail falsely claim a verbal capture and make command-level metrics, debugging, and reversals ambiguous.

### Meeting Notes passage fact eligibility

A Meeting Notes passage may support `statement_wording`, `statement_timing`, and `applies_to` facts. These fact types are source-neutral. No Cited-specific timing or Applies To fact types are introduced. Unknown Applies To is represented by an existing `applies_to` fact with an empty member set, not by absence of a fact. The source segment supplies evidence context and `human_principal` records who entered the normalized fact; the normalized date or scope identifiers need not appear byte-for-byte in the source passage.

### Do Not Add

Do Not Add is a statement-level record disposition, not a fact value. Before `mark_statement_not_relevant` records its decision, the save ensures that the statement source and candidate fact bundle exist. The `statement_wording` fact is the anchor for the statement's Commitment Lineage. An active Do Not Add decision suppresses the lineage's Project Record facts. A legacy statement may be materialized on the spine as part of a new human save; this is a forward write and does not mutate or bulk-migrate legacy data. `restore_statement_not_relevant` writes a compensating decision against the same lineage. It restores the predecessor state and does not imply inclusion.

### Discrepancy Resolution

Every resolved field belongs to the discrepancy's target Commitment Lineage. Commitment wording maps to `statement_wording`, timing maps to `statement_timing`, and Applies To maps to `applies_to`. Selecting an unchanged existing fact reuses that fact when it already belongs to the target lineage. A synthesized or cross-lineage result creates a new human-attributed fact of the matching type and retains provenance to the discrepancy and contributing facts. A resolution may emit multiple field decisions. They share one operation identifier, use one expected predecessor per field lineage, and commit atomically with the legacy resolution. A stale field aborts the complete save.

### Supporting Documentation in Use

`supporting_documentation_in_use` is a relationship fact between a Project Record subject or Commitment Lineage and one immutable document revision. Designation records a human decision for that relationship fact. Resolution writes a compensating decision against the same fact. Supporting documents are modeled independently rather than as one replaceable set: designation and resolution are member-local, so one relationship fact per document avoids full-set rewrites, spurious stale-write conflicts, first-save backfill of every legacy designation, and set-level (rather than document-level) attribution. The identity is an immutable document revision or digest-backed identity, not a filename or mutable current-document reference.
