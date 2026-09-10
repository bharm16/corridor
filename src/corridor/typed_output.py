"""Turn provider JSON into strict typed models before factual use.

Model-backed modules previously accepted generic dictionaries and each rebuilt
its own partial shape checks.  That let coercion, undeclared fields, and factual
reference checks differ by caller.  This module owns the common boundary:
strict schema generation, strict local parsing, and typed factual validators.
It also owns `ClosedModel`, the one declaration of the closed-and-frozen model
configuration that the measurement contracts, the pipeline contracts and the
sealed render and token identities each used to repeat for themselves.
It has no database dependency and grants no write authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError


class TypedOutputValidationError(ValueError):
    """A provider result failed its declared or factual local contract."""


class ClosedModel(BaseModel):
    """Base for a shape whose fields are exactly the declared ones, and fixed.

    Six modules declared this same configuration for themselves - the two
    evaluation contracts, the pipeline contracts, the render profiles, the
    token layers and the reader page inventory - because a receipt, a frozen
    experiment input and a sealed identity all need the same two guarantees:
    an undeclared field is an error rather than a silent extra, and a value
    cannot change after the shape carrying it was built. Only the declaration
    is shared; what a subclass means is still its own module's business.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class StrictOutputModel(ClosedModel):
    """A closed shape that also rejects coercion: the provider boundary's rule.

    Strictness belongs to a value that arrived from a model, not to every
    declared shape, so it is added here rather than in `ClosedModel`.
    """

    model_config = ConfigDict(strict=True)


OutputT = TypeVar("OutputT", bound=StrictOutputModel)
FactualCheck = Callable[[OutputT], None]


def strict_output_schema(model_type: type[OutputT]) -> dict:
    """Return the exact JSON schema passed to the strict provider call."""

    if not issubclass(model_type, StrictOutputModel):
        raise TypeError("typed output schemas require StrictOutputModel")
    return model_type.model_json_schema()


def validate_typed_output(
    model_type: type[OutputT],
    raw: Mapping[str, object] | object,
    *,
    factual_checks: Sequence[FactualCheck[OutputT]] = (),
) -> OutputT:
    """Parse once, then run contextual checks against the typed result."""

    try:
        # Validate with JSON semantics because provider arrays are JSON arrays;
        # Pydantic may then freeze them as tuples without permitting scalar
        # coercion such as a string into an integer.
        output = model_type.model_validate_json(
            json.dumps(raw, ensure_ascii=False), strict=True
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise TypedOutputValidationError(
            f"model output violates the strict contract: {exc}"
        ) from exc
    for check in factual_checks:
        try:
            check(output)
        except TypedOutputValidationError:
            raise
        except (TypeError, ValueError) as exc:
            raise TypedOutputValidationError(str(exc)) from exc
    return output
