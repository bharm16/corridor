"""One strict local validation layer for every typed model output."""

import pytest

from corridor.typed_output import (
    StrictOutputModel,
    TypedOutputValidationError,
    strict_output_schema,
    validate_typed_output,
)


class ExampleOutput(StrictOutputModel):
    reference_id: int
    value: str


def test_strict_output_rejects_extra_fields_and_scalar_coercion():
    with pytest.raises(TypedOutputValidationError, match="strict contract"):
        validate_typed_output(
            ExampleOutput,
            {"reference_id": "7", "value": "exact", "extra": True},
        )


def test_strict_output_runs_field_level_factual_checks_on_typed_models():
    def known_reference(output: ExampleOutput) -> None:
        if output.reference_id != 7:
            raise ValueError("reference is outside the prepared input")

    with pytest.raises(TypedOutputValidationError, match="outside the prepared input"):
        validate_typed_output(
            ExampleOutput,
            {"reference_id": 8, "value": "exact"},
            factual_checks=(known_reference,),
        )

    output = validate_typed_output(
        ExampleOutput,
        {"reference_id": 7, "value": "exact"},
        factual_checks=(known_reference,),
    )
    assert output == ExampleOutput(reference_id=7, value="exact")


def test_strict_schema_forbids_undeclared_properties_at_the_provider_boundary():
    schema = strict_output_schema(ExampleOutput)

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["reference_id", "value"]
