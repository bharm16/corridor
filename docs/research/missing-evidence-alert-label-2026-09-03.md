# Customer label for the ported `MISSING_EVIDENCE` alert

**Date:** 2026-09-03. **Question raised by:** [#596](https://github.com/bharm16/corridor/issues/596), implementing [ADR-0090](../adr/0090-the-accepted-record-has-its-own-alerts-not-the-legacy-task-systems.md), which ports `MISSING_EVIDENCE` to the accepted record's check set with a corrected predicate and records that its current label "states the predicate this ADR removes, so it cannot be carried over". **Procedure:** [Research before proposing terminology](../agents/domain.md#research-before-proposing-terminology).

## 1. The concept, and the adopted terms that already cover most of it

The rule's retained identifier is `MISSING_EVIDENCE`. Its **legacy** predicate — still computed unchanged over `dependencies` for a legacy project under `RULESET_VERSION = "v0.4"` — is that no supporting document on a Constraint had its cited passage found in its source. Its customer label in `src/corridor/presentation.py` says exactly that: *"No supporting document passed the source passage check."*

ADR-0090 ports the rule to the accepted record with a different predicate:

> no effective Supporting Documentation is currently in use for the accepted proposition under the applicable support requirement.

Concretely, on the spine the check fires when the accepted value has no effective **Support Assessment** with an outcome of `supported` or `partially_supported` naming any Source Segment (`SUPPORTING_OUTCOMES = ("supported", "partially_supported")` in `issue_rendering.py`). It reads the Support Assessment relation established by #530 and the accepted support designation, and it never infers support from locator validation — the boundary [ADR-0082](../adr/0082-provenance-follows-the-value-class-and-support-is-a-relation.md) drew and [ADR-0017](../adr/0017-operative-support-is-role-scoped-and-resolved-in-one-place.md) restates as the role-scoped Supporting Documentation in Use resolver, which never treats locator validation as support.

So the label has to say **absence of a current designation**, and must not say anything about a passage being locatable.

Three adopted terms already carry most of that meaning, and the procedure's step 1 says reusing an accepted term with its unchanged meaning needs no fresh naming exercise:

| Adopted term ([Project Record glossary](../../CONTEXT.md)) | Customer wording already adopted | What it contributes here |
|---|---|---|
| **Supporting Documentation** | *Supporting documents* | The object that is absent. Defined as the Source Segments "assessed as supporting or partially supporting a recorded proposition for a stated role" — i.e. the object is constituted by the assessment, which is exactly the ported predicate's input. |
| **Supporting Documentation in Use** | *Used for this value* or *Used for this review* | The currency and the role scoping. "The Source Segments currently designated, by purpose, for a published value or Documentation Review." |
| **Support Assessment** | *(internal concept — no customer word)* | The mechanism the rule reads. Its glossary entry explicitly lists "Source Passage Check" under *Avoid*, and it must not surface as a customer word. |

What is genuinely missing is a name for the **state** of a value that has none of it — the same gap the [#600 note](source-passage-check-state-labels-2026-09-03.md) found for the Source Passage Check's outcomes.

## 2. What the primary sources supply

### 2.1 Federal-aid construction records: the requirement exists; the negative state has no name

**23 CFR 635.123, "Determination and documentation of pay quantities"** ([govinfo, Title 23 CFR, revised as of April 1, 2025](https://www.govinfo.gov/content/pkg/CFR-2025-title23-vol1/pdf/CFR-2025-title23-vol1-sec635-123.pdf); source note `[56 FR 37004, Aug. 2, 1991, as amended at 85 FR 7293, Nov. 16, 2020]`). Paragraph (a) is the closest regulatory statement found to "a recorded value must carry the source document it rests on":

> "All such determinations and all related source documents upon which payment is based shall be made a matter of record."

Paragraph (b) adds that "Initial source documents pertaining to the determination of pay quantities" are among the records that must be retained. The section names the obligation and the object ("source documents"). It names **no state, status, or term for a determination whose source document is absent**.

FHWA's own guidance on that section says the same and stops in the same place. The [FHWA Construction Program Guide, "Source Documentation and Determination of Pay Quantities"](https://www.fhwa.dot.gov/construction/cqit/source.cfm) (updated 03/21/2023) states the policy — agencies must have procedures giving "adequate assurance that the quantities of completed work are determined accurately and on a uniform basis" — and cites 23 CFR 635.123 and 2 CFR 200 as its authority. It names no term for an undocumented quantity. [FHWA's "Additional Guidance on 23 CFR 635 A"](https://www.fhwa.dot.gov/construction/contracts/0635asup.cfm) goes one step further and quantifies *sufficiency* — haul tickets "should be validated both at the point of loading and at the point of delivery", and "a lesser amount of documentation may be permitted for miscellaneous material items and small quantities" — but sufficiency thresholds are still not a name for the empty case.

### 2.2 Federal-aid utility relocation: support is a condition on eligibility, not a status on a record

**23 CFR 645.117(b), "Direct labor costs"** ([govinfo, Title 23 CFR, revised as of April 1, 2025](https://www.govinfo.gov/content/pkg/CFR-2025-title23-vol1/pdf/CFR-2025-title23-vol1-sec645-117.pdf)):

> Salaries, wages and related expenses "are reimbursable when supported by adequate records."

and at (b)(2), overhead-organization salaries "may be reimbursed for the time worked directly on the project when supported by adequate records."

This is the domain's own phrase for our concept — **"supported by adequate records"** — and it is written as a *positive* condition on eligibility. The regulation never names the failing side. A cost that is not supported by adequate records is simply not reimbursable; there is no noun for its condition.

**TxDOT ROW Utilities Manual, Chapter 2 Section 2, "Right of Way Utility Adjustment Sub-process"** ([txdot.gov](https://www.txdot.gov/manuals/row/utl/chapter-2--txdot-utility-cooperative-management-pr/section-2--right-of-way-utility-adjustment-sub-pro.html)) uses "supporting documentation" as an ordinary requirement on a transmittal, not as a designation on a value:

> "All payment requests shall be accompanied by supporting documentation."

and, on the assembly, "Utility billings should be prepared and submitted in a format that is compatible with the approved estimate and in sufficient detail for analysis and documentation." Note the shape: documentation *accompanies a request*. Nothing in the manual attaches a per-value support designation to a stored record, and nothing names the absence of one.

### 2.3 The Utility Conflict Matrix itself has no support concept at all

This is the most directly on-point negative finding, because the UCM is the artifact Corridor's accepted record most resembles.

The **SHRP2 R15B standalone Utility Conflict List** ([TRB, SHRP2 R15B training materials, Lesson 3 — Utility Conflict Matrix](https://onlinepubs.trb.org/onlinepubs/shrp2/R15BTrainingMaterials/Lesson3UtilityConflictMatrix.pdf); the sample sheet in it is headed "Sample DOT", developed 3/28/2011, reviewed 4/28/2011) defines these columns and no others:

> Utility Owner and/or Contact Name · Conflict ID · Drawing or Sheet No. · Utility Type · Size and/or Material · Utility Conflict Description · Start Station · End Station · Start Offset · End Offset · Utility Investigation Level Needed · Test Hole · Recommended Action or Resolution · Estimated Resolution Date · Resolution Status

There is **no column for a source document, a citation, a supporting record, or a documentation status**. The one thing resembling confidence is "Utility Investigation Level Needed" (`QLA`, `QLC` in the sample), which is the ASCE utility-quality-level scale describing how a *physical location* was determined — not whether a recorded value carries documentary support.

The companion [SHRP2 R15B "Utility Conflict Management (UCM) Introduction"](https://shrp2.transportation.org/Documents/R15B_Generalized_UCM_Stages_and_Activities.pdf) describes the fuller data model, and it does have documents — but as a peer entity group, not as a per-value designation:

> "the database enables the management of six groups of data that are related to utility conflict information: utility conflict, utility facility, utility agreement, document, project, and user."

Documents relate to conflicts. Nothing in the described model records *which document backs which value*, so nothing in it can name the state where none does. The same document notes the standalone list is designed "for exchange of information between a DOT and utility owners" with columns "most commonly used by most state DOTs around the country" — i.e. this is the domain's consensus column set, and support is not in it.

### 2.4 The one place a real name exists — and why it must not be borrowed

Federal grant-audit practice does have a defined term, and it is the nearest counterpart anywhere in the search:

**2 CFR 200.1, "Questioned cost"** ([govinfo, 2 CFR Ch. II, 1-1-23 Edition, § 200.1](https://www.govinfo.gov/content/pkg/CFR-2023-title2-vol1/pdf/CFR-2023-title2-vol1-sec200-1.pdf)) — a cost questioned by the auditor because of an audit finding:

> "(2) Where the costs, at the time of the audit, are not supported by adequate documentation".

(The 1-1-25 edition renumbers this definition; the page carrying (4)–(6) was fetched and the page carrying (1)–(3) was not retrievable, so the verbatim clause above is quoted from the edition actually read, the 1-1-23 one.)

And the adjectival form is in live federal-highway use: **DOT OIG report ST2019053, "Inadequate Data and Guidance Hinder FHWA Force Account Oversight," May 29, 2019** ([oig.dot.gov](https://www.oig.dot.gov/sites/default/files/FHWA%20Force%20Account%20Final%20Report%5E5-29-19.pdf)) carries a table headed "Unsupported Costs in Force Account-Funded Projects Reviewed by OIG" and recommends action on "the 18 projects related to the $22.3 million in unsupported costs." The report uses the word throughout without defining it.

So a term does exist — **unsupported** — but note precisely what it names in both sources: an amount, in an audit finding, whose consequence is recovery of funds. It is an accusation about money, made by an auditor, after a review. It is not a status on a record, it does not describe an ordinary pre-designation state, and it is not utility-coordination vocabulary. Section 3 rejects it for exactly those reasons.

### 2.5 What could not be fetched, stated plainly

- **eCFR** (`ecfr.gov`) returned a 302 redirect to an interstitial (`unblock.federalregister.gov`) on every attempt, so no eCFR page was read. Every CFR quotation above comes from a govinfo annual-edition PDF whose edition date is given.
- **AASHTO, *A Guide for Accommodating Utilities Within Highway Right-of-Way*, October 2005** ([store.transportation.org](https://store.transportation.org/Common/DownloadContentFiles?id=576)): only the free front matter and table of contents were retrievable. Its contents are entirely physical accommodation (clearances, cover, encasements, overhead facilities); it has no records or documentation chapter. Its glossary was not read, so no claim is made that it contains no counterpart — only that its structure gives no reason to expect one.
- **WSDOT Utilities Manual M 22-87** returned HTTP 403 and was not read.
- The **TxDOT Construction Contract Administration Manual** project-records pages (`onlinemanuals.txdot.gov`, and the current `txdot.gov/manuals/cst/cah/...` path) returned a connection refusal and a 404 respectively. The CCA manual's documentation requirements are therefore represented here only through the federal rule it implements, 23 CFR 635.123.

**Conclusion for procedure step 6: there is no industry counterpart to adopt.** The domain has a phrase for the positive condition — "supported by adequate records" (23 CFR 645.117(b)), "supporting documentation" (TxDOT ROW Utilities Manual) — and audit practice has a name for the accusatory negative — "unsupported" (2 CFR 200.1, DOT OIG). Neither is a status a coordination record can carry, and the reference Utility Conflict Matrix has no support concept at all. The label must be scoped plain-language product wording, like the Source Passage Check states before it.

## 3. Proposal

| Retained rule identifier (unchanged) | Customer label on the accepted record |
|---|---|
| `MISSING_EVIDENCE` | **No supporting document in use for this value** |

Full presentation, beside the field it is about, in the Constraint alerts section under the declared check set `accepted_record_checks_v2`.

**Why it holds against each test:**

- **It states the new predicate exactly.** "Supporting document" is the adopted customer wording for Supporting Documentation, whose glossary definition is already constituted by the Support Assessment. "In use" is the adopted wording for Supporting Documentation in Use, carrying both the currency (*currently* designated, not ever cited) and the fact that a lapsed designation counts as absent. "For this value" is the adopted customer label *Used for this value* turned around. Every content word is an adopted term at its unchanged meaning; nothing is coined.
- **It is not a verdict.** It reports what the record contains, not what is true about the world. Nothing in it says the value is wrong, that anyone failed, or that a document was examined and found wanting. This is the specific failure that sank "Passed"/"Failed" in #600, and the phrasing sidesteps it by naming an absence rather than an outcome.
- **It cannot collide with the Source Passage Check.** #600's three labels are all built on *cited location* — Found at cited location, Not found at cited location, No cited location recorded. This label shares no content word with any of them and never mentions a location, a passage, or a citation. A reader cannot confuse "no supporting document is in use" with "the passage was not where the citation said."
- **It matches the register of the labels beside it.** The retained alert labels are terse noun phrases — "No person assigned", "No key date linked", "No exact promised date for this check", "Supporting document replaced". "No supporting document in use for this value" is the same shape, and the singular deliberately matches its sibling `SUPERSEDED_CITATION` ("Supporting document replaced"). The singular also states the threshold correctly: one is enough to clear the check, so the alert fires when there is not even one.

### Alternatives considered, and why each loses

**"No supporting documents used for this value."** The closest to the adopted plural customer label. It loses on tense and number. Past-tense "used" reads as a claim about history — *none was ever used* — when the predicate is about the current designation, and a value whose support lapsed under supersession has certainly had documents used for it. "In use" is the tense the glossary itself chose ("Supporting Documentation **in Use**") and the tense ADR-0016 depends on when it insists the predicate is Supporting Documentation in Use, never "any old link exists". The plural also weakens the threshold statement: it invites the reading "not enough documents", when the rule fires only at zero.

**"Unsupported value" (or "Not supported by documentation").** The wording nearest to real primary-source language — 2 CFR 200.1's "not supported by adequate documentation" and DOT OIG's "unsupported costs". It loses on the exact ground #600 rejected "Failed". In both sources "unsupported" is an auditor's adverse finding about money whose consequence is recovery of funds; it reads as *this value is unfounded*, or *someone did something wrong*. Corridor's predicate is very often satisfied simply because nobody has recorded the assessment yet, which is an ordinary state of an adopted baseline, not a defect in the value. It has a second, sharper problem: it would make the empty case indistinguishable from a real adverse reading. A Support Assessment can record `contradicted` or `unclear` — a named person's conclusion that the source cuts against the proposition — and "unsupported value" would describe both that and the case where nothing has been assessed at all. The two are different facts with different next steps, and #530 was built to keep them apart.

**"Documentation requirement not met."** It loses because it collides head-on with **Documentation Review**, an adopted glossary term whose definition is a named person's judgment of whether the exact current supporting passages satisfy the stated documentation requirement. [ADR-0017](../adr/0017-operative-support-is-role-scoped-and-resolved-in-one-place.md) is explicit that these are different facts: "Supporting Documentation can meet a stated documentation requirement or support a value being published; those are different facts. A record can have a Coordination Report citation while its documentation requirement remains unmet." The ported rule reads the value-support designation, not the review judgment, so this wording would assert on the report that a named person reached a conclusion nobody reached.

**"Missing evidence."** Rejected on two counts. "Evidence" was retired as a customer word by [ADR-0048](../adr/0048-complete-glossary-adoption-preserves-record-and-source-identity.md), and `presentation.py` already maps `evidence` → "Supporting documents" for exactly that reason. And promoting the internal rule code to the customer label is the thing the procedure forbids in its closing paragraph: "Internal technical names must remain visibly internal rather than becoming customer concepts without the same research."

### Concrete example

A 30-inch water main crosses the proposed alignment at station 37+00 and is Conflict C43 on the project's utility conflict matrix. Its accepted **Promised for** date is March 14. The utility owner's relocation letter is registered, revision B, and page 2 is cited beside the date.

The Source Passage Check reads **Found at cited location**: the stored sentence is on page 2 of the registered file, exactly where the citation says. That is all it means. If nobody has yet recorded a Support Assessment that judges the passage to support the date — or if the only assessment on file gives the passage the `attribution` role, establishing *who* stated the date rather than that the date holds — then the accepted record also reads:

> **No supporting document in use for this value**

Both statements are true at once, and the report shows both. A document is attached, its quotation is exactly where it should be, and no supporting documentation is in use for the date. That pair is not a contradiction; it is the whole point of ADR-0082's split, made visible.

The alert clears the moment a named person (or a released policy citing the assessment it relied on) records an assessment of `supported` or `partially_supported` naming that segment. The letter does not change, the citation does not change, the March 14 date does not change — the record gains a designation it did not have.

The reverse case is just as clean. If the sentence turns out to be on page 3 rather than the cited page 2, the Source Passage Check reads **Not found at cited location** while the support alert may fire or not, because the two are computed from different inputs and neither implies the other.

## 4. Boundaries preserved

The label must never be read as, and nothing in the product may present it as, any of these:

- **A verdict on the value.** It does not say the accepted value is wrong, doubtful, or unreliable. It says the record does not currently designate documentation for it. This is the boundary #600 established for the Source Passage Check states, applied to the other half of ADR-0082's split.
- **A statement about a passage or a citation.** It is not "the quotation could not be located". That finding is the Source Passage Check, with its own three labels (#600), computed by replaying a typed locator. The two findings share no input, and a value can carry either, both, or neither.
- **A Documentation Review judgment.** It is not "the documents do not meet this requirement" and not the negation of *Documents marked sufficient*. No named person has judged anything when this alert fires; that is what it reports.
- **Proof that physical work is unfinished.** `docs/agents/domain.md` states the rule directly: explain a missing supporting record as missing support, not proof that the physical work is unfinished. A relocation can be complete on the ground and carry this alert, and a relocation can carry no alert and not be complete.
- **A claim that the project holds no relevant document.** The predicate is about the *current designation for this value*, not about the corpus. Documents may be attached, registered, quoted, and locatable while this alert stands.
- **The same finding as a contradicted source.** A value whose only effective assessment reads `contradicted` or `unclear` does fire this alert, because nothing supporting is in use — but the contradiction is a separate finding with its own surface: a source that disagrees with the accepted record is incoming evidence raised as a contradiction Proposed Delta and #528's focused question (ADR-0090). This label must never be used to report that disagreement.
- **A demand.** ADR-0090's central line is that Corridor must not recreate the legacy task system; four rules were retired precisely because they asserted that an empty administrative field is a project defect. This one survives because it speaks to whether the accepted record stands on anything — but its label states an absence, not an instruction, and it must not acquire imperative wording ("needs supporting documentation", "documentation required") in any surface.

Unchanged by this proposal: the retained rule identifier `MISSING_EVIDENCE`; the released legacy ruleset in `src/corridor/exceptions.py` (`RULESET_VERSION = "v0.4"`), which keeps computing the legacy predicate over `dependencies` for a legacy project until ADR-0081 stage 6 retires those tables; and the legacy label "No supporting document passed the source passage check", which stays exactly as it is because it still describes the legacy predicate under it. The two check sets are allowed to differ and the report declares the set it ran. The new wording lives only in the accepted record's set.

## 5. Open item for the maintainer

Procedure step 6 requires that where the sources supply no exact equivalent, the proposed product wording be stated as such and carry **explicit user agreement** before it enters the canonical vocabulary. Section 2 reached that outcome: the domain has a phrase for the positive condition and audit practice has an accusatory name for the negative, and neither is adoptable here.

**Status: proposed, pending explicit maintainer agreement**, following the [#600 precedent](source-passage-check-state-labels-2026-09-03.md#5-open-item-for-the-maintainer-settled-2026-09-03) — where a placeholder was recorded, then rejected and replaced by the maintainer's own words on the same day. The label below is therefore scoped plain-language product wording, not an adopted industry term, and it is presentation-only:

> `MISSING_EVIDENCE` on the accepted record → **No supporting document in use for this value**

If the maintainer wants different customer words, the constraints any replacement must satisfy are the four tests in section 3 and the seven boundaries in section 4 — in particular, it must not read as a verdict on the value (the ground on which "Passed"/"Failed" and "Unsupported value" both fail), and it must share no content word with #600's *cited location* family.

No successor ADR is proposed. ADR-0090 already decided the port and its predicate and named this label change as its own consequence; the procedure permits a wording correction, once the missing-counterpart decision is explicitly approved, provided it alters neither authority nor lifecycle. This alters neither: the stored identifier, the ruleset version mechanics, the Support Assessment relation, and the boundary against reading support out of locator validation are all untouched. If the wording is approved, step 7 updates the [Project Record glossary](../../CONTEXT.md) entry for Supporting Documentation in Use with the alert's wording as a cross-reference, and nothing else.
