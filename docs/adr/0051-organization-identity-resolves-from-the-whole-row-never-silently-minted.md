---
status: accepted
---

# Organization identity resolves from the whole row, never silently minted

Documents spell one External Organization many ways: "ABC Gas," "ABC Gas Company," "ABC." The name string alone under-determines identity — but the name arrives on a row that also states the facility, its type, and its location, and that context usually determines the identity completely. Today the code silently creates a new External Organization for any unfamiliar spelling. ADR-0034 (decision 46) gives the identity judgment to the coordinator. The decision ruling is on [#325](https://github.com/bharm16/corridor/issues/325).

## Decision

Matching uses the whole row. It resolves in three tiers:

1. **Known spelling — automatic, silent.** After normalization (case, punctuation, legal suffixes such as Co., Company, Inc., LLC), the name matches a registered External Organization or a saved alias. Widen the normalizer until every trivial variant lands here.
2. **Row context leaves exactly one candidate — automatic, under an exact rule.** The name stem fits a registered organization, and the row's facility class is owned by exactly one organization on the project. Every input comes verbatim from the document. The rule fires only when precisely one candidate survives, and the resolution records the rule's name. No similarity scores. No guessing.
3. **Anything else — one card, one click.** Two candidates survive, the facility column is blank, the context contradicts the name, or the organization is new. The card shows the row's context beside the choices. The person picks or creates. The click records the person, the choice, and the alias — that spelling never asks again.

Silent creation ends. An unresolved name causes an Abstention; the Extracted Proposal waits, visibly, for a person. The waiting item never gates the automatic tiers.

A confirmed alias applies registry-wide: one company is one identity everywhere. Confirmations change future matching only; facts recorded in another project change only through that project's own correction flow. Merging two already-recorded organizations is separate future work.

Each confirmation may also capture which facility classes the organization owns — the one piece of registry data tier 2 needs. The registry teaches itself as the project runs.

## Rejected

- **Name-only matching.** Two CenterPoint entities — one owning gas, one owning electric — read as one company to any string matcher. The row's "12-inch gas main" answers the question the string cannot.
- **Silent creation.** It never fabricates — it copies the document's spelling — but it fragments one company into accidental duplicates, and fragmentation is invisible until a report is wrong in front of that company. Visibly waiting beats invisibly wrong.

## Consequences

Implements ADR-0034 decision 46. Ticket #345 gains the facility-class lookup. Resolves #322's decision gate 3.
