---
status: accepted
domain: extraction
scope: current product
amends:
  - ADR-0094
---

# One live-transmission rule: experiment approval is distinct from customer-processing approval, and replay is not a transmission

**Amends ADR-0094.** It moves one clause: how the outbound authorization
check gates on the provider posture's status. Everything else in ADR-0094's
"Provider posture versus customer authorization" section stands: the posture
is bound to its document by digest, the customer authorization accepts it per
project, source class, purpose and region, the check runs in the adapter
before the network call, and a missing or mismatched record is a refusal with
the reason and zero outbound requests.

The decision was recorded by the maintainer on 2026-09-10 on issue #808, after
reviewing `main` at a2424b0 and PRs #802 to #806.

## Context

ADR-0094 said that no customer page may be transmitted without a signed
customer authorization accepting the posture, and left the posture's own
approval implicit in a single `status` string. The shared check built from the
two adapters (#805, `src/corridor/provider_authorization.py`) found that the
two copies had read that string differently. The Textract adapter refused a
*customer record* while its posture was not `accepted` and let an experiment
scope through unexamined; the model-provider adapter refused *every request*
while its posture was not `approved`, experiments included. #805 declared the
divergence as two constants, `CUSTOMER_RECORDS_NEED_ACCEPTED` and
`EVERY_REQUEST_NEEDS_APPROVED`, and said in the module that unifying them was
the maintainer's decision, not a side effect of removing a copy.

Neither reading was right. The Textract posture document is `proposed`, with
retention, effective opt-out and workload permissions recorded as unverified,
and nothing in it approves a new live experiment either; the adapter let one
through because the status string only ever spoke about customer pages. The
model-provider document is `approved` for the experiment stage on public and
synthetic material and `blocked` for every customer stage, two different facts
that one word cannot carry. Meanwhile the offline replay of retained responses,
which transmits nothing, was in the Textract adapter's reasoning for letting
experiments through, as if a replay needed the same permission as a call.

## Decision

Every new live provider transmission requires an explicitly approved posture
for that use, plus its applicable authorization record. Public or synthetic
experiments use an approved experimental scope; customer processing
additionally requires the customer-ready posture and the signed customer
authorization. Offline replay of a retained response is not a new transmission.
No live transmission may proceed under a merely proposed or unknown processing
approval.

From #808:

| Operation | Required authority |
|---|---|
| Local processing or offline retained-response replay | Applicable source-access and retention controls; no new transmission approval |
| New provider call using public/synthetic material | An explicitly approved experimental provider posture plus its recorded Experiment Scope |
| New provider call using customer material | A customer-approved provider posture plus the applicable signed customer authorization |

### The rule, over the posture's approval facts and the record kind

1. A new live transmission under a customer authorization requires the
   posture's **customer-processing approval** and the signed customer
   authorization, matched on every field the check already runs.
2. A new live transmission under an experiment scope requires the posture's
   **experimental approval** and the recorded Experiment Scope. An
   experimental approval names exactly what it permits (source classes and
   purposes, on the posture whose region or model it belongs to) and what
   remains unverified. It can never authorize a customer stage.
3. Offline replay of a retained response is not a new transmission and needs
   no new transmission approval. It stays governed by source-access and
   retention controls. A replay path is not gated by (1) or (2), and it makes
   zero outbound requests: it never constructs a client or a transport.
4. A posture that is merely proposed, or whose approval fact for the requested
   use is absent, produces zero outbound requests and a refusal naming the
   missing approval.

The two approvals are declared fields on the provider posture, each either
absent or stating what it permits and what it leaves unverified, and each read
from the posture document the posture is bound to by digest. The `status`
string remains the document's lifecycle word and no longer decides anything at
the check. The two per-adapter constants are replaced by this one rule,
expressed once in the shared check.

### Boundaries

- Unverified AWS facts (account, retention, opt-out, permissions) are not
  marked verified by this decision or by any approval. A limited experimental
  approval says exactly what it permits and what remains unknown, and cannot
  authorize customer processing.
- The model-provider boundary is not weakened: its customer processing stays
  blocked, with the two blocking fields named in the refusal.
- A proposed customer posture is not general permission for anything.
- No historical record is renamed and no approval is manufactured. The
  Textract posture document stays `proposed` with no experimental approval
  until the maintainer records one in a new revision; under this rule that
  refuses every new live Textract experiment while replay keeps working.
- Source class, purpose, stage, region or model, posture identity and digest,
  and budgets continue to be checked. Unknown or mismatched approval produces
  zero outbound requests.

## Considered options

**Keep the two declared constants.** Rejected. The declaration made the drift
visible, which was #805's job, but it left one adapter transmitting
experiments under a proposed posture and the other refusing experiments its
document approves. A guard that means two things is not one guard.

**Adopt one of the two existing readings for both adapters.** Rejected either
way. "Customer records need `accepted`" leaves a live experiment ungated by
the posture. "Every request needs `approved`" gates experiments on the same
word that opens customer processing, so approving an experiment would read as
approving customer pages, or the word would have to mean something different
per document.

**Overload `status` with a richer vocabulary (`approved-for-experiments`,
`accepted-for-customers`, and so on).** Rejected. Two facts belong in two
fields, each able to say what it permits and what it leaves unverified; a
status word can carry neither list.

**Gate replay through the same check, with a relaxed rule for cache hits.**
Rejected. Replay reads a retained response and constructs no client; putting
it behind a transmission check would either invent an approval for it or
refuse work that transmits nothing. The replay modules stay outside the check
and the tests hold them there.

## Consequences

- ADR-0094 carries `amended_by: ADR-0098`. Its posture-status clause is
  replaced by the rule above; its other clauses are untouched.
- `provider_authorization.ProviderPosture` declares `experimental_approval`
  and `customer_processing_approval`; `PostureStatusRule` and its two
  constants are removed; the shared `mismatches` applies the one rule.
- The model-provider posture records the experimental approval its document
  states (#557, 2026-09-03) and no customer-processing approval. The Textract
  posture records neither. Neither document's bytes or digest change.
- The refusal for a missing approval is
  `posture-approval: the posture is '<status>' and records no <experimental |
  customer-processing> approval; no <experiment | customer> page may be
  transmitted until the maintainer records one`. An approval that does not
  permit the request's source class or purpose is named the same way.
- What remains unresolved: the Textract experimental approval is the
  maintainer's act in a new posture revision, preserving the current one in
  the posture history first; operational posture acceptance for customer
  processing remains #732; customer authorization remains #522.
