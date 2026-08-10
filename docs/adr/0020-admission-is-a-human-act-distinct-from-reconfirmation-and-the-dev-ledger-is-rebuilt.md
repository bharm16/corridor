# Admission is a human act distinct from Reconfirmation, and the development Ledger is rebuilt, not ratified

> The development-Ledger rebuild in this decision is superseded by ADR-0021, and human-only Admission by ADR-0029. The distinction between Admission and Reconfirmation remains in force.

The Ledger's constitution — nothing enters except by a human decision — stands as written. The live database does not: all 141 Dependencies were admitted by `accept_candidate` under actors `agent` (96) and `demo` (45), zero by a human. The machinery rule held — only Adjudication ever wrote, extractors never did — but the actor claim failed. The records yield, not the definition.

Two enforcement holes made the drift possible, and they close first. The audit log records an actor *label*, not an identity: `actor` is unrestricted caller-supplied text and the web routes hard-code `reviewer`, so canonical accept/merge operations gain a required, stable human principal. And the demo path operates on the real NHHIP project — `demo._reset` deletes its Dependencies, Candidates and their audit entries, then auto-accepts as `demo` — contradicting human-only admission and append-only history at once. The demo isolates to its own project.

The 141 current rows are development artifacts, not compliant Ledger contents. They are archived as a legacy snapshot — never relabeled human — and rebuilt through ordinary, attributable human Adjudication from their uniquely matching pending Candidates: each of the 141 has exactly one, from the `matrix_tiered_v2` generation. No Active Run has been declared and the code now carries `matrix_tiered_v3`, so the rebuild names the run it draws from rather than assuming a current one. The rebuild also waits for operative-support designation (ADR-0017) to exist, so the human pass captures publication support once instead of requiring a second pass. The pending Candidates (6,685 as of 2026-08-05) stay pending: workload, not violation. The rebuild pays the constitutional debt only — 141 rows of one project's matrix are not the held-out, multi-document human slice M7's triage still owes.

**Admission and Reconfirmation are separate acts.** Reconfirmation (ADR-0016) re-points operative support at the current revision; it never revisits whether the Dependency should exist, how it was merged, or what its fields conclude — those are Adjudication's questions. A future UI gesture may perform both in one motion, but it records two distinct decisions.

## Considered options

**Weaken the definition to "accountable, recorded decision."** Rejected: it dilutes the single sentence a skeptical reviewer most needs to believe, and normalizes what the M7 gate write-up already treated as a debt.

**Human Ratification of the existing records in place.** Rejected as a permanent escape hatch nothing justifies: the development-era Dependency ids carry no value worth preserving, so a clean rebuild needs no new domain concept. If in-place ids ever matter, ratification with quarantine-until-ratified would need its own ADR.

**Routing the cleanup through M8's Supersession Review.** Rejected twice over: reconfirmation is not admission (the category error), and the live data disproves the convergence — every one of the 141 cites the manifest's current revision, so none would enter Supersession Review at all.
