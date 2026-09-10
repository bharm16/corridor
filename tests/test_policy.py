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
    models,
    organization_identity,
    policy,
    schedule_linking,
    statement_scope_matching,
)

FAMILY = (event_admission, dependency_admission, automatic_carry_forward)
EVERY_FAMILY = FAMILY + (
    organization_identity,
    schedule_linking,
    statement_scope_matching,
)
FAMILIES_PINNING_THE_SCHEMA = FAMILY + (organization_identity, schedule_linking)
# Read from the package directory rather than declared, so the next split of
# the schema is caught here instead of narrowing five digests silently.
SCHEMA_MEMBERS = {
    f"corridor.models.{path.stem}"
    for path in Path(models.__file__).parent.glob("*.py")
    if path.stem != "__init__"
}


def _rule_sources(module) -> tuple[tuple[str, bytes], ...]:
    if module is automatic_carry_forward:
        return module.AutomaticCarryForwardRuntime.deployed().safety_sources
    return module._rule_source_bytes()


def _rule_source_names(module) -> list[str]:
    return [name for name, _ in _rule_sources(module)]


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


@pytest.mark.parametrize(
    "module", EVERY_FAMILY, ids=lambda m: m.__name__.split(".")[-1]
)
def test_every_declared_pin_resolves_to_deployed_bytes(module):
    """A wrong path used to raise only at first use; the resolver refuses
    a name that resolves to nothing, so calling it is the proof."""
    sources = _rule_sources(module)

    assert sources
    assert all(body for _, body in sources)
    assert len({name for name, _ in sources}) == len(sources)


@pytest.mark.parametrize(
    "module", FAMILIES_PINNING_THE_SCHEMA, ids=lambda m: m.__name__.split(".")[-1]
)
def test_a_package_pin_covers_every_member_module(module):
    """#797 turned `corridor.models` into a package, and a pin through
    `models_module.__file__` then covered only `models/__init__.py`, the
    re-export list, while every column, CHECK and relationship lived in
    submodules outside the digest. A package pin resolves to its members."""
    names = set(_rule_source_names(module))

    assert len(SCHEMA_MEMBERS) > 1
    assert "corridor.models" in names
    assert SCHEMA_MEMBERS <= names


def test_a_pin_that_resolves_to_nothing_is_refused():
    with pytest.raises(LookupError):
        policy.pinned_sources("corridor.no_such_module")

    with pytest.raises(LookupError):
        policy.pinned_sources("corridor.migrations.000000000000")


def test_a_package_pin_lists_its_members_in_a_stable_order():
    names = [name for name, _ in policy.pinned_sources("corridor.models")]

    assert names[0] == "corridor.models"
    assert names[1:] == sorted(names[1:])
    assert set(names[1:]) == SCHEMA_MEMBERS


def test_an_inert_migration_pin_reads_the_retained_bytes():
    """The 13 files under `migrations/versions` are not loaded by Alembic;
    they exist only so released fingerprints keep their exact bytes."""
    [(name, body)] = policy.pinned_sources("corridor.migrations.e9a4b7c2d158")
    retained = (
        Path(policy.__file__).parent
        / "migrations/versions/e9a4b7c2d158_add_dependency_admission.py"
    )

    assert name == "corridor.migrations.e9a4b7c2d158"
    assert body == retained.read_bytes()
