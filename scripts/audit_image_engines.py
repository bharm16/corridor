"""Build Corridor's deployable image and audit it for the retired engines.

#461 closes by decision "only when #741 proves the built image no longer
contains the dependency", and #741's own criterion is that the audit receipt
is stored.  Neither is satisfied by reading the Dockerfile: the image is what
is distributed and operated, and a dependency can arrive in it through a
transitive requirement, a second uv project, or an apt package that no line of
this repository names.  So this builds the image and asks the image.

Four questions, because the two engines arrive by different routes:

  1. Is either distribution installed in the application environment?
  2. Is either distribution installed in the render worker's environment?
     It is a second uv project synced into the same image, with its own lock.
  3. Is the OCR executable on PATH anywhere in the image?
  4. Is the operating-system package that provides it installed?

And one question the replacement raises rather than the incumbent: are the
PDFium build's retained dependency notices present in the image?  The reader
package ships them, and shipping the reader without them is a licence
failure of the replacement, not a leftover of the thing being retired.

The audit reports rather than assumes.  Run before the removal it will exit
non-zero and record a truthful before state; that receipt is the evidence
that the after state is a change and not a restatement.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RECEIPT_DIR = REPO_ROOT / "artifacts" / "pdf-engine-retirement"

RETIRED_ENGINES = json.loads(
    (REPO_ROOT / "scripts" / "retired_engines.json").read_text()
)["engines"]

NOTICES = "src/corridor_pdf_reader/replacement/third-party-notices"

PROBE = r'''
import json, os, shutil, subprocess, sys
from pathlib import Path

notices_root = Path(sys.argv[1])
engines = json.loads(sys.argv[2])

def site_packages(venv):
    matches = sorted(Path(venv).glob("lib/python*/site-packages"))
    return matches[0] if matches else None

report = {"environments": {}, "executables": {}, "system_packages": {}}

for label, venv in (
    ("application", "/opt/corridor/.venv"),
    ("render_worker", "/opt/corridor/workers/render/.venv"),
):
    site = site_packages(venv)
    entry = {"virtualenv": venv, "site_packages": str(site) if site else None}
    if site is None:
        entry["present"] = None
        entry["error"] = "no site-packages directory"
    else:
        prefixes = tuple(
            sorted(
                {n.lower() for e in engines for n in e["import_names"]}
                | {d.lower() for e in engines for d in e["distributions"]}
            )
        )
        entry["present"] = sorted(
            p.name for p in site.glob("*") if p.name.lower().startswith(prefixes)
        )
    report["environments"][label] = entry

for engine in engines:
    for name in engine["executables"]:
        report["executables"][name] = shutil.which(name)
    for package in engine["system_packages"]:
        found = subprocess.run(
            ["dpkg-query", "--showformat=${Status}|${Version}", "--show", package],
            capture_output=True,
            text=True,
        )
        report["system_packages"][package] = (
            found.stdout.strip() if found.returncode == 0 else None
        )

manifest_path = notices_root / "manifest.json"
notices = {"root": str(notices_root), "manifest_present": manifest_path.exists()}
if manifest_path.exists():
    manifest = json.loads(manifest_path.read_text())
    package_root = notices_root.parent.parent
    missing = []
    for package in manifest:
        for item in package["files"]:
            if not (package_root / item["path"]).exists():
                missing.append(item["path"])
    notices["packages"] = [p["package"] for p in manifest]
    notices["declared_files"] = sum(len(p["files"]) for p in manifest)
    notices["missing_files"] = missing
    notices["files_on_disk"] = sum(
        1 for p in notices_root.rglob("*") if p.is_file()
    )
    # The retained copy is the notice set of the wheel the reader was
    # measured against, which was built for a different platform than the
    # image runs. What has to accompany *this* distribution is the notice
    # set of the binary *this* image installs, so look for that separately
    # rather than letting the retained copy stand in for it.
    installed = {}
    site = site_packages("/opt/corridor/.venv")
    for package in notices.get("packages", []):
        normalized = package.replace("-", "_").lower()
        found = []
        for info in sorted(site.glob("*.dist-info")) if site else []:
            if not info.name.lower().startswith((normalized, package.lower())):
                continue
            licences = sorted(
                str(f.relative_to(info))
                for f in info.rglob("*")
                if f.is_file() and "licens" in str(f.relative_to(info)).lower()
            )
            found.append({"dist_info": info.name, "licence_files": licences})
        installed[package] = found
    notices["installed_in_image"] = installed
report["notices"] = notices
report["path"] = os.environ.get("PATH", "")
# The selected production route must be constructible in the image without a
# checkout, a credential, or a provider request. This also checks that its
# render profiles and prompt/schema sources actually accompany deployment.
try:
    from corridor.native_pipeline import RecordedPipelineClient, native_pipeline_configuration
    from corridor.native_provider_boundary import POSTURE
    from corridor.pipeline_contracts import ObservationPlan, content_digest
    configuration = native_pipeline_configuration(RecordedPipelineClient([]), ObservationPlan(
        mode="fresh_provider", origin_sha256="0" * 64, source_permission="public",
        provider_posture_sha256=POSTURE.digest, description="Image runtime import and configuration smoke test",
    ))
    report["native_matrix_runtime"] = {"configuration_sha256": content_digest(configuration),
                                       "code_revision": configuration["code_revision"]}
except Exception as exc:
    report["native_matrix_runtime"] = {"error": f"{type(exc).__name__}: {exc}"}
print(json.dumps(report))
'''


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=REPO_ROOT, text=True, capture_output=True, check=False, **kwargs
    )


def build(tag: str, revision: str) -> dict[str, object]:
    command = [
        "docker",
        "build",
        "--build-arg",
        f"GIT_REVISION={revision}",
        "-t",
        tag,
        ".",
    ]
    completed = _run(command)
    if completed.returncode != 0:
        raise SystemExit(
            "the image did not build, so there is nothing to audit:\n"
            + completed.stderr[-4000:]
        )
    inspected = _run(["docker", "image", "inspect", tag, "--format", "{{.Id}}|{{.Size}}"])
    identity, _, size = inspected.stdout.strip().partition("|")
    return {
        "command": " ".join(command),
        "tag": tag,
        "image_id": identity,
        "size_bytes": int(size) if size.isdigit() else None,
    }


def probe(tag: str) -> dict[str, object]:
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            # The probe arrives on stdin, which docker only attaches with -i.
            "-i",
            "--entrypoint",
            "python",
            tag,
            "-",
            f"/opt/corridor/{NOTICES}",
            json.dumps(RETIRED_ENGINES),
        ],
        input=PROBE,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"the probe failed inside the image:\n{completed.stderr}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def findings(report: dict[str, object]) -> list[str]:
    problems: list[str] = []
    if report.get("native_matrix_runtime", {}).get("error"):
        problems.append(f"native Matrix runtime cannot configure in the built image: {report['native_matrix_runtime']['error']}")
    for label, entry in report["environments"].items():  # type: ignore[union-attr]
        if entry.get("error"):
            problems.append(f"{label}: {entry['error']}")
        for name in entry.get("present") or []:
            problems.append(f"{label} environment still installs {name}")
    for name, where in report["executables"].items():  # type: ignore[union-attr]
        if where:
            problems.append(f"the {name} executable is on PATH at {where}")
    for package, status in report["system_packages"].items():  # type: ignore[union-attr]
        if status:
            problems.append(f"the {package} system package is installed ({status})")
    notices = report["notices"]
    if not notices.get("manifest_present"):  # type: ignore[union-attr]
        problems.append("the third-party notices manifest is absent from the image")
    for path in notices.get("missing_files") or []:  # type: ignore[union-attr]
        problems.append(f"a declared notice is absent from the image: {path}")
    for package, found in (notices.get("installed_in_image") or {}).items():  # type: ignore[union-attr]
        if not found:
            problems.append(
                f"{package} declares retained notices but is not installed in "
                "the image, so the retained copy describes nothing shipped"
            )
            continue
        for entry in found:
            if not entry["licence_files"]:
                problems.append(
                    f"{entry['dist_info']} is installed in the image without "
                    "its own licence files"
                )
    return problems



def summary(receipt: dict[str, object]) -> str:
    """A readable receipt beside the machine-readable one.

    The decision this evidence serves -- #461's licence closure -- is made by
    a person reading it, not by a program parsing it.
    """

    audited = receipt["audited"]
    image = receipt["image"]
    size = image.get("size_bytes")
    lines = [
        "# Built-image retired-engine audit",
        "",
        f"Recorded {receipt['recorded_at']} from revision "
        f"`{receipt['revision']}`"
        + (" with a dirty working tree" if receipt["working_tree_dirty"] else "")
        + ".",
        "",
        f"Image `{image.get('tag')}`, id `{image.get('image_id')}`"
        + (f", {size / 1_000_000:.0f} MB uncompressed" if size else "")
        + ".",
        "",
        "## Verdict",
        "",
        (
            "The built image carries neither retired engine."
            if receipt["clear_of_retired_engines"]
            else "**The built image still carries a retired engine.** This is a "
            "recorded before state, not a passing check."
        ),
        "",
    ]
    for problem in receipt["findings"]:  # type: ignore[union-attr]
        lines.append(f"- {problem}")
    lines += ["", "## What was inspected", ""]
    for label, entry in audited["environments"].items():  # type: ignore[index]
        present = entry.get("present")
        lines.append(
            f"- The {label} environment at `{entry['virtualenv']}`: "
            + (
                "nothing matching either engine"
                if present == []
                else f"{', '.join(present or [])}"
            )
        )
    for name, where in audited["executables"].items():  # type: ignore[index]
        lines.append(
            f"- The `{name}` executable on the image's PATH: "
            + (f"`{where}`" if where else "absent")
        )
    for package, status in audited["system_packages"].items():  # type: ignore[index]
        lines.append(
            f"- The `{package}` operating-system package: "
            + (f"`{status}`" if status else "not installed")
        )
    notices = audited["notices"]  # type: ignore[index]
    lines += [
        "",
        "## Replacement notices",
        "",
        f"The retained notice set under `{NOTICES}` declares "
        f"{notices.get('declared_files')} files; "
        f"{len(notices.get('missing_files') or [])} are missing from the image.",
        "",
        "The retained set records the wheels the reader was measured against. "
        "What must accompany this distribution is the notice set of the binary "
        "this image installs, so that is checked separately:",
        "",
    ]
    for package, found in (notices.get("installed_in_image") or {}).items():
        if not found:
            lines.append(f"- `{package}`: **not installed in the image**")
        for entry in found:
            count = len(entry["licence_files"])
            lines.append(
                f"- `{entry['dist_info']}`: {count} licence "
                f"{'file' if count == 1 else 'files'} installed"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="corridor-engine-audit:local")
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="audit an already-built tag instead of building it",
    )
    parser.add_argument("--receipt", type=Path, default=None)
    options = parser.parse_args()

    revision = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    dirty = bool(_run(["git", "status", "--porcelain"]).stdout.strip())
    built = (
        {"skipped": True, "tag": options.tag}
        if options.skip_build
        else build(options.tag, revision)
    )
    report = probe(options.tag)
    problems = findings(report)

    receipt = {
        "schema_version": "corridor.built-image-engine-audit.v1",
        "recorded_at": datetime.now(UTC).isoformat(),
        "revision": revision,
        "working_tree_dirty": dirty,
        "image": built,
        "audited": report,
        "findings": problems,
        "clear_of_retired_engines": not problems,
    }
    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = options.receipt or (RECEIPT_DIR / f"image-audit-{stamp}.json")
    destination.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    destination.with_suffix(".md").write_text(summary(receipt))

    print(f"receipt: {destination.relative_to(REPO_ROOT)}")
    if problems:
        print("the built image still carries a retired engine:")
        for problem in problems:
            print(f"  {problem}")
    else:
        print("the built image carries neither retired engine")
    notices = report["notices"]
    print(
        f"notices: {notices.get('declared_files')} declared, "
        f"{notices.get('files_on_disk')} present, "
        f"{len(notices.get('missing_files') or [])} missing"
    )
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
