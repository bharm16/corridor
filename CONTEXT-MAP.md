# Corridor Context Map

## Contexts

- [Project Record](./CONTEXT.md): the Constraints, External Organization facts, project decisions, and publications that the project team relies on, within Corridor's coordination scope.
- [Corridor Operations](./docs/operations/CONTEXT.md): the document-processing, Record Inclusion, supporting-source updates, and measurement machinery that supplies and maintains the Project Record.

## Relationships

- **Corridor Operations -> Project Record**: on the legacy route, supported Extracted Proposals enter through Record Inclusion; an Abstention leaves the proposed change unapplied, while a Processing Failure remains a failed attempt. On the adopted-baseline route, captured Source Facts are compared with the accepted record and enter only as Proposed Deltas that a person or a narrow released policy resolves; no legacy path silently replaces an accepted value.
- **Project Record -> Corridor Operations**: Supporting Documentation in Use and registered Supersession identify the revision work that Corridor Operations must process.
- **Project Record -> Publication**: Coordination Reports and Coordination Summaries read the Project Record; a Report Approved for Release is the complete immutable set of configured customer artifacts a named person approved for sharing, sealed as one Release Package, not evidence that it was sent or received.

## Terminology

[ADR-0048](./docs/adr/0048-complete-glossary-adoption-preserves-record-and-source-identity.md) adopts the [complete glossary research](./docs/research/glossary-terminology-review-2026-08-27.md), including Key dates as the customer label for Milestones, and extends [ADR-0047's legacy mapping](./docs/adr/0047-domain-language-follows-researched-construction-practice.md). The glossaries distinguish construction terms, plain product labels, and retained internal concepts. Before proposing a new term or meaning, follow the [terminology research procedure](./docs/agents/domain.md#research-before-proposing-terminology).
