---
status: accepted
domain: terminology
scope: current product
---

# Complete glossary adoption preserves record and source identity

The user accepted the [complete glossary research](../research/glossary-terminology-review-2026-08-27.md), including the [Milestone findings](../research/milestone-terminology-2026-08-27.md). This amends ADR-0047 with the full recommendation set: retain established terms where they fit, use clearer scoped language for internal coordination concepts, and explain the additional concepts already used by Corridor. The mapped glossaries remain the definition authority; the research records the sources, alternatives, and reasons.

## Adoption scope

Adopt the recommended terminology in the glossaries, current ADR prose, ordinary screens, Report and workbook presentation, and operator-facing descriptions. Use Key dates for the customer schedule view while retaining Milestone as the technical event concept. Name the actual source, organization, question, or action instead of displaying a technical abstraction when possible.

The source discrepancy, discrepancy resolution, coordination decision, Documentation Review, Completion Reported, and report-release concepts remain distinct. None grants construction acceptance, inspection, certification, or permission to start field work.

All 24 proposed additions are accounted for as definitions or boundary references. Document Transmittal and Receipt Acknowledgment remain deferred capabilities. Schedule Data Date, actual or estimated completion fields, formal Contract Acceptance, and Utility Accommodation Rules Exceptions are explained without claiming those workflows or data are newly captured. The existing Statement Review Assistant and Product Test Run retain their restricted authority and claim limits.

## Presentation and compatibility

- Translate system-owned labels and known enumerated kinds at presentation boundaries. Never rewrite source quotations, names, filenames, dates, source-authored descriptions, model output, or retained artifact bytes through a global text replacement.
- Keep database tables, columns, API/form keys, route paths, rule codes, provenance class identifiers, receipt formats, and existing command names compatible. Current language can differ from a historical or implementation identifier without changing its identity.
- Preserve released PDF bytes and historical receipts. Newly generated Reports use the current labels; older releases remain the exact approved artifacts.
- Keep extractor prompts, schemas, model settings, deterministic policy sources, and their configuration fingerprints unchanged. This vocabulary adoption is not a new extraction or policy release.
- A legacy sufficiency marker can be described as documentation marked sufficient. It cannot alone generate a specific factual outcome such as Relocation Complete or Permit Issued. Show the stated requirement and review/support facts; preserve unknown or unsupported outcomes.
- Distinguish the current use of a supporting passage from whether its source revision is current. Keep replaced-source warnings visible.
- Keep each required Constraint Alert represented in the Coordination Summary, individually or through a bucket that retains its members.

The broader industry definitions of Utility Inventory and Utility Conflict Matrix do not silently change legacy extraction categories or field admission rules. Those implementation classifications concern which source fields are available; they are not definitions of industry terminology.

## Work not implied by the naming decision

This adopts the terminology recommendations, not the separate functional corrections from the two earlier audits. It does not decide requirement-revision storage, the reassessment rule after schedule changes, or the lineage-based replacement for legacy scalar statement projections. It does not create a mutable lifecycle, infer an actual completion date, or implement document delivery. Those changes require their own behavioral work and verification.

The research-before-proposal rule in `AGENTS.md` and [the domain guide](../agents/domain.md#research-before-proposing-terminology) remains in force. A code identifier or an old prompt is not permission to restore a retired customer label.
