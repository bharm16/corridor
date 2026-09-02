# Implementation readiness review, 2026-09-01 (evening)

Received after PR #515 merged. Adopted as the execution guide for the two
preparation PRs and the tracker cleanup that follow. Reproduced verbatim.

---

# Verdict
**You are not quite clear for broad implementation yet. You are clear for implementation after one small readiness-cleanup PR.**
The strategic work is now substantially correct. **No additional market pivot, architecture rewrite, or technology-stack change is warranted.** The remaining problems are execution-contract problems:
1. `main` is currently red.
2. Several active issues contain mutually contradictory old and new acceptance criteria.
3. #510 conflicts with ADR-0083.
4. The pilot sampling contract is internally impossible at its minimum volume.
5. Contributor-facing documentation still describes the superseded product flow.
6. The running code still performs old-style automatic Record Inclusion.
7. External customer data needs a few additional security and governance gates.
After those are corrected, start implementation with #492 and #509.
---
# 1. Repair `main` before doing feature work
PR #515's primary `test` workflow completed **with failure after the PR had already merged**. The standalone `check` job and the slow/migration work succeeded, but the ordinary pytest job failed.
The failure is straightforward: `tests/test_ci_policy.py` still requires the workflow's jobs to be exactly `{"pytest", "slow"}` but PR #515 added the third standalone `check` job.
This appears to be a **CI-policy test mismatch, not a product-behavior failure**, but `main` must still be made green.
The correct fix is not merely changing the expected set. The purpose of that test is to ensure the gates do not run redundantly. Do this:
1. Keep the standalone `check` job.
2. Remove `make check` from the `pytest` job.
3. Update the test to expect `{"check", "pytest", "slow"}`.
4. Assert that `make check`, `make test`, and `make test-slow` each occur **exactly once** across the workflow.
5. Run the focused CI-policy test, `make check`, and `make test`.
6. Wait for every GitHub job to finish before merging the hotfix.
Because branch protection cannot presently require the status on this private plan, #506's closing comment correctly records that server-side enforcement remains unavailable. Until that changes, the repository procedure should require `gh pr checks <pr-number> --watch --fail-fast` before merge. Reopen #506 or create a small regression issue linked to #515; do not leave the failure only in this conversation.
---
# 2. Rewrite amended issues into one authoritative contract
This is the largest remaining implementation risk.
The issue-triage pass prepended a correct amendment but retained the complete obsolete body and acceptance criteria directly below it. That is useful history, but it makes the issue itself contradictory. The current roadmap says issue numbers are the implementation authority, so an agent can reasonably follow the lower checklist and violate the amendment.
Examples:
| Issue | Current contradiction |
|---|---|
| **#492** | The amendment requires separate source appenders and accepted-record writers, database roles, `SECURITY DEFINER` functions, static checks, and runtime bypass tests. The old acceptance criteria below it require only an AST/static allowlist. |
| **#450** | The amendment requires one design-partner format, Source Facts, and Proposed Deltas. The retained body requires three formats and automatic accepted Record Inclusion. |
| **#495** | The amendment requires preserving the adopted native workbook and placing provenance in an optional sheet or sidecar. The retained body requires appending provenance columns and leaving unmapped customer columns blank. |
| **#487** | The amendment says the isolated render process uses locally staged bytes while the parent owns persistence. The retained contract says the render worker itself uses the storage interface. |
Do a single tracker-cleanup pass:
- Replace each active issue body with **one current problem statement and one current acceptance checklist**.
- Move the prior body into a dated issue comment or rely on the linked realignment report and GitHub edit history.
- Remove `ready-for-agent` while an issue contains conflicting requirements.
- Restore the label only after the body can be implemented without interpreting which half wins.
- Keep closed and superseded issues intact; do not delete them.
This is especially important for #450, #487, #492, #494, #495, #496, #498, #503, and the deployment issues.
---
# 3. Correct and split #510 before anyone implements it
ADR-0083 explicitly says: exact unchanged support transfer moves Supporting Documentation in Use, never changes an accepted value, and never produces a Proposed Delta.
But #510 currently includes `support-only/no-semantic-change` as a Proposed Delta type, and automatic support-transfer/no-semantic-change resolution as part of the delta lifecycle. That would recreate the very conflation ADR-0083 corrected.
Revise #510 as follows:
- Remove `support-only` from Proposed Delta types.
- Exact unchanged support movement remains an Automatic Support Update receipt.
- Exact normalization that changes no semantic value is a recorded no-op or normalization receipt, not a coordinator-facing delta.
- A support change becomes a Proposed Delta only when it creates a real semantic question, for example the newer source contradicts or no longer supports the accepted proposition.
Also, #510 is too large for one safe implementation PR. Make it the umbrella and split it into:
### #510A — Proposed Delta identity and lifecycle
Own: schema; type; subject and field identity; incoming source version; accepted revision observed; grouping; coalescing; supersession; recurrence and reopening; duplicate prevention; open/current queries.
### #510B — Resolve Delta commands
Own: accept; edit; reject; defer; stale revision refusal; human principal; support assessments used; atomic Project Record revision; compensation/correction behavior; database-authorized write path.
### #510C — Baseline/delta operating mode
Own the critical transition rule: once a project has an adopted baseline, legacy dependency admission, structured-cell automatic inclusion, event admission, and schedule update paths may capture Source Facts and create Proposed Deltas, but may not silently replace accepted values.
Without that third slice, you could build Adopt Baseline and Proposed Delta while the old pipeline continues writing around them.
---
# 4. Fix the pilot sampling mathematics
The pilot contract currently requires at least 40 source arrivals per partner over eight weeks (five per week) but a weekly random sample of 20 source arrivals per partner (as many as 160).
Replace the material-change sampling rule with: each week, inspect all eligible source arrivals when the partner has 20 or fewer. When more than 20 arrive, draw a random sample of 20 without replacement. Across the full pilot, continue until at least the predeclared minimum number of eligible material-change cases has been reviewed; extend the pilot or report insufficient evidence if that minimum is not reached.
Also clarify: sampling is without replacement within the measurement period; a confirmed material automatic false write still fails that policy-class criterion even when the pilot continues for diagnosis; "99% baseline field accuracy" needs an explicit denominator, checked populated material fields, not merely sampled rows.
---
# 5. Bring contributor documentation in line with the new system
`AGENTS.md` still teaches `extract → adjudicate → ledger` as the operative architecture and says `adjudicate` is the only writer of Constraint Records. That remains useful as a description of the legacy compatibility path, but it is not the target architecture. Add a Target architecture section (SourceEnvelope → Source Segment → Source Fact → Proposed Delta → Resolve Delta → Project Record Revision → current/as-of projections) and a Transitional warning: current `admission.py`, `dependency_admission.py`, `event_admission.py`, and structured-cell inclusion are legacy paths, frozen against new capability; new work must write the spine first and must not introduce another legacy-only write. Also document the manual all-CI-green merge rule.
`../history/v0-build-spec-2026-07.md` remains titled "Cited Readiness Ledger (build this first)". Move it to `docs/history/` or retitle it unmistakably as historical. Do the same for `../history/corpus-acquisition-spec-2026-07.md` unless individual corpus procedures are extracted into a current design-partner-data guide.
`README.md`: replace the v0-build-spec table entry with the current roadmap and add an implementation-status paragraph: the baseline-plus-delta target is accepted architecture; Adopt Baseline and Proposed Delta are not yet implemented; the current admission pipeline remains transitional and must not be used as the authority model for a customer pilot.
---
# 6. Tighten #509 before building Adopt Baseline
Add these acceptance conditions:
1. A project may have only one initial Adopt Baseline act. Re-importing or replacing it is a new record change, not another initial adoption.
2. A nonempty existing Project Record cannot be silently adopted over. It needs an explicit migration mode, reconciliation preview, or a fresh customer environment.
3. The preview creates no accepted authority. It may register bytes, segments, and Source Facts, but not effective accepted decisions.
4. Source-row identity and Project Record subject identity remain distinct. Duplicate matrix IDs must not silently collapse distinct subjects.
5. The baseline template is immutable by digest. #495 must render from the exact adopted workbook family and revision.
6. After successful adoption, baseline/delta operating mode is enabled atomically. The old automatic-admission path can no longer update accepted values.
7. The accepted-write portion is blocked by #492.
---
# 7. Make #492 the first substantive code change
After the CI and issue-contract cleanup, #492 should be the first architecture implementation PR, before #509 writes accepted baseline decisions or #510 resolves deltas. Desired boundary: an application runtime role that can read all permitted project data but cannot directly write accepted spine authority; a source append role that can append Source Segments, Source Facts, proposals, support assessments but cannot make them effective; a record decision role executable only through narrow SECURITY DEFINER commands that write revision and decision atomically. The static architecture test remains useful, but the database must refuse a direct application-role INSERT, UPDATE, or raw SQL bypass.
---
# 8. The old runtime must be treated as transitional
Today `admission.load_project` still declares runs, runs legacy dependency admission, automatically includes structured-cell facts, and runs event admission; structured spreadsheet-cell facts can still become effective FactDecision rows automatically; email still enters through one configured service address and is routed from content; recorded-verbal Source Segments still carry the legacy statement identity; the reader-equivalence mechanism overlays only selected spine values onto a frozen legacy reading. None of this requires ripping out working code immediately. It requires an explicit project mode: a development legacy project may continue current compatibility behavior; an adopted-baseline project does source capture and delta generation only, with no legacy automatic accepted-value updates. Do not use current `main` as the authoritative workflow for a real customer pilot until that boundary exists.
---
# 9. Add one missing external-data governance issue
Before a real customer's email, UCM, agreement, or meeting minutes are sent to any model or third-party processing service, create a dedicated gate covering: customer authorization for processing; permitted source classes; data classification and PII handling; subprocessors and contractual disclosures; provider retention, storage, and training configuration; redaction or no-model fallback; secrets and connector credential custody; incident response; auditability; deletion and custody transfer; who approves changing the provider or model. It requires a recorded operational/customer decision before live project data enters staging. Add it as a blocker for external-data activation in #489, but not for local fixtures or synthetic development.
---
# 10. Strengthen #490 before external intake
Before external data, require one of: a real scanning adapter enabled in staging and production; or a named, time-bounded risk acceptance stating why it is disabled. Also require: parser and rendering timeouts; CPU and memory ceilings; archive recursion/decompression ceilings; ephemeral working directories; no unnecessary outbound network from document-processing workers; quarantine objects inaccessible to ordinary project readers; no LLM, OCR, PDF, or spreadsheet parser invocation before intake checks pass. This should remain a pre-pilot blocker.
---
# 11. Decide whether Recorded Verbal Statements are in the pilot
Recommended initial pilot: exclude newly recorded calls from the gated source population. The pilot evaluates UCM revisions, project mailbox/folder, minutes, and schedule exports where present. Recorded Verbal Statements may remain available as legacy-compatible project context, but they are not counted toward source coverage or delta accuracy. Alternative: move #512 before #509/#510. Do not let verbal statements be nominally in scope while their target source identity remains a post-pilot migration.
---
# Go/no-go by workstream
| Work | Status |
|---|---|
| CI hotfix | Go immediately |
| Issue-body and current-doc cleanup | Go immediately |
| #428 buyer discovery | Go now, in parallel |
| #461 licensing decision | Go now, in parallel |
| #492 database/write authority | First substantive code PR |
| #487 storage interface | Go after its body is made unambiguous |
| #490 intake hardening | Go now; required before external data |
| #491 logs and metrics | Go now |
| #509 Adopt Baseline design and fixtures | Go now |
| #509 accepted-record writer | After #492 |
| #510 as currently written | No-go; correct and split first |
| #494 change inbox | After Proposed Delta read model exists |
| #495 customer workbook export | After #509 and current revision reads |
| External staging with synthetic data | After storage and operational shell |
| External staging with customer data | After licensing, intake security, identity, disposition, and data-governance gates |
| Paid pilot | After the complete vertical slice and corrected measurement contract |
| Assistant display, universal readiness, broad PDF platform | Remain frozen |
---
# Exact next sequence
Preparation PR 1 restores green main (fix the CI job/test mismatch, remove duplicate `make check`, rerun every PR workflow, record the regression against #506/#515). Preparation PR 2 makes contracts implementation-ready (rewrite contradictory issue bodies; correct and split #510; fix pilot sampling; update #459 to include ADR-0082/0083 and the corrected Phase 0 state; update AGENTS.md and README with target versus transitional architecture; move or prominently archive the old v0/corpus specifications; add the baseline-mode/legacy-admission shutdown issue; add the customer-data/provider-governance issue; add #492 as a blocker of accepted-write work in #509/#510). Then three lanes run in parallel: commercial (#428, #461, pilot agreement and data governance, one real native UCM and source), platform (#492, #487, #490, #491, #496/#511 for the selected channel, #503/#514 before live customer staging), product (#509, corrected #510A/B/C, #494, #495, narrow #425). The next code should establish the authority boundary and Adopt Baseline.
