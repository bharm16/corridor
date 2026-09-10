"""ADR-0050's replay gate: one comparison, one activation ledger, every family.

ADR-0050 says four things about letting a deterministic rule write to the
Project Record on its own, and they are the whole of this module:

1. **The cases a person decided are the answer key.** Replay the rule over the
   project's own recorded human decisions, from saved outputs, never re-reading
   a source file. A difference from what the person decided is a contradiction
   and blocks.
2. **A rule abstaining is not a contradiction.** A tie, a candidate that no
   longer exists, coverage that moved — the rule declining to answer is not
   evidence against it. One family (organization identity) treats an abstention
   on an answer-key case as blocking too, because its whole-row tiers claim to
   be able to reach every case a person reached; that is a per-family decision
   the caller states, not a reinterpretation of the ADR.
3. **Zero real cases never pass.** A brand-new rule with no history waits for a
   real case; a person does the small task by hand meanwhile.
4. **A deliberate human suspension beats every passing test**, and the pass is
   void only when the rule's own fingerprint changes.

Four policy expansions each grew their own copy of that, plus their own
activation table:

- ``schedule_linking`` — ``ReplayResult`` / ``replay_matches_human_decisions``
  / ``activation_status`` / ``suspend_auto_link``, over
  ``schedule_link_activations``.
- ``unreadable_cell_admission`` — the same two names, character for character
  in the nine lines that decided ``passed``, over
  ``unreadable_cell_admission_activations``.
- ``statement_scope_matching`` — ``StatementScopeReplay`` /
  ``replay_matches_human_scope_decisions``, with no ledger of its own.
- ``organization_identity`` — ``IdentityReplay`` / ``activation_status``, over
  ``organization_identity_activations``.
- the event-admission trio (``event_admission``,
  ``event_admission_acceptance``, ``event_admission_reproof``) — over
  ``event_admission_activations``.

``passed = case_count >= 1 and not contradictions`` was written four times, and
the nine lines of ``activation_status`` twice verbatim. Each expansion now
contributes only the two things that are actually its own: *what are this
project's recorded human decisions for my family*, and *what would my rule do
for one of those cases*. The comparison, the pass rule, and the ledger live
here once.

The event-admission trio keeps its clone-based proof (copy the database, erase
the rule's own signed answers, re-run, record one immutable receipt) as an
*extension* over this gate rather than a rival to it: it reads and appends the
same ledger through :func:`latest_ledger_entry`, :func:`record_activation` and
:func:`record_suspension`, and derives its richer ``proof_status`` from its own
receipts on top.

One relation backs all of it — ``policy_activations``, keyed by policy family
and rule fingerprint. The four per-family model classes are typed views onto
that one relation (single-table inheritance on ``family``), so a query written
against one family cannot read another family's rows.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Hashable

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import PolicyActivation

# The families that keep an activation ledger. Each name is one policy family's
# ``polymorphic_identity`` in ``models``; :func:`ledger_model` resolves it
# there rather than repeating the mapping, so the two cannot drift.
FAMILY_SCHEDULE_LINK = "schedule_link"
FAMILY_UNREADABLE_CELL_ADMISSION = "unreadable_cell_admission"
FAMILY_ORGANIZATION_IDENTITY = "organization_identity"
FAMILY_EVENT_ADMISSION = "event_admission"

# Identifying language matches statement scope under the event-admission
# acceptance ledger rather than one of its own, so it names a family for its
# replay and appends nothing.
FAMILY_STATEMENT_SCOPE = "statement_scope"

INACTIVE = "inactive"
ACTIVE = "active"
SUSPENDED = "suspended"

ACTIVATE = "activate"
SUSPEND = "suspend"


class ReplayGateRefusal(Exception):
    """A replay-gate act that cannot be recorded as asked."""


# The sentinel a ``recompute`` returns for a case its rule declines to answer.
# ``None`` is a real answer for some families (an unset value a person cleared),
# so abstention is its own object rather than a falsy value.
class _Abstained:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "ABSTAINED"


ABSTAINED = _Abstained()


@dataclass(frozen=True)
class RuleFingerprint:
    """The identity a passing replay is bound to (ADR-0050's "what a rule is").

    ``policy_sha256`` is the digest over the rule's canonical policy — its
    configuration and its deployed bytes. The event-admission family binds a
    passing proof to an immutable acceptance receipt instead, and so carries no
    digest on the ledger row itself.
    """

    policy_version: str
    policy_sha256: str | None = None


@dataclass(frozen=True)
class ReplayOutcome:
    """The comparison that is the test (ADR-0050), for one policy family.

    ``contradictions`` and ``abstentions`` carry each family's own case keys —
    a Constraint id, a cell triple, a Candidate id, a statement event id — so a
    caller can name what blocked without a second pass over the records.
    """

    family: str
    case_count: int
    contradictions: tuple[Any, ...]
    abstentions: tuple[Any, ...] = ()
    abstention_blocks: bool = False

    @property
    def passed(self) -> bool:
        # Zero real cases never pass; a contradiction always blocks. An
        # abstention blocks only for a family that said it should.
        if self.case_count < 1 or self.contradictions:
            return False
        return not (self.abstention_blocks and self.abstentions)


def replay(
    *,
    family: str,
    human_decisions: Iterable[tuple[Hashable, Any]],
    recompute: Callable[[Hashable], Any],
    abstention_blocks: bool = False,
) -> ReplayOutcome:
    """Compare one rule against the project's own recorded human decisions.

    ``human_decisions`` pairs each answer-key case with what the person decided;
    ``recompute`` says what the current rule would do for that case, or returns
    :data:`ABSTAINED` when it declines to answer. Every answer-key case counts,
    including an abstained one: the case is real history whether or not the rule
    reaches it, and dropping it would let a rule that abstains on everything
    report zero cases and fail for the wrong reason.
    """

    contradictions: list[Any] = []
    abstentions: list[Any] = []
    case_count = 0
    for case_key, human_answer in human_decisions:
        case_count += 1
        answer = recompute(case_key)
        if answer is ABSTAINED:
            abstentions.append(case_key)
        elif answer != human_answer:
            contradictions.append(case_key)
    return ReplayOutcome(
        family=family,
        case_count=case_count,
        contradictions=tuple(sorted(contradictions)),
        abstentions=tuple(sorted(abstentions)),
        abstention_blocks=abstention_blocks,
    )


def ledger_families() -> tuple[str, ...]:
    """Every policy family that keeps activation history, from the mapping."""

    return tuple(
        sorted(
            identity
            for identity in PolicyActivation.__mapper__.polymorphic_map
            if identity != PolicyActivation.__mapper__.polymorphic_identity
        )
    )


def ledger_model(family: str) -> type[PolicyActivation]:
    """The typed view onto ``policy_activations`` for one policy family."""

    mapper = PolicyActivation.__mapper__.polymorphic_map.get(family)
    if mapper is None or family == PolicyActivation.__mapper__.polymorphic_identity:
        raise ReplayGateRefusal(f"{family!r} keeps no activation ledger")
    return mapper.class_


def latest_ledger_entry(
    session: Session, *, family: str, project_id: int
) -> PolicyActivation | None:
    """The newest activation or suspension recorded for one family."""

    model = ledger_model(family)
    return session.scalars(
        select(model)
        .where(model.project_id == project_id)
        .order_by(model.id.desc())
        .limit(1)
    ).first()


def activation_status(
    session: Session,
    *,
    family: str,
    project_id: int,
    fingerprint: RuleFingerprint,
) -> str:
    """``inactive`` | ``active`` | ``suspended`` for the current fingerprint.

    A suspension is a standing human act and outranks any passing test. An
    activation recorded under a different fingerprint is void: the rule or its
    schema changed, so the pass no longer stands for what is deployed. A deploy
    that changes neither leaves it active.
    """

    newest = latest_ledger_entry(session, family=family, project_id=project_id)
    return _status_of(newest, fingerprint)


def family_statuses(
    session: Session,
    project_id: int,
    fingerprints: Mapping[str, RuleFingerprint],
) -> dict[str, str]:
    """One read of the ledger for every family named, in one statement.

    A screen or an operator report that wants the whole automatic surface for a
    project asks once instead of once per family.
    """

    families = tuple(fingerprints)
    for family in families:
        ledger_model(family)
    newest: dict[str, PolicyActivation] = {}
    if families:
        rows = session.scalars(
            select(PolicyActivation)
            .where(
                PolicyActivation.project_id == project_id,
                PolicyActivation.family.in_(families),
            )
            .order_by(PolicyActivation.family, PolicyActivation.id.desc())
            .distinct(PolicyActivation.family)
        ).all()
        newest = {row.family: row for row in rows}
    return {
        family: _status_of(newest.get(family), fingerprint)
        for family, fingerprint in fingerprints.items()
    }


def _status_of(
    newest: PolicyActivation | None, fingerprint: RuleFingerprint
) -> str:
    if newest is None:
        return INACTIVE
    if newest.action == SUSPEND:
        return SUSPENDED
    if (newest.policy_version, newest.policy_sha256) == (
        fingerprint.policy_version,
        fingerprint.policy_sha256,
    ):
        return ACTIVE
    # The rule or its schema changed since this activation — the pass is void.
    return INACTIVE


def record_activation(
    session: Session,
    *,
    family: str,
    project_id: int,
    fingerprint: RuleFingerprint,
    replay_case_count: int | None,
    reason: str,
    recorded_by: str,
    acceptance_receipt_id: int | None = None,
) -> PolicyActivation:
    """Append the activation a passing replay earned, under its fingerprint."""

    return _append(
        session,
        family=family,
        project_id=project_id,
        action=ACTIVATE,
        fingerprint=fingerprint,
        replay_case_count=replay_case_count,
        reason=reason,
        recorded_by=recorded_by,
        acceptance_receipt_id=acceptance_receipt_id,
    )


def record_suspension(
    session: Session,
    *,
    family: str,
    project_id: int,
    fingerprint: RuleFingerprint,
    reason: str,
    recorded_by: str,
    acceptance_receipt_id: int | None = None,
) -> PolicyActivation:
    """Append a human suspension; it beats every passing test until lifted.

    A suspension proves nothing, so it carries no replay case count.
    """

    return _append(
        session,
        family=family,
        project_id=project_id,
        action=SUSPEND,
        fingerprint=fingerprint,
        replay_case_count=None,
        reason=reason,
        recorded_by=recorded_by,
        acceptance_receipt_id=acceptance_receipt_id,
    )


def _append(
    session: Session,
    *,
    family: str,
    project_id: int,
    action: str,
    fingerprint: RuleFingerprint,
    replay_case_count: int | None,
    reason: str,
    recorded_by: str,
    acceptance_receipt_id: int | None,
) -> PolicyActivation:
    model = ledger_model(family)
    if not reason or not reason.strip():
        raise ReplayGateRefusal("an activation ledger entry must state a reason")
    if not recorded_by or not recorded_by.strip():
        raise ReplayGateRefusal("an activation ledger entry names who recorded it")
    entry = model(
        project_id=project_id,
        action=action,
        policy_version=fingerprint.policy_version,
        policy_sha256=fingerprint.policy_sha256,
        replay_case_count=replay_case_count,
        reason=reason.strip(),
        recorded_by=recorded_by.strip(),
        acceptance_receipt_id=acceptance_receipt_id,
    )
    session.add(entry)
    session.flush([entry])
    return entry
