"""The one translator from a SECURITY DEFINER command's refusal to a refusal kind.

``fact_decisions`` and ``support_assessments`` each matched the substring
``"predecessor is stale"`` at their own catch site, so a reworded RAISE in
either command would have turned a STALE refusal into a generic DBAPIError
with nothing failing first.  These tests need no database: the plpgsql source
is the authority for the sentence, and the translator is a pure function.
"""

from __future__ import annotations

from pathlib import Path

from corridor.refusals import (
    STALE,
    STALE_PREDECESSOR_SENTENCES,
    database_refusal_kind,
)


# The live definition of each command: the file Alembic executes and the
# header the function body follows.  ``migrations/versions`` is inert source
# and is deliberately not read here.
COMMAND_SOURCES = {
    "record_human_fact_decision": (
        "src/corridor/migrations/baseline_versions/a1c4e7b0d2f3_consolidated_schema.sql",
        "CREATE FUNCTION public.record_human_fact_decision(",
    ),
    "append_support_assessment": (
        "src/corridor/migrations/baseline_versions/b2d5f8a1c4e7_source_append_commands.py",
        "create function public.append_support_assessment(",
    ),
}


class _Wrapped(Exception):
    """A DBAPIError stand-in: the driver's message sits on ``orig``."""

    def __init__(self, orig: str):
        super().__init__("(psycopg) wrapped")
        self.orig = Exception(orig)


def _command_body(command: str) -> str:
    path, header = COMMAND_SOURCES[command]
    source = Path(path).read_text(encoding="utf-8")
    start = source.index(header)
    end = source.index("$$;", start)
    return source[start:end]


def test_every_declared_stale_sentence_is_the_one_its_command_raises() -> None:
    """Read from the migration source, not retyped: a reworded RAISE fails here."""

    assert set(STALE_PREDECESSOR_SENTENCES) == set(COMMAND_SOURCES)
    for command, sentence in STALE_PREDECESSOR_SENTENCES.items():
        body = _command_body(command)
        raised = [
            line.strip()
            for line in body.splitlines()
            if "raise exception" in line and "predecessor is stale" in line
        ]
        assert raised == [f"raise exception '{sentence}'"], command


def test_a_stale_predecessor_refusal_translates_to_the_stale_kind() -> None:
    for sentence in STALE_PREDECESSOR_SENTENCES.values():
        assert database_refusal_kind(_Wrapped(sentence)) == STALE
        # The driver prefixes and suffixes the sentence; the kind survives that.
        assert (
            database_refusal_kind(_Wrapped(f"ERROR:  {sentence}\nCONTEXT: ..."))
            == STALE
        )


def test_an_unrecognized_command_refusal_has_no_kind() -> None:
    assert database_refusal_kind(_Wrapped("deadlock detected")) is None
    assert (
        database_refusal_kind(
            _Wrapped("Human Record Decision key is bound to different content")
        )
        is None
    )
    # An exception without ``orig`` is read as itself.
    assert database_refusal_kind(Exception("nothing to translate")) is None
