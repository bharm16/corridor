"""The digests every authorized policy stands on (ADRs 0022, 0026, 0027).

Three families now enter records or move support under a named, versioned
policy that an accountable principal authorizes: Automatic Carry-Forward
(ADR-0022), event admission (ADR-0026) and dependency admission
(ADR-0027). ADR-0027 says outright that it mirrors the family it joined,
and the code mirrored it by copying: `_digest_of_sources` was byte-for-
byte identical in two modules, comment included, and a canonical-JSON
digest existed three times in three encodings.

Three encodings is not a tidiness complaint. Each module's docstring
calls its own function the canonical digest, and for any value carrying a
character outside ASCII they disagree — the same policy JSON hashing to
two different values depending on which module asked. The authorization
means "this exact rule set, these exact deployed bytes"; a digest that
depends on who computed it cannot carry that claim.

What stays with each family is what actually differs: the eligibility
rules, the reason vocabulary, the receipt tables. This module owns the
two digests and nothing else, so it can never become the place where a
policy's *decisions* quietly converge.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from functools import lru_cache
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.models import Project
from corridor.principals import HumanPrincipal

__all__ = [
    "canonical_json",
    "canonical_sha256",
    "current_approval",
    "digest_of_sources",
    "record_approval",
    "source_digest",
]

Approval = TypeVar("Approval")


def canonical_json(value: object) -> str:
    """One encoding, so one value has one digest.

    `ensure_ascii=False` because escaping is a rendering choice and must
    not change what a digest covers: an External Party's name is the same
    name whether or not its accented characters arrive as `\\uXXXX`.
    `allow_nan=False` because `NaN` and `Infinity` are not JSON, and a
    receipt that cannot be re-parsed cannot be re-verified — the strictest
    of the three encodings this replaces, and the only one that refuses.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_sha256(value: object) -> str:
    """The digest of a policy, a configuration, or a set of fields."""
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def source_digest(sources: Iterable[tuple[str, bytes]]) -> str:
    """The digest of the deployed bytes that decide something.

    ADR-0022's guarantee: a digest over stated configuration alone would
    let someone loosen a check — widen a date format, relax a party match
    — and keep running under an authorization nobody re-read. The name is
    fed in beside the bytes so that renaming a module is a change too.
    """
    digest = hashlib.sha256()
    for module_name, source_bytes in sources:
        digest.update(module_name.encode())
        digest.update(b"\0")
        digest.update(source_bytes)
        digest.update(b"\0")
    return digest.hexdigest()


@lru_cache(maxsize=8)
def digest_of_sources(source_fn) -> str:
    """`source_digest`, cached per source function.

    The deployed bytes cannot change within a process, and the queue page
    checks policy currency on every render. A code change is a new
    process — and the drift tests swap the source function, which is a
    new cache key.
    """
    return source_digest(source_fn())


def record_approval(
    session: Session,
    model: type[Approval],
    *,
    project_id: int,
    policy_version: str,
    policy_json: dict,
    principal: HumanPrincipal,
    action: str,
    also_recorded: dict | None = None,
) -> Approval:
    """Append one human authorization of a policy version, and audit it.

    Authorization covers the rules rather than the rows, so what is stored
    is the policy itself and its digest; `also_recorded` carries the one
    or two facts a family needs beside them, such as which documents an
    agreement policy named.

    The caller validates and takes the project lock first: what counts as
    a valid policy differs per family, and the lock belongs with the
    family's own reads.
    """
    approval = model(
        project_id=project_id,
        policy_version=policy_version,
        approved_by=principal.subject,
        policy_json=policy_json,
        policy_sha256=canonical_sha256(policy_json),
    )
    session.add(approval)
    session.flush([approval])
    audit.record(
        session,
        principal=principal,
        action=action,
        entity_type=audit.PROJECT,
        entity_id=project_id,
        after={
            "policy_approval_id": approval.id,
            "policy_version": approval.policy_version,
            "policy_sha256": approval.policy_sha256,
            **(also_recorded or {}),
        },
    )
    return approval


def current_approval(
    session: Session,
    model: type[Approval],
    *,
    project_id: int,
    policy_version: str,
    recompute: Callable[[Project, Approval], dict],
) -> Approval | None:
    """The newest approval, and only if it still describes what would run.

    A policy that changed without a new authorization is not authorized:
    the approval names the version and the digest it approved, and both
    must still describe what would run today. `recompute` rebuilds the
    policy from what is deployed and stored right now — raising
    `ValueError` if it no longer can, which is itself an answer of no.

    Newest-matching rather than an explicit pointer, which is what both
    admission families were written to do. Automatic Carry-Forward keeps
    its own pointer and its own audit re-validation, so it does not call
    this: that difference is a decision, not drift.
    """
    approval = session.scalars(
        select(model)
        .where(model.project_id == project_id)
        .order_by(model.id.desc())
        .limit(1)
    ).first()
    if approval is None:
        return None
    if approval.policy_version != policy_version:
        return None
    project = session.get(Project, project_id)
    if project is None:
        return None
    try:
        fresh = recompute(project, approval)
    except ValueError:
        return None
    if approval.policy_sha256 != canonical_sha256(fresh):
        return None
    return approval
