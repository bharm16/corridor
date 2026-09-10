"""Run Corridor's tests in an environment where neither retired engine exists.

Why this is a script and not a pytest fixture: a fixture that stubs
`sys.modules["fitz"]` proves that one import site tolerates a stub, and
nothing about the environment the product actually ships.  ADR-0094 retires
PyMuPDF and Tesseract from the product, and #741's acceptance criterion is
that *the complete suite* passes with "PyMuPDF, its import aliases,
pytesseract and the Tesseract executable absent, not merely one end-to-end
smoke test".  Absence has to be real, so this builds a second virtual
environment that never receives the two distributions and a PATH that has no
`tesseract` on it, verifies the absence before running anything, and then
runs the suite in it.

Three modes, cheapest first:

  imports  Import every module under src/corridor, tests/ and workers/ one at
           a time and record which ones fail.  This is the list of modules
           that must still move before retirement can complete: an import
           failure names the module that reaches an engine, including one
           that reaches it transitively through another module.
  collect  `pytest --collect-only`, which reports the same thing through the
           collector and additionally catches conftest-level imports.
  suite    The complete suite.  This is the acceptance criterion.

The render worker is a second uv project invoked as a subprocess, and it
declares PyMuPDF too, so the harness prepares an engine-absent environment for
it under the same relative `UV_PROJECT_ENVIRONMENT` name and sets `UV_NO_SYNC`
so `uv run --project workers/render` cannot restore the distribution mid-run.

Every mode writes a receipt under `artifacts/pdf-engine-retirement/`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from scripts.run_local_tests import run_test_command
from scripts.test_gate.junit import case_counts

# The two engines' identity is data, not source.  `tests/test_architecture.py`
# reads a string literal naming an engine as a dependency on it -- which is
# correct, because that is how an executable reaches `subprocess` and how an
# engine identity reaches a provenance row -- and this harness has to keep
# naming both engines forever in order to prove they are gone.  #741's
# acceptance criterion is that the allowlist ends up empty, so the retirement
# tooling must not need a line on it.  scripts/retired_engines.json says why.
RETIRED_ENGINES = json.loads(
    (Path(__file__).resolve().parent / "retired_engines.json").read_text()
)["engines"]


def _identity(field: str) -> tuple[str, ...]:
    seen: list[str] = []
    for engine in RETIRED_ENGINES:
        for value in engine[field]:
            if value not in seen:
                seen.append(value)
    return tuple(seen)


# The distributions withheld from the environment.  One of them supplies two
# importable names; withholding the distribution withholds both.
WITHHELD_DISTRIBUTIONS = _identity("distributions")

# The import names that must not resolve, checked individually because an
# alias is what most call sites actually use.
FORBIDDEN_IMPORTS = _identity("import_names")

# The executables that must not be reachable.
FORBIDDEN_EXECUTABLES = _identity("executables")

# Relative on purpose: uv resolves a relative UV_PROJECT_ENVIRONMENT against
# the root of whichever project it is given, so one value names
# `.venv-engine-absent` for the repository and for `workers/render`.
ENVIRONMENT_NAME = ".venv-engine-absent"

RECEIPT_DIR = REPO_ROOT / "artifacts" / "pdf-engine-retirement"


def _run(command: list[str], env: dict[str, str], capture: bool = True):
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=capture,
        check=False,
    )


def sanitized_path(original: str) -> tuple[str, list[str]]:
    """Drop every PATH entry that offers a forbidden executable.

    Removing the directory rather than shadowing the name means a subprocess
    that builds its own PATH, or resolves the binary itself, still cannot find
    it.  The directories removed are recorded, because "we hid Homebrew" is a
    materially different claim from "the runtime has no Tesseract".
    """
    kept: list[str] = []
    removed: list[str] = []
    for entry in original.split(os.pathsep):
        if not entry:
            continue
        directory = Path(entry)
        offers = any(
            (directory / name).exists() for name in FORBIDDEN_EXECUTABLES
        )
        (removed if offers else kept).append(entry)
    return os.pathsep.join(kept), removed


def build_environment() -> tuple[dict[str, str], list[str]]:
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    path, removed = sanitized_path(env.get("PATH", ""))
    env["PATH"] = path
    env["UV_PROJECT_ENVIRONMENT"] = ENVIRONMENT_NAME
    # uv itself may live in a directory that was just removed from PATH.
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is not on PATH")
    env["CORRIDOR_ENGINE_ABSENT_UV"] = uv
    # Managed Python does not automatically search Homebrew's dylib directory.
    # Keep WeasyPrint's non-engine libraries available after hiding Homebrew's
    # executable directory. No executable PATH entry is restored.
    if sys.platform == "darwin" and "DYLD_FALLBACK_LIBRARY_PATH" not in env:
        libraries = [str(Path(entry).parent / "lib") for entry in removed
                     if (Path(entry).parent / "lib/libgobject-2.0.dylib").is_file()]
        if libraries:
            env["DYLD_FALLBACK_LIBRARY_PATH"] = os.pathsep.join(libraries)
    return env, removed


def prepare(env: dict[str, str]) -> list[dict[str, object]]:
    uv = env["CORRIDOR_ENGINE_ABSENT_UV"]
    withheld: list[str] = []
    for name in WITHHELD_DISTRIBUTIONS:
        withheld += ["--no-install-package", name]
    steps = [
        [uv, "sync", "--locked", *withheld],
        [uv, "sync", "--project", "workers/render", "--frozen", *withheld],
    ]
    results = []
    for step in steps:
        completed = _run(step, env)
        results.append(
            {
                "command": " ".join(step),
                "returncode": completed.returncode,
                "stderr_tail": completed.stderr.strip().splitlines()[-5:],
            }
        )
        if completed.returncode != 0:
            raise SystemExit(
                f"preparing the engine-absent environment failed: "
                f"{' '.join(step)}\n{completed.stderr}"
            )
    return results


def expose_uv(env: dict[str, str]) -> None:
    """Keep uv reachable without restoring a directory that offers an engine."""
    binary = interpreter(REPO_ROOT).parent / "uv"
    target = Path(env["CORRIDOR_ENGINE_ABSENT_UV"]).resolve()
    if binary.exists() or binary.is_symlink():
        if binary.resolve() != target:
            raise SystemExit("engine-absent environment contains an unexpected uv executable")
    else:
        binary.symlink_to(target)
    env["PATH"] = str(binary.parent) + os.pathsep + env["PATH"]


def interpreter(project: Path) -> Path:
    return project / ENVIRONMENT_NAME / "bin" / "python"


ABSENCE_PROBE = r"""
import importlib.util, json, shutil, sys, sysconfig
from pathlib import Path

