---
status: accepted
---

# A fact is one typed row, not a JSON payload

The extraction pipeline stores what a source said as `Candidate.payload_json` — a mutable JSONB document holding fields, citations, and quotes together. Because it is mutable (`edit_candidate` reassigns it, `adjudicate.py:530`), every downstream consumer defends itself by copying: the run snapshot (`extraction_runs.py:292` → `models.py:977`), the abstention receipts, the audit field maps. An accepted field map physically exists in at least six tables. The 2026-08-30 storage research resolved how source facts are stored instead.

Decision: **one `facts` envelope table with typed value columns, satellites only where a payload is genuinely not a scalar or a reference.**

- The envelope carries what every fact shares: project, fact type, subject reference, provenance (extraction run, segment support via ADR-0069's join), recorded-at, recorded-by.
- Values live in typed columns per value class — a date column, a date-range column, a text column, and real foreign-key columns for reference values (an External Organization, a Document). A CHECK constraint per fact type enforces that exactly the right columns are populated. References buried in JSON have no integrity; foreign-key columns do.
- Set-valued and structured payloads get satellite tables with explicit columns — the Applies To reference set, and the typed closure result (closure kind, governing segment references, optional successor). This is existing house idiom: `dependency_event_timings` and `dependency_event_scopes` are satellites of `dependency_events`.
- Fact-type contracts — subject type, value class, validation, current-value rule, Record Inclusion rule (ADR-0070) — live in code, versioned like released policy modules, not in a database registry.

## Considered options

**One generic value column (classic EAV).** Rejected on the literature's own test: EAV is justified only when attributes are numerous, sparse, and uncontrolled ([Nadkarni's EAV guidelines](https://pmc.ncbi.nlm.nih.gov/articles/PMC2110957/) — measured 3–12× slower for attribute-centric queries, and even its defenders split values by type). Corridor has 15–20 controlled fact types; that answers "conventional typed columns."

**A subtype table per fact type (full class-table inheritance).** Rejected as the default: the current-record and as-of queries become a UNION over twenty tables, appending becomes N shapes, and the migration surface multiplies, for integrity the CHECK-constrained envelope already provides. Satellites remain available where a type earns one.

**JSONB payload validated by JSON Schema.** Rejected for authoritative values: shape validation is not reference integrity, and the practitioner guidance that endorses JSONB does so for rare, volatile attributes — the opposite of a controlled vocabulary.

## Consequences

`assertions.asserted_value` (a text column keyed by `field_name` — a mild EAV) is superseded for new writes by facts referencing segments; existing rows stay readable. New extraction state appends envelope rows; nothing edits them (corrections follow ADR-0071). ADR-0001's separation of source values from conclusions is preserved and strengthened: the fact is the source value, typed decisions are the conclusions.
