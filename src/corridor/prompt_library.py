"""One resolved identity for every prompt this product installs.

A prompt's interface is four things a receipt has to agree on: the file, the
version string the receipt records, the exact bytes, and the output schema
those bytes promise.  Twelve modules used to restate the first two
independently -- under two constant names and three path conventions -- and
nothing held the version to the file it named.  The drift that predicts
reached a receipt: ``evidence_investigator_runtime`` declared
``evidence-investigator-v3`` for the bytes of
``prompts/evidence_investigator_v2.md`` while its own error message, four
retained cohort artifacts and the shadow-cohort schema version all called
those bytes v2.

Three of those modules resolved their file against the *process working
directory* while ``extractor_lineage`` joined the same constant against the
checkout, so one constant named two files in one run.  From any directory but
the repository root the run failed closed on "runtime prompt bytes do not
match their extractor seal" -- an error naming bytes rather than the working
directory that caused it.  The fix had already been made twice elsewhere, each
time with a test named after it; concentrating resolution here is what stops
it coming back a fourth time.

The path is derived from the version rather than declared beside it, so the
two cannot disagree.  A hyphen in a version becomes an underscore in the file
name: a version string is persisted evidence and cannot be respelled, and
these file stems have always been spelled with underscores.

The twelfth module is ``corridor_pdf_reader.replacement.semantics``, and it is
where this rule was taken from -- it already derived its path from its version,
which is why it is the one that never drifted.  Its prompt stays inside that
package beside the reader it serves, so it keeps its own one-line loader rather
than reaching across into this directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from corridor import digests


PROMPTS = Path(__file__).resolve().parents[2] / "prompts"


@dataclass(frozen=True)
class Prompt:
    """One installed prompt file, resolved from the checkout and read once."""

    version: str
    path: Path
    data: bytes
    sha256: str
    schema: Mapping[str, Any] | None

    @property
    def text(self) -> str:
        """The system message sent to the model."""
        return self.data.decode("utf-8")


def installed_prompt(
    version: str, *, schema: Mapping[str, Any] | None = None
) -> Prompt:
    """Load the installed prompt one version names, with its digest and schema."""
    path = PROMPTS / f"{version.replace('-', '_')}.md"
    data = path.read_bytes()
    return Prompt(
        version=version,
        path=path,
        data=data,
        sha256=digests.sha256_bytes(data),
        schema=schema,
    )


def require_installed_prompt(
    prompt_version: str,
    *,
    installed: Prompt,
    error: type[ValueError],
    family: str,
) -> None:
    """Refuse a configuration that does not name this family's installed prompt.

    The five model-assistance families each raise their own configuration
    error, so the caller passes the type it owns; the rule and its sentence
    live here once.
    """
    if prompt_version != installed.version:
        raise error(f"the configured prompt is not the installed {family} prompt")
