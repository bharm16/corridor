# Corridor Operations

Corridor Operations turns registered Documents into reproducible extraction output and maintains supporting sources without asking customer users to operate technical machinery. Project decisions and their construction meaning belong to the [Project Record](../../CONTEXT.md).

Two routes exist. The **legacy route** produces Extracted Proposals that enter the Project Record through Record Inclusion; it is frozen against new capability ([ADR-0081](../adr/0081-the-spine-is-the-target-model-and-legacy-tables-retire-by-staged-exit-criteria.md)). The **adopted-baseline route** captures Source Segments and Source Facts and compares them with the accepted record to produce Proposed Deltas, which only a person or a narrow released policy resolves ([ADR-0076](../adr/0076-the-record-changes-by-captured-fact-adopted-baseline-and-resolved-delta.md)).

## Language

[ADR-0048](../adr/0048-complete-glossary-adoption-preserves-record-and-source-identity.md) records the adopted language and compatibility boundaries. Processing terms describe exact inputs, permitted changes, and retained outcomes; they do not confer construction authority.

### Sources and extraction

**Document**:
Source material registered with an identity, type, date, and known renditions; a spreadsheet and its PDF can be renditions of one Document rather than independent sources.
_Customer label_: Source document
_Avoid_: every uploaded file as an independent source, every Document as Supporting Documentation

**Document Revision**:
An identifiable issued version of a Document, with its replacement relationship recorded separately.
_Avoid_: a different file format, a newer upload automatically replacing an earlier source

**Document Rendition**:
A particular file representation of the same Document Revision, such as its issued spreadsheet or PDF representation, with its exact file identity retained.
_Customer label_: File, with Format shown separately
_Avoid_: format metadata alone, automatically an independent source or newer revision, unproven equivalence

**Source Segment**:
One addressable piece of a source, holding its exact text or value once, a digest of those bytes, and a typed locator identifying where it sits in a stated Document Revision — page and span, or sheet, row, and cell ([ADR-0068](../adr/0068-a-source-segment-stores-its-exact-text-once.md)). A Source Fact references its segments rather than restating their text, and a Support Assessment relates a proposition to them.
_Customer label_: The exact wording, shown at its place in the source
_Avoid_: a paraphrase, a page number without the text, one segment per document, a semantic judgment about what the passage supports

**Extraction Run**:
One recorded attempt to extract information from one exact Document using an identified extractor configuration, with its input, configuration, outcome, and output identity preserved.
_Customer label_: Document processing attempt, in technical history
_Avoid_: proof of success or production eligibility, replacing one attempt with another

**Current Production Run**:
The explicitly selected, eligible production Extraction Run whose outputs are used for a Document's current work, with the selection attributable.
_Avoid_: latest attempt, still executing, a test or failed attempt selected by timestamp

**Extracted Proposal**:
One source-cited extraction result proposed for inclusion as a Constraint or External Party Statement, preserved with its handling outcome even after recording or exclusion.
_Customer label_: Proposed constraint or Proposed statement, with the actual handling outcome
_Avoid_: an accepted fact, every proposal still pending review

**Source Passage Check**:
A check that a Cited Passage is present in the identified source page or row, under the stated matching method and its limits. It says nothing about what the passage supports; that is the Support Assessment ([ADR-0082](../adr/0082-provenance-follows-the-value-class-and-support-is-a-relation.md)).
_Avoid_: Documentation Review, physical inspection, proof the statement is true, Support Assessment

### Record decisions and outcomes

**Record Inclusion**:
Recording the supported fact from an Extracted Proposal in the Project Record through a person's permitted decision or an exact deterministic rule, while retaining the proposal and handling history.
_Customer label_: Added to Project Record or Recorded by exact rule
_Avoid_: extraction alone, a policy decision without the record change, universal human approval

**Human Record Decision**:
An attributable person's permitted decision on an unresolved Extracted Proposal, a Source Discrepancy, or removal of an incorrect entry from current work.
_Customer label_: Add record, Record conclusion, or Remove incorrect entry from active log
_Avoid_: contractual adjudication, unrestricted authority, every decision as Documentation Review

