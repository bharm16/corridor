---
status: accepted
---

# Ready judgment is an explicit human act

Admission is mechanical (ADR-0029), and exact unchanged Operative Support may move
without a reviewer adding judgment (ADR-0022, as amended by ADR-0034). Ready is
different. It concludes that exact current Evidence satisfies an exact readiness
requirement, so it adds judgment that deterministic processing cannot originate.

Decision: a **Ready judgment** is a guided human act over one stated bar and one
stated proof set: **does this exact Evidence satisfy this exact
`evidence_required` requirement?** The screen shows the requirement, the Evidence,
and the Operative Support that a Yes answer would establish. The available answers
are **Yes**, **Not yet**, and **Needs clarification**. One designated project person
is the default judge. Fine-grained construction-worker permission splits remain
deferred.

Ready remains a Derivation, never a mutable status. The judgment establishes
Evidence sufficiency; when current support later lapses through supersession, loss
of support, or a changed current record, Corridor removes Ready automatically and
records visible high-priority work. It notifies the Internal Owner and the person
who made the prior judgment, showing the requirement and newer Evidence. Nobody is
asked to make an inverse “unready” decision.

## Specific changed-record work replaces a Reconfirmation lane

The user-facing Reconfirmation lane disappears. Exact unchanged support moves
automatically under the fail-closed proof bar. Changed, ambiguous, dropped, or
otherwise ineligible work becomes a specific question on the affected record:
settle a Dispute, verify a citation, attach a statement, judge readiness against new
Evidence, or dismiss junk. Durable receipts remain, but the user works the project
question rather than transfer mechanics.

Citation checking is exception-driven rather than blanket review. Corridor verifies
quotes mechanically. Human citation work is reserved for failed verification,
superseded Operative Support, contradictions that matter to a current conclusion,
the Ready sufficiency question itself, or an external release integrity blocker.

User-facing history remains plain first and technical second. It names the Ready
judgment, later Ready loss, actor, time, requirement, and exact Evidence. Policy
digests and carry-forward receipts stay available behind technical details; they do
not become customer tasks.

## Considered options

**Let exact Evidence set Ready automatically.** Rejected. Exact text presence does
not prove that the text meets the project's stated sufficiency bar.

**Keep a generic Reconfirmation queue.** Rejected. An exact unchanged transfer adds
no judgment, while a changed record presents a more specific question than
“reconfirm.”

**Require two people for every Ready judgment.** Deferred. One designated person is
the first-release default; a project may require a second person later when its
contract justifies that control.

## Consequences

Ready needs a dedicated Evidence-and-requirement flow with one designated default
human. Automatic Ready loss creates visible work and preserves the earlier
judgment. The supersession surface presents concrete record questions rather than a
general Reconfirmation lane. This ADR does not govern internal Report generation or
external artifact release; ADR-0040 owns that separate lifecycle.

## Decision map

This ADR records decisions 27-29, 31, 33, and 41 from the original
product-workflow interview. The 2026-08-12 post-foundation interview did not change
the Ready boundary.
