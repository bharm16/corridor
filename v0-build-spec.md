# v0 Build Spec — Cited Readiness Ledger (build this first)

Covers milestones M0-M5 of `phase-1-roadmap.md`. Target: ~6 focused solo weeks.

## 1. Objective

Given one real highway project's document set, produce three things, with a human (you) adjudicating everything the AI proposes:

1. A dependency ledger: every utility/external-party dependency as a structured record, every factual field cited to a source document, page, and quote.
2. An exception list: what's missing an owner, a date, or evidence; what's stale, due soon, overdue, or contradictory.
3. A generated weekly readiness report that could replace the one a project builds by hand.

## 2. Non-goals for v0

No auth, no multi-user, no integrations, no write-back, no external-party access, no chat interface, no plan-sheet vision extraction, no schedule import beyond a milestone CSV. Plan sheets and drawings are registered as evidence documents and linked manually during adjudication — vision extraction of utility plans is a separate hard project; do not block the ledger on it.

## 3. Pipeline overview

```
raw files → ingest (hash, register, extract text/OCR, render pages)
          → extract (type-specific LLM extractors → cited candidates)
          → verify citations (quote must match cited page)
          → adjudicate (review queue: accept/edit/merge/reject)
          → ledger (canonical records + event history + audit log)
          → link (milestones/need dates) → exceptions engine
          → outputs (weekly report HTML/PDF, XLSX export)
          → eval (gold set, recall/precision/citation metrics)
```

## 4. Stack

Opinionated defaults; swap freely, but don't spend week 1 on stack shopping.

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| DB | Postgres 16 in Docker; SQLAlchemy + Alembic |
| File store | Local dir, content-addressed by SHA-256; originals immutable |
| PDF/text | PyMuPDF; `ocrmypdf` (Tesseract) fallback for scanned docs |
| XLSX | openpyxl |
| Email | Python `email` stdlib for EML; `extract-msg` for MSG |
| LLM | Claude API, structured outputs (tool use / JSON schema) |
| App/UI | FastAPI + Jinja2 + HTMX + Tailwind. No SPA. |
| Report PDF | HTML template → WeasyPrint |
| Eval | pytest + a metrics runner writing to an `eval_runs` table |

## 5. Data model

Schema is keyed by `project_id` from day one even though v0 runs one project.

**documents** — id, project_id, sha256, filename, doc_type (`matrix | minutes | agreement | email | plan | schedule | spec | other`), source, doc_date, pages, parse_status, superseded_by (null in v0)

**doc_pages** — document_id, page_no, text, image_path

**external_orgs** — id, name, org_type (`utility | railroad | agency | consultant | other`), aliases[]

**dependencies** — id, project_id, ref_code (human-readable, e.g. `UTL-014`), dep_type (`utility_relocation | agreement | permit | row | railroad | access | other`), title, location_desc, station_from, station_to, external_org_id, external_contact, internal_owner, status (`identified | in_progress | committed | ready | closed | blocked`), criticality (`critical | high | normal`), committed_date, need_date, milestone_id, evidence_required (text: what closes this), last_verified_at, notes

**dependency_events** — id, dependency_id, event_type (`commitment | response | slip | escalation | status_change | closure`), event_date, description, created_by

**evidence_links** — id, parent (dependency_id or event_id), document_id, page_no, quote, verified (bool)

**candidates** — id, project_id, kind (`dependency | event`), payload_json, source_document_id, source_pages[], confidence, state (`pending | accepted | merged | rejected`), merged_into, adjudicated_at

**milestones** — id, project_id, code, name, need_date, source

**audit_log** — id, actor, action, entity_type, entity_id, before_json, after_json, ts. Append-only. Every ledger mutation writes here.

**eval_runs** — id, ts, corpus, prompt_version, metrics_json

## 6. Ingestion rules

- Dedupe by sha256; re-ingest is a no-op.
- Every page gets text and a rendered image (evidence display needs both).
- If a page's extracted text is under ~50 chars, OCR it.
- doc_type set by folder convention on import, correctable in UI.
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
- Extractors never write to the ledger. They only create candidates.

Per document type:

| Type | Approach |
|---|---|
| Conflict matrix (XLSX) | Deterministic column mapping first (one config per matrix layout); LLM only for freeform cells. Each row → dependency candidate. |
| Meeting minutes | LLM per document. Targets: action items, commitments with dates, status assertions, attendance (org → contact). Emit events against `dedupe_hint`, not new dependencies, unless clearly new. |
| Agreements / permits | LLM per document. Targets: obligations, milestone dates, notice periods, responsible party. |
| Email (EML/MSG) | LLM per thread. Latest commitment wins; earlier ones become `slip` events if dates moved. Capture non-response (thread with outbound ask, no reply) as a signal. |

Prompt files live in the repo, versioned (`prompts/minutes_v3.md`); `prompt_version` is recorded on every candidate and eval run.

## 8. Adjudication UI

Three screens. Keyboard-driven; you will adjudicate hundreds of candidates.

