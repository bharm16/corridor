# Terminology research: Source Fact, Adopt Baseline, Proposed Delta, Resolve Delta

Prepared 2026-09-01 under the procedure in
[docs/agents/domain.md](../agents/domain.md#research-before-proposing-terminology)
for the four terms ADR-0076 introduced without glossary entries. Each entry
records what published practice means by the nearest term, where Corridor's use
matches it, and where Corridor's use is product-specific.

## Baseline and change control

**Published meaning.** The PMI *Lexicon of Project Management Terms* and the
PMBOK Guide define a *baseline* as the approved version of a work product,
changed only through formal change control, used as the basis for comparison
(paraphrased; PMI lexicon at https://www.pmi.org/pmbok-guide-standards/lexicon).
*Change control* there is the process by which modifications to documents,
deliverables, or baselines are identified, documented, approved, or rejected.
ISO 10007:2017 (configuration management guidelines) uses *configuration
baseline* for approved product configuration information that serves as the
reference for change control, with changes evaluated and dispositioned before
the baseline moves (https://www.iso.org/standard/70400.html). In utility
coordination, the SHRP2 R15B Utility Conflict Matrix is the working record of
conflicts and their resolution, maintained across project phases (FHWA/AASHTO
SHRP2 R15B materials; see [ADR-0066](../adr/0066-the-machine-keeps-the-record-people-do-coordination.md)
for the citations gathered on 2026-08-30).

**Corridor's use.** *Adopt Baseline* is the named person's approval of one
exact UCM workbook or system export as the accepted Project Record. This is the
PMI and ISO sense of establishing a baseline under change control. The
product-specific part is that the baseline is adopted from the customer's
existing artifact rather than authored, and that adoption binds a byte digest.

**Customer label.** "Adopt baseline" or "Adopt this matrix as the starting
record". *Avoid*: "import" alone (imports capture; they do not adopt), "approve
the project", "sign-off".

## Proposed change, change request, revision

**Published meaning.** PMI's *change request* is a formal proposal to modify a
document, deliverable, or baseline. Construction contracts (AIA A201, ConsensusDocs
200) use *change order* for an approved contract modification and *change
proposal* or *request for change* for the unapproved stage. "Delta" has no
standard meaning in construction or coordination practice; in engineering
usage it is informal for "difference".

**Corridor's use.** A *Proposed Delta* is the typed difference between a
captured source fact and the current accepted record, awaiting a decision. It
is the "proposed, not yet approved" stage of change control. Corridor does not
use *change request* because the customer's own contract vocabulary reserves
it for contract modifications, and does not use *change order* for the same
reason. *Delta* is retained as the product-internal identifier; the customer
label is "proposed change".

**Customer label.** "Proposed change" (with the type: new conflict, field
changed, promise moved, and so on). *Avoid*: change order, change request,
contract modification, an automatic update.

**Resolve Delta** is the decision that closes a proposed change: accept, edit,
reject, or defer. Published practice calls this *dispositioning* a change
(ISO 10007) or *approving/rejecting* a change request (PMI). Customer label:
"Decide this change". *Avoid*: "approve" alone (an edit is also a decision),
"close" (a deferred delta is resolved for now and reopens on new evidence).

## Source fact, assertion, source field value

**Published meaning.** Records practice distinguishes what a source states from
what the record concludes; Corridor already carries this as *Assertion*
(customer label *Source field value*, ADR-0001). Configuration management
calls the incoming information a *change* or *proposed configuration
information*; there is no standard term for "a value a source states, captured
before any decision".

**Corridor's use.** A *Source Fact* is what an incoming source says: the typed
value, its Source Segment, the mapping that produced it, and the identity it
resolves to. It generalizes Assertion (which is field-shaped and legacy-keyed)
to every typed fact on the spine (ADR-0067). Assertion remains the retained
internal name for the legacy field carrier and is not renamed (ADR-0048
adoption boundary).

**Customer label.** "What the source says". *Avoid*: the project's conclusion,
a verified physical fact, an accepted value.

## Gaps recorded

- No primary source uses "delta" as a coordination term; it is product-specific
  and is confined to identifiers and internal prose.
- The consultant-first buyer statement in ADR-0075 is a hypothesis pending
  #428 and does not affect these terms.
- Source-passage validation versus semantic support (ADR-0082) is not a
  terminology question and is not covered here.
