"""Stable human identity at the Ledger write boundary.

M8 does not build authentication.  It does stop treating a role label such
as ``reviewer`` as a person.  The web ingress resolves one configured,
namespaced subject into this value and the domain accepts only the value --
never caller-supplied free text -- for Admission.

The namespace is deliberately authentication-provider agnostic.  ``local``
is enough for the single-user development deployment; M9 can supply an OIDC
subject without changing the adjudication interface or rewriting history.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

_NAMESPACE = re.compile(r"^[a-z][a-z0-9._-]{1,31}$")
_RESERVED_IDENTIFIERS = frozenset(
    {"agent", "demo", "extractor", "reviewer", "system"}
)


class InvalidHumanPrincipal(ValueError):
    """The supplied value is not an attributable, stable human subject."""


@dataclass(frozen=True, slots=True)
class HumanPrincipal:
    """A stable ``namespace:subject`` identifying one human reviewer."""

    subject: str

    def __post_init__(self) -> None:
        if not isinstance(self.subject, str):
            raise InvalidHumanPrincipal("human principal must be a string subject")
        if self.subject != self.subject.strip() or any(
            character.isspace() for character in self.subject
        ):
            raise InvalidHumanPrincipal(
                "human principal must not contain surrounding or embedded whitespace"
            )
        namespace, separator, identifier = self.subject.partition(":")
        if not separator or not _NAMESPACE.fullmatch(namespace) or not identifier:
            raise InvalidHumanPrincipal(
                "human principal must be a namespaced subject such as "
                "'local:alice' or 'oidc:00u123'"
            )
        if identifier.casefold() in _RESERVED_IDENTIFIERS:
            raise InvalidHumanPrincipal(
                f"{identifier!r} is a role or system label, not a human principal"
            )


def require_human_principal(value: object) -> HumanPrincipal:
    """Fail before a Ledger write when identity did not cross the typed seam."""
    if not isinstance(value, HumanPrincipal):
        raise InvalidHumanPrincipal(
            "Admission requires a HumanPrincipal; free-text actor labels are refused"
        )
    # Construction validates the subject, but rechecking through the public
    # constructor keeps the boundary honest if an object was deserialized or
    # manufactured without running ``__post_init__``.
    HumanPrincipal(value.subject)
    return value