**Abstention**:
A completed policy assessment that cannot establish that one exact record change is permitted, so that change is not made and its reason is retained.
_Customer label_: Not applied automatically, with the specific reason
_Avoid_: rejection of the underlying statement, Processing Failure, a hidden timeout or exhausted budget

**Processing Failure**:
An attempt that did not complete the required processing contract, including transport failure, exhausted resource budget, or invalid output, with its failure record preserved.
_Avoid_: successful Abstention, successful processing because the Project Record did not change

**Statement Needing Clarification**:
An extracted External Party Statement whose attribution, timing, or affected Constraints still needs a permitted human decision before the relevant fact can be recorded.
_Customer label_: Statement to review, naming the missing fact
_Avoid_: missing geographic location, an already recorded statement with accepted unknown Applies To links

**Statement Review Assistant**:
A bounded, read-only assistant that gathers supported options for one unresolved statement without authority to make the project decision.
_Avoid_: reviewer of record, autonomous record writer, unsupported options as established facts

### Document revision work

**Revision Comparison**:
A preserved comparison of exact Extraction Runs for a predecessor Document and its registered successor, retaining matched rows, differences, missing rows, and uncertain correspondences.
_Customer label_: Compare document revisions
_Avoid_: proof a row disappeared after failed extraction, today's worklist, implicit record changes

**Document Revision Review**:
The current work needed when recorded conclusions still rely on a replaced Document or processing its replacement has not finished.
_Customer label_: Newer document needs attention, with the specific missing step
_Avoid_: approval of the replacement, technical failure presented as awaiting review

**Human Support Update**:
A person's attributable replacement of the supporting source for an already accepted conclusion, without changing that conclusion or originating a Documentation Review judgment.
_Customer label_: Confirm replacement supporting document
_Avoid_: document reapproval, a changed conclusion, confirmation of field inspection

**Automatic Support Update**:
A deterministic rule's update of an existing supporting-source link after proving an exact, unique, unchanged replacement, preserving the established conclusion and human judgment.
_Customer label_: Supporting document updated; recorded conclusion unchanged
_Avoid_: new facts, guessed equivalence, expanded Applies To links, a new Documentation Review judgment

**Automatic Support Update Rules**:
The named, versioned rules released by Corridor that determine whether an Automatic Support Update is permitted, with their identity and refusal reasons preserved.
_Avoid_: customer authorization, blanket approval, a reviewer bot

**Support Update Run Record**:
The immutable record of one support-update rule execution, including exact inputs, rule version, and every applied or abstained outcome; a later attempt creates another record.
_Customer label_: Automatic update history, with applied and not-applied results
_Avoid_: only successful updates, a transient log, a Processing Failure recast as Abstention

**Document Revision Processing**:
The ordered operation that verifies the preserved Revision Comparison between exact document runs before applying Automatic Support Update Rules.
_Customer label_: Processing newer document, or the specific unresolved consequence
_Avoid_: comparison write-back, implicit approval, a completed comparison meaning all revision work is complete

### Measurement and bounded testing

**Extraction Measurement**:
A scored comparison of exact extraction outputs with an identified Reference Dataset, including its coverage, origin, shared dependencies, and limits.
_Avoid_: Constraint Check, independent completeness from machine agreement, unqualified recall, an industry benchmark

**Reference Dataset**:
The identified comparison data used for an Extraction Measurement, with its origin, coverage, and whether expected values were independently established.
_Avoid_: a machine-produced reference called independent truth, an assumed human gold standard

**Rehearsal Input Manifest**:
The immutable membership of one bounded rehearsal, with the selection rule, source identities, and digest fixing which items bound its permitted actions.
_Customer label_: Cases included in this test, in technical history
_Avoid_: a saved query with changing membership, a convenient filter without an authority boundary

**Processing Scope**:
The explicit set of items and permitted operations for one bounded processing or review path; the offered items and allowed record changes obey the same scope.
_Avoid_: a second customer queue, a display filter, permission inferred from a tab

**Product Test Run**:
One bounded exercise from raw Documents to a published report through the actual application, with a stated simulated practitioner, exact inputs, retained success or failure record, restored starting state, and the required frontend and two-pass checks.
_Avoid_: construction acceptance testing, proof of real practitioner performance, a failed gate called a pass
