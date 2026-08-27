# Corridor Context Map

## Contexts

- [Project Record](./CONTEXT.md): the Constraints, External Party facts, project decisions, and publications that the project team relies on.
- [Corridor Operations](./docs/operations/CONTEXT.md): the document-processing, Admission, Supersession, and measurement machinery that supplies and maintains the Project Record.

## Relationships

- **Corridor Operations -> Project Record**: verified Candidates enter through Admission; an Abstention leaves the Project Record unchanged.
- **Project Record -> Corridor Operations**: Operative Support and registered Supersession identify the revision work that Corridor Operations must process.
- **Project Record -> Publication**: Reports and Briefings read the Project Record; an Approved Export seals one fixed external Report.

## Terminology

[ADR-0047](./docs/adr/0047-domain-language-follows-researched-construction-practice.md) records the industry-backed vocabulary and maps retained historical and implementation names. Before proposing a new term or meaning, follow the [terminology research procedure](./docs/agents/domain.md#research-before-proposing-terminology).
