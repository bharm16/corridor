---
status: accepted
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
