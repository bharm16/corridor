# v0 Build Spec — Cited Readiness Ledger (build this first)

Covers milestones M0-M5 of `docs/history/phase-1-roadmap-2026-08.md` (historical). Target: ~7 focused solo weeks.

Corpus assembly is specified separately in `corpus-acquisition-spec.md` and runs in parallel from week 0. `CONTEXT-MAP.md` locates the domain vocabulary; use those terms. Decisions with lasting consequences are recorded in `docs/adr/`.

## 1. Objective

Given one real highway project's document set, produce three things, with a human (you) adjudicating everything the AI proposes:

1. A Project Record with a Dependency Ledger: every utility/External Party Dependency as a structured record, every document-sourced field cited to a Document, page, and quote.
2. An exception list: what's missing an owner, a date, or evidence; what's stale, due soon, overdue, or contradictory.
3. A generated weekly readiness report that could replace the one a project builds by hand.

## 2. Non-goals for v0

No auth, no multi-user, no integrations, no write-back, no external-party access, no chat interface, no plan-sheet vision extraction, no schedule import beyond a milestone CSV. Plan sheets and drawings are registered as evidence documents and linked manually during adjudication — vision extraction of utility plans is a separate hard project; do not block the ledger on it.

## 3. Pipeline overview

```
manifest → fetch (download, hash, stamp provenance)
         → ingest (register, extract text/OCR, render pages)
         → extract (type-specific LLM extractors → cited candidates)
         → verify citations (quote must match cited page)
         → Admission / adjudicate (policy Abstention or human decision)
         → Project Record (Dependency Ledger + Assertions + External Party Statements + Work Decisions)
         → register Milestones / derive Need Dates → Evaluation
         → outputs (reviewed Report → Approved Export, XLSX export)
         → eval (gold set, recall/precision/citation metrics)
```

The Project Record stores accepted conclusions, External Party facts, and project-controlled Work Decisions; Assertions store what each Document claimed. Extractors write only Candidates. Admission is either an attributable human act or one enumerated deterministic policy class with an immutable receipt; every unsupported case Abstains.

## 4. Stack

Opinionated defaults; swap freely, but don't spend week 1 on stack shopping.

| Layer | Choice |
|---|---|
| Language | Python 3.12, pinned and installed via `uv` |
| DB | Postgres 16 in Docker; SQLAlchemy + Alembic |
| File store | Local dir, content-addressed by SHA-256; originals immutable |
| PDF/text | PyMuPDF; `ocrmypdf` (Tesseract) fallback for scanned docs |
| XLSX | openpyxl |
| Email | Python `email` stdlib for EML; `extract-msg` for MSG |
| LLM | OpenAI API, strict `json_schema` structured outputs |
| Models | `gpt-5.6-luna` for agreements. `model` recorded on every candidate and eval run. |

The LLM provider changed from the Claude API during M2 — a deliberate call, not a drift. The `model` field on `candidates` and `eval_runs` exists precisely so a provider or tier change is visible in eval history rather than silently shifting the numbers. The client in `corridor/llm.py` is a thin structured-output wrapper with the model injected, so swapping back or running a comparison is a config change.
| App/UI | FastAPI + Jinja2 + HTMX + Tailwind. No SPA. |
| Report PDF | HTML template → WeasyPrint |
| Eval | pytest + a metrics runner writing to an `eval_runs` table |

Python 3.13 is what's installed on the machine; 3.12 is pinned deliberately because PyMuPDF, `ocrmypdf`, and WeasyPrint all carry native dependencies where N-1 is the safer bet. `uv` installs the interpreter, so this costs nothing.

## 5. Data model

Schema is keyed by `project_id` from day one even though v0 runs one project.

**documents** — id, project_id, sha256, filename, doc_type (`matrix | minutes | agreement | email | plan | schedule | spec | status_report | other`), source_url, retrieved_at, doc_date, pages, parse_status, superseded_by (null in v0)

**doc_pages** — document_id, page_no, text, image_path

**external_orgs** — id, name, org_type (`utility | railroad | agency | consultant | other`), aliases[]

