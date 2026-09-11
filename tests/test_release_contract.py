"""A release refuses a stack that does not carry what it reads off it.

The workflow runs these before it configures AWS credentials, beside the other
two release helpers, so a contract that cannot resolve stops the release while
it is still harmless.
"""

from __future__ import annotations

import pytest

from scripts.release_contract import (
    RELEASE_STACK_OUTPUTS,
    UNSET_OUTPUT_VALUE,
    ReleaseContractError,
    resolve,
)


def _outputs(**overrides: str | None) -> list[dict[str, str]]:
    values = {key: f"value-for-{key}" for key in RELEASE_STACK_OUTPUTS}
    values.update(overrides)
    return [
        {"OutputKey": key, "OutputValue": value}
        for key, value in values.items()
        if value is not None
    ]


def test_every_declared_output_becomes_the_assignment_the_release_reads():
    assignments = dict(line.split("=", 1) for line in resolve(_outputs()))
    assert assignments == {
        name: f"value-for-{key}" for key, name in RELEASE_STACK_OUTPUTS.items()
    }


@pytest.mark.parametrize("value", [None, "", UNSET_OUTPUT_VALUE])
def test_an_absent_or_unset_output_refuses_the_release(value):
    with pytest.raises(ReleaseContractError, match="ApplicationUrl"):
        resolve(_outputs(ApplicationUrl=value))


def test_every_missing_output_is_named_at_once():
    """The release is about to spend an environment approval; one round trip
    should say everything that is wrong with the stack."""
    with pytest.raises(ReleaseContractError) as refusal:
        resolve(_outputs(RepositoryUri=None, WebServiceName=""))
    assert "RepositoryUri" in str(refusal.value)
    assert "WebServiceName" in str(refusal.value)
