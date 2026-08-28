---
status: accepted
---

# Supersession is registry metadata — declared, never inferred, and not an Assertion

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

M8 needs `documents.superseded_by` populated. TxDOT's RID index records each revision's "Replaced on" date; the manifest today transcribes those supersession facts only partially, as free-text `notes`. The chain now enters as structured manifest fields and ingest persists it as registry metadata, exactly the way `doc_date`, `doc_type` and `sealed` already enter: the manifest declares, ingest records, the authority document remains the human-checkable source.

Four requirements keep the declaration honest. The chain references stable registered-document identifiers, never filenames. Ingest validates it — a missing target, a self-supersession, a cycle, or a cross-project edge is a registration error, not data. The manifest entry carries a structured pointer to a **registered authority document and an exact page**, so the UI can show "superseded by …" with a link to the page that says so, without the pointer living in prose and without pretending the edge went through Human Record Decision. And the declaration records the replacement date that authority states — anything published as "days since superseded" reads the authority's date, never the successor's `doc_date` or ingestion time.

The authority is normally an agency index, and lawfully may be the successor itself: a revision stating what it replaces is an ordinary way agencies declare a chain, and is checkable exactly as an index is. It may never be the predecessor. A document cannot be the authority for its own replacement — the replacement postdates it, so the page a reader would check to confirm the edge predates the fact it is meant to confirm. That prohibition is enforced at the manifest validator, at the registration boundary, and by a database constraint, because it is an invariant of the record rather than a rule about one write path.

## Considered options

**The full claims machinery** — supersession as an Assertion citing the RID index page, adjudicated like a field. Rejected as overbuilt: it would extend Assertions to document subjects and drag human record decision lifecycle onto metadata that has one authoritative source. The Assertion machinery exists to arbitrate sources that disagree about Constraints; nobody has two sources disagreeing about a revision chain. If that day comes, this decision is the one to revisit.

**Date or filename inference** — same project, same type, later date ⇒ supersedes. Rejected outright; `docs.py` already refuses the reasoning for undated files, and two same-type documents that legitimately coexist would false-positive. No supersession is ever inferred.

## Consequences

The chain a user sees traces to the manifest, not to a quoted page — checkable, not cited. That is accepted for registry metadata: nobody demands a citation for `doc_date` either, and the structured source pointer keeps the authority one click away.