**dependencies** — id, project_id, ref_code (human-readable, e.g. `DEP-014`), dep_type (`utility_relocation | agreement | permit | row | railroad | access | other`), title, location_desc, station_from, station_to, external_org_id, resolution_strategy (`relocate | remove | abandon_deactivate | adjust_vertical | protect_in_place | change_design | exception`, nullable), need_date, milestone_id, evidence_required. There is no authoritative Dependency status field: Ready, Criticality, Exceptions, and current coordination are derived or projected from their own receipts.

**assertions** — id, dependency_id, field_name, asserted_value, evidence_link_id, doc_date, created_at. One row per claim by one document about one field. See ADR-0001.

**dependency_events** — stored External Party Statements (`commitment | committed_date_change | commitment_closure`) with attributable External Party, timing, Commitment Lineage, Commitment Scope, and Evidence or Verbal provenance. Extracted responses and status changes remain Candidates until the product can represent an honest supported outcome.

**evidence_links** — id, parent (dependency_id or event_id), document_id, page_no, quote, verified (bool), satisfies_requirement (bool, default false)

**candidates** — id, project_id, kind (`dependency | event`), payload_json, source_document_id, source_pages[], confidence, prompt_version, model, state (`pending | accepted | merged | rejected`), merged_into, adjudicated_at

**milestones / milestone_registrations** — stable Milestone identity plus an append-only chain binding each exact source row, source digest, recording principal, and predecessor. Current dates are projections of the registration chain; Need Dates derive from them.

**report_runs** — id, project_id, ts, ruleset_version, snapshot_json, output_path. The "changes since last report" diff reads the previous run's snapshot.

**audit_log** — id, principal, action, entity_type, entity_id, before_json, after_json, ts. Append-only. Authority-bearing facts also retain their typed immutable receipt; the generic audit row is not their source of truth.

**eval_runs** — id, ts, corpus, prompt_version, model, ruleset_version, metrics_json

### Derived, not stored

- **Ready** — a Dependency is ready when it has an `evidence_links` row with `verified = true` and `satisfies_requirement = true`. Not a status value. See ADR-0002.
- **`last_evidenced_at`** — the max `doc_date` across verified evidence on the Dependency or its events, falling back to `retrieved_at` when a document carries no date. Drives `STALE`.

Two distinct senses of "verified" were previously sharing a word. `evidence_links.verified` now means one thing only: *the quote actually appears on the cited page*.

## 6. Ingestion rules

- Dedupe by sha256; re-ingest is a no-op.
- Every document records `source_url` and `retrieved_at` from the corpus manifest. A citation that bottoms out at "a file on my laptop" is not a citation.
- Every page gets text and a rendered image (evidence display needs both).
- If a page's extracted text is under ~50 chars, OCR it.
- doc_type set by the manifest on import, correctable in UI.
- No file is ever mutated or deleted; supersession comes in M8.

## 7. Extraction contracts

One shared output schema for all extractors:

```json
{
  "kind": "dependency | event",
  "fields": { },
  "citations": [{ "document_id": "", "page": 0, "quote": "" }],
  "confidence": 0.0,
  "dedupe_hint": "org + location + type summary string"
}
```

Rules that are not negotiable:

- Every asserted field value must be supported by at least one citation.
- The verifier checks each quote against the cited page text (normalized fuzzy match, ≥0.9 similarity). Failed citations mark the candidate `unverified` and sink it in the queue — they are never silently dropped.
- Extractors never write to the Project Record. They only create Candidates.
- Every candidate records `prompt_version` and `model`. Without both, eval history across runs is not comparable and you cannot tell which change moved the numbers.
- **One source does not mean one layout.** A document series changes shape over time and across agencies: the Rockwall county reports switch from per-owner narrative to a four-bucket form mid-series; TxDOT bid proposals have at least three confirmed column schemas, one lacking stationing entirely. Extractors are written against a *record type* with multiple layout variants, never against one observed layout. An extractor that assumes its first sample's shape fails silently on the rest of the same series.

Per document type:

