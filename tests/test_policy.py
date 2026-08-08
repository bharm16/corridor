"""The digests the authorized-policy families share (ADRs 0022, 0026, 0027).

These need no database: they are about the bytes a digest covers, which is
the whole content of an authorization.
"""

import math
import re
from pathlib import Path

import pytest

from corridor import (
    automatic_carry_forward,
    dependency_admission,
    event_admission,
    policy,
)

FAMILY = (event_admission, dependency_admission, automatic_carry_forward)


def _rule_source_names(module) -> list[str]:
    if module is automatic_carry_forward:
        runtime = module.AutomaticCarryForwardRuntime.deployed()
        return [name for name, _ in runtime.safety_sources]
    return [name for name, _ in module._rule_source_bytes()]


def test_the_digest_does_not_depend_on_who_computed_it():
    """The defect this module closed.

    Three families each called their own helper "the canonical digest" and
    the three encodings agreed only while every value stayed inside ASCII.
    An External Party's name is exactly the value that leaves it.
    """
    value = {"external_party": "Cañada Power & Light", "note": "sûreté"}

    assert policy.canonical_sha256(value) == policy.canonical_sha256(dict(value))
    # The accented characters are covered as themselves, not as escapes.
    assert "Cañada" in policy.canonical_json(value)
    assert "\\u" not in policy.canonical_json(value)


def test_a_receipt_that_could_not_be_reparsed_is_refused():
    """`NaN` is not JSON, and an unreadable receipt cannot be re-verified."""
    with pytest.raises(ValueError):
        policy.canonical_json({"ratio": math.nan})

    with pytest.raises(ValueError):
        policy.canonical_sha256({"ratio": math.inf})


def test_key_order_never_changes_a_digest():
    assert policy.canonical_sha256({"a": 1, "b": 2}) == policy.canonical_sha256(
        {"b": 2, "a": 1}
    )


def test_renaming_a_module_changes_the_source_digest():
    """The name is fed in beside the bytes, so a rename is a change."""
    body = b"# unchanged\n"

    assert policy.source_digest([("corridor.a", body)]) != policy.source_digest(
        [("corridor.b", body)]
    )


@pytest.mark.parametrize("module", FAMILY, ids=lambda m: m.__name__.split(".")[-1])
def test_the_code_that_computes_the_digest_is_under_the_digest(module):
    """The guarantee the extraction could have quietly removed.

    ADR-0022's authorization covers the deployed bytes of the deciding
    code. Moving the digest helpers into `corridor.policy` moved decision-
    relevant bytes out from under every family's rules digest unless the
    new module was named in each source list.
    """
    assert "corridor.policy" in _rule_source_names(module)


@pytest.mark.parametrize("module", FAMILY, ids=lambda m: m.__name__.split(".")[-1])
def test_no_family_member_keeps_a_private_canonical_digest(module):
    """The copies are gone, and a re-added copy fails here rather than in
    production, where it would show up as two digests for one policy."""
    source = Path(module.__file__).read_text()

    assert not re.search(r"def _json_sha256\b", source)
    assert not re.search(r"def _digest_of_sources\b", source)
    assert "hashlib.sha256(" not in source


def test_editing_a_check_still_moves_every_family_digest():
    """The pause-on-drift guarantee survives the shared module."""
    real = event_admission._rule_source_bytes

    def edited():
        return tuple((name, body + b"\n# widened\n") for name, body in real())

    assert policy.digest_of_sources(real) != policy.digest_of_sources(edited)
