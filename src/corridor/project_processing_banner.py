"""What a project surface may truthfully say about the pass that reads its
sources, and nothing more (#900).

#841 asked the source register for a per-row processing state — queued,
*processing*, completed, held, failed, excluded — and #900 recorded why the
middle one cannot be built: **there is no record that a particular document is
being read right now.** ``active_extraction_runs`` is the Current Production
Run pointer of ADR-0019, a selection among *completed* runs; ``extract_project``
commits per document and writes nothing while a document is mid-read. A row
saying "Processing" would assert something nothing carries.

What the record does carry is one project-level fact: the Due Work runtime's
``project_processing`` occurrence, and whether a worker has claimed it. That is
this module, and the distance between the two is the whole point — it says a
*pass* is claimed, never which file the pass is on, and no reader may present
it as per-document progress.

**A claimed occurrence is not a running worker, and this module refuses to say
it is.** #918 established that precisely. ``claim_due_work`` is safe about
*taking* work — ``for update skip locked``, concurrency limited to one — but
its candidate predicate deliberately admits ``state = 'claimed' and
lease_expires_at <= now``, an expired lease stops counting toward the
concurrency limit, and nothing fences the worker that holds the old claim: it
never re-checks its claim token between taking the lease and finalizing. So an
expired lease means *the claim lapsed*. It does not mean the worker stopped,
and this module never says it did. The three sentences below are the only
things the record supports:

- a claim whose lease is still in force,
- a claim whose lease has expired, with recovery outstanding,
- a pass the runtime recorded as failed with its retries spent,
- no claim at all, which the page says nothing about.

There is no fifth reading available. A worker heartbeat was considered and is
not one: ``operational_health.heartbeat_reading`` is a deployment-wide cadence
reading over every schedule at once, explicitly not authoritative and not a
product measure (#532), and it would need ``due_work_receipts`` as well. The
lease is the only per-project liveness evidence the record holds.

**The projection is the database's, not this module's.** The page reads
``current_project_processing_pass``, a view that carries the project partition
in its own body and exposes four columns: the project, the occurrence state,
when it was claimed and when that claim lapses. ``corridor_web`` holds SELECT
on that view and nothing at all on ``due_work_occurrences`` or
``due_work_schedules``, so a banner cannot reach a claim token, an owner, a
gate-7 declaration, or any write path into the scheduler. See
``corridor.migrations.source_append_commands.web_capability``.

**The clock is an argument.** Whether a lease is in force is a comparison
against an instant the caller supplies, exactly as every other review reading
takes its cutoff (ADR-0084, #488), so a test states the moment rather than
racing the wall clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session


# A worker holds the pass and its lease has not lapsed.
CLAIMED = "claimed"
# A worker took the pass and the lease ran out before anything finalized it.
CLAIM_EXPIRED = "claim_expired"
# The runtime spent this occurrence's retries and recorded it as failed. It is
# the one unclaimed state the page still speaks about, because a failure that
# nothing says out loud is a failure a coordinator waits through.
FAILED = "failed"
# The occurrence is in none of the above: pending, retried or completed. The
# banner does not tell those apart, because "is anything holding the pass" is
# the one question it answers and the register's own rows already carry what
# became of each source.
UNCLAIMED = "unclaimed"

# The words the page prints, for the three states that have any. `CLAIMED` is
# quoted from #900's own correction and is deliberately about the *pass*: it
# names no document, because no record says which document is being read.
#
# `UNCLAIMED` has no sentence. A line that says a worker holds nothing is true
# for almost the whole life of almost every project, so printing it permanently
# is a banner a reader learns to stop seeing, and it displaces the register's
# own rows, which carry what actually became of each source. The page prints
# nothing there.
#
# `CLAIM_EXPIRED` says only what the record says, and says it only while
# recovery remains open: `_status` reaches that state exactly when the
# occurrence is still `claimed` and its lease has lapsed, which is precisely
# `claim_due_work`'s own recovery-candidate predicate, so the sentence cannot
# outlive the recovery it promises. A spent occurrence is `FAILED` instead.
SENTENCES: Mapping[str, str] = {
    CLAIMED: "A worker has claimed this project's document-processing pass.",
    CLAIM_EXPIRED: "The processing claim expired. Recovery is pending.",
    FAILED: "This project's document-processing pass failed.",
}

# What the record cannot settle, kept out of the sentence and put underneath
# it. The claim lapsing is a fact; what became of the worker that held it is
# not one, because nothing fences that worker when its lease runs out and
# nothing records its liveness. Saying so in the headline made the headline
# about the uncertainty rather than about the claim.
DETAILS: Mapping[str, str] = {
    CLAIM_EXPIRED: (
        "Nothing records whether the worker that claimed it has stopped."
    ),
}


@dataclass(frozen=True, slots=True)
class ProcessingPassBanner:
    """One project's pass, as the claim record answers it at one instant.

    ``claimed_at`` and ``lease_expires_at`` are present exactly when a claim was
    taken, which is a fact about the occurrence rather than a gap: an occurrence
    nobody has claimed carries neither, and the sentence says so without them.

    ``sentence`` is empty for a state the page says nothing about, so a caller
    renders the banner on the words rather than on the reading's existence.
    ``detail`` is the qualification that belongs under the sentence and not in
    it, and is empty for every state that has none.
    """

    status: str
    sentence: str
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    detail: str = ""

    @property
    def claim_held(self) -> bool:
        return self.status == CLAIMED


_PASS = text(
    "select occurrence_state, claimed_at, lease_expires_at "
    "  from public.current_project_processing_pass "
    " where project_id = :project_id"
)


def read_processing_pass(
    session: Session, *, project_id: int, now: datetime
) -> ProcessingPassBanner | None:
    """This project's source-processing pass, or ``None`` when it has none.

    ``None`` is the honest answer in three different situations that a banner
    should treat identically: the project has no ``project_processing``
    schedule, it has one that has never reached a due slot, or the caller has
    declared a partition this project is not in. A page shows nothing rather
    than asserting that nothing is running, because those are different claims.
    """

    row = session.execute(_PASS, {"project_id": project_id}).mappings().first()
    if row is None:
        return None
    status = _status(
        str(row["occurrence_state"]),
        lease_expires_at=row["lease_expires_at"],
        now=_aware(now),
    )
    return ProcessingPassBanner(
        status=status,
        sentence=SENTENCES.get(status, ""),
        claimed_at=row["claimed_at"],
        lease_expires_at=row["lease_expires_at"],
        detail=DETAILS.get(status, ""),
    )


def _status(
    occurrence_state: str, *, lease_expires_at: datetime | None, now: datetime
) -> str:
    """Claimed, lapsed, failed, or held by nobody.

    The lease is compared the way ``claim_due_work`` compares it, so the state
    this reports and the state that decides whether the occurrence is a
    recovery candidate cannot disagree: at the exact instant the lease expires
    the runtime already treats the occurrence as recoverable, and so does this.
    Which is also why ``failed`` is read off the column rather than inferred:
    the runtime writes it only once the retries are spent, and that is the one
    unclaimed state a reader has to be told about.
    """

    if occurrence_state == FAILED:
        return FAILED
    if occurrence_state != "claimed":
        return UNCLAIMED
    if lease_expires_at is None or _aware(lease_expires_at) <= now:
        return CLAIM_EXPIRED
    return CLAIMED


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
