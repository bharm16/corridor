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
rules, the reason vocabulary, the receipt tables — and which modules each
family pins. This module owns the two digests and the resolution of a
pinned name to deployed bytes, and nothing else, so it can never become
the place where a policy's *decisions* quietly converge.

The resolver arrived after #797 turned `corridor.models` into a package.
Five families pinned the schema through `models_module.__file__`, which
from that day named `models/__init__.py`, the re-export list; every
column, CHECK and relationship in the submodules had left every rules
digest, and no test noticed because each family resolved its own paths
and nothing asserted what a pin covered. One resolver, one test.
"""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Callable, Iterable
from functools import lru_cache
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit, digests
from corridor.models import PolicyApproval, Project
from corridor.principals import HumanPrincipal

__all__ = [
    "canonical_json",
    "canonical_sha256",
    "current_approval",
    "digest_of_sources",
    "has_matching_abstention",
    "pinned_sources",
    "record_approval",
    "source_digest",
]

# Alembic does not load this directory; its files retain the exact bytes
# released policy fingerprints were computed over (migrations/versions/README.md).
_RETAINED_MIGRATIONS = Path(__file__).parent / "migrations" / "versions"
_RETAINED_MIGRATION_PREFIX = "corridor.migrations."


def canonical_json(value: object) -> str:
    """This module's historical name for the one canonical encoding.

    The encoding itself moved to `corridor.digests` once the same three-way
    disagreement this module resolved was found fifty more times across the
    codebase. The names stay here because authorized-policy callers read
    them as policy vocabulary; `str` rather than `bytes` is likewise the
    shape those callers already hold.
    """
    return digests.canonical_json(value).decode()


def canonical_sha256(value: object) -> str:
    """The digest of a policy, a configuration, or a set of fields."""
    return digests.canonical_sha256(value)


def has_matching_abstention(
    outcomes: Iterable[object],
    *,
    input_receipt: dict,
    verdict: str,
    reason_version: str,
) -> bool:
    """Whether immutable history already carries this exact versioned verdict."""
    return any(
        isinstance(getattr(outcome, "eligibility_json", None), dict)
        and outcome.eligibility_json.get("input") == input_receipt
        and outcome.eligibility_json.get("verdict") == verdict
        and outcome.eligibility_json.get("reason_version") == reason_version
        for outcome in outcomes
    )


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


def pinned_sources(*names: str) -> tuple[tuple[str, bytes], ...]:
    """The deployed bytes behind each name a policy family pins.

    A family declares *which* modules decide for it; this resolves *what
    bytes* that declaration names, once for every family:

    - a module name resolves to its deployed file;
    - a package resolves to its own file and then to every member module,
      each under its dotted name, in sorted order, so splitting a module
      into a package cannot narrow a digest;
    - `corridor.migrations.<revision>` resolves to the inert source retained
      under `migrations/versions` for a released fingerprint;
    - a name that resolves to nothing is refused here, not at first use.

    Every family's `rules_digest` changed once when this replaced the six
    hand-kept path lists: #797 had already changed them by turning
    `corridor.models` into a package, leaving each `models_module.__file__`
    pin on `models/__init__.py` alone. The digests written between #797
    and this change attested less than they said; these attest the schema.
    """
    sources: list[tuple[str, bytes]] = []
    for name in names:
        sources.extend(_resolve_pin(name))
    return tuple(sources)


def _resolve_pin(name: str) -> list[tuple[str, bytes]]:
    if name.startswith(_RETAINED_MIGRATION_PREFIX):
        revision = name.removeprefix(_RETAINED_MIGRATION_PREFIX)
        retained = sorted(_RETAINED_MIGRATIONS.glob(f"{revision}_*.py"))
        if len(retained) == 1:
            return [(name, retained[0].read_bytes())]
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        raise LookupError(
            f"policy pin {name!r} names no deployed module and no retained migration"
        ) from exc
    if getattr(module, "__file__", None) is None:
        raise LookupError(f"policy pin {name!r} has no deployed file to digest")
    path = Path(module.__file__)
    sources = [(name, path.read_bytes())]
    if path.name == "__init__.py":
        sources.extend(_package_members(name, path.parent))
    return sources


def _package_members(package: str, directory: Path) -> list[tuple[str, bytes]]:
    members: list[tuple[str, bytes]] = []
    for entry in sorted(directory.iterdir()):
        if entry.is_dir() and (entry / "__init__.py").is_file():
            members.append((f"{package}.{entry.name}", (entry / "__init__.py").read_bytes()))
            members.extend(_package_members(f"{package}.{entry.name}", entry))
        elif entry.suffix == ".py" and entry.name != "__init__.py":
            members.append((f"{package}.{entry.stem}", entry.read_bytes()))
    return members


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
    family: str,
    *,
    project_id: int,
    policy_version: str,
    policy_json: dict,
    principal: HumanPrincipal,
    action: str,
    also_recorded: dict | None = None,
) -> PolicyApproval:
    """Append one human authorization of a policy version, and audit it.

    Authorization covers the rules rather than the rows, so what is stored
    is the policy itself and its digest; `also_recorded` carries the one
    or two facts a family needs beside them, such as which documents an
    agreement policy named.

    The caller validates and takes the project lock first: what counts as
    a valid policy differs per family, and the lock belongs with the
    family's own reads.
    """
    approval = PolicyApproval(
        project_id=project_id,
        family=family,
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
    family: str,
    *,
    project_id: int,
    policy_version: str,
    recompute: Callable[[Project, PolicyApproval], dict],
) -> PolicyApproval | None:
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
        select(PolicyApproval)
        .where(
            PolicyApproval.project_id == project_id,
            PolicyApproval.family == family,
        )
        .order_by(PolicyApproval.id.desc())
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
