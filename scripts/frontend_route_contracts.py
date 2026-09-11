"""Guard and document the one frontend receipt route-contract registry.

``corridor.frontend_request_receipts.ROUTE_CONTRACTS`` is the closed vocabulary
of routes that may carry a Product Proving receipt.  ``record_frontend_request``
raises ``ValueError("frontend request route identity is invalid")`` when a
route's name is not in it, so a handler that writes a receipt under an
unregistered name **500s on every request**, and nothing was reading the call
sites: ``tests/test_architecture.py`` checked only that every *contract* named a
served route.  #837 built two correspondence routes that both would have 500'd
on first use and found out from a sibling lane's red CI.

The other half of the same afternoon was a second, hand-authored copy of the
registry inside ``product_proving_frontend_capture.py``, which raised at import
when the two disagreed.  That copy is gone (#909): one registry, and the
readable documentation is generated from it into
``docs/operations/frontend-request-route-contracts.md``.

So this module holds both directions of the one relation:

* ``receipt_route_names`` reads the route names the application actually writes
  receipts under.  It resolves a name through the helper it was handed to --
  ``_project_workflow_response`` takes ``route_name`` as a parameter and seven
  callers pass their own -- because a scan for literal ``route_name=`` in
  ``web/app.py`` would have covered neither that helper's default nor a call
  site in some future module.  A name it *cannot* determine statically is a
  problem it reports, never a call site it quietly drops: an unresolved
  registration is exactly the case where the 500 would still be waiting.
* ``render`` produces the documentation, naming each route's served path and
  method beside the statuses its receipt may carry, so the page describes the
  surface rather than reprinting a dict.

Usage: ``uv run python scripts/frontend_route_contracts.py`` rewrites the
document; ``--check`` exits 1 when the registry, the call sites and the
document do not agree.  ``make check`` runs the same assertions through
``tests/test_architecture.py``.
"""

from __future__ import annotations

import argparse
import ast
import sys
from functools import lru_cache
from collections.abc import Iterable, Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src" / "corridor"
DOCUMENT_PATH = REPO_ROOT / "docs" / "operations" / "frontend-request-route-contracts.md"

RECEIPT_MODULE = "corridor.frontend_request_receipts"
RECEIPT_WRITER = "record_frontend_request"
ROUTE_NAME_ARGUMENT = "route_name"


def _modules(source_root: Path = SOURCE_ROOT) -> dict[Path, tuple[str, ast.Module]]:
    """Every application module, its source and its tree.

    Alembic revisions serve no request, so they are not read. The source text
    is kept because a module that never spells ``record_frontend_request`` --
    which is every module but one today, and 363 of 364 files -- cannot call
    it under any alias, so there is nothing in it for the walks below to find.
    """

    return {
        path: (text, ast.parse(text))
        for path in sorted(source_root.rglob("*.py"))
        if "migrations" not in path.relative_to(source_root).parts
        for text in (path.read_text(encoding="utf-8"),)
    }


def _calls(node: ast.AST, enclosing: ast.AST | None, found: list) -> None:
    """Collect ``(enclosing function, call)`` for every call under ``node``."""

    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _calls(child, child, found)
            continue
        if isinstance(child, ast.Call):
            found.append((enclosing, child))
        _calls(child, enclosing, found)


def _called_name(call: ast.Call) -> str | None:
    """The bare name a call names, through an attribute or not."""

    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _writer_bindings(tree: ast.Module) -> set[str]:
    """The local names this module binds to the receipt writer, aliases included."""

    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
            RECEIPT_MODULE.rsplit(".", 1)[-1]
        ):
            bound.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == RECEIPT_WRITER
            )
    return bound


def _parameter(function, name: str) -> tuple[int | None, ast.AST | None, bool]:
    """Where ``name`` sits in a signature: position, default, and whether found.

    The position is ``None`` for a keyword-only parameter, which is how
    ``record_frontend_request`` and its helpers declare ``route_name``.
    """

    positional = list(function.args.posonlyargs) + list(function.args.args)
    for index, argument in enumerate(positional):
        if argument.arg == name:
            offset = len(positional) - len(function.args.defaults)
            default = (
                function.args.defaults[index - offset] if index >= offset else None
            )
            return index, default, True
    for argument, default in zip(function.args.kwonlyargs, function.args.kw_defaults):
        if argument.arg == name:
            return None, default, True
    return None, None, False


