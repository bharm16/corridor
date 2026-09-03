---
status: accepted
domain: project-record
scope: current product
amends:
  - ADR-0010
---

# The accepted record's alerts are its own, not the legacy task system's

**Amends ADR-0010.**

ADR-0010 abolished the Constraint Alert severity scalar and was careful to say what it was *not* doing. Its closing paragraph reads: "What this does not change: the rules, their predicates, their thresholds, the fact that everything is computed at read time and stored nowhere, and `MISSING_EVIDENCE`'s relationship to whether the documentation requirement is met. This ADR removes the unsupported ranking; it does not change the authority of the underlying facts." Twelve rules were preserved deliberately. That is precisely why retiring or re-basing one of them is a decision that needs an ADR rather than an implementer's judgment.

#534 rendered the change summary and the weekly coordination report from one frozen accepted Project Record revision, and its Constraint Alert section covers **three** of the twelve — `OVERDUE`, `DUE_SOON` and `MISSING_DATE` — under a declared check set named `accepted_record_date_checks`, version `v1`. Those are the three derivable from accepted spine values alone. The other nine read `dependencies`, `operative_support`, `work_decisions` and the dispute tables, which ADR-0081 freezes against new capability and which ADR-0084 section 3 forbids extending for an adopted-baseline project. #534 could not implement them without breaking the freeze, and correctly did not try. The consequence is that an adopted-baseline project's weekly report **silently under-reports** relative to what a legacy project's report shows, with no declared reason for the difference.

#596 was written to close that gap, and it asked an implementer to inventory the twelve rules and decide, per rule, whether to port, supersede, or retire. Delegating that was the mistake this ADR corrects. Each of those choices changes what Corridor asserts about a project's accepted record, and ADR-0010 explicitly preserved every one of them.

## The disposition of all twelve rules

| Rule | Decision | Treatment on the accepted record |
|---|---|---|
| `OVERDUE` | keep | already implemented in the v1 check set |
| `DUE_SOON` | keep | already implemented in the v1 check set |
| `MISSING_DATE` | keep | already implemented in the v1 check set |
| `MISSING_EVIDENCE` | **port, re-based** | derived from effective Support Assessments and Supporting Documentation in Use — **not** from the Source Passage Check |
| `SUPERSEDED_CITATION` | **port** | fires only when the accepted proposition still depends on a superseded Document Revision and no effective current replacement support exists |
| `CONTRADICTION` | **supersede** | an open contradiction Proposed Delta and #528's focused question |
| `ACTION_DUE_SOON` | **supersede** | #425's accepted follow-up bundles and the report's Follow-up section |
| `ACTION_OVERDUE` | **supersede** | #425's accepted follow-up bundles and the report's Follow-up section |
| `MISSING_OWNER` | **retire** | Corridor does not require an internal owner for every Utility Conflict |
| `MISSING_ACTION` | **retire** | Corridor does not require a task merely because a conflict exists |
| `STALE` | **retire** | replaced by #425's source-backed no-response rule |
| `ORPHAN` | **retire** | the absence of a Key Date link is not inherently a defect |

Keep 3, port 2, supersede 3, retire 4.

### Why `MISSING_EVIDENCE` is ported with a corrected meaning

The legacy rule fires when a Constraint has no supporting document whose cited passage was found in its source — its customer label today says exactly that: "No supporting document passed the source passage check". ADR-0082 decided that this is not what evidence means. A passage being locatable is mechanical and replayable and "says nothing about the claim"; whether a source supports a proposition is a separate typed relation with a role, an assessment, and an author. #493 built the locator half and is deliberately limited to it.

