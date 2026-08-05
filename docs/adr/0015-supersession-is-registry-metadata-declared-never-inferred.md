# Supersession is registry metadata — declared, never inferred, and not an Assertion

M8 needs `documents.superseded_by` populated. TxDOT's RID index records each revision's "Replaced on" date; the manifest today transcribes those supersession facts only partially, as free-text `notes`. The chain now enters as structured manifest fields and ingest persists it as registry metadata, exactly the way `doc_date`, `doc_type` and `sealed` already enter: the manifest declares, ingest records, the index remains the human-checkable source.

Four requirements keep the declaration honest. The chain references stable registered-document identifiers, never filenames. Ingest validates it — a missing target, a self-supersession, a cycle, or a cross-project edge is a registration error, not data. The manifest entry carries a structured pointer to the chain's source (the index document and page), so the UI can show "superseded by …" with a link to the page that says so, without the pointer living in prose and without pretending the edge went through Adjudication. And the declaration records the replacement date the index states — anything published as "days since superseded" reads the authority's date, never the successor's `doc_date` or ingestion time.

## Considered options

**The full claims machinery** — supersession as an Assertion citing the RID index page, adjudicated like a field. Rejected as overbuilt: it would extend Assertions to document subjects and drag adjudication lifecycle onto metadata that has one authoritative source. The Assertion machinery exists to arbitrate sources that disagree about Dependencies; nobody has two sources disagreeing about a revision chain. If that day comes, this decision is the one to revisit.

**Date or filename inference** — same project, same type, later date ⇒ supersedes. Rejected outright; `docs.py` already refuses the reasoning for undated files, and two same-type documents that legitimately coexist would false-positive. No supersession is ever inferred.

## Consequences

The chain a user sees traces to the manifest, not to a quoted page — checkable, not cited. That is accepted for registry metadata: nobody demands a citation for `doc_date` either, and the structured source pointer keeps the index one click away.