| Type | Approach | Model |
|---|---|---|
| Conflict matrix (PDF) | Deterministic table extraction and synonym-mapped headers — no LLM. TxDOT's layout family, not SHRP2; there is no single layout even within one project's own revisions. Each row → dependency candidate. | none |
| Agreements / permits | LLM **per page**, so the page number is supplied by us and only the quote comes from the model. Targets: obligations, milestone dates, notice periods, responsible party. | `gpt-5.6-luna` |
| Meeting minutes | LLM per document. Targets: action items, commitments with dates, status assertions, attendance (org → contact). Emit events against `dedupe_hint`, not new dependencies, unless clearly new. | *no corpus yet* |
| Serial status reports | LLM per edition. Targets: status assertions and dates per named dependency. Consecutive editions are the primary source of `slip` events. | *no corpus yet* |
| Email (EML/MSG) | LLM per thread. Latest commitment wins; earlier ones become `slip` events if dates moved. Capture non-response (thread with outbound ask, no reply) as a signal. | *no corpus yet* |

**Extract per page, not per document.** The page number is then ours and the model's only contribution to a citation is the quote — which is mechanically verified against the exact text the model was shown. A hallucinated page number becomes impossible by construction rather than merely unlikely.

**Quotes and field values are held to different standards**, and conflating them is a real bug rather than a nicety. A quote must be verbatim, OCR artifacts and all, or it cannot be verified. A field value must be the best *legible* reading, or null. Prompt v1 told the model not to clean up OCR and it applied that to field values too, yielding an external party named `CitoJ' (d Innoa. H&n1.a CD1I1lV, tau` — a name that can never match the same party elsewhere, silently splitting one organization into many and breaking merge blocking before M3 even starts.

Prompt files live in the repo, versioned (`prompts/minutes_v3.md`); `prompt_version` and `model` are recorded on every candidate and eval run.

## 8. Adjudication UI

Three screens. Keyboard-driven; you will adjudicate hundreds of candidates.

1. **Queue** — one candidate at a time: extracted fields left, cited page image with quote highlighted right. Actions: `a` accept, `e` edit-then-accept, `m` merge into existing (pre-ranked, see below), `r` reject (reason: duplicate / wrong / irrelevant / bad-citation). Target throughput: ≥60 candidates/hour.
2. **Ledger** — table of Dependencies; filter by External Party, Resolution Strategy (including derived Criticality), Milestone, Ready, and Exception type. Row → detail: fields, **the Assertions behind each field with their sources**, External Party Statement timeline, Evidence gallery, and receipt history. A field value shown without its competing Assertions reproduces the silent-overwrite behavior this tool exists to replace.
3. **Prepare Report** — render one fixed PDF, review those exact bytes, then release an Approved Export without regeneration (section 10).

Merging is the core interaction: the same dependency will arrive from the matrix, minutes, status reports, and email. Accepting a duplicate instead of merging corrupts the ledger — the pre-ranked merge search must be good before anything else gets polish.

### Merge ranking

Deterministic and explainable, not embeddings:

1. **Block** on resolved `external_org`, via `aliases[]` — "AT&T", "AT and T", and "Southwestern Bell" must collapse to one party before anything else runs.
2. **Score** within the block on: station-range overlap, `dep_type` match, and text similarity of title/location.
3. **Show the per-signal contribution** in the UI next to each suggestion.

Stationing carries most of the discriminating power because it is *numeric*. `245+00` and `445+00` are near-identical as strings and two thousand feet apart on the ground — which is exactly the distinction embedding similarity destroys. Fall back to text-only scoring when stationing is absent, which is common in minutes and email.

When a candidate is merged, its claims become **assertions** against the target Dependency. Merging never silently overwrites a field.

## 9. Exception engine