def _argument(call: ast.Call, name: str, position: int | None):
    """The node one call passes for a parameter: keyword, positional, or absent.

    Returns ``(node, spread)``: ``spread`` is true when the call could be
    supplying the parameter through ``**kwargs``, which is a value no reader
    here can determine.
    """

    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value, False
    spread = any(keyword.arg is None for keyword in call.keywords)
    if position is not None and position < len(call.args):
        argument = call.args[position]
        if not isinstance(argument, ast.Starred):
            return argument, spread
    return None, spread


class _Resolver:
    """Resolve every ``route_name`` the application writes a receipt under."""

    def __init__(self, modules: Mapping[Path, tuple[str, ast.Module]]) -> None:
        self.modules = modules
        self.problems: list[str] = []
        self.calls: dict[Path, list] = {}
        self.by_name: dict[str, list[tuple[Path, ast.AST | None, ast.Call]]] = {}
        for path, (_text, tree) in modules.items():
            found: list = []
            _calls(tree, None, found)
            self.calls[path] = found
            for enclosing, call in found:
                name = _called_name(call)
                if name is not None:
                    self.by_name.setdefault(name, []).append((path, enclosing, call))

    def _where(self, path: Path, node: ast.AST) -> str:
        return f"{path.relative_to(REPO_ROOT)}:{getattr(node, 'lineno', 0)}"

    def route_names(self) -> dict[str, list[str]]:
        """Each resolved route name, with every call site that writes it."""

        written: dict[str, list[str]] = {}
        for path, (text, tree) in self.modules.items():
            if RECEIPT_WRITER not in text:
                continue
            bound = _writer_bindings(tree)
            self._refuse_indirect_use(path, tree, bound)
            for enclosing, call in self.calls[path]:
                if not self._is_receipt_write(call, bound):
                    continue
                node, spread = _argument(call, ROUTE_NAME_ARGUMENT, None)
                where = self._where(path, call)
                if node is None:
                    self.problems.append(
                        f"{where}: {RECEIPT_WRITER} is called with no literal "
                        f"{ROUTE_NAME_ARGUMENT}"
                        + (" (it would come from **kwargs)" if spread else "")
                    )
                    continue
                for name in self._resolve(node, path, enclosing, frozenset()):
                    written.setdefault(name, []).append(where)
        return written

    def _is_receipt_write(self, call: ast.Call, bound: set[str]) -> bool:
        if isinstance(call.func, ast.Name):
            return call.func.id in bound
        return isinstance(call.func, ast.Attribute) and call.func.attr == RECEIPT_WRITER

    def _refuse_indirect_use(self, path: Path, tree: ast.Module, bound: set[str]) -> None:
        """A receipt writer passed around as a value has no readable contract."""

        called = {
            id(call.func)
            for _enclosing, call in self.calls[path]
            if isinstance(call.func, (ast.Name, ast.Attribute))
        }
        for node in ast.walk(tree):
            if id(node) in called:
                continue
            names = isinstance(node, ast.Name) and node.id in bound
            attribute = isinstance(node, ast.Attribute) and node.attr == RECEIPT_WRITER
            if (names or attribute) and isinstance(
                getattr(node, "ctx", None), ast.Load
            ):
                self.problems.append(
                    f"{self._where(path, node)}: {RECEIPT_WRITER} is used other "
                    "than as a direct call, so the route it writes cannot be read"
                )

    def _resolve(
        self, node, path: Path, enclosing, seen: frozenset
    ) -> set[str]:
        """The route names one ``route_name`` argument can carry."""

        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        where = self._where(path, node)
        if not isinstance(node, ast.Name) or enclosing is None:
            self.problems.append(
                f"{where}: {ROUTE_NAME_ARGUMENT} is not a literal and not a "
                "parameter this reader can follow"
            )
            return set()
        position, default, declared = _parameter(enclosing, node.id)
        if not declared:
            self.problems.append(
                f"{where}: {ROUTE_NAME_ARGUMENT} is {node.id!r}, which is not a "
                f"parameter of {enclosing.name}"
            )
            return set()
        key = (path, enclosing.name, node.id)
        if key in seen:
            return set()
        seen = seen | {key}
        return self._through_callers(enclosing, node.id, position, default, path, seen)

    def _through_callers(
        self, function, parameter: str, position, default, path: Path, seen: frozenset
    ) -> set[str]:
        """Every literal a parameter can hold: its default, and its callers'."""

        names: set[str] = set()
        if default is not None:
            if isinstance(default, ast.Constant) and isinstance(default.value, str):
                names.add(default.value)
            else:
                self.problems.append(
                    f"{self._where(path, default)}: the default for {parameter} on "
                    f"{function.name} is not a literal route name"
                )
        callers = self.by_name.get(function.name, [])
        for caller_path, caller_enclosing, call in callers:
            node, spread = _argument(call, parameter, position)
            if node is None:
                if default is None:
                    self.problems.append(
                        f"{self._where(caller_path, call)}: {function.name} is "
                        f"called without {parameter} and it has no default"
                    )
                elif spread:
                    self.problems.append(
                        f"{self._where(caller_path, call)}: {function.name} may "
                        f"take {parameter} from **kwargs, which is not readable"
                    )
                continue
            names |= self._resolve(node, caller_path, caller_enclosing, seen)
        if not callers and default is None:
            self.problems.append(
                f"{self._where(path, function)}: {function.name} takes {parameter} "
                "with no default and is called nowhere this reader can see"
            )
        return names