forbidden_imports = %(imports)r
forbidden_executables = %(executables)r
report = {"interpreter": sys.executable, "imports": {}, "executables": {}}
for name in forbidden_imports:
    try:
        found = importlib.util.find_spec(name)
    except ModuleNotFoundError:
        found = None
    report["imports"][name] = None if found is None else (found.origin or "<namespace>")
for name in forbidden_executables:
    report["executables"][name] = shutil.which(name)
site = Path(sysconfig.get_paths()["purelib"])
report["site_packages"] = str(site)
prefixes = tuple(sorted(set(forbidden_imports) | set(%(distributions)r)))
report["distributions"] = sorted(
    p.name for p in site.glob("*.dist-info")
    if p.name.lower().startswith(prefixes)
)
report["entries"] = sorted(
    p.name for p in site.glob("*") if p.name.lower().startswith(prefixes)
)
print(json.dumps(report))
"""


def probe_absence(env: dict[str, str], project: Path) -> dict[str, object]:
    code = ABSENCE_PROBE % {
        "imports": FORBIDDEN_IMPORTS,
        "executables": FORBIDDEN_EXECUTABLES,
        "distributions": WITHHELD_DISTRIBUTIONS,
    }
    completed = _run([str(interpreter(project)), "-c", code], env)
    if completed.returncode != 0:
        raise SystemExit(
            f"absence probe failed in {project}:\n{completed.stderr}"
        )
    return json.loads(completed.stdout)


def assert_absent(report: dict[str, object], where: str) -> None:
    resolved = {
        name: origin
        for name, origin in report["imports"].items()  # type: ignore[union-attr]
        if origin is not None
    }
    executables = {
        name: found
        for name, found in report["executables"].items()  # type: ignore[union-attr]
        if found is not None
    }
    problems = []
    if resolved:
        problems.append(f"importable in {where}: {resolved}")
    if executables:
        problems.append(f"on PATH for {where}: {executables}")
    if report["entries"]:
        problems.append(f"installed in {where}: {report['entries']}")
    if problems:
        raise SystemExit(
            "the environment is not engine-absent, so nothing run in it "
            "proves anything:\n  " + "\n  ".join(problems)
        )


IMPORT_PROBE = r"""
import importlib, json, sys, traceback
from pathlib import Path