1. **Queue** — one candidate at a time: extracted fields left, cited page image with quote highlighted right. Actions: `a` accept, `e` edit-then-accept, `m` merge into existing (search by dedupe_hint similarity, pre-ranked), `r` reject (reason: duplicate / wrong / irrelevant / bad-citation). Target throughput: ≥60 candidates/hour.
2. **Ledger** — table of dependencies; filter by status, org, criticality, milestone, exception type. Row → detail: fields, event timeline, evidence gallery, audit history.
3. **Run report** — button + preview (section 10).

Merging is the core interaction: the same dependency will arrive from the matrix, minutes, and email. Accepting a duplicate instead of merging corrupts the ledger — the pre-ranked merge search must be good before anything else gets polish.

## 9. Exception engine

Computed as queries, not stored state. Severity = rule severity × criticality.

| Rule | Logic |
|---|---|
| MISSING_OWNER | internal_owner is null and status not `closed` |
| MISSING_DATE | committed_date is null and status in (`identified`,`in_progress`,`committed`) |
| MISSING_EVIDENCE | no verified evidence_link on the record or its latest commitment event |
| STALE | last_verified_at > 14 days ago and status not (`ready`,`closed`) |
| DUE_SOON | need_date within 30 days and status not (`ready`,`closed`) |
| OVERDUE | committed_date < today and no closure event |
| CONTRADICTION | ≥2 verified citations asserting different committed_date or status |
| ORPHAN | milestone_id is null |

Thresholds (14 d, 30 d) are per-project config.

## 10. Weekly readiness report

HTML → PDF. Every factual cell carries a citation marker (`[D12 p.4]`) linking to evidence. Zero uncited assertions — enforced by the same verifier, not by convention.

1. **Milestone readiness rollup** — per milestone: total dependencies, ready, at-risk, blocked, % with verified evidence.
2. **Critical items** — top N by (need-date proximity × criticality): owner, next action, committed date, status, citation.
3. **Exceptions summary** — counts by rule, worst offenders.
4. **Changes since last report** — new, closed, slipped, escalated (from event log; diff against prior report snapshot).
5. **Aging** — overdue items by days overdue.
6. **Appendix** — full ledger export.

## 11. Eval harness

- Gold set: hand-label 60-100 dependencies and ~100 events from a held-out project (not the one used to develop prompts).
- Metrics per run: critical-dependency recall (target ≥95%), overall dependency recall (≥85%), candidate precision (track for cost/junk, no hard gate), citation validity (100%, enforced), correction rate (% of accepted candidates that needed edits).
- One command: `make eval`. Results append to `eval_runs`. Prompt changes without an eval run don't merge.

## 12. Corpus assembly (M0)

Minimum viable corpus, one project: a conflict-matrix-like artifact, meeting minutes (≥3 meetings), ≥2 utility agreements or permits, a milestone list, and the utility special provisions. Sources:

- State DOT letting/bid portals (e.g. TxDOT letting documents, Caltrans, FDOT) publish plan sets, proposals, special provisions, and addenda.
- Utility agreements and coordination artifacts often ride in the bid package; if no matrix is public, seed one from the utility special provisions and plan general notes.
- Meeting minutes: many DOT/MPO project pages publish them; public-records request is the fallback.
- If you have access to any live project's real coordination files, use them — real inbox mess beats clean public documents.

Two projects minimum by end of v0: develop on A, hold out B for eval.

## 13. Build order

**Week 1 — M0 + M1.** Repo, Docker Postgres, schema migration 1, corpus downloaded and cataloged, content-addressed store, ingest CLI, text + OCR + page images. Exit: every document browsable page-by-page.

**Week 2 — M2 (easy half).** Matrix (deterministic) and minutes (LLM) extractors, shared candidate schema, citation verifier. Exit: candidate queue populated for Project A with verified quotes.

**Week 3 — M3.** Adjudication queue UI, merge search, ledger tables + detail view, event history, audit log. Exit: 100% of Project A candidates adjudicated; you can run a review session from the ledger screen.

**Week 4 — M2 (hard half) + M4.** Agreement and email extractors, slip/non-response events, milestone CSV import, need-date linkage, exception engine. Exit: exception list matches your hand-check of Project A.

**Week 5 — M5.** Report template, snapshot + changes-since-last diff, PDF render, XLSX export, demo path scripted end-to-end. Exit: zero-uncited-assertion report generated in one command.

**Week 6 — hardening + eval seed.** Ingest Project B, fix everything that breaks, label the gold set, first `make eval` run, iterate prompts once. Exit: definition of done below.

## 14. Definition of done (v0)

- [ ] One command: raw files → verified candidates for a new project.
- [ ] All candidates adjudicated through the UI at ≥60/hour.
- [ ] Exception list matches a manual check on Project A.
- [ ] Weekly report generates with zero uncited assertions.
- [ ] Entire pipeline runs on Project B with config-only changes.
- [ ] First eval run recorded; critical recall measured (gate to hit in M7: ≥95%).
- [ ] Live demo, raw docs to report, in under 15 minutes.

When every box is checked, return to `phase-1-roadmap.md` at M6.