So the spine-native rule means something narrower and more defensible: **no effective Supporting Documentation is currently in use for the accepted proposition under the applicable support requirement.** It reads the Support Assessment relation (#530) and the accepted support designation, and it never infers support from locator validation. This is the rule ADR-0010 said it was not changing, corrected by a later decision about what evidence is — which is why it is a port and not a keep, and why its customer label cannot survive the change unaltered.

### Why `SUPERSEDED_CITATION` is ported unchanged in predicate

ADR-0016 already fixed this rule's predicate and defended it against the obvious wrong version: Corridor computes it "for every Constraint whose **Supporting Documentation in Use** ... relies on a non-current Document", and "the predicate is Supporting Documentation in Use, never 'any old link exists'". A blanket predicate would fire on history that is doing no work, could never clear without deleting that history, and would quietly pressure users to erase provenance to silence it.

The port keeps that predicate exactly and moves its carrier from `operative_support` to the accepted record. It **must not** fire merely because a document has a successor. It fires when the accepted proposition still depends on the superseded revision and no effective current replacement support exists. It still describes a real accepted-record risk, which is why it is ported rather than retired.

### Why the contradiction and action-date alerts are superseded rather than kept

`CONTRADICTION` fires when sources disagree on a field. On the spine, a source that disagrees with the accepted record is not a second accepted fact — it is incoming evidence relative to the accepted record, which is exactly a Proposed Delta (ADR-0076). It belongs in a contradiction delta, in #528's side-by-side focused question, and in a Follow-up Plan when the coordinator chooses "needs coordination". Keeping the alert as well would give one question two action surfaces: a coordinator who resolved the delta would still see the alert, and a coordinator who read the alert would have no way to act on it from there.

`ACTION_DUE_SOON` and `ACTION_OVERDUE` are the same shape one layer over. #425 derives due and overdue follow-up from accepted authority and renders it as contact-ready bundles and a Follow-up section. An alert that says the same thing from the legacy Action Due Date is a parallel task list that can disagree with the real one.

Superseded is not the same as dropped. Each of these three findings still reaches the coordinator; it reaches them once, in the surface that can act on it.

### Why four rules are retired outright, and the line this draws

This is the part of the decision with the most consequence, so it gets stated plainly: **Corridor must not recreate the legacy task system.**

`MISSING_OWNER` and `MISSING_ACTION` check only that a legacy Work Decision has an assigned person and a next action. Firing an alert on their absence asserts that every Utility Conflict on the project ought to have an internal owner and an open task. Many legitimately do not — a conflict whose resolution is agreed and whose next move belongs to the utility owner needs no internal task, and a project with three thousand records would generate three thousand findings that mean "nobody typed anything here". ADR-0010 already found what that produces: 141 records generating 141 `MISSING_OWNER`, 141 `MISSING_ACTION`, 141 `STALE` and 141 `ORPHAN` rows, a table where every row is identical and no row is actionable. Requiring an owner and a task for every conflict turns Corridor into the administrative system it exists to avoid, and it does it by way of an alert list nobody can read.

A Follow-up Plan still has to be complete and attributable when one exists — that is a command invariant on the write, enforced where the plan is recorded. It is not a project-wide alert about every record that has no plan.

`STALE` fires when no document has spoken to a record in fourteen days. The threshold is a real predicate in ADR-0010's sense — it defines a checkable fact — but the fact it defines is not the one the alert implies. Nobody failing to send a document is not evidence that anyone failed to respond, and the rule fires just as hard on a record that is correctly quiet. #425's rule is the defensible one: a no-response exists only after a retained outgoing request and an expected-response boundary, both of which are facts Corridor holds.

`ORPHAN`'s entire predicate is `dependency.milestone_id is null`. Whether a record needs a Key Date link is a project's own configuration question, and where a partner genuinely requires one, the answer is an explicit configured rule scoped to the records that need it — not the preservation of an ambiguous rule that fires on every record nobody happened to link.

Retiring these four does not touch ADR-0025. A Coordination Decision is still the attributable class that carries Assigned To, Next Action and Action Due Date, and a project that records them still gets them read back. What is retired is the assertion that their absence is a defect worth a project-wide alert.

## The declared check set advances to v2

The report's declared check set advances from `accepted_record_date_checks` version `v1` to **`accepted_record_checks_v2`**. The name loses "date" because the set is no longer only date checks once `MISSING_EVIDENCE` and `SUPERSEDED_CITATION` join it.

Each check keeps what the three existing ones already carry: rule identity, rule version, the input record identities it read, and the evaluation date. ADR-0010's discipline is unchanged — the checks are computed at read time, stored nowhere, presented by rule with each rule's own quantity, and never ranked against each other.

The version change has a second use, and it is the reason to make it deliberately rather than incidentally. #534 could not write a test that a **ruleset version change is reported as a rules change rather than as project movement**, because there was only one released version of the accepted-record check set to compare against. `changes.py` does exactly this for the legacy path and is the model. The v1 → v2 advance supplies the second released version that test needs, and #596 carries it.

## What this does not change for legacy projects

The released legacy ruleset in `src/corridor/exceptions.py` (`RULESET_VERSION = "v0.4"`) keeps computing all twelve rules over `dependencies` for a **legacy project**, unchanged, until those tables retire under ADR-0081 stage 6 (#458). This ADR does not delete a released rule from a running reader; it decides what the **accepted record's** check set contains. The two sets are allowed to differ, and the difference is declared rather than silent — which is the actual defect #596 opened with.

## Considered options

**Port all nine remaining rules to the spine.** Rejected. It treats the twelve as a specification to be reproduced rather than as twelve separate claims about a project. Four of them assert that an empty administrative field is a project problem, and reproducing those on the accepted record would carry the legacy task system across the very boundary ADR-0081 and ADR-0084 drew, at the moment Corridor is deciding what it is for.

**Ship the three date checks and declare the other nine out of scope.** Rejected. It is the status quo with a note attached. An adopted-baseline project's report would keep under-reporting `MISSING_EVIDENCE` and `SUPERSEDED_CITATION`, which are the two rules that speak to whether the accepted record stands on anything — the closest thing in the set to Corridor's actual subject.

**Let the implementer decide per rule, as #596 originally asked.** Rejected, and this ADR exists to close it. ADR-0010 preserved these rules explicitly; an implementation ticket cannot un-preserve them. The rules also encode a product boundary — whether Corridor demands owners and tasks — that is not an implementation question at all.

**Keep `MISSING_EVIDENCE` defined on the Source Passage Check.** Rejected under ADR-0082. It would tell a customer that a record is unsupported because a quotation could not be located, or supported because one could, and neither statement is true. #493 delivered the locator check precisely so that it could stop standing in for support.

**Keep `CONTRADICTION` as an alert beside the contradiction Proposed Delta.** Rejected. Two surfaces for one question, one of which cannot be acted on, and no rule for which one is authoritative when they disagree.

**Make `MISSING_OWNER`, `MISSING_ACTION`, `STALE` and `ORPHAN` configurable rather than retiring them.** Rejected as stated, with a narrow door left open. Preserving an ambiguous rule behind a default-off switch keeps the ambiguity and adds a setting; the rule still means "a field is empty" whether or not it is enabled. Where a partner genuinely requires a Key Date link, or a named owner for a defined class of records, the answer is a configured explicit rule scoped to those records, defined against their requirement — a different rule that happens to overlap, not this one revived.

**Edit ADR-0010 in place.** Rejected under `docs/adr/README.md`. Code complying with ADR-0010 — an engine computing all twelve preserved rules — would fail review under this wording, so this is a new sequential decision carrying reciprocal `amends` metadata rather than an edit to an accepted body.

## Consequences

- **ADR-0010's preservation clause is amended.** Its removal of the severity scalar, its group-by-rule presentation, its refusal of any total order across categories, and its threshold-versus-weight distinction are untouched and still govern. What changes is the sentence preserving all twelve rules and their predicates: on the accepted record, three are kept, two are ported (one with a corrected definition), three are superseded by surfaces that can be acted on, and four are retired.
- **ADR-0016 needs no amendment.** Its `SUPERSEDED_CITATION` predicate is carried over exactly, including its refusal of the "any old link exists" reading; only the relation it reads changes.
- **ADR-0017 and ADR-0082 supply the ported `MISSING_EVIDENCE`.** The role-scoped Supporting Documentation in Use resolver, as amended by ADR-0082 to consult support assessments and never treat locator validation as support, is the input. No new decision is needed for it.
- **ADR-0025 is untouched.** Coordination Decisions remain the attributable class for workflow state; only the alerts on their absence are retired.
- **`presentation.exception_name` changes for the ported rules.** The current `MISSING_EVIDENCE` label — "No supporting document passed the source passage check" — states the predicate this ADR removes, so it cannot be carried over. A replacement customer label follows the terminology-research procedure in `docs/agents/domain.md`, and it must not collide with the Source Passage Check wording #600 is settling separately. Labels for the retained three are unchanged.
- **#596 carries the implementation** and becomes `ready-for-agent` with this ADR recorded. It advances the declared set to `accepted_record_checks_v2`, ports the two rules, and writes the ruleset-version attribution test the v1 → v2 change now makes possible.
- **#596's matched-pair acceptance criterion is satisfied by declaration, not by equality.** An adopted-baseline project's report will not show the same alerts a legacy project's does, because four rules are retired and three are superseded. The difference is the table above, and it belongs in the report's own coverage statement rather than in a test that would otherwise be unsatisfiable.
- **#425 and #528 inherit three findings each with a place to act.** The contradiction question lands in #528's focused item; due and overdue follow-up land in #425's bundles and the report's Follow-up section. Neither is new scope for those issues; both already own the surface.
- **No new write to `dependencies`, `operative_support`, `work_decisions` or the dispute tables** is created by any of this, which is what made the porting question necessary in the first place.
