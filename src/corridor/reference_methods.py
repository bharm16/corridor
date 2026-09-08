"""Declared machine-reference methods, independent of authoring engines.

The evaluator formerly knew one global parser name and limitation. A second
method must not relabel old CSVs or require an old engine merely to load an
archived enumeration. This module holds only immutable method contracts and
validation of their recorded provenance; actual reading lives elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

SCOPE_SCHEMA = "corridor.machine-reference-scope.v2"
NATIVE_AUTHORING_SCHEMA = "corridor.native-reference-authoring.v1"


@dataclass(frozen=True)
class ReferenceMethod:
    name: str
    version: str
    limitations: tuple[str, ...]


LEGACY_METHOD = ReferenceMethod(
    "pymupdf-table-grid",
    "1",
    (
        "Semi-independent ceiling: the machine reference and extractor share "
        "PyMuPDF table detection, so a region omitted by that library is invisible "
        "to both.",
    ),
)
NATIVE_METHOD = ReferenceMethod(
    "native-pdf-cell-grid",
    "1",
    (
        "Semi-independent ceiling: native reference authoring and the native "
        "extractor share the paired-rendition reader and table reconstruction, "
        "plus the WSDOT resolution and retirement vocabulary. Shared omissions "
        "and classification errors remain possible. This source-reference and "
        "critical-subset enumeration is not independent field or physical-occurrence gold. "
        "The recipe supports one eligible WSDOT grid per page and refuses multiple eligible grids.",
    ),
)
METHODS = (LEGACY_METHOD, NATIVE_METHOD)

# The scope's source bytes, not its current project name, identify this spent
# population. These are pinned against the unchanged scope file in tests.
SPENT_SOURCE_HASHES = frozenset(
    {
        "94fd41adde257c8a2948b529d2c8a0547021e578462241efa7325f523f8d6c27",
        "e1c7895b56021f86317efa8074aa6f298087d8f0bd4ac8652f17721f99bb0d3b",
        "1b949a1a1e26fcdb457df7ba265888d7bfe107b1d63c14ea0b1e56082620e228",
        "36acb221898e1a771f955a80e1136d3bd7117e1fd6fd4b36d542c84454bf3361",
        "3c53895423d57b61959b74d146cd0076d11245328c354540a1e509fc17cb6f1c",
        "d794938ff8c9c29004586a98ab5d7aeeb8904305527980c3dc5a74fede5b26db",
    }
)


def reference_method(name: str, version: str = "1") -> ReferenceMethod:
    for method in METHODS:
        if name == method.name and version == method.version:
            return method
    raise ValueError("machine-reference scope manifest has unsupported method/version")


def is_digest(value: object, length: int = 64) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None
    )


def validate_native_authoring(value: object, document_hashes: tuple[str, ...]) -> dict:
    """Validate archived identity as data, without importing a PDF engine.

    Historical engine builds and recipe digests need not equal the installed
    ones. Regeneration performs that comparison separately before replay.
    """
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "recipe_sha256",
        "rules_sha256",
        "readings",
    }:
        raise ValueError("native reference authoring provenance is incomplete")
    if (
        value["schema_version"] != NATIVE_AUTHORING_SCHEMA
        or not is_digest(value["recipe_sha256"])
        or not is_digest(value["rules_sha256"])
        or not isinstance(value["readings"], list)
    ):
        raise ValueError("native reference authoring provenance is invalid")
    found = []
    for item in value["readings"]:
        if not isinstance(item, dict) or set(item) != {
            "document_sha256",
            "reading_sha256",
            "reader_identity",
        }:
            raise ValueError("native reference reading identity is incomplete")
        if not is_digest(item["document_sha256"]) or not is_digest(
            item["reading_sha256"]
        ):
            raise ValueError("native reference reading digest is invalid")
        identity = item["reader_identity"]
        if (
            not isinstance(identity, dict)
            or identity.get("scheme") != "corridor.pdf-segments.v1"
        ):
            raise ValueError("native reference reader scheme is unsupported")
        native = identity.get("native_layer")
        if not isinstance(native, dict):
            raise ValueError("native reference reader identity is missing")
        configuration = native.get("configuration")
        if (
            native.get("engine") != "corridor-pdf-reader"
            or native.get("origin") != "native"
            or native.get("adapter_version") != "native-reader-v2"
            or type(native.get("dpi")) is not int
            or native["dpi"] != 36
            or not isinstance(native.get("engine_version"), str)
            or not native["engine_version"]
            or not isinstance(configuration, dict)
            or configuration.get("reader_engine") != "tagged"
            or not is_digest(configuration.get("source_commit"), 40)
            or not is_digest(identity.get("reader_runtime_sha256"))
            or not is_digest(identity.get("integration_sha256"))
            or not isinstance(identity.get("pypdf_version"), str)
            or not identity["pypdf_version"]
        ):
            raise ValueError("native reference reader configuration is unsupported")
        found.append(item["document_sha256"])
    if tuple(sorted(found)) != tuple(sorted(document_hashes)) or len(set(found)) != len(
        found
    ):
        raise ValueError("native reference reading scope differs from its document set")
    return value
