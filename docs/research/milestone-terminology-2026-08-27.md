# Milestone terminology in construction schedules

> **Adoption update:** These recommendations were subsequently accepted under [ADR-0048](../adr/0048-complete-glossary-adoption-preserves-record-and-source-identity.md). See the [adoption and coverage receipt](glossary-terminology-implementation-2026-08-27.md) for current definitions, implementation surfaces, and compatibility boundaries. The research text below retains its original research-time status and findings.

Researched: 2026-08-27. Status: research and naming recommendations; not an adopted terminology change. Scope: highway project development, construction scheduling, and Corridor's Project Record.

## Finding

**Milestone is established industry language.** It means a significant event in a schedule, such as construction starting or a phase finishing. The event has no duration; the work leading to it does. AACE, GAO, FHWA, and NJDOT independently support this distinction. The word itself is not a terminology defect. [AACE][aace] [GAO][gao] [FHWA][fhwa] [NJDOT][njdot]

There is also an existing, plainer label: **Key Dates**. TxDOT explicitly uses **Key Dates (Milestones)**. This supports a customer view called **Key dates**, with each event's actual name and scheduled date shown separately. It does not justify reducing an event and its history to one anonymous date. [TxDOT, p. 7 and Appendix A][txdot]

## Primary-source evidence

All sources below were opened and their relevant passages checked. Page numbers below are printed.

| Source and edition | Passage | Meaning and applicability |
| --- | --- | --- |
| [AACE Recommended Practice 10S-90, Cost Engineering Terminology][aace]; online revision February 18, 2026 | Milestone and Key Events entries, June 2007; Activity; Contract Dates; Contract Completion Date | Milestones mark a point for reference or measurement and should have agreed validation conditions. Activities consume time. Contract dates carry a separate contractual basis. This is cost-engineering terminology, including planning and scheduling. |
| [GAO-16-89G, Schedule Assessment Guide][gao]; December 2015 | Best Practice 1, “Milestone, Detail, and Summary Activities,” pp. 13–14; Figure 2 | Milestones identify major events or deliverables and need clear completion conditions. Starts, finishes, and handoffs are examples. Detail activities represent the actual work. This is general project-schedule guidance, not a construction contract specification. |
| [FHWA Central Federal Lands, Guidelines for Developing Critical Path Method Schedules][fhwa]; October 2006 | §4.3, p. 16; §5.4, p. 26 | Construction scheduling instructions give milestones zero duration and distinguish them from tasks with duration. The glossary describes a milestone as a reference point for a major project event. This is direct highway-construction usage, although the software instructions are old. |
| [TxDOT, Schedule Guide for Transportation Development Projects][txdot]; revised May 2023 | Create Initial Schedule, §1 Key Dates, p. 7; Appendix A, pp. 11–12 | Uses Key Dates (Milestones), including construction beginning or ending. It lists Utilities Adjusted separately from Utility Clearance Certification, each with its own definition. The guide mainly governs project-development schedules; it is not a universal contract dictionary. |
| [NJDOT, Scheduling Manual for Design Projects][njdot]; page updated January 17, 2020 | §3.0 Definitions; §5.4 Schedule Updates | Distinguishes a work activity with duration from a milestone with none. It separates the current schedule from an approved baseline. This corroborates transportation-agency usage in design-project scheduling. |
| [Oracle Primavera Cloud, Assign an Activity Type to an Activity][oracle]; live help inspected August 27, 2026, no edition date shown | Step 4, Start Milestone and Finish Milestone | Start and finish milestones identify phase boundaries; neither has duration or resource assignments. This verifies scheduling-tool semantics, not a legal requirement. |

## Nearby terms are not interchangeable

| Term | Use it for |
| --- | --- |
| **Milestone** | The named event: for example, “Relocation complete.” Its scheduled date and actual achievement are separate facts. [GAO][gao] |
| **Start milestone / Finish milestone** | A significant beginning or ending. These are established, more specific terms. [Oracle][oracle] |
| **Key dates** | A plain heading for selected schedule events and their dates. TxDOT uses this language; that does not make it mandatory everywhere. [TxDOT][txdot] |
| **Activity** | The work over time: “Relocate power poles.” This is not a substitute for the instant that work finishes. [FHWA][fhwa] |
| **Contract completion date** | A completion date established by the contract for all or a specified portion of work. It is too narrow for every event in Corridor. [AACE][aace] |

Do not rename all milestones **deadlines**. That would suggest each date is a required limit, whereas the sources distinguish event dates from contract dates. Likewise, do not call every milestone **construction start** or **completion**: milestones can mark either end of a phase or a handoff. These are naming recommendations based on the distinctions above. [AACE][aace] [GAO][gao]

## Fit with the current product

Corridor's [`Milestone` model](../../src/corridor/models.py#L2084) stores an event code, name, date, source, and revision reference; it has no duration or resources. The [CSV importer](../../src/corridor/milestones.py#L26) requires code, name, and date columns; dates can be blank. The [checked-in example](../../corpus/sh99-milestones.csv) contains design completion, agreement execution, and relocation-construction completion. These are consistent with event records. Their presence does not establish that their dates are current or approved.

The misleading part is the [Report's “Milestone readiness” section](../../src/corridor/report.py#L389). It groups linked Constraints and counts their legacy Ready results. It does not establish that the named construction event occurred. A clearer heading must preserve that limitation.

## Recommendation, pending agreement

Keep **Milestone** where an exact schedule term or source identity is needed. Use **Key dates** for the customer view. Show concrete event names, not just the category name:

> **Key date:** Start drainage construction in Area B
>
> **Scheduled date:** September 1
>
> **Constraint:** Move the identified power poles out of Area B
>
> **Required by:** September 1, based on the linked schedule revision
>
> **Promised for:** September 10
>
> **Question:** Does the later promise affect the planned September 1 start?

This is an illustrative example, not Project Record data. The view should retain the exact event and revision behind Required By. A report section grouping Constraints by these events could be **Constraints by key date**; that is proposed product wording, not an industry-defined status. Neither a naming change nor a later promise establishes actual project delay or milestone achievement. The [current glossary](../../CONTEXT.md#constraints-and-commitments) and [ADR-0045](../adr/0045-milestone-registration-is-the-input-to-need-date-derivations.md) retain those authority boundaries.

No invented domain term is needed. No glossary, ADR, code, schema, or Project Record changes were made for this research.

[aace]: https://library.aacei.org/terminology/welcome.shtml
[gao]: https://www.gao.gov/assets/d1689G.pdf#page=27
[fhwa]: https://highways.fhwa.dot.gov/federal-lands/pddm/cfl/cfl-guide-develop-cpm.pdf#page=26
[txdot]: https://ftp.txdot.gov/pub/txdot-info/tpd/project-portfolio/schedule-guide.pdf#page=8
[njdot]: https://nj.gov/transportation/eng/documents/scheduling/schedmanual.shtm
[oracle]: https://docs.oracle.com/cd/E80480_01/help/en/user/88237.htm
