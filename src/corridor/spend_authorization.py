"""One immutable, attributable authorization to spend model budget (#811).

Five model-assistance families — the Coordination Summary, the production-run
explanation, the extraction-failure diagnosis, the revision-change
explanation and the source-intake draft — each opened with the same rule: no
supported default spends money, an attributable declaration names every bound
first, and a changed bound is a new declaration rather than an edit.  Each of
them then wrote that rule out in full: the same nine checks in five
``declare_configuration`` functions, and the same nine CHECK constraints on
five tables.  Audit card C03 counted the copies; this module is where the
rule lives once.

``declare_spend_authorization`` performs the nine checks — the model is
named; the input, output and time limits sit inside their ranges; exactly one
request and no automatic retry; the retention class is ``class_b_30_days``;
the observation context is the internal working view; the actor is a typed
human principal — and one more that only exists because the rule is shared:
the declaration names exactly one permitted operation.  An authorization for
an intake draft does not authorize a Coordination Summary, and the database
says so through the composite foreign key every family configuration carries
(``models/assistance.py``).  Two declarations whose limits happen to match are
two rows; nothing here looks for an existing row to reuse, because merging two
independently declared acts would attribute one person's declaration to the
other.

What was considered and not built: a generic AI-request table (the families
recorded why that failed once already, and only the authorization was ever
common); a wallet or budget account with a balance (a declaration permits one
bounded request under stated limits, it does not hold funds); and a reusable
authorization shared across operations (the scope is the whole point).  The
family modules keep their own prompt check, their request tables, receipts,
retention and validation, and call this once before writing their own row.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from corridor.models import SPEND_OPERATIONS, SpendAuthorization
from corridor.principals import HumanPrincipal, require_human_principal


class InvalidSpendAuthorization(ValueError):
    """A declaration would permit an ambiguous, unbounded or unscoped spend."""


RETRY_POLICY = "none"
RETENTION_POLICY = "class_b_30_days"
OBSERVATION_CONTEXT = "internal_working_view"


def declare_spend_authorization(
    session: Session,
    *,
    project_id: int,
    principal: HumanPrincipal,
    operation: str,
    model: str,
    max_input_tokens: int,
    max_output_tokens: int,
    timeout_seconds: int,
    max_requests: int,
    retry_policy: str,
    retention_policy: str,
    observation_context: str,
) -> SpendAuthorization:
    """Append the one complete declaration required before any model spend.

    There is intentionally no environment, credential, or previous-declaration
    fallback, and no search for an equal row: a declaration is one person's
    act for one project and one operation, and the row is that act.
    """
    require_human_principal(principal)
    if operation not in SPEND_OPERATIONS:
        raise InvalidSpendAuthorization(
            "a spend authorization names exactly one permitted operation"
        )
    text_values = {
        "model": model,
        "retry_policy": retry_policy,
        "retention_policy": retention_policy,
        "observation_context": observation_context,
    }
    if any(
        not isinstance(value, str) or not value.strip()
        for value in text_values.values()
    ):
        raise InvalidSpendAuthorization(
            "every spend authorization field must be declared"
        )
    if retry_policy != RETRY_POLICY or max_requests != 1:
        raise InvalidSpendAuthorization(
            "a spend authorization permits one request and no automatic retry"
        )
    if retention_policy != RETENTION_POLICY:
        raise InvalidSpendAuthorization(
            "retention must be declared as class_b_30_days"
        )
    if observation_context != OBSERVATION_CONTEXT:
        raise InvalidSpendAuthorization(
            "observation context must be internal_working_view"
        )
    if not 1 <= max_input_tokens <= 200_000:
        raise InvalidSpendAuthorization(
            "input budget must be between 1 and 200000 tokens"
        )
    if not 1 <= max_output_tokens <= 20_000:
        raise InvalidSpendAuthorization(
            "output budget must be between 1 and 20000 tokens"
        )
    if not 1 <= timeout_seconds <= 600:
        raise InvalidSpendAuthorization(
            "time budget must be between 1 and 600 seconds"
        )
    authorization = SpendAuthorization(
        project_id=project_id,
        operation=operation,
        model=model.strip(),
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
        max_requests=max_requests,
        retry_policy=retry_policy,
        retention_policy=retention_policy,
        observation_context=observation_context,
        declared_by=principal.subject,
    )
    session.add(authorization)
    session.flush()
    return authorization
