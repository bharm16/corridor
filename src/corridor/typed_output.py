"""Turn provider JSON into strict typed models before factual use.

Model-backed modules previously accepted generic dictionaries and each rebuilt
its own partial shape checks.  That let coercion, undeclared fields, and factual
reference checks differ by caller.  This module owns the common boundary:
strict schema generation, strict local parsing, and typed factual validators.
It has no database dependency and grants no write authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError


class TypedOutputValidationError(ValueError):
    """A provider result failed its declared or factual local contract."""


class StrictOutputModel(BaseModel):
    """Base for model outputs that reject coercion and undeclared fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


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