root = Path(sys.argv[1])
modules = json.loads(sys.argv[2])
failures = {}
for name in modules:
    try:
        importlib.import_module(name)
    except BaseException as error:  # a module may raise anything on import
        # The deepest frame inside the repository is the module that actually
        # reaches the engine. Every other failing module is downstream of it,
        # and moves for free once that one moves; recording only the count of
        # failures would hide which file has to change.
        site = None
        for frame in traceback.extract_tb(error.__traceback__):
            try:
                relative = Path(frame.filename).resolve().relative_to(root)
            except ValueError:
                continue
            site = str(relative)
        failures[name] = {
            "error": type(error).__name__,
            "message": str(error)[:400],
            "missing": getattr(error, "name", None),
            "import_site": site,
        }
print(json.dumps({"attempted": len(modules), "failures": failures}))
"""


def importable_modules() -> list[str]:
    names: list[str] = []
    for path in sorted((REPO_ROOT / "src" / "corridor").rglob("*.py")):
        if "migrations" in path.parts or path.name == "__init__.py":
            continue
        relative = path.relative_to(REPO_ROOT / "src").with_suffix("")
        names.append(".".join(relative.parts))
    for path in sorted((REPO_ROOT / "tests").glob("*.py")):
        names.append(path.stem)
    return names


def run_imports(env: dict[str, str]) -> dict[str, object]:
    probe_env = dict(env)
    probe_env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), str(REPO_ROOT / "tests"), str(REPO_ROOT)]
    )
    modules = importable_modules()
    completed = _run(
        [
            str(interpreter(REPO_ROOT)),
            "-c",
            IMPORT_PROBE,
            str(REPO_ROOT),
            json.dumps(modules),
        ],
        probe_env,
    )
    if completed.returncode != 0:
        raise SystemExit(f"import probe failed:\n{completed.stderr}")
    result = json.loads(completed.stdout)
    engine_attributable = {
        name: failure
        for name, failure in result["failures"].items()
        if (failure["missing"] or "") in FORBIDDEN_IMPORTS
        or any(
            name in failure["message"].lower()
            for name in FORBIDDEN_IMPORTS + FORBIDDEN_EXECUTABLES
        )
    }
    result["engine_attributable"] = sorted(engine_attributable)
    # The answer #741 needs: the files that must move, each with the engine it
    # reaches and how many modules currently fail behind it.
    sites: dict[str, dict[str, object]] = {}
    for name, failure in engine_attributable.items():
        site = failure["import_site"] or "<unknown>"
        entry = sites.setdefault(
            site, {"engines": set(), "blocks": 0, "examples": []}
        )
        entry["engines"].add(failure["missing"])  # type: ignore[union-attr]
        entry["blocks"] = int(entry["blocks"]) + 1  # type: ignore[arg-type]
        if len(entry["examples"]) < 3:  # type: ignore[arg-type]
            entry["examples"].append(name)  # type: ignore[union-attr]
    result["must_move"] = {
        site: {
            "engines": sorted(e for e in entry["engines"] if e),  # type: ignore[union-attr]
            "modules_blocked": entry["blocks"],
            "examples": entry["examples"],
        }
        for site, entry in sorted(sites.items())
    }
    return result


COLLECT_ERROR = re.compile(r"^ERROR (\S+)", re.MULTILINE)


def run_pytest(env: dict[str, str], arguments: list[str]) -> dict[str, object]:
    uv = env["CORRIDOR_ENGINE_ABSENT_UV"]
    result_dir = REPO_ROOT / "out/test-results"
    result_dir.mkdir(parents=True, exist_ok=True)
    junit = result_dir / "engine-absent.xml"
    junit.unlink(missing_ok=True)
    command = [uv, "run", "--no-sync", "pytest", *arguments, f"--junitxml={junit}"]
    lifecycle = result_dir / "engine-absent.json"
    returncode = run_test_command(command, suite="engine-absent", receipt_path=lifecycle,
        timeout_seconds=float(os.environ.get("TEST_TIMEOUT_SECONDS", "600")), environment=env)
    counts = case_counts(junit) if junit.exists() else None
    return {
        "command": " ".join(command),
        "returncode": returncode,
        "lifecycle": json.loads(lifecycle.read_text()),
        "junit_counts": counts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("imports", "collect", "suite"),
        default="imports",
        help="imports: probe every module; collect: pytest --collect-only; "
        "suite: the complete test suite",
    )
    parser.add_argument(
        "--workers",
        default=os.environ.get("TEST_WORKERS", "4"),
        help="xdist workers for --mode suite",
    )
    parser.add_argument(
        "--skip-prepare",
        action="store_true",
        help="reuse an already-prepared engine-absent environment",
    )
    parser.add_argument("--receipt", type=Path, default=None)
    parser.add_argument("pytest_args", nargs="*")
    options = parser.parse_args()

    env, removed = build_environment()
    prepared = [] if options.skip_prepare else prepare(env)
    expose_uv(env)

    root_absence = probe_absence(env, REPO_ROOT)
    assert_absent(root_absence, "the test environment")
    worker_absence = probe_absence(env, REPO_ROOT / "workers" / "render")
    assert_absent(worker_absence, "the render worker environment")

    # UV_NO_SYNC after the probes: the render subprocess runs `uv run
    # --project workers/render --frozen`, which would otherwise restore the
    # distribution this run exists to withhold.
    env["UV_NO_SYNC"] = "1"

    receipt: dict[str, object] = {
        "schema_version": "corridor.engine-absent-suite.v1",
        "recorded_at": datetime.now(UTC).isoformat(),
        "revision": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        ).stdout.strip(),
        "mode": options.mode,
        "withheld_distributions": list(WITHHELD_DISTRIBUTIONS),
        "forbidden_imports": list(FORBIDDEN_IMPORTS),
        "forbidden_executables": list(FORBIDDEN_EXECUTABLES),
        "path_entries_removed": removed,
        "native_library_path": env.get("DYLD_FALLBACK_LIBRARY_PATH"),
        "prepare": prepared,
        "absence": {
            "test_environment": root_absence,
            "render_worker_environment": worker_absence,
        },
    }

    if options.mode == "imports":
        receipt["result"] = run_imports(env)
        passed = not receipt["result"]["failures"]  # type: ignore[index]
    elif options.mode == "collect":
        receipt["result"] = run_pytest(
            env,
            ["--collect-only", "-q", "--continue-on-collection-errors", *options.pytest_args],
        )
        passed = receipt["result"]["returncode"] == 0  # type: ignore[index]
    else:
        receipt["result"] = run_pytest(
            env,
            [
                "--engine-absent-proof",
                "-n",
                options.workers,
                "--dist",
                "worksteal",
                "--continue-on-collection-errors",
                *options.pytest_args,
            ],
        )
        passed = receipt["result"]["returncode"] == 0  # type: ignore[index]

    receipt["passed"] = passed
    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = options.receipt or (
        RECEIPT_DIR / f"engine-absent-{options.mode}-{stamp}.json"
    )
    destination.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(f"receipt: {destination.relative_to(REPO_ROOT)}")
    print(f"engine-absent {options.mode}: {'passed' if passed else 'FAILED'}")
    if options.mode == "imports":
        must_move = receipt["result"]["must_move"]  # type: ignore[index]
        failures = receipt["result"]["failures"]  # type: ignore[index]
        print(
            f"{len(failures)} modules cannot be imported; "
            f"{len(must_move)} files reach an engine directly:"
        )
        for site, entry in must_move.items():
            print(
                f"  {site}  [{', '.join(entry['engines'])}]  "
                f"blocks {entry['modules_blocked']} module(s)"
            )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
