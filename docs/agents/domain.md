# Domain documentation and terminology

## Before exploring

Start with [the context map](../../CONTEXT-MAP.md), then read each glossary relevant to the work and the applicable [ADRs](../adr/). The Project Record glossary owns construction and coordination concepts; the Corridor Operations glossary owns processing and measurement concepts.

Use the current glossary's meaning. [ADR-0047](../adr/0047-domain-language-follows-researched-construction-practice.md) maps older domain words and retained implementation names. An old filename, table, prompt, receipt, or quotation is not authority to restore an obsolete customer label.

## Research before proposing terminology

**Trigger:** before suggesting, adding, renaming, or materially redefining a domain term. This includes a status, field label, relationship, or named business concept in conversation, plans, UI copy, Reports, glossaries, ADRs, and domain models in code. Research must precede the suggested name, not merely precede its implementation.

1. **Identify the meaning.** Check the glossaries and relevant ADRs first. Determine whether an existing term already covers the concept. Reusing an accepted term with its unchanged meaning does not require another naming exercise.
2. **Research applicable practice.** Inspect primary sources from the responsible agency, standards body, or recognized industry organization. Match the context: highway utility coordination, construction planning, contract administration, or the actual field involved. A term used by one method or jurisdiction is not automatically a universal standard.
3. **Read the source passage.** Check the definition, adjacent distinctions, edition or date, and scope. Existing notes in [research](../research/) can locate the evidence, but verify the underlying source before reusing it for a new proposal. A search snippet, model memory, or a vendor's unexplained label is not sufficient.
4. **Record the evidence.** Add or extend a focused research note with the question, source title and URL, edition/date and section, observed meaning, applicability, and any uncertainty. Explain how the proposed meaning differs from nearby concepts. For a claim of broad industry usage, corroborate it beyond one local form or method.
5. **Propose in plain language.** State whether the term is established industry language, a scoped plain-language product label, or an internal technical concept. Give a concrete construction example. Preserve distinctions such as work ready to start, work reported complete, documents reviewed, and formal authorization.
6. **Handle a missing counterpart explicitly.** If the sources do not provide an exact equivalent, say so. Explain the proposed product wording and obtain explicit user agreement before adding it to the canonical vocabulary. If the research cannot be completed, report the gap and keep the existing vocabulary; do not invent a term to unblock naming.
7. **Update the right records.** Once the term is agreed, update the relevant glossary and current ADR language together. Keep the glossary to concise definitions. Record meaningful decisions or exceptions in an ADR; a wording correction alone does not require a new ADR. Preserve historical quotations and implementation identifiers with a clear mapping.

**Completion criterion:** each new or changed term has a cited primary-source meaning, a stated domain/jurisdiction, an explanation of its fit, an explicit distinction between source terminology and product interpretation, and any required agreement on an exception. The review must be able to trace the proposal back to that evidence before approving its adoption.

Ordinary software helper names are outside this construction-terminology gate. A name that represents a domain concept or appears in customer language is inside it, even if first introduced in code. Internal technical names must remain visibly internal rather than becoming customer concepts without the same research.

## Preserve meaning during adoption

- Name the actual object and outcome. Use Utility Conflict for utility interference, not for every external condition.
- Keep source verification, Documentation Review, Completion Reported, project actions, and formal construction acceptance or authorization separate.
- Preserve a promise's wording, precision, speaker, and scope. Required By and Promised For answer different questions.
- Explain a missing supporting record as missing support, not proof that the physical work is unfinished.
- Retain immutable source and decision history. Change schema names, source-sensitive prompts, or policy identifiers only in a separately scoped and verified implementation.
- Treat old ADR passages marked historical as history. A terminology amendment changes the current vocabulary without rewriting source quotations or pretending a code migration has already occurred.

## Flag decision conflicts

If a proposal would change an accepted domain decision, identify that conflict before editing. Cite the ADR and describe the change in plain language. Terminology work must not silently grant authority, introduce a lifecycle, change a calculation, or settle an open policy question.

The [August 27 terminology research](../research/construction-industry-terminology-2026-08-27.md) is the starting evidence for the adopted construction language, not a substitute for research on future concepts.
