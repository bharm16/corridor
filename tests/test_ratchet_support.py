"""A ratchet that cannot see direction is a record of the last measurement."""

from __future__ import annotations

from dataclasses import replace
import subprocess

import pytest

from ratchet_support import (
    Relocation,
    assert_ratchet,
    assert_reviewed_relocations,
    recorded_at_merge_base,
)


def _repository(tmp_path, recorded: str):
    root = tmp_path / "repository"
    root.mkdir()
    guard = root / "guard.py"
    guard.write_text(f"REASON = 'one seam'\nRECORDED = {recorded}\n", encoding="utf-8")
    run = lambda *args: subprocess.run(("git", *args), cwd=root, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "guard@example.invalid")
    run("config", "user.name", "Guard")
    run("add", "guard.py")
    run("commit", "-q", "-m", "record the baseline")
    run("checkout", "-q", "-b", "change")
    return root, guard, run


def test_a_count_may_fall_and_may_never_rise_against_the_merge_base(tmp_path, monkeypatch):
    root, guard, _ = _repository(tmp_path, "3")
    monkeypatch.setenv("CORRIDOR_RATCHET_BASE_REF", "main")
    monkeypatch.chdir(root)
    name = "guard.py:RECORDED"

    assert recorded_at_merge_base(name, repository=root) == 3
    assert_ratchet(name, measured=3, recorded=3, repository=root)

    guard.write_text("REASON = 'one seam'\nRECORDED = 2\n", encoding="utf-8")
    assert_ratchet(name, measured=2, recorded=2, repository=root)

    guard.write_text("REASON = 'one seam'\nRECORDED = 4\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="rose to 4 from the 3 recorded at"):
        assert_ratchet(name, measured=4, recorded=4, repository=root)


def test_a_name_may_leave_and_may_never_join(tmp_path, monkeypatch):
    """The unit is the name, so the failure says which one joined."""
    root, guard, _ = _repository(tmp_path, "(('reader', 'legacy', REASON),)")
    monkeypatch.setenv("CORRIDOR_RATCHET_BASE_REF", "main")
    monkeypatch.chdir(root)
    name = "guard.py:RECORDED"
    edges = lambda value: {(source, target) for source, target, _ in value}

    assert recorded_at_merge_base(name, repository=root) == (("reader", "legacy", "one seam"),)
    assert_ratchet(
        name, measured={("reader", "legacy")}, recorded={("reader", "legacy")},
        as_measured=edges, repository=root,
    )
    assert_ratchet(name, measured=set(), recorded=set(), as_measured=edges, repository=root)

    with pytest.raises(AssertionError, match="joined since the merge base: .*second"):
        assert_ratchet(
            name,
            measured={("reader", "legacy"), ("second", "legacy")},
            recorded={("reader", "legacy"), ("second", "legacy")},
            as_measured=edges,
            repository=root,
        )


def test_the_working_tree_check_still_runs_when_no_base_ref_resolves(tmp_path, monkeypatch):
    """Local runs and shallow CI clones lose the history, not the guard."""
    root, guard, _ = _repository(tmp_path, "3")
    monkeypatch.setenv("CORRIDOR_RATCHET_BASE_REF", "no-such-branch")
    monkeypatch.chdir(root)
    name = "guard.py:RECORDED"

    assert recorded_at_merge_base(name, repository=root) is None
    assert_ratchet(name, measured=9, recorded=9, repository=root)
    with pytest.raises(AssertionError, match="measures 9 against the 3 recorded"):
        assert_ratchet(name, measured=9, recorded=3, repository=root)


def test_a_constant_absent_from_the_merge_base_leaves_the_direction_unjudged(tmp_path, monkeypatch):
    """A ratchet introduced by this very change has no baseline to fall from."""
    root, guard, _ = _repository(tmp_path, "3")
    monkeypatch.setenv("CORRIDOR_RATCHET_BASE_REF", "main")
    monkeypatch.chdir(root)

    guard.write_text("REASON = 'one seam'\nRECORDED = 3\nADDED = 12\n", encoding="utf-8")

    assert recorded_at_merge_base("guard.py:ADDED", repository=root) is None
    assert recorded_at_merge_base("guard.py:MISSING", repository=root) is None
    assert_ratchet("guard.py:ADDED", measured=12, recorded=12, repository=root)


# The mechanism's subject in miniature. `statement_screen` reads two legacy
# classes; `other_screen` reads a third and one of those two, and goes on doing
# so, because a relocation says nothing about the rest of the module it left.
_APP_BEFORE = '''from corridor.models import Candidate, CommitmentLineage, Dependency


def statement_screen(session, project):
    return session.query(Candidate).filter(Dependency.project == project).all()


def other_screen(session):
    return session.query(CommitmentLineage).join(Dependency).all()
'''

_APP_AFTER = '''from corridor.models import CommitmentLineage, Dependency
from corridor.web.statement_view import statement_reading


def statement_screen(session, project):
    return statement_reading(session, project)


def other_screen(session):
    return session.query(CommitmentLineage).join(Dependency).all()
'''

_VIEW = '''from corridor.models import Candidate, Dependency


def statement_reading(session, project):
    return session.query(Candidate).filter(Dependency.project == project).all()


def other_reading(rows):
    return [row.label for row in rows]
'''

# A stub that delegates its whole implementation and goes on typing what it is
# handed. The reading left; the signature did not, and a signature never read
# anything.
_APP_DELEGATING = '''from corridor.models import Candidate, CommitmentLineage, Dependency
from corridor.web.statement_view import statement_reading


def statement_screen(session, project, candidate: Candidate) -> list[Dependency]:
    return statement_reading(session, project, candidate)


def other_screen(session):
    return session.query(CommitmentLineage).join(Dependency).all()
'''

# The undeclared half of a two-extraction change: `other_screen` left too, and
# nobody said so.
_APP_TRADED = '''from corridor.web.statement_view import statement_reading


def statement_screen(session, project):
    return statement_reading(session, project)
'''

_EXPORT = '''from corridor.models import CommitmentLineage, Dependency


def other_screen(session):
    return session.query(CommitmentLineage).join(Dependency).all()
'''

_WIDER_VIEW = '''from corridor.models import Candidate, CommitmentLineage, Dependency


def statement_reading(session, project):
    session.query(CommitmentLineage).all()
    return session.query(Candidate).filter(Dependency.project == project).all()
'''

_BEFORE = {
    "Candidate": ("web.app",),
    "CommitmentLineage": ("web.app",),
    "Dependency": ("web.app",),
}

_AFTER = {
    "Candidate": ("web.statement_view",),
    "CommitmentLineage": ("web.app",),
    "Dependency": ("web.app", "web.statement_view"),
}

_MOVED = Relocation(
    source="web.app",
    source_reading="statement_screen",
    destination="web.statement_view",
    destination_reading="statement_reading",
    models=("Candidate", "Dependency"),
    card="#857",
)


def _write(root, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _census(consumers) -> str:
    return "CONSUMERS = {\n" + "".join(
        f"    {name!r}: {tuple(modules)!r},\n"
        for name, modules in sorted(consumers.items())
    ) + "}\n"


def _extraction(tmp_path, monkeypatch):
    """A repository whose merge base holds the screen's reading in `web.app`."""
    root = tmp_path / "repository"
    root.mkdir()
    _write(root, {"census.py": _census(_BEFORE), "src/web/app.py": _APP_BEFORE})
    _run(root, "init", "-q", "-b", "main")
    _run(root, "config", "user.email", "guard@example.invalid")
    _run(root, "config", "user.name", "Guard")
    _commit(root, "one screen, one module")
    _run(root, "checkout", "-q", "-b", "change")
    monkeypatch.setenv("CORRIDOR_RATCHET_BASE_REF", "main")
    return root


def _run(root, *arguments):
    subprocess.run(("git", *arguments), cwd=root, check=True, capture_output=True)


def _commit(root, message):
    _run(root, "add", "-A")
    _run(root, "commit", "-q", "-m", message)


def _moved(root, consumers, *, app=_APP_AFTER, view=_VIEW):
    """Put the extraction in the working tree, with the census it produces."""
    _write(root, {
        "census.py": _census(consumers),
        "src/web/app.py": app,
        "src/web/statement_view.py": view,
    })


def _census_holds(root, consumers, relocations):
    """The legacy-consumer call site, in miniature."""
    pairs = lambda listed: {
        (name, module) for name, modules in listed.items() for module in modules
    }
    relocated = assert_reviewed_relocations(
        relocations, consumers=consumers, source_root="src", repository=root
    )
    assert_ratchet(
        "census.py:CONSUMERS",
        measured=pairs(consumers) - relocated,
        recorded=pairs(consumers) - relocated,
        as_measured=pairs,
        repository=root,
    )


def test_a_reviewed_relocation_is_how_an_existing_reading_changes_module(tmp_path, monkeypatch):
    """The case the mechanism exists for, and the only thing it lets through.

    `web.statement_view` joins the census and the repository depends on nothing
    it did not depend on before. `web.app` keeps `CommitmentLineage` and its own
    `Dependency` reading, which this rule never asked about.
    """
    root = _extraction(tmp_path, monkeypatch)
    _moved(root, _AFTER)

    _census_holds(root, _AFTER, (_MOVED,))


def test_a_delegating_stub_may_go_on_typing_what_it_is_handed(tmp_path, monkeypatch):
    """"Gone or delegating" means the reading, not the signature.

    `web.app` keeps consuming both classes here and the census keeps saying so;
    what it no longer does is read them in the implementation that moved.
    """
    root = _extraction(tmp_path, monkeypatch)
    consumers = {
        "Candidate": ("web.app", "web.statement_view"),
        "CommitmentLineage": ("web.app",),
        "Dependency": ("web.app", "web.statement_view"),
    }
    _moved(root, consumers, app=_APP_DELEGATING)

    _census_holds(root, consumers, (_MOVED,))


def test_the_same_extraction_without_a_declaration_is_a_new_consumer(tmp_path, monkeypatch):
    """Recording the destination in the census buys nothing; the base still says no."""
    root = _extraction(tmp_path, monkeypatch)
    _moved(root, _AFTER)

    with pytest.raises(AssertionError, match="joined since the merge base: .*statement_view"):
        _census_holds(root, _AFTER, ())


def test_a_destination_may_not_take_a_class_the_declaration_never_named(tmp_path, monkeypatch):
    """The approved extraction is the destination's whole legacy surface."""
    root = _extraction(tmp_path, monkeypatch)
    consumers = {**_AFTER, "CommitmentLineage": ("web.app", "web.statement_view")}
    _moved(root, consumers, view=_WIDER_VIEW)

    with pytest.raises(AssertionError, match="whole legacy surface"):
        _census_holds(root, consumers, (_MOVED,))


def test_a_declaration_may_not_claim_a_dependency_that_reading_never_had(tmp_path, monkeypatch):
    """`other_screen` read `CommitmentLineage`; the reading that moved did not."""
    root = _extraction(tmp_path, monkeypatch)
    _moved(root, _AFTER)
    overclaimed = replace(_MOVED, models=("Candidate", "CommitmentLineage", "Dependency"))

    with pytest.raises(
        AssertionError,
        match="at the merge base web.app.statement_screen does not name CommitmentLineage",
    ):
        _census_holds(root, _AFTER, (overclaimed,))


def test_a_reading_left_executing_in_both_places_is_a_new_consumer(tmp_path, monkeypatch):
    """A copy is not a move, however faithfully the destination reproduces it."""
    root = _extraction(tmp_path, monkeypatch)
    consumers = {**_AFTER, "Candidate": ("web.app", "web.statement_view")}
    _moved(root, consumers, app=_APP_BEFORE)

    with pytest.raises(AssertionError, match="still reads Candidate, Dependency"):
        _census_holds(root, consumers, (_MOVED,))


def test_an_unrelated_consumer_is_not_paid_for_by_an_unrelated_removal(tmp_path, monkeypatch):
    """The unit is the name, and an approved move authorizes its own pairs only.

    Two readings leave `web.app` here and one of them is declared. The census
    is no longer than it was, which buys nothing: `web.export` joined, nobody
    proved it was a move, and the failure says so by name.
    """
    root = _extraction(tmp_path, monkeypatch)
    traded = {
        "Candidate": ("web.statement_view",),
        "CommitmentLineage": ("web.export",),
        "Dependency": ("web.export", "web.statement_view"),
    }
    _moved(root, traded, app=_APP_TRADED)
    _write(root, {"src/web/export.py": _EXPORT})

    with pytest.raises(AssertionError, match="joined since the merge base: .*web.export"):
        _census_holds(root, traded, (_MOVED,))


def test_one_reading_moves_once_and_a_spent_declaration_is_not_reusable(tmp_path, monkeypatch):
    """Two destinations claiming one extraction is two consumers for one removal."""
    root = _extraction(tmp_path, monkeypatch)
    consumers = {
        "Candidate": ("web.statement_panel", "web.statement_view"),
        "CommitmentLineage": ("web.app",),
        "Dependency": ("web.app", "web.statement_panel", "web.statement_view"),
    }
    _moved(root, consumers)
    _write(root, {"src/web/statement_panel.py": _VIEW})
    reused = replace(_MOVED, destination="web.statement_panel")

    with pytest.raises(AssertionError, match="already declared relocated"):
        _census_holds(root, consumers, (_MOVED, reused))


def test_a_relocation_that_has_landed_is_spent_and_its_declaration_has_to_go(tmp_path, monkeypatch):
    """The permission does not survive the merge that used it.

    Once the extraction is what the merge base holds, the source reading no
    longer carries the dependency, the evidence stops existing, and the
    destination is an ordinary consumer the census counts like any other.
    """
    root = _extraction(tmp_path, monkeypatch)
    _moved(root, _AFTER)
    _commit(root, "extract the screen's reading")
    _run(root, "branch", "-qf", "main", "HEAD")

    _census_holds(root, _AFTER, ())
    with pytest.raises(AssertionError, match="already landed is spent"):
        _census_holds(root, _AFTER, (_MOVED,))


def test_a_declaration_names_the_implementation_on_each_end(tmp_path, monkeypatch):
    """A module name is not a relocation; the two readings are what moved."""
    root = _extraction(tmp_path, monkeypatch)
    _moved(root, _AFTER)

    with pytest.raises(AssertionError, match="exactly one statement_row at the merge base"):
        _census_holds(root, _AFTER, (replace(_MOVED, source_reading="statement_row"),))
    with pytest.raises(AssertionError, match="exactly one statement_panel"):
        _census_holds(root, _AFTER, (replace(_MOVED, destination_reading="statement_panel"),))
    with pytest.raises(AssertionError, match="has to land in the implementation"):
        _census_holds(root, _AFTER, (replace(_MOVED, destination_reading="other_reading"),))


def test_a_declaration_says_what_moved_and_where_the_move_is_explained(tmp_path, monkeypatch):
    """Unreviewed, unsorted or invented: three ways a line is not a declaration."""
    root = _extraction(tmp_path, monkeypatch)
    _moved(root, _AFTER)

    with pytest.raises(AssertionError, match="name the card or pull request"):
        _census_holds(root, _AFTER, (replace(_MOVED, card="a refactor"),))
    with pytest.raises(AssertionError, match="sorted and once"):
        _census_holds(root, _AFTER, (replace(_MOVED, models=("Dependency", "Candidate")),))
    with pytest.raises(AssertionError, match="not a legacy class this census tracks"):
        _census_holds(root, _AFTER, (replace(_MOVED, models=("Candidate", "Invented")),))


def test_without_a_base_ref_there_is_nothing_to_authorize(tmp_path, monkeypatch):
    """Losing the history loses the direction check, so it authorizes nothing too."""
    root = _extraction(tmp_path, monkeypatch)
    _moved(root, _AFTER)
    monkeypatch.setenv("CORRIDOR_RATCHET_BASE_REF", "no-such-branch")

    unsupportable = replace(_MOVED, source_reading="never_existed")
    assert assert_reviewed_relocations(
        (unsupportable,), consumers=_AFTER, source_root="src", repository=root
    ) == frozenset()
