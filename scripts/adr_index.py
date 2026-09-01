"""Generate docs/adr/INDEX.md, the current-decision index, from ADR frontmatter.

Each ADR carries YAML frontmatter: `status` (accepted, proposed, deprecated,
or `superseded by ADR-NNNN[ and ADR-NNNN]`), `domain`, `scope` (current
product, optional module, historical, future), optional `supersedes`,
`amends`, `amended_by` lists, and an optional `migration` note naming what
is still unresolved. The lifecycle policy is docs/adr/README.md; the
architecture test validates the metadata and that INDEX.md is current.

Usage: `uv run python scripts/adr_index.py` validates the frontmatter and
rewrites INDEX.md; `--check` exits 1 when the metadata is invalid or the
file is stale. ADR numbers are chronology, not authority: the index never
infers a "governing" ADR; it lists the accepted, non-historical set.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ADR_DIR = REPO_ROOT / "docs" / "adr"
SOURCE_ROOT = REPO_ROOT / "src" / "corridor"
INDEX_PATH = ADR_DIR / "INDEX.md"

STATUS_PATTERN = re.compile(
    r"^(accepted|proposed|deprecated|superseded by ADR-\d{4}(?: and ADR-\d{4})*)$"
)
REFERENCE_PATTERN = re.compile(r"^ADR-\d{4}$")
FILENAME_PATTERN = re.compile(r"^\d{4}-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
SCOPES = ("current product", "optional module", "historical", "future")
DOMAINS = (
    "product",
    "project-record",
    "record-inclusion",
    "extraction",
    "supporting-documentation",
    "human-work",
    "reports",
    "operations",
    "intake",
    "retention",
    "testing",
    "terminology",
    "migration",
)
LIST_KEYS = ("supersedes", "amends", "amended_by")
KNOWN_KEYS = ("status", "domain", "scope", "migration", *LIST_KEYS)


@dataclass
class Adr:
    number: str
    path: Path
    title: str
    status: str
    domain: str
    scope: str
    supersedes: list[str] = field(default_factory=list)
    amends: list[str] = field(default_factory=list)
    amended_by: list[str] = field(default_factory=list)
    migration: str = ""
    keys: tuple[str, ...] = ()

    @property
    def ref(self) -> str:
        return f"ADR-{self.number}"

    @property
    def superseded_by(self) -> list[str]:
        return re.findall(r"ADR-\d{4}", self.status) if self.status.startswith("superseded") else []

    @property
    def active(self) -> bool:
        """Accepted and not historical: the decisions that govern today."""
        return self.status == "accepted" and self.scope != "historical"

    @property
    def proposed(self) -> bool:
        return self.status == "proposed"


def parse_frontmatter(path: Path) -> tuple[dict[str, str | list[str]], list[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError(f"{path.name}: missing frontmatter")
    end = lines.index("---", 1)
    fm: dict[str, str | list[str]] = {}
    key = None
    for line in lines[1:end]:
        if line.startswith("  - "):
            if key is None or not isinstance(fm.get(key), list):
                raise ValueError(f"{path.name}: list item outside a list")
            fm[key].append(line[4:].strip())
        else:
            key, sep, value = line.partition(":")
            if not sep:
                raise ValueError(f"{path.name}: malformed frontmatter line {line!r}")
            key = key.strip()
            value = value.strip()
            fm[key] = value if value else []
    return fm, lines[end + 1 :]


def load_adrs(adr_dir: Path = ADR_DIR) -> dict[str, Adr]:
    adrs: dict[str, Adr] = {}
    for path in sorted(adr_dir.glob("[0-9][0-9][0-9][0-9]-*.md")):
        if path.name[:4] in adrs:
            raise ValueError(f"duplicate ADR number {path.name[:4]}: {path.name}")
        fm, body = parse_frontmatter(path)
        title = next((l[2:].strip() for l in body if l.startswith("# ")), path.stem)
        adr = Adr(
            number=path.name[:4],
            path=path,
            title=title,
            status=str(fm.get("status", "")),
            domain=str(fm.get("domain", "")),
            scope=str(fm.get("scope", "")),
            migration=str(fm.get("migration", "")) if fm.get("migration") else "",
            keys=tuple(fm),
        )
        for key in LIST_KEYS:
            value = fm.get(key, [])
            setattr(adr, key, list(value) if isinstance(value, list) else [value])
        adrs[adr.number] = adr
    return adrs


def validate(adrs: dict[str, Adr]) -> list[str]:
    """Every lifecycle rule in docs/adr/README.md, as a list of violations."""
    problems: list[str] = []
    for adr in adrs.values():
        if not FILENAME_PATTERN.match(adr.path.name):
            problems.append(f"{adr.ref}: malformed filename {adr.path.name}")
        unknown = [k for k in adr.keys if k not in KNOWN_KEYS]
        if unknown:
            problems.append(f"{adr.ref}: unknown frontmatter keys {unknown}")
        if not STATUS_PATTERN.match(adr.status):
            problems.append(f"{adr.ref}: invalid status {adr.status!r}")
        if adr.domain not in DOMAINS:
            problems.append(f"{adr.ref}: invalid domain {adr.domain!r}")
        if adr.scope not in SCOPES:
            problems.append(f"{adr.ref}: invalid scope {adr.scope!r}")
        if adr.status.startswith("superseded") and adr.scope != "historical":
            problems.append(f"{adr.ref}: a superseded ADR has scope historical")
        for key in LIST_KEYS:
            for ref in getattr(adr, key):
                if not REFERENCE_PATTERN.match(ref):
                    problems.append(f"{adr.ref}: malformed reference {ref!r} in {key}")
                elif ref[4:] not in adrs:
                    problems.append(f"{adr.ref}: {key} names missing {ref}")
                elif ref == adr.ref:
                    problems.append(f"{adr.ref}: {key} names itself")
        if adr.proposed and adr.supersedes:
            problems.append(f"{adr.ref}: a proposed ADR may not supersede an accepted decision")
        if adr.status == "deprecated" and adr.superseded_by:
            problems.append(f"{adr.ref}: a deprecated ADR names no successor")
        for successor in adr.superseded_by:
            target = adrs.get(successor[4:])
            if target is None:
                problems.append(f"{adr.ref}: superseded by missing {successor}")
                continue
            if target.proposed:
                problems.append(f"{adr.ref}: superseded by proposed {successor}")
            if adr.ref not in target.supersedes:
                problems.append(f"{successor} must list supersedes: {adr.ref}")
        for predecessor in adr.supersedes:
            target = adrs.get(predecessor[4:])
            if target is not None and adr.ref not in target.superseded_by and adr.status != "deprecated":
                problems.append(f"{predecessor} must carry status superseded by {adr.ref}")
        for predecessor in adr.amends:
            target = adrs.get(predecessor[4:])
            if target is not None and adr.ref not in target.amended_by:
                problems.append(f"{predecessor} must list amended_by: {adr.ref}")
        for successor in adr.amended_by:
            target = adrs.get(successor[4:])
            if target is not None and adr.ref not in target.amends:
                problems.append(f"{successor} must list amends: {adr.ref}")
    return problems


def citing_modules(adrs: dict[str, Adr], source_root: Path = SOURCE_ROOT) -> dict[str, list[str]]:
    """Modules whose source text cites the ADR by number."""
    cited: dict[str, list[str]] = {n: [] for n in adrs}
    for path in sorted(source_root.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        name = ".".join(path.relative_to(source_root).with_suffix("").parts)
        for number in set(re.findall(r"ADR-(\d{4})", text)):
            if number in cited:
                cited[number].append(name)
    return cited


def _link(adr: Adr) -> str:
    return f"[{adr.ref}]({adr.path.name})"


def _refs(refs: list[str], adrs: dict[str, Adr]) -> str:
    return ", ".join(_link(adrs[r[4:]]) if r[4:] in adrs else r for r in refs) or "—"


def render(adrs: dict[str, Adr], cited: dict[str, list[str]]) -> str:
    out = [
        "# Current-decision index",
        "",
        "Generated by `scripts/adr_index.py` from each ADR's frontmatter; do not edit by hand.",
        "`make adr-index` regenerates it and `make check` fails when it is stale.",
        "The lifecycle rules are in [README.md](README.md).",
        "",
        "ADR numbers are chronology, not authority. No ADR is inferred to govern",
        "another. The accepted table below is the set of decisions in force: every",
        "accepted ADR that is neither superseded nor historical. Where two accepted",
        "ADRs touch one question, the `Amends` and `Amended by` columns say which",
        "clauses moved; read the amending ADR for those clauses and the amended ADR",
        "for everything else.",
        "",
        "## Accepted decisions by domain",
        "",
    ]
    for domain in DOMAINS:
        active = [a for a in adrs.values() if a.domain == domain and a.active]
        if not active:
            continue
        out += [
            f"### {domain}",
            "",
            "| ADR | Title | Scope | Supersedes | Amends | Amended by | Citing modules | Unresolved migration |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for adr in active:
            modules = ", ".join(f"`{m}`" for m in cited[adr.number]) or "—"
            out.append(
                f"| {_link(adr)} | {adr.title} | {adr.scope} | "
                f"{_refs(adr.supersedes, adrs)} | {_refs(adr.amends, adrs)} | "
                f"{_refs(adr.amended_by, adrs)} | {modules} | {adr.migration or '—'} |"
            )
        out.append("")
    out += [
        "## Proposed decisions",
        "",
        "Not in force. A proposed ADR governs nothing and may not supersede an accepted decision until it is accepted.",
        "",
        "| ADR | Title | Domain | Amends |",
        "|---|---|---|---|",
    ]
    proposed = [a for a in adrs.values() if a.proposed]
    for adr in proposed:
        out.append(f"| {_link(adr)} | {adr.title} | {adr.domain} | {_refs(adr.amends, adrs)} |")
    if not proposed:
        out.append("| — | none | — | — |")
    out += [
        "",
        "## Historical, superseded, and deprecated decisions",
        "",
        "Retained in sequence as history; never governing.",
        "",
        "| ADR | Title | Status | Scope | Domain |",
        "|---|---|---|---|---|",
    ]
    for adr in adrs.values():
        if adr.active or adr.proposed:
            continue
        status = adr.status
        for ref in adr.superseded_by:
            if ref[4:] in adrs:
                status = status.replace(ref, _link(adrs[ref[4:]]))
        out.append(f"| {_link(adr)} | {adr.title} | {status} | {adr.scope} | {adr.domain} |")
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    adrs = load_adrs()
    problems = validate(adrs)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    text = render(adrs, citing_modules(adrs))
    if "--check" in argv:
        current = INDEX_PATH.read_text(encoding="utf-8") if INDEX_PATH.exists() else ""
        if current != text:
            print("docs/adr/INDEX.md is stale; run `make adr-index`.", file=sys.stderr)
            return 1
        return 0
    INDEX_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {INDEX_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