@lru_cache(maxsize=1)
def receipt_route_names(
    source_root: Path = SOURCE_ROOT,
) -> tuple[dict[str, list[str]], list[str]]:
    """The route names the application writes receipts under, and what is unreadable.

    The second element is the explicit-failure half of the contract: a call
    whose route name cannot be determined statically is reported, not skipped.

    Cached, and the result is read-only: three assertions in
    ``tests/test_architecture.py`` ask the same question of the same tree, and
    parsing 364 modules three times inside ``make check`` buys nothing.
    """

    resolver = _Resolver(_modules(source_root))
    written = resolver.route_names()
    return written, resolver.problems


def served_routes(names: Iterable[str]) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Each contract's served ``(path, method)``, and the ones serving none."""

    from corridor.frontend_request_receipts import served_route_identity
    from corridor.web.app import app

    served: dict[str, tuple[str, str]] = {}
    missing: list[str] = []
    for name in names:
        identity = served_route_identity(app.routes, name)
        if identity is None:
            missing.append(name)
        else:
            served[name] = identity
    return served, missing


def render(
    contracts: Mapping[str, frozenset[int]],
    served: Mapping[str, tuple[str, str]],
) -> str:
    """The registry as a page a person reads, in the router's own terms."""

    lines = [
        "# Frontend request route contracts",
        "",
        "Generated by `scripts/frontend_route_contracts.py` from",
        "`corridor.frontend_request_receipts.ROUTE_CONTRACTS`; do not edit by hand.",
        "`make route-contracts` regenerates it and `make check` fails when it is stale.",
        "",
        "One closed vocabulary of the routes that may carry a Product Proving",
        "receipt. `record_frontend_request` refuses a route name that is not here,",
        "and refuses a status the named route may not return; the template and the",
        "method are read from the route the router matched, never retyped beside a",
        "decorator that already declared them.",
        "",
        "A route that records a receipt without an entry below fails `make check`",
        "rather than the first request it receives (#909).",
        "",
        "| Route name | Method | Path | Statuses the receipt may carry |",
        "|---|---|---|---|",
    ]
    for name in sorted(contracts):
        template, method = served[name]
        statuses = ", ".join(str(status) for status in sorted(contracts[name]))
        lines.append(f"| `{name}` | {method} | `{template}` | {statuses} |")
    lines.append("")
    return "\n".join(lines)


def documentation() -> tuple[str, list[str]]:
    """The current document text, and every disagreement that forbids writing it."""

    from corridor.frontend_request_receipts import ROUTE_CONTRACTS

    written, problems = receipt_route_names()
    served, missing = served_routes(ROUTE_CONTRACTS)
    problems = list(problems)
    problems += [
        f"{name}: a contract naming no route the application serves with one "
        "GET or POST method"
        for name in sorted(missing)
    ]
    problems += [
        f"{name}: a route records a receipt with no contract, written at "
        + ", ".join(written[name])
        for name in sorted(set(written) - set(ROUTE_CONTRACTS))
    ]
    if problems:
        return "", problems
    return render(ROUTE_CONTRACTS, served), problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when the document is stale instead of rewriting it; this "
        "is what make check asserts, through tests/test_architecture.py",
    )
    arguments = parser.parse_args(argv)
    text, problems = documentation()
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    if arguments.check:
        current = (
            DOCUMENT_PATH.read_text(encoding="utf-8") if DOCUMENT_PATH.exists() else ""
        )
        if current != text:
            print(
                f"{DOCUMENT_PATH.relative_to(REPO_ROOT)} is stale; run "
                "`make route-contracts`.",
                file=sys.stderr,
            )
            return 1
        return 0
    DOCUMENT_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {DOCUMENT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
