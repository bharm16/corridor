---
status: accepted
---

# Row identity is declared by the registry, never inferred from the number

The first per-party matrix broke the guess every reader was making. SR 789's FDOT form gives each External Party its own conflict list counting from 1 — nine different conflicts, each correctly numbered 1 — and mechanical admission (ADR-0029), grouping by `utility_id` alone, loaded 1 of its 66 rows and held the rest as false duplicates. The TxDOT UCM form had hidden the guess: its numbers are project-unique, and its Retired Rows exist precisely to keep them stable. Decision: **how a matrix names its rows is a Numbering Scheme the registry declares at registration** — `project-unique` (the default) or `per-party` — **and identity is derived under the declared scheme in exactly one place**, used by admission grouping, the record's reference check, statement resolution, and lane sibling grouping. A new agency format becomes one new scheme value in one function; no reader changes.

The scheme is never inferred from the data. Repeated numbers under an undeclared scheme keep abstaining, visibly — a document that genuinely printed one row twice must not become two conflicts because repetition was read as a numbering style — and declaring the scheme then re-running the load, which is idempotent, puts the rows on. Identity is stored as its parts, not a formatted string: a Dependency keeps the document's stated number in `source_ref` and its party as the External Party relation, so no separator becomes load-bearing and a party's recorded aliases keep working. A statement that names a number several parties hold resolves by its stated party through the same alias-bounded match the party check already applies; still-ambiguous references abstain. The declared scheme rides in the admission policy's canonical form, so re-declaring it changes the digest the receipt records.

## Considered options

**A compound key hardcoded in admission** — group by (party, number) when numbers repeat. Rejected twice over: baked into the wrong layer (admission learns a format guess while four other readers keep the old one), and triggered by inference (repetition read as scheme).

**Identity as a stored formatted string** ("AT&T TCA · 1" in `source_ref`). Rejected: it bakes a display separator into the record forever, bypasses the External Party relation and its aliases, and every rename propagates nowhere.

**Fuzzy matching on station and facility.** Rejected: not a replayable admission proof. Cross-revision correspondence where identity genuinely cannot match — a format that renumbers between revisions — is Revision Comparison's job, with its versioned matcher.

## Consequences

`documents.numbering_scheme` is registry metadata beside `doc_date`, declared in the corpus manifest and re-declarable by re-registration (a declaration, unlike provenance, may be corrected). `corridor.identity` owns derivation and the alias-bounded party match, and both admission families digest it. Dependency admission gains the `no_row_identity` abstention (reason vocabulary v2) for a per-party row stating no party. CONTEXT.md gains the Numbering Scheme entry. #211's confirms-and-disputes build on identity rather than on `utility_id`.
