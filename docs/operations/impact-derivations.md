# Proposed Delta impact readback

Key Date table capture now appends each Impact Derivation to
`proposed_delta_impact_derivations` in the same transaction as its Proposed
Deltas. The capture receipt and audit payload retain their existing content.

Review and packet consumers use `read_impact_derivations(session,
project_id=..., delta_ids=...)`. Each returned reading includes the rule and
version, compared accepted revision, exact input payload and digest, evaluation
time, affected Constraint and Key Date identities, and derivation digest.
`ChildReading.impacts` exposes the same readings on the Review screen.

Re-evaluating identical inputs returns the original row, including its original
evaluation time. Changing the rule version creates a separate immutable row;
returning different consequences for identical inputs and rule version refuses.
The evaluation time is injected with `impact_evaluated_at`; production calls
default to the current UTC instant. Readback marks a derivation stale when the project's accepted
revision has advanced. A stale derivation is historical evidence, not a current
impact assertion, and no derivation authorizes an accepted-record change.

Existing audit-only captures remain available in their original receipts. They
are not silently backfilled from today's accepted record. Replaying their exact
capture supplies the normal current comparison; historical audit payloads retain
their historical meaning.

The existing rule identifies Constraints sharing the accepted Key Date's Required
By value; it does not compute CPM or assert a construction delay. This storage
change preserves that rule and its declared input limitations.
