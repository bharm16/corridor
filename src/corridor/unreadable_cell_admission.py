"""Gate and drive corroborated cross-document admission of unreadable cells.

A cell value the reading harness resolved as *corroborated* is proven by one
thing only: the value is literal text on a readable source, and code — never
model agreement — verifies that source's citation (ADR-0064, ADR-0042). Letting
such a value be *admitted* (record-contributing) automatically is an expansion of
automatic Record Inclusion behavior, so ADR-0050 governs it exactly as it governs
the schedule-link rule (#337) and the unknown-scope Commitment class (#370/#371):
the class may not auto-admit until a regression replay of the project's own
recorded human cell-value decisions passes with at least one real case and no
contradiction. It ships inactive. A seeded case where a person decided a value
contrary to what the harness would admit makes the replay fail, mechanically, and
so blocks activation.

Two automatic behaviors live here and must not be confused:

- **Auto-upgrade** turns an *unconfirmed* reading into *corroborated* the moment a
  corroborating document arrives. That is the same code-verified proof as a
  first-pass corroboration, carries no new authority, and is always on — it is
  the living-document machinery ADR-0064 requires (no ceremony, no human step),
  wired through ``load_project``.
- **Admission** turns a *corroborated* value into *admitted*. That expands
  automatic record behavior and is gated by the ADR-0050 replay above.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import policy, replay_gate
from corridor.models import (
    Document,
    DocPage,
    UnreadableCellAdmissionActivation,
    UnreadableCellResolution,
)
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal, require_human_principal
from corridor.project_lock import lock_project
from corridor.verify import literal_quote_on_page

POLICY_VERSION = "unreadable-cell-corroborated-admission-v1"
ACTIVATION_ACTOR = "corridor:unreadable-cell-admission"


def canonical_policy() -> dict:
    """The fingerprinted rule: what admits, and how corroboration is proven."""
    return {
        "policy_version": POLICY_VERSION,
        "admit_predicate": "one-distinct-value-literal-on-a-readable-source-v1",
        "corroboration_check": "verify.literal_quote_on_page+value_appears_on-v1",
        "model_agreement_is_a_predicate": False,
    }


def policy_fingerprint() -> tuple[str, str]:
    """The current (version, sha256) the admission rule runs under."""
    return POLICY_VERSION, policy.canonical_sha256(canonical_policy())


# --------------------------------------------------------------------------- #
# Reading current cell state                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Cell:
    document_id: int
    page_no: int
    cell_key: str


def _human_decisions(
    session: Session, project_id: int
) -> dict[_Cell, UnreadableCellResolution]:
    rows = session.scalars(
        select(UnreadableCellResolution)
        .where(
            UnreadableCellResolution.project_id == project_id,
            UnreadableCellResolution.origin == "human_decision",
        )
        .order_by(UnreadableCellResolution.id)
    ).all()
    latest: dict[_Cell, UnreadableCellResolution] = {}
    for row in rows:
        latest[_Cell(row.document_id, row.page_no, row.cell_key)] = row
    return latest


def _machine_latest(
    session: Session, project_id: int
) -> dict[_Cell, UnreadableCellResolution]:
    rows = session.scalars(
        select(UnreadableCellResolution)
        .where(
            UnreadableCellResolution.project_id == project_id,
            UnreadableCellResolution.origin != "human_decision",
        )
        .order_by(UnreadableCellResolution.id)
    ).all()
    latest: dict[_Cell, UnreadableCellResolution] = {}
    for row in rows:
        latest[_Cell(row.document_id, row.page_no, row.cell_key)] = row
    return latest


# --------------------------------------------------------------------------- #
# Corroboration search (shared by the harness upgrade and the replay)          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CorroborationHit:
    document_id: int
    page_no: int
    quote: str


def find_corroboration(
    session: Session,
    project_id: int,
    value: str,
    *,
    exclude_document_id: int,
    exclude_page_no: int | None = None,
) -> CorroborationHit | None:
    """Whether the value is literal text on some readable source right now.

    Searches current registered pages in the project (excluding the cell's own
    scan page and superseded documents) for a readable source that literally
    contains the value. This is the mechanical corroboration predicate — a real
    document-supplied value, never model agreement.
    """
    if not value or not value.strip():
        return None
    rows = session.execute(
        select(Document.id, DocPage.page_no, DocPage.text)
        .join(DocPage, DocPage.document_id == Document.id)
        .where(
            Document.project_id == project_id,
            Document.superseded_by.is_(None),
        )
        .order_by(Document.id, DocPage.page_no)
    ).all()
    for document_id, page_no, text in rows:
        if document_id == exclude_document_id and (
            exclude_page_no is None or page_no == exclude_page_no
        ):
            continue
        page_text = text or ""
        if not page_text.strip():
            continue
        # Strict literal match only: the value's exact characters must be on the
        # readable page (never a token-overlap heuristic that a short value could
        # satisfy vacuously). This is the mechanical, document-supplied proof.
        literal = literal_quote_on_page(value, page_text)
        if literal is not None:
            return CorroborationHit(document_id, page_no, _window(page_text, literal))
    return None


def _window(text: str, literal: str, *, radius: int = 40) -> str:
    index = text.find(literal)
    if index < 0:
        return literal
    start = max(0, index - radius)
    end = min(len(text), index + len(literal) + radius)
    return text[start:end].strip() or literal


# --------------------------------------------------------------------------- #
# Auto-upgrade: unconfirmed -> corroborated when a document arrives             #
# --------------------------------------------------------------------------- #


def reconsider_unconfirmed_cell_readings(
    session: Session, project_id: int
) -> tuple[UnreadableCellResolution, ...]:
    """Upgrade every unconfirmed reading that now has corroboration.

    Wired through ``load_project`` so it runs automatically the moment a
    corroborating document lands (ADR-0064's living-document upgrade). No human
    step, no ceremony. Idempotent: an upgraded cell's latest state is no longer
    unconfirmed, so a repeat pass finds nothing.

    Observation versus resolution (#809): the upgrade row is bound to the same
    observation as the unconfirmed row it upgrades, never to the cell key
    alone. A new provider observation of a cell whose earlier observation was
    corroborated appends a new unconfirmed row bound to its own observation;
    that row earns its own corroboration here, and the earlier corroboration
    stays where it was, bound to the observation it was checked against.
    """
    machine = _machine_latest(session, project_id)
    unconfirmed = [
        (cell, row)
        for cell, row in machine.items()
        if row.state == "unconfirmed" and row.value
    ]
    if not unconfirmed:
        return ()
    upgraded: list[UnreadableCellResolution] = []
    for cell, row in unconfirmed:
        hit = find_corroboration(
            session,
            project_id,
            row.value,
            exclude_document_id=cell.document_id,
            exclude_page_no=cell.page_no,
        )
        if hit is None:
            continue
        upgrade = UnreadableCellResolution(
            project_id=project_id,
            document_id=cell.document_id,
            page_no=cell.page_no,
            cell_key=cell.cell_key,
            state="corroborated",
            value=row.value,
            run_id=row.run_id,
            # The corroboration is of *this* observation's value (#809): the
            # upgrade carries the reading's observation and region forward, so
            # a later observation of the same cell key cannot inherit it.
            observation_id=row.observation_id,
            source_region_id=row.source_region_id,
            corroboration_document_id=hit.document_id,
            corroboration_page_no=hit.page_no,
            corroboration_quote=hit.quote,
            origin="corroboration_upgrade",
        )
        session.add(upgrade)
        upgraded.append(upgrade)
    session.flush()
    return tuple(upgraded)


# --------------------------------------------------------------------------- #
# The human answer key (contrary-decision seeder)                              #
# --------------------------------------------------------------------------- #


def record_human_cell_value(
    session: Session,
    *,
    project_id: int,
    document_id: int,
    page_no: int,
    cell_key: str,
    value: str | None,
    principal: HumanPrincipal,
) -> UnreadableCellResolution:
    """Record one person's decision about a cell value — the ADR-0050 answer key.

    This is a coordination decision a person makes in the ordinary flow, not a
    transcription-review card. It is what the regression replay compares the rule
    against: a value here that contradicts what the rule would admit fails the
    replay and blocks activation.
    """
    recorder = _human_actor(principal)
    resolved = value.strip() if isinstance(value, str) and value.strip() else None
    row = UnreadableCellResolution(
        project_id=project_id,
        document_id=document_id,
        page_no=page_no,
        cell_key=cell_key,
        state="corroborated" if resolved else "absent",
        value=resolved,
        origin="human_decision",
        recorded_by=recorder.subject,
    )
    session.add(row)
    session.flush()
    return row


# --------------------------------------------------------------------------- #
# The deterministic admission rule                                             #
# --------------------------------------------------------------------------- #


def _rule_would_admit(
    session: Session,
    project_id: int,
    cell: _Cell,
    resolution: UnreadableCellResolution,
) -> str | None:
    """The value the current rule would admit for this cell, or None to abstain.

    Admits only a corroborated value whose citation still verifies against the
    live readable source. Anything else — an unconfirmed reading, an absent cell,
    or a corroboration that no longer holds — abstains.
    """
    if resolution.state not in ("corroborated", "admitted"):
        return None
    if not resolution.value:
        return None
    hit = find_corroboration(
        session,
        project_id,
        resolution.value,
        exclude_document_id=cell.document_id,
        exclude_page_no=cell.page_no,
    )
    if hit is None:
        return None
    return resolution.value


def admit_corroborated_cell_values(
    session: Session, project_id: int
) -> tuple[UnreadableCellResolution, ...]:
    """Admit corroborated values — only while the class is active for the project.

    Inactive (the default) is a visible no-op: corroborated values stay recorded
    with their citation but never become record-contributing automatically. Active
    (after a passing ADR-0050 replay) appends one ``admitted`` resolution per
    corroborated cell whose citation still verifies and which no person decided
    contrary to. Idempotent, and it never overrides a human's contrary decision.
    """
    if not is_corroborated_admission_active(session, project_id):
        return ()
    lock_project(session, project_id)
    machine = _machine_latest(session, project_id)
    human = _human_decisions(session, project_id)
    version, sha256 = policy_fingerprint()
    admitted: list[UnreadableCellResolution] = []
    for cell, resolution in machine.items():
        if resolution.state != "corroborated":
            continue
        admit_value = _rule_would_admit(session, project_id, cell, resolution)
        if admit_value is None:
            continue
        decision = human.get(cell)
        if decision is not None and (decision.value or None) != admit_value:
            # A person decided otherwise; never write over them.
            continue
        row = UnreadableCellResolution(
            project_id=project_id,
            document_id=cell.document_id,
            page_no=cell.page_no,
            cell_key=cell.cell_key,
            state="admitted",
            value=admit_value,
            run_id=resolution.run_id,
            observation_id=resolution.observation_id,
            source_region_id=resolution.source_region_id,
            corroboration_document_id=resolution.corroboration_document_id,
            corroboration_page_no=resolution.corroboration_page_no,
            corroboration_quote=resolution.corroboration_quote,
            origin="admission",
            policy_version=version,
            policy_sha256=sha256,
        )
        session.add(row)
        admitted.append(row)
    session.flush()
    return tuple(admitted)


# --------------------------------------------------------------------------- #
# ADR-0050 regression replay + activation (through the shared gate)            #
# --------------------------------------------------------------------------- #
#
# ``corridor.replay_gate`` owns the comparison, the pass rule and the one
# activation ledger. This family contributes only which cell values a person
# decided, and what the admission rule would admit for one of them.


def _fingerprint() -> replay_gate.RuleFingerprint:
    version, sha256 = policy_fingerprint()
    return replay_gate.RuleFingerprint(version, sha256)


def replay_matches_human_decisions(
    session: Session, project_id: int
) -> replay_gate.ReplayOutcome:
    """Replay the admission rule against every recorded human cell-value decision.

    The cases a person decided are the answer key. For each cell a person
    decided, recompute what the current rule would admit from the saved harness
    outputs — never re-reading the source scan. A contradiction is the rule
    admitting a *different* non-null value than the person recorded; the rule
    abstaining is not a contradiction.
    """
    human = _human_decisions(session, project_id)
    machine = _machine_latest(session, project_id)

    def recompute(key: tuple[int, int, str]) -> object:
        cell = _Cell(*key)
        resolution = machine.get(cell)
        if resolution is None:
            return replay_gate.ABSTAINED
        admit_value = _rule_would_admit(session, project_id, cell, resolution)
        if admit_value is None:
            return replay_gate.ABSTAINED
        return admit_value

    return replay_gate.replay(
        family=replay_gate.FAMILY_UNREADABLE_CELL_ADMISSION,
        human_decisions=[
            ((cell.document_id, cell.page_no, cell.cell_key), decision.value or None)
            for cell, decision in human.items()
        ],
        recompute=recompute,
    )


def activation_status(session: Session, project_id: int) -> str:
    """`inactive` | `active` | `suspended` for the current rule fingerprint."""
    return replay_gate.activation_status(
        session,
        family=replay_gate.FAMILY_UNREADABLE_CELL_ADMISSION,
        project_id=project_id,
        fingerprint=_fingerprint(),
    )


def is_corroborated_admission_active(session: Session, project_id: int) -> bool:
    return activation_status(session, project_id) == replay_gate.ACTIVE


def attempt_activation(
    session: Session, project_id: int
) -> UnreadableCellAdmissionActivation | None:
    """Activate the admission class iff its replay passes and no human suspended it.

    A deliberate human suspension beats every passing test, so an already-suspended
    project stays suspended until a person lifts it (ADR-0050).
    """
    if activation_status(session, project_id) != replay_gate.INACTIVE:
        return None
    replay = replay_matches_human_decisions(session, project_id)
    if not replay.passed:
        return None
    lock_project(session, project_id)
    return replay_gate.record_activation(
        session,
        family=replay_gate.FAMILY_UNREADABLE_CELL_ADMISSION,
        project_id=project_id,
        fingerprint=_fingerprint(),
        replay_case_count=replay.case_count,
        reason="regression replay passed on recorded human cell-value decisions",
        recorded_by=ACTIVATION_ACTOR,
    )


def suspend_corroborated_admission(
    session: Session,
    *,
    project_id: int,
    reason: str,
    principal: HumanPrincipal,
) -> UnreadableCellAdmissionActivation:
    """Append a human suspension; corroborated values stop auto-admitting at once."""
    if not reason.strip():
        raise ValueError("a suspension requires a reason")
    recorder = _human_actor(principal)
    lock_project(session, project_id)
    return replay_gate.record_suspension(
        session,
        family=replay_gate.FAMILY_UNREADABLE_CELL_ADMISSION,
        project_id=project_id,
        fingerprint=_fingerprint(),
        reason=reason,
        recorded_by=recorder.subject,
    )


def _human_actor(principal: object) -> HumanPrincipal:
    principal = require_human_principal(principal)
    if principal.subject.partition(":")[0] == "corridor":
        raise InvalidHumanPrincipal(
            "a human principal cannot use the corridor machine namespace"
        )
    return principal


def process_unreadable_cell_upgrades(
    session: Session, project_id: int
) -> tuple[UnreadableCellResolution, ...]:
    """One idempotent landing pass: upgrade readings, then admit if the class is active.

    Called from ``load_project`` when a project's documents land. A cheap no-op
    when the project has no unreadable-cell readings. Upgrading unconfirmed
    readings is always safe; admission respects the ADR-0050 gate and is a no-op
    while the class is inactive.
    """
    if not _has_any_resolution(session, project_id):
        return ()
    upgraded = reconsider_unconfirmed_cell_readings(session, project_id)
    admitted = admit_corroborated_cell_values(session, project_id)
    return upgraded + admitted


def _has_any_resolution(session: Session, project_id: int) -> bool:
    return session.scalar(
        select(UnreadableCellResolution.id)
        .where(UnreadableCellResolution.project_id == project_id)
        .limit(1)
    ) is not None
