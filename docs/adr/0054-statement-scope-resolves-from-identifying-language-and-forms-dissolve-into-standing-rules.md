---
status: accepted
---

# Statement scope resolves from identifying language, and forms dissolve into standing rules

A promise gets found in a document: "CenterPoint will relocate the gas main by March 15." Some promises record themselves. The rest waited for a person with a form: pick the constraints, pick the assignee, pick a next action. This ADR removes the form. It is the scope counterpart of ADR-0051, and it follows the same reasoning as ADR-0052: automate what the evidence determines, set the rest as standing rules, and leave people only what no document can answer.

Today the system matches a statement to a Constraint only through an explicit reference, such as a conflict ID. The context matcher — station overlap, facility terms — already exists in code, but only as a ranking aid inside the Statement Review Assistant. It sorts suggestions. It never decides. That is the gap this ADR closes.

## A statement is four questions. Three answer themselves.

**1. Who said it?** Automatic. ADR-0051's three tiers resolve the External Organization.

**2. Which Constraints does it apply to?** Resolve it from the statement's own identifying language, in tiers:

- An explicit reference (a conflict ID) resolves as it does today.
- The identifying details in the sentence and its surrounding passage — facility type, subtype, size, material, station, street or crossing name, and the document section the statement appears under — are matched against the organization's active Constraints' own row data. When exactly one candidate survives, an exact rule applies the scope and the statement records itself. Every input comes verbatim from the source. No similarity scores. No guessing.
- When several candidates survive, the card shows the narrowed set ("matches conflict 41 or 52") with the identifying details beside it. One click.
- When the statement has no identifying language ("we will relocate all our facilities by Q1"), the answer is not in any document. The statement records itself with Applies To not yet known, and one task goes on the owner's list: call the organization and ask.

Adding the exact tier is an expansion of an automatic class and goes through the regression replay of ADR-0050 before it runs.

**3. Who on our team handles it?** A standing rule, set once per organization: "Maria handles CenterPoint." Every statement from that organization routes to that person automatically. A wrong assignment is a correction to the rule, not a per-card confirmation.

**4. What do we do next?** Nothing, until the promised date passes. The system already derives due and overdue from recorded dates. An explicit plan is recorded only when the case is genuinely unusual.

## What is left for a person

Only the statement whose scope no document determines — and for it, the person's job is the phone call, not a form. The Statement Review Assistant's role shrinks to this residue, as a reading aid that orders and cites but never selects.

## Consequences

- Amends the guided-statement shape in #322 and reshapes tickets #333 and #334: the per-statement form gives way to standing assignment rules, derived follow-up, and the narrowed-set card.
- The setup screen gains the standing assignment rule per organization (with #336).
- The existing shortlist scorer moves from the assistant's private tool layer into the shared matcher, now allowed to decide in the exactly-one case, not only to rank.
- ADR-0035's boundary holds: suggestions still never select in the ambiguous tiers; the exact tier is a rule, not a suggestion.
