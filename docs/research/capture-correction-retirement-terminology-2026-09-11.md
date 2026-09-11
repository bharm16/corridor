# Terminology research: retiring a Proposed Delta whose capture was corrected

Prepared 2026-09-11 under the procedure in
[docs/agents/domain.md](../agents/domain.md#research-before-proposing-terminology),
for the named lifecycle relationship
[ADR-0101](../adr/0101-a-corrected-capture-retires-its-obsolete-proposed-delta-through-a-dedicated-append-only-relationship.md)
introduces. The relationship is initially internal, and the procedure covers it
anyway: a named lifecycle relationship in an ADR and in code is a domain
concept, not an ordinary software helper.

## The question

When Corridor misreads a source, corrects the capture, and the corrected value
turns out to match the accepted record, the Proposed Delta that existed only
because of the misreading has to stop being actionable. What is that
relationship called, and does published practice already name it?

Three things must survive the naming. The evidence is retained, not destroyed.
The customer's own decision history — including a decision made on this
proposal before the correction — is preserved. And the act is Corridor's
repair of its own reading, not the customer concluding anything.

## Method

Step 1, the existing records: the [Project Record glossary](../../CONTEXT.md),
the [Corridor Operations glossary](../operations/CONTEXT.md), ADR-0076,
ADR-0083, ADR-0084, ADR-0100, and the earlier terminology note
[baseline-delta-terminology-2026-09-01.md](baseline-delta-terminology-2026-09-01.md).

Step 2 and 3, primary sources. The field involved here is provenance and
record-keeping rather than highway utility coordination: the question is what
relates a corrected record entry to the obsolete one it displaces. The
recognized standards body is the W3C, and its PROV Recommendation is the
published vocabulary for exactly this. I opened the Recommendation itself
rather than a summary of it. The construction change-control pass was done in
the 2026-09-01 note above and is not repeated; its finding is applied below.

## Published meaning: W3C PROV

**Source.** *PROV-DM: The PROV Data Model*, W3C Recommendation, 30 April 2013,
<https://www.w3.org/TR/2013/REC-prov-dm-20130430/>. Read 2026-09-11. **VERIFIED**
— I opened the Recommendation and read both sections.

PROV names two distinct relations that are near this case, and keeps them
apart.

- **Revision** (§5.2.2) is a kind of derivation: the resulting entity is a
  revised version of an original, carrying substantial content from it. It
  relates a revised thing to the earlier thing it revises.
- **Invalidation** (§5.1.8) is the start of an entity's destruction, cessation
  or expiry by an activity. After it, the entity is, in the Recommendation's
  words, "no longer available for use".

Neither section says anything about retaining the earlier entity's evidence,
and neither says anything about a downstream decision or approval history
attached to the invalidated entity. PROV describes what happened to an entity;
it does not supply an application's rule about what must be kept when it
happens.

**Applicability.** Corridor's relationship has one foot in each and is neither.
The corrected *capture* is a PROV revision of the erroneous capture — a new
entity carrying the corrected content. The obsolete *Proposed Delta* is closer
to invalidation: it ends its availability for use. But Corridor requires two
things PROV does not express at all. The retired proposal keeps its complete
evidence and remains readable in history, which invalidation does not promise
and, read plainly, points away from. And the retirement must preserve any
customer decision already recorded against that proposal — an application rule
about authority, which PROV has no vocabulary for.

So PROV supplies the distinction (a revised thing is not an ended thing) and
does not supply the concept.

## Published meaning: construction change control

From [baseline-delta-terminology-2026-09-01.md](baseline-delta-terminology-2026-09-01.md),
which did this pass against PMI's *Lexicon*, AIA A201, ConsensusDocs 200 and
ISO 10007: published change control names the *proposal* stage (change
request, change proposal), the *approved modification* (change order), and the
act of *dispositioning* or approving/rejecting a change request. "Delta" has no
standard construction meaning.

**Applicability: none of those is this act.** Every published term names a
decision about the merit of a proposed change. Withdrawing a proposal because
the proposal itself was computed from a misreading of the source, while the
underlying question was never decided, is not dispositioning it. Published
practice does not appear to have a term for it, because published practice
does not have a machine that proposes changes by reading documents.

This is the missing counterpart the procedure's step 6 asks to be stated
explicitly: **the sources do not provide an exact equivalent, and one is not
invented from them.**

## Corridor's use, and what it is not

**Proposed name.** `DeltaCaptureCorrection`, retained as the proposed internal
implementation name from the maintainer's decision text. It is documented as a
**Corridor-specific relationship — not an established construction term and
not a verbatim PROV concept.** Nothing in this note licenses printing it to a
customer, and it is not adopted into the Project Record glossary.

**What it asserts.** This particular Proposed Delta no longer represents an
actionable comparison, because the particular capture on which it depended was
corrected.

The precise distinctions, each against a concept Corridor already has:

| Nearby concept | Where defined | How this differs |
|---|---|---|
| **Document Revision** | [Operations glossary](../operations/CONTEXT.md) | A new issued version of the source document. Here the document and its bytes are unchanged; only Corridor's reading of them was wrong. |
| **Supersession** | [Project Record glossary](../../CONTEXT.md), ADR-0083 | Identifies the Document Revision, or the newer source version, that replaces an earlier one for current use. Here there is no newer version and often no successor at all. |
| **Resolve Delta `reject` / Keep current** | CONTEXT.md, ADR-0084 | A named person's conclusion that the accepted value stands. Here nobody concluded anything; the comparison lost its basis. |
| **Defer** | CONTEXT.md, ADR-0084 | Scheduling that leaves the proposal open to return. This is terminal, and returns nothing. |
| **Deletion or disposition** | ADR-0032, ADR-0080 | Removing data. Here nothing is removed: the request, the original capture, the corrected capture, the comparison, any deferral receipt and any Follow-up history are all retained and readable. |

**Customer label.** None is adopted. ADR-0101 approves three plain sentences
that describe what happened without naming a type — "Corridor corrected its
reading of this source", and what followed from it. That is description, not
vocabulary, and it is the same treatment ADR-0100 gave "Report an extraction
error". Should any surface later want a defined customer-facing *type* for this
outcome, this note is the starting point and the research is completed then,
not assumed now.

## Gaps recorded

- No primary source consulted here names the act of withdrawing an
  automatically generated proposal because the generator misread its input.
  The gap is stated rather than filled with a borrowed word.
- PROV's *invalidation* is the closest published relation and is deliberately
  not adopted as the name, because Corridor's retention and decision-history
  rules are the opposite of what "no longer available" suggests to a reader.
- The [Project Record glossary](../../CONTEXT.md) entry for **Proposed Delta**
  still reads that a delta remains open "until accepted, edited, or rejected,
  or until a newer occurrence supersedes it". ADR-0101 adds a third exit, so
  that sentence is now incomplete. It is a customer-facing definition and is
  left for the maintainer rather than edited here.
- Whether the internal name stays `DeltaCaptureCorrection` in code is an
  implementation choice for #836 and #842; this note fixes its meaning and its
  boundaries, not its spelling.