Computed as queries, not stored state. Exceptions carry no severity (ADR-0010, which superseded this section's `rule severity × criticality` arithmetic): each carries its rule, its detail, and its quantities — days overdue, days of silence, days to need date — and views group by rule, sort within a rule by its own quantity, and filter by Criticality. Implementation lands with #112.

| Rule | Logic |
|---|---|
| MISSING_OWNER | the current Coordination Plan has no Internal Owner and the Dependency is not Ready |
| MISSING_DATE | no current supported External Party Statement supplies a Committed Date |
| MISSING_EVIDENCE | no verified evidence_link on the record or its latest commitment event |
| STALE | last_evidenced_at > 14 days ago and the Dependency is not Ready |
| DUE_SOON | Need Date is within 30 days and the Dependency is not Ready |
| OVERDUE | committed_date < today and no closure event |
| CONTRADICTION | ≥2 assertions on the same (dependency, field) with distinct asserted_value, each backed by verified evidence |
| ORPHAN | milestone_id is null |

Thresholds (14 d, 30 d) are per-project config. The ruleset carries a version, recorded on every report run and eval run.

`MISSING_EVIDENCE` cannot fire on a Ready dependency by construction — readiness requires verified evidence. That is the point.

`STALE` measures **document silence**, not reviewer attention: nothing has said anything about this dependency in 14 days. On an archived corpus everything eventually goes stale, which is correct — a closed-out project genuinely has no fresh evidence.

## 10. Weekly readiness report

HTML → fixed reviewed PDF → Approved Export. **No cell is bare.** Every published cell carries one of four provenance classes (ADR-0003, ADR-0025, ADR-0040):

- an **Assertion** — citation marker `[D12 p.4]` linking to the verified quote
- a **Derivation** — ruleset version plus the record IDs aggregated, drilling through to those records' evidence
- a **Work Decision** — the exact decision ids displayed: recording principal, timestamp, and before/after values, field-exact
- a **Verbal** — an attributable External Party Statement heard by a named project person on a stated date

Enforced by the verifier, not by convention.

1. **Milestone readiness rollup** — per milestone: total dependencies, ready, at-risk, blocked, % with verified evidence.
2. **Critical items** — the critical records (Criticality read from Resolution Strategy, as a filter — ADR-0010), ordered by Need Date proximity as declared presentation; where no dates exist the section says so rather than faking an order: Internal Owner, Next Action, Committed Date, Ready, provenance.
3. **Exceptions summary** — counts by rule; within a rule, the largest quantity (most days overdue, longest silence), not a severity-ranked "worst" (ADR-0010).
4. **Changes since last report** — new, closed, slipped, escalated. Diffed against the previous `report_runs.snapshot_json`.
5. **Aging** — overdue items by days overdue.
6. **Appendix** — full ledger export.

Because the ruleset version is stored per run, a number that moved between two weekly reports can be attributed to a rule change rather than a data change.

## 11. Testing

The codebase splits into two halves with opposite testability. Do not let the untestable half set the discipline for the whole repo.

**Strict TDD** — the deterministic half, where bugs are silent and corrupting:

- content hashing and re-ingest idempotency
- the citation verifier (fuzzy match threshold behavior, normalization, failure marking)
- exception queries, every rule, including the boundary days
- merge blocking and scoring
- the Ready predicate
- report provenance enforcement — a bare cell must fail the build

**Recorded fixtures** — the extractors. LLM responses are captured once and replayed, so tests are deterministic, fast, and free in CI. These assert *shape and wiring*, never quality.

**`make eval`** — the only measure of extraction quality. A unit test cannot assert a statistical property; mocking the model only tests the mock. Prompt or model changes without an eval run don't merge.

## 12. Corpus

Specified in full in `corpus-acquisition-spec.md`. Summary of what this spec depends on:

- **Project A** (develop): TxDOT NHHIP Segment 3C-2 — five dated Utility Conflict Matrix versions with explicit supersession, plus SUE, railroad inventory, and executed agreements. Spine and stream in one project.
- **Project B** (held out for eval): FDOT SR 789 @ Broadway Roundabout — different agency, different matrix layout, 66 fully populated conflict rows. Chosen over the richer TxDOT SH 99 Grand Parkway specifically so M6's config-only claim is tested across agencies rather than across two projects at one agency.
- Each project needs a **spine** (dependency records: filled conflict matrix, utility agreements, special provisions) and a **stream** (dated assertions that change over time: serial status reports, meeting minutes, board packets). Neither role alone exercises the data model.
- **Project B must have a filled Utility Conflict Matrix** — it is the eval gold set (§13).
- Records requests are an upgrade path, not a prerequisite. Public sources yielded matrices, dated revisions, and coordination minutes; they did not yield DOT-to-utility **email**, which is the one thing requests are still filed for.
- Synthetic fixtures may exercise code paths. No synthetic document may ever contribute to an `eval_run`, a quality number, or a demo — enforced by an `is_synthetic` project flag that `make eval` refuses.

## 13. Eval harness

- **Primary gold set:** Project B's filled Utility Conflict Matrix — an independent, professionally authored enumeration of that project's utility conflicts. Recall against it answers a sharp question: did the tool find everything the coordinator found? It is more credible than self-authored labels precisely because you didn't write it.
- **Supplement:** one bounded slice of Project B (a fixed set of documents spanning all types) labeled exhaustively by hand, covering permits, ROW, railroad, and events — the categories a UCM doesn't reach.
- **Stated limits:** a matrix cannot be ground truth for its own omissions, so this measures parity with the coordinator, not the tool's ability to find what the coordinator missed. Say so out loud in any demo; it is a more honest claim than the alternative.
- Metrics per run: critical-dependency recall (target ≥95%), overall dependency recall (≥85%), candidate precision (track for cost/junk, no hard gate), citation validity (100%, enforced), correction rate (% of accepted candidates that needed edits).
- One command: `make eval`. Results append to `eval_runs` with `prompt_version`, `model`, and `ruleset_version`.

## 14. Build order

Week 0 runs in parallel with everything and is specified in `corpus-acquisition-spec.md`.

**Week 0 — corpus, in parallel.** Candidate project selection, records requests filed, manifest-driven fetcher, spine and stream documents downloaded for Project A. Exit: manifest resolves to files on disk with provenance.

**Week 1 — walking skeleton.** One end-to-end slice on ~3 documents: fetch → ingest → minimal schema (`documents`, `dependencies`, `assertions`, `evidence_links`) → one hardcoded matrix extractor → accept via CLI → one ledger row → a one-page HTML report exercising every provenance class. Exit: `make demo` produces a cited one-row report from raw files.

The skeleton is not throwaway. Its purpose is to surface schema gaps while migrations are still free — two were already found by reading the report spec against the data model, and the third is cheaper to find in week 1 than in week 5.

**Week 2 — M1.** Ingest proper: OCR fallback, page images, idempotent re-ingest, document browser. Matrix extractor with SHRP2 R15B column mapping. Exit: every document browsable page-by-page; matrix candidates generated.

**Week 3 — M2 (easy half).** Minutes extractor, status-report extractor, citation verifier, shared candidate schema. Adjudication queue UI. Exit: candidate queue populated for Project A with verified quotes, adjudicable by keyboard.

**Week 4 — M3.** Merge search and ranking, ledger table + detail view with assertions surfaced, event history, audit log. Exit: 100% of Project A candidates adjudicated; you can run a review session from the ledger screen.

**Week 5 — M2 (hard half) + M4.** Agreement and email extractors, slip/non-response events, milestone CSV import, need-date linkage, exception engine. Exit: exception list matches your hand-check of Project A.

**Week 6 — M5.** Report template, all provenance classes enforced, snapshot + changes-since-last diff, PDF render, XLSX export, demo path scripted end-to-end. Exit: report generates with no bare cells, in one command.

**Week 7 — hardening + eval seed.** Ingest Project B, fix everything that breaks, assemble the gold set, first `make eval` run, iterate prompts once. Exit: definition of done below.

Nights-and-weekends pace roughly doubles the calendar.

## 15. Definition of done (v0)

- [ ] One command: manifest → verified candidates for a new project.
- [ ] All candidates adjudicated through the UI at ≥60/hour.
- [ ] Merge suggestions rank the correct existing dependency first, with the reason visible.
- [ ] Exception list matches a manual check on Project A.
- [ ] Weekly Report generates with no bare cells — every value is an Assertion, Derivation, Work Decision, or Verbal.
- [ ] Ready is derived only from current verified Evidence plus an attributable sufficiency judgment; no status field can set it.
- [ ] Entire pipeline runs on Project B with config-only changes.
- [ ] First eval run recorded; critical recall measured (gate to hit in M7: ≥95%).
- [ ] Live demo, raw docs to report, in under 15 minutes.

When every box is checked, return to `docs/history/phase-1-roadmap-2026-08.md` (historical) at M6.
