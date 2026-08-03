# Phase 1 Roadmap — External-Party Readiness Ledger

**Goal of Phase 1:** a working, demoable system of record for utility and external-party readiness, running on 2-3 real highway projects, operable by one person, no external accounts required.

**Companion doc:** `v0-build-spec.md` covers milestones M0-M5 in implementation detail. Build nothing past M5 until M5 is done.

---

## Scope guardrails

Phase 1 never includes: CAD/GIS, scheduling engine, document-management or PMIS replacement, chatbot interface, autonomous schedule changes, legal notices, permit engine, external-party portal, marketplace, SSO/enterprise security, deep bidirectional integrations. If a task list contains any of these, cut it.

---

## Milestones

### M0 — Corpus and environment (0.5 wk)

- Assemble document sets for 2-3 real projects from public DOT sources (plan sets, proposal/special provisions, utility agreements, conflict matrix if available, meeting minutes, addenda). Construct a milestone list per project.
- Repo, Postgres, object storage dirs, one-command boot.

**Done when:** documents cataloged by project and type; stack boots with one command.

### M1 — Evidence store and ingestion (1 wk)

- Content-addressed (SHA-256) immutable file store; originals never modified.
- Document registry: project, type, source, date, parse status.
- Text extraction with OCR fallback; per-page text + page images for evidence display.

**Done when:** every file retrievable with page text and page image; re-ingest is idempotent.

### M2 — Extraction pipeline (1.5-2 wk)

- Type-specific LLM extractors: conflict matrix, meeting minutes, agreements, email. Output = candidate dependency records and events, every field cited to doc/page/quote.
- Automatic citation verifier (quote must match cited page).

**Done when:** candidates generated for Project A with verified citations; candidate junk rate under ~30%.

### M3 — Adjudication and ledger (1.5-2 wk)

- Review queue: accept / edit / merge / reject, keyboard-driven.
- Entity resolution assist (same dependency across matrix, minutes, email → merge).
- Canonical dependency ledger with event history and append-only audit log.

**Done when:** all Project A candidates adjudicated; ledger browsable by location, organization, status, criticality.

### M4 — Schedule link and exceptions (1 wk)

- Milestone import (CSV), need-date linkage per dependency.
- Exception engine: missing owner, missing date, missing evidence, stale, due-soon, overdue, contradiction, orphan (exact rules in build spec).

**Done when:** exception list matches a hand-check of Project A.

### M5 — Reporting and demo (1 wk) — **v0 complete**

- Cited weekly readiness report (HTML/PDF): milestone rollup, critical items, exceptions, changes since last report, aging.
- Ledger export to XLSX.
- End-to-end demo path: raw documents → adjudicated ledger → report, live in under 15 minutes.

**Done when:** report contains zero uncited assertions; full run works on Project A. This is the demo checkpoint artifact.

### M6 — Second-project generalization (1 wk)

- Run the entire pipeline on Project B with zero code changes; fix every schema or prompt assumption that breaks.

**Done when:** differences between projects are config only.

### M7 — Quality gate and eval (1 wk)

- Hand-labeled gold set on a held-out project (dependencies + events).
- Metrics per run: critical-dependency recall, overall recall/precision, citation validity, human correction rate.

**Done when:** ≥95% recall on labeled critical dependencies; 100% citation validity; metrics recorded automatically every run.

### M8 — Document versioning and revisions (1-1.5 wk)

- Document supersession (rev B → rev C), re-extraction diff: what changed, which ledger records are affected, which evidence went stale.

**Done when:** uploading a revised document produces a change report and flags affected records; nothing is overwritten.

### M9 — Second user and write-back lite (1.5-2 wk)

- Basic auth, three roles (admin / reviewer / viewer).
- P6 XER import (replacing the CSV milestone stopgap).
- Round-trip export: ledger back out in the matrix format the project already uses.
- Backup/restore.

**Done when:** a second person can review safely; the ledger round-trips into the project's existing matrix format.

---

## Total effort

~10-13 focused solo weeks. Nights-and-weekends pace: roughly double the calendar time.

## Sequencing rules

1. Nothing gets built ahead of M5 — no auth, no integrations, no polish beyond the demo path.
2. Every milestone is exercised on real project documents, never synthetic samples.
3. No integration work (M9's XER import included) before two projects run clean.
4. M7's eval gate is not skippable; it is the only objective measure that the core works.

## Phase 1 exit criteria (unlocks Phase 2)

- Pipeline runs on 3 projects with zero project-specific code.
- Eval gate passing on a held-out project.
- A full weekly readiness meeting can be run from the tool alone.
- The generated report fully replaces a manually built weekly report format.

## Phase 2 pointer (out of scope here)

External link/email-based responses without accounts, action assignment and approvals, closure evidence requirements, schedule write-back, reusable owner templates, SSO and security package.
