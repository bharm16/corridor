# Implementation contract review, 2026-09-01 (late evening)

Received after PRs #517 and #523 merged. Adopted as the guide for Preparation PR 3 and the accompanying tracker pass. Reproduced verbatim.

---

# Verdict
**Yes—one more focused revision pass is needed before substantive implementation.**
The good news is that the previous readiness work landed correctly: PRs #517 and #523 are merged, the CI-policy regression and test-state leaks were fixed, and the current `main` points at the #523 merge. PR #523's workflow completed successfully. The branch is still unprotected, so the manual all-green rule remains necessary.
This is **not another product pivot, stack change, or architecture rewrite**. The remaining problems are implementation-contract defects exposed by making the backlog more precise. I would not hand #492, #509, #518, #519, or #520 to an implementation agent yet.
---
# 1. Revise #492 before calling it agent-ready
This is the most important correction.
## The current contract does not yet establish real database isolation
#492 correctly calls for application-runtime, source-append, and record-decision roles. But the repository currently:
- initializes PostgreSQL using the `corridor` credential;
- configures the application to connect with that same `corridor` credential;
- constructs the production SQLAlchemy engine directly from that URL.
That means #492 must explicitly replace the one-credential arrangement. Merely adding grants and testing a temporary role will not protect the real application connection.
Existing `SECURITY DEFINER` functions are also executable by `PUBLIC`. The human decision command accepts the claimed principal as an argument and is owned by the decision-writer role, but any database login currently granted public execution may call it. The original fact-decision migration likewise revokes direct table writes while granting public function execution.
## Amend #492 to require
1. **Separate deployment credentials**: migration/DDL credential; web application credential; worker credential; no application process connects as the database owner, migration user, or superuser.
2. **NOLOGIN function-owner roles**: one owner for validated source-append functions; one owner for accepted-record decision functions; web and worker roles receive only specific `EXECUTE` grants; they are not members of the owner roles and cannot `SET ROLE` into them.
3. **No `PUBLIC` execution**: revoke `EXECUTE` from `PUBLIC` on every authority-bearing function; grant human-decision commands only to the web capability; grant machine-policy commands only to the worker capability; revoke public schema creation and set safe default privileges.
4. **Source appends through commands, not raw table grants**: the source-append role should own narrow `SECURITY DEFINER` append commands; the application should not receive unrestricted `INSERT` on `source_segments`, `facts`, proposals, or support assessments; those commands enforce project scope, typed references, locators, digests, row accounting, and idempotency.
5. **A clear legacy compatibility rule**. #492 currently says the production runtime cannot write accepted authority while also retaining `adjudicate` as the legacy writer. Pick one explicit transition. **Recommended:** customer/staging credentials cannot directly write legacy accepted tables. A separate legacy-development credential may exist only in local/test environments for legacy projects. Alternative: wrap every permitted legacy write in constrained database commands. Do not let the implementing agent decide this incidentally.
6. **Deployment-realistic tests**: assert `rolsuper = false`, `rolcreaterole = false`, `rolcreatedb = false`, and no owner-role membership for app logins; run the application integration test with the actual runtime URL, not as the test database owner; prove direct ORM writes, raw SQL, disabling triggers, changing role, and calling the wrong command family are refused.
7. **Future-entity extension points**. #492 should secure the entities that exist when it lands. #518 and #519 should be required to extend the role matrix and static checks when they add Proposed Delta and Resolve Delta tables. #492 should not pretend to enumerate tables that do not exist yet.
Remove `ready-for-agent` from #492 until those points are incorporated.
---
# 2. Add the missing Support Assessment foundation
ADR-0082 says the support-assessment relation does not exist yet. It defines that relation as one proposition linked to one or more Source Segments, with a role, assessment, actor or released policy, and time. It also says support assessment and the record decision remain distinct acts.
But the backlog currently distributes responsibility incorrectly: #493 explicitly excludes the relation and says it will be built through #518/#519; #519 assumes support-assessment rows already exist and requires Resolve Delta to cite them; #509 does not require support assessments for the source-backed values adopted into the initial baseline.
Create one dedicated foundational issue before the accepted portion of #509 and before #519. Its contract should include: an enforced proposition reference to a Source Fact, Extracted Proposal, Proposed Delta, or accepted field proposition; one or more Source Segment members; role: `value_support`, `attribution`, `timing`, `scope`, or `context`; assessment: `supported`, `partially_supported`, `contradicted`, `unclear`, or `not_assessed`; human-principal XOR released-policy authority; project and source-scope enforcement; append-only correction or supersession; idempotency and competing-worker tests; current/as-of queries; no inference of semantic support from locator validation; support-accuracy measurement for the pilot. Use real foreign keys or an enforced proposition registry. Do not use an unchecked `(object_type, object_id)` pair.
The dependency graph should become: #492 authority boundary → Support Assessment relation → #509 accepted baseline adoption and #519 Resolve Delta. #493 can still proceed independently because locator validation remains a separate mechanical concern.
---
# 3. Correct #518, #519, and #520
## #518 — Proposed Delta identity and lifecycle
The current issue contains four modeling defects.
### A. A new subject has no existing Project Record subject
#518 includes `new subject`, but also requires every delta to name the Project Record subject and field it questions. For a genuinely new conflict, that subject does not exist yet. Use a discriminated target: `existing_subject` (accepted subject identity + affected field) or `proposed_subject` (source-bound proposed identity + proposed initial field set). A new subject should ordinarily be one delta group containing its proposed initial fields, not ten unrelated field deltas.
### B. "Never mutated" conflicts with "marked superseded"
Model: ProposedDelta (immutable occurrence); DeltaGroup (one atomic source change); DeltaDisposition (accept / edit / reject); DeltaSupersession (old occurrence → new occurrence); DeltaDeferral (temporary work scheduling). Then derive the current live state through a view. Do not combine an immutable delta row with mutable lifecycle columns unless you deliberately permit one narrowly constrained supersession update.
### C. Coalescing must follow source lineage
A newer revision of the **same source family** may supersede or coalesce the prior live delta. An independent email, meeting record, or agreement mentioning the same field must not silently replace another source's delta. It may add support; contradict it; create a separate delta member; cause the current delta group to be recomputed. Add source-family/revision identity to the rule.
### D. Apparent removal needs a completeness proof
An absence can become `apparent removal` only when the incoming source is a complete enumerative revision for the relevant population and its row accounting is sealed. Absence from an email, minutes document, partial export, filtered worksheet, or failed extraction is not removal evidence.
Also change "impact facts" to **impact Derivations** when the system computes affected constraints or Key Dates. A computed consequence is not something the source stated.
## #519 — Resolve Delta commands
The current contract directly conflicts with ADR-0083: ADR-0083 classifies `deferred` as a resolved delta disposition; #519 says a deferred delta stays live. The better model is: `accept`, `edit`, and `reject` are semantic delta dispositions; **defer is Work List scheduling**, not a decision about what the Project Record says; the delta remains open but is hidden from immediate work until its return date or a defined wake-up event; deferral writes its own attributable receipt and no Project Record revision. That changes an accepted ADR clause, so it needs an explicit amendment—not merely an issue edit.
### Constrain "edit"
"Edit" must not allow a person to type an unsupported external fact and have it appear source-backed. Permit an edited resolution only when it: selects an existing captured Source Fact; composes supported Source Facts under a named transformation; performs a proven lossless normalization; or records a separate attributable source origin such as a Recorded Verbal Statement. For Utility Owner, attribution, Promised For, completion, Applies To, agreement status, and similar external facts, unsupported free text should produce **Needs clarification**, not an accepted value.
### Define effects by delta type
| Delta type | Required effect |
|---|---|
| New subject | Create subject plus initial effective decisions atomically |
| Changed field | Supersede the effective field decision |
| Timing change | Preserve previous and new timings and statement lineage |
| Organization change | Distinguish identity correction from an actual owner change |
| Apparent removal | Record a supported active-log or source disposition; never delete |
| Contradiction | Record a conclusion while retaining all source propositions |
| Schedule/Key Date | Create a new Key Date or Required By relationship decision |
| Closure | Record the supported exact closure outcome and scope |
## #520 — Operating mode and the #509 cycle
#509 requires successful adoption to activate baseline/delta mode atomically. But #520 currently says it depends on #509. That is a logical cycle. Revise #520 so that it builds the mode infrastructure first: mode is derived from an immutable baseline-adoption/activation receipt, not a freely editable `projects.mode` toggle; transition is one-way: legacy → adopted baseline; database guards refuse legacy accepted-value writes for an adopted project; legacy SQL functions themselves check the mode—the protection cannot live only in Python branches; tests can create an adopted-mode fixture without requiring the final #509 UI or importer; #509 later invokes the already-existing transition in the same transaction as baseline adoption.
The corrected order is: #492 → Support Assessment foundation → #518 → #520 mode rails and database guards → #509 Adopt Baseline → #519 Resolve Delta. Non-authoritative #509 workbook parsing, preview, and fixture work may proceed earlier, but the accepted adoption command should not merge before #520.
---
# 4. Split the staging issue from live-customer activation
The current dependency graph still contains a phase cycle. #489 has seven native blockers. One is #488, but #488 itself depends on #518's Proposed Delta model. Another is #491, whose full contract includes delta and decision metrics that cannot finish until #518/#519. Meanwhile #514 wants to destroy databases, objects, keys, backups, and snapshots before the environment and control plane it must destroy have been chosen. That means Phase 1's staging environment is currently blocked by Phase 2's delta implementation.
## Split #489 into two issues
### Synthetic environment foundation
Own: application image; web and worker logical roles; managed PostgreSQL; object storage; migration job; basic secret injection; DB/storage/worker health; basic structured logs; backup and one restore rehearsal; synthetic infrastructure smoke test. Dependencies should be approximately #492, #487, #490 byte/parser sandbox foundation, #491A basic operational telemetry. It should not require licensing, a full connector polling system, customer identity decisions, or live-data disposition before synthetic deployment.
### Live customer activation
Own: actual customer environment and source activation; licensing; production identity and project authorization; customer-data governance; scanner or accepted time-bounded risk decision; disposition and backup-expiration process; selected connector; adopted-baseline mode; no legacy automatic accepted-value writes; end-to-end pilot slice. This issue must additionally depend on **#509, #518, and #520**. The current external-data condition does not include them, which means real customer data could enter while the legacy automatic-admission path is still authoritative. Raw partner bytes needed for development may enter only an isolated, explicitly non-authoritative environment with processing disabled until those gates are satisfied.
## Split #491
### #491A — operational telemetry: structured app/worker logs; correlation IDs; database/storage health; worker heartbeat; retries, lag, failures, and last-success age.
### #491B — product and pilot measurement: source arrival → capture → delta → decision; review and operations minutes; model/provider cost; false-write classification; per-project and source-class reporting.
Project and customer identifiers belong in logs and database-backed analytical reports. Avoid turning every project ID into a high-cardinality infrastructure metric label.
## Move #488 later
#488 should block live pilot operations, not the synthetic environment foundation. Its delta-generation handler cannot exist before #518.
## Separate #503 decision from implementation
#503 is currently a decision contract. Add an implementation child or put explicit implementation acceptance into live activation. Closing a decision document alone does not establish working deprovisioning or project authorization.
## Revise #514
ADR-0083 has already amended ADR-0080, so #514's first checkbox—create the necessary ADR amendment—is complete. Its current work should be: implement export-and-destroy against the platform selected by the synthetic-environment issue; use provider-native whole-environment destruction where possible; retain its receipt in the control plane; rehearse partial failure and backup expiration; avoid building a generic row-by-row disposition engine.
---
# 5. Add explicit ownership for the change summary and weekly report
ADR-0075 promises four first-slice outputs: updated UCM; change summary; chase list; weekly report. The current open tracker has no issue that owns the **change summary** as a deliverable. Create an output contract—one issue or two closely linked ones—that owns: resolved changes since the last released weekly artifact; rejected/deferred/open changes shown separately rather than described as accepted changes; one frozen Project Record revision as the source of all four outputs; partner-required weekly format; exact provenance and report baseline; deterministic rerendering; external release binding; explicit coverage when a source class failed or was not processed.
This is also where the adopted-baseline project's reader path should be settled. The cleaner design is: legacy project → existing legacy readers; adopted-baseline project → spine-native Work List, UCM export, change summary, chase list, weekly report. Do not dual-write the entire new commercial model merely so old development readers can see it.
That requires a small clarification to ADR-0081, because it currently presents the staged legacy migration and says spine-native verbal origin is the first implementation task. The amendment should state: non-verbal baseline/delta work may precede #512 while Recorded Verbal Statements are explicitly excluded from the pilot; #512 remains mandatory before verbals join the target product or before full cutover; new adopted-baseline surfaces may be spine-native without mandatory legacy dual-write; legacy-project compatibility remains until #458.
---
# 6. Strengthen three downstream contracts
## #495 — workbook fidelity and new-row behavior
Add: workbook capability inventory during Adopt Baseline; declared supported file family and extension; fail-closed handling for unsupported macros, external links, controls, signatures, pivots, protection, or unknown package parts; package-level OOXML comparison, not just loaded cell comparison; unchanged ZIP parts and relationships remain byte-identical where feasible; explicit row-insertion template for a new conflict; how formulas, validation, formatting, tables, filters, hidden content, and row identity are copied or updated; existing-subject changes modify only mapped cells; new subjects may add one declared row and the minimum associated table metadata; apparent removals follow a customer-declared retirement/status mapping and never silently delete a row; adding a provenance worksheet is opt-in because it changes the customer workbook. The exporter should refuse a workbook family it cannot safely round-trip rather than emit a subtly damaged file.
## #425 — chase-list semantics
Revise it to: use auditable consequence bands; use one declared quantity within each band; omit a deferred delta from immediate chase work until its return date or wake-up condition; replace the old `Dispute` wording with `Source Discrepancy`; define "no movement" as an exact elapsed-time predicate; surface "thread has no response" only when Corridor has a source-backed outgoing request and an expected response boundary. Merely seeing no email is not evidence someone was asked.
## #490 — staged hostile-input checks
Define three stages: byte gate (size, magic/type, digest, top-level archive envelope, scanner); sandboxed structural inspection (page count, archive members, nested expansion, OOXML structure); rich processing (PDF/spreadsheet parsing, OCR, rendering, LLM). Nothing reaches rich processing until the first two stages pass. Add fixtures for: path traversal and absolute archive paths; symlink, device, and special-file entries; duplicate archive member names; total expanded bytes across nested archives; recursion depth; XML entity/resource bombs in OOXML-style packages; timeout, memory, and process termination; quarantine access isolation. Then rewrite #490 as one contract rather than an original section plus an appended amendment.
---
# 7. Small cleanup revisions
- Rename #459 from `ADR-0075..0081` to `ADR-0075..0083`.
- Replace `#510C` with the actual issue number `#520` in `AGENTS.md`.
- Change the workflow comment from "required as a status" to "emitted as a standalone status"; branch protection is not enabled.
- Consolidate #509 and #490 into one current body each rather than retaining an "Added…" acceptance section.
- Add #446 as a prerequisite for the actual email/minutes source implementations so a model literal cannot become a Source Fact without deterministic materialization.
- Replace the manually maintained `SPINE_TABLES` cleanup list with either the `runtime_database` fixture for committed scenarios or an architecture inventory test that fails whenever a new project-scoped spine table is omitted.
- Treat the uploaded `PROJECT-MAP.md` as historical. Regenerate it only after the authority and delta foundations settle.
---
# Recommended implementation order
## Preparation PR 3 — narrow, mostly contracts
1. Add one amendment ADR (ADR-0084) covering: deferral as work scheduling rather than semantic delta resolution; non-verbal pilot work preceding spine-native verbal origin; adopted-baseline projects using spine-native product surfaces without mandatory legacy dual-write.
2. Amend #492. 3. Create the Support Assessment foundation issue. 4. Amend #518, #519, #520, and their dependency edges. 5. Split #489 and #491; move #488 to the live-pilot path. 6. Amend #503 and #514 sequencing. 7. Add explicit change-summary/weekly-report ownership. 8. Amend #495, #425, and #490. 9. Apply the minor documentation corrections above.
## Code sequence after that
1. #492 real least-privileged database authority; 2. Support Assessment relation; 3. #446 replayable Source Fact materialization enforcement; 4. #518 Proposed Delta identity, groups, lifecycle, queries; 5. #520 adopted-mode rails and database refusal; 6. #509 Adopt Baseline, including atomic activation; 7. #519 typed Resolve Delta commands; 8. #494 Proposed Delta Work List; 9. #495 + change summary + chase list + weekly report; 10. #450/#455/#456 only the selected partner's source classes.
In parallel: Commercial #428, #486, #461. Platform #487 → #490 byte/sandbox gates → #491A → synthetic environment foundation. Live activation: identity implementation + #514 + #522 + selected connector + #509/#518/#520 + full vertical slice.
---
# What remains sound
None of these revisions changes Corridor's core model. **Final status:** strategically clear, stack clear, core architecture clear—but not yet safe to begin #492/#509/#510 implementation from the current issue text. After this narrowly scoped contract pass, further broad review would have diminishing value and implementation should begin.
