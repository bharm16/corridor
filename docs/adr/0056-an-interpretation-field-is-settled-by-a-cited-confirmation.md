---
status: accepted
domain: project-record
scope: optional module
amends:
  - ADR-0052
---

# An interpretation field is settled by a cited confirmation

ADR-0052 left one human field in the documentation checklist: reading whether the organization's letter really says approved. The first design showed the person the scanned letter and asked them to judge it — a reading assignment. That was rejected as ceremony. Full automation was considered and rejected too: classifying hedged prose has no mechanical check — two model readers can share the same blind spot, so agreement on interpretation is not verification the way agreement on transcription is — and this field is the last gate before Ready, the highest-consequence derived word in the product.

## Decision

**The system does the reading and states its conclusion. The person clicks Confirm.**

- The system reads the letter, classifies it, and cites the exact sentence it stands on.
- A clean letter presents one standard confirmation: the stated conclusion, the quoted sentence, and the letter one tap away. The person clicks Confirm. The record keeps the person, the time, the conclusion, and the cited basis the screen showed them.
- A hedged letter presents its condition, quoted ("…approved pending final inspection of segment B"), with explicit choices — record as conditional, or record as approval — and the letter one tap away.
- The confirmation is thin by design because the evidence is on the button. This deliberately amends ADR-0035's preselection caution for this field: a cited conclusion with a confirm is not a blank acknowledgment, and the deposition answer survives — a named person confirmed, and the record shows the exact basis they were shown.
- The confirm can retire, per classification class, the same way every automatic rule earns trust: the letter classification passes ADR-0050's regression replay against accumulated human confirmations. Until it does, the confirm stands.

## Consequences

Amends ADR-0052's interpretation-field wording — the person no longer "reads the letter"; they confirm a cited conclusion. Ticket #347 implements the cited-confirm pattern for interpretation fields. No other checklist behavior changes: machine fields still compute themselves with sources attached, and Ready still derives.
