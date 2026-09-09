"""The Briefing: a model-drafted, cited narrative over the record (ADR-0011).

The exception engine is deterministic by construction and blind by
measurement — the record is full of prose no rule reads. A model may read
all of it, and this module is what its reading is allowed to be: the
paragraph a sharp deputy would write, where every sentence stands on a
citation, the deterministic floor is something it may explain but never bury,
and the whole thing is a view — regenerable, stamped, never the record.

The division of labour is ADR-0006's, one layer up. The model composes
prose over citable objects this module names and supplies; it is never
asked to invent a citation format, and a reference it invents anyway is a
citation to nothing and takes its sentence with it. The checker is pure
code, because deciding whether a cited object exists, whether a quote
appears on its page, and whether every fired Exception is referenced are
lookups — and lookups are code's.

What the checker deliberately does not judge: whether a sentence's claim
*follows from* its citation. That split is `verify.py`'s, held everywhere
in this system — presence on the page is mechanical, truth is the
reviewer's work.

Nothing here writes. There is no path from a Briefing to the database, so
ADR-0011's constraint that model observations reach the Ledger only
through Candidate → Adjudication is satisfied by construction in this
version: the proposing path is a deliberate later ticket.
"""

from __future__ import annotations

from corridor.accepted_field_reading import accepted_field_text, visible_native_statements, native_field_visible
from corridor.presentation import field_label

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.dependency_events import (
    StatementPublication,
    current_statement_evidence_memberships,
    published_dependency_statements,
)
from corridor.check_configuration import effective_thresholds
from corridor.exceptions import (
    Evaluation,
    Thresholds,
    evaluate_dependency,
    format_exception_name,
)
from corridor.models import (
    Assertion,
    Dependency,
    DocPage,
    EvidenceLink,
)
from corridor.project_reading import FrozenProjectReading, freeze_project_reading
from corridor.presentation import label, provenance_label
from corridor.verify import quote_appears_on, threshold_for

# Versioned like every extractor, and for the same reason (ADR-0003's
# discipline): a briefing is a reading, and readings from different
# prompts are different readings. The prompt file is kept beside any
# superseded successor rather than edited, because an overwritten prompt
# cannot say what produced the output a reader is holding.
PROMPT_VERSION = "briefing_v2"
PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "briefing_v2.md"

SENTENCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sentences"],
    "properties": {
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "cites"],
                "properties": {
                    "text": {"type": "string"},
                    # References into the citables the prompt supplied —
                    # E<n> evidence, A<n> assertions, V<n> verbals, X<n>
                    # exception instances, or XB<n> project buckets.
                    "cites": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}


@dataclass(frozen=True)
class Citable:
    """One object a sentence may stand on, named by the ref we assigned.

    `page_text` and `text_source` ride along for evidence refs only, so
    the checker can re-verify the quote against the page it claims —
    at the threshold that page's provenance earns (`verify.threshold_for`).
    """

    ref: str
    kind: str  # evidence | assertion | verbal | exception | exception_bucket
    text: str
    quote: str | None = None
    page_text: str | None = None
    text_source: str | None = None
    dependency_id: int | None = None
    count: int | None = None
    covers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Sentence:
    text: str
    cites: tuple[str, ...]


@dataclass(frozen=True)
class Briefing:
    """A view, never the record. Deleting it loses no fact.

    The stamps are the re-check contract: prompt version and model say
    what drafted it; evaluation time and ruleset version say what its
    Exception citations are checkable against, because Exceptions are
    computed at read time and stored nowhere (ADR-0003's rule for
    Derivations, applied to prose).
    """

    ref_code: str
    sentences: tuple[Sentence, ...]
    withheld: dict[str, int]
    floor: tuple[str, ...]
    citables: tuple[Citable, ...]
    prompt_version: str
    model: str | None
    evaluated_at: date
    ruleset_version: str
    # The thresholds the Exception citations were computed under. The
    # export published all three parts of the reading and the briefing
    # published two, so one artifact could not be checked against another.
    thresholds: Thresholds = field(default_factory=Thresholds)
    refused: bool = False
    refusal_reason: str = ""


def brief(
    session: Session,
    dependency_id: int,
    *,
    client,
    today: date | None = None,
) -> Briefing:
    """Draft, check, and either present or refuse — never show unchecked."""
    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise LookupError(f"no dependency {dependency_id}")
    publication = published_dependency_statements(
        session, (dependency_id,), project_id=dependency.project_id
    )
    return _brief(
        session,
        [dependency],
        ref_code=dependency.ref_code,
        client=client,
        evaluation=evaluate_dependency(
            session,
            dependency_id,
            today=today,
            thresholds=effective_thresholds(session, dependency.project_id),
            statement_publication=publication,
        ),
        publication=publication,
    )


def brief_project(
    session: Session,
    project_id: int,
    *,
    client,
    today: date | None = None,
    frozen_reading: FrozenProjectReading | None = None,
) -> Briefing:
    """One narrative over every record in the project (#119).

    The floor changes unit with the scope: every fired rule bucket must be
    cited by a surviving sentence, and every individual Exception remains
    underneath its bucket. Omitting a bucket refuses the draft whole. Cost
    stays one read and one model call per invocation; there is no per-record
    fan-out and no batch path.
    """
    reading = frozen_reading or freeze_project_reading(
        session, project_id, today=today
    )
    dependencies = [row.dependency for row in reading.rows]
    publication = reading.statement_publication
    return _brief(
        session,
        dependencies,
        ref_code=f"{reading.project.slug} — {len(dependencies)} records",
        client=client,
        # One reading for the whole briefing. Every record used to take
        # its own `exceptions_for`, so a project briefing spanning N
        # records computed N clocks and stamped one of them.
        evaluation=reading.evaluation,
        publication=publication,
        project_scope=True,
    )


def _brief(
    session: Session,
    dependencies: list[Dependency],
    *,
    ref_code: str,
    client,
    evaluation: Evaluation,
    publication: StatementPublication,
    project_scope: bool = False,
) -> Briefing:
    if evaluation.statement_publication is not publication:
        raise ValueError(
            "the briefing evaluation must use the exact frozen statement publication"
        )
    if evaluation.statement_publication_fingerprint != publication.fingerprint:
        raise ValueError(
            "the briefing evaluation and statement publication describe different statement provenance"
        )
    citables, floor, committed_dates = _assemble(
        session,
        dependencies,
        evaluation,
        publication,
        project_scope=project_scope,
    )
    if not citables:
        # Nothing to cite means nothing a sentence could stand on: the
        # honest briefing is empty, and a model call would burn money to
        # draft prose the checker must then withhold in full.
        return Briefing(
            sentences=(),
            withheld={},
            ref_code=ref_code,
            floor=floor,
            citables=(),
            prompt_version=PROMPT_VERSION,
            model=getattr(client, "model", None),
            evaluated_at=evaluation.today,
            ruleset_version=evaluation.ruleset_version,
            thresholds=evaluation.thresholds,
        )

    result = client.complete(
        system=PROMPT.read_text(),
        user=_user_message(dependencies, citables, committed_dates, floor),
        schema=SENTENCE_SCHEMA,
    )
    drafted = [
        Sentence(text=(item.get("text") or "").strip(), cites=tuple(item.get("cites") or []))
        for item in (result.get("sentences") or [])
    ]

    kept, withheld = _check(drafted, {c.ref: c for c in citables})

    stamps = dict(
        ref_code=ref_code,
        floor=floor,
        citables=tuple(citables),
        prompt_version=PROMPT_VERSION,
        model=getattr(client, "model", None),
        evaluated_at=evaluation.today,
        ruleset_version=evaluation.ruleset_version,
        thresholds=evaluation.thresholds,
    )

    # The floor, judged on the sentences that survived: one Dependency uses
    # Exception instances; a project uses the facet buckets that put every
    # instance underneath. There is no partially honest briefing.
    cited = {ref for sentence in kept for ref in sentence.cites}
    missing = [ref for ref in floor if ref not in cited]
    if missing:
        return Briefing(
            sentences=(),
            withheld=withheld,
            refused=True,
            refusal_reason=(
                "floor uncovered: no surviving sentence cites "
                + ", ".join(missing)
            ),
            **stamps,
        )

    return Briefing(sentences=tuple(kept), withheld=withheld, **stamps)


def _assemble(
    session: Session,
    dependencies: list[Dependency],
    evaluation: Evaluation,
    publication: StatementPublication,
    *,
    project_scope: bool,
) -> tuple[list[Citable], tuple[str, ...], dict[int, date | None]]:
    """Everything a sentence may stand on, and which refs are the floor.

    One numbering across however many records the briefing spans, so a
    ref is unique project-wide and each citable names its record — the
    model cannot attribute one record's fact to another without the
    reader seeing the ref resolve elsewhere.

    The Exceptions come from the same engine every other view reads —
    never a private re-computation, so the Briefing and the exception
    views cannot disagree about what fired.
    """
    citables: list[Citable] = []
    floor: list[str] = []
    counters = {"E": 0, "A": 0, "V": 0, "X": 0, "XB": 0}
    instance_refs = {}
    committed_dates = publication.committed_dates
    if evaluation.native_population is not None:
        return assemble_native_citables(evaluation.native_population, evaluation, publication, project_scope=project_scope)
    memberships = current_statement_evidence_memberships(
        session, (dependency.id for dependency in dependencies)
    )
    event_link_ids = tuple(
        dict.fromkeys(member.evidence_link.id for member in memberships.members)
    )
    event_pages_by_link = (
        dict(
            session.execute(
                select(EvidenceLink.id, DocPage)
                .outerjoin(
                    DocPage,
                    (DocPage.document_id == EvidenceLink.document_id)
                    & (DocPage.page_no == EvidenceLink.page_no),
                )
                .where(EvidenceLink.id.in_(event_link_ids))
            ).all()
        )
        if event_link_ids
        else {}
    )

    def ref(prefix: str) -> str:
        counters[prefix] += 1
        return f"{prefix}{counters[prefix]}"

    for dependency in dependencies:
        statement = publication.by_dependency[dependency.id]
        committed_event = statement.event
        if (
            committed_event is not None
            and committed_event.source_kind == "verbal"
            and statement.source_attribution is not None
        ):
            citables.append(
                Citable(
                    ref=ref("V"),
                    kind="verbal",
                    text=(
                        f"{dependency.ref_code}: {statement.source_attribution}. "
                        f"{committed_event.description}"
                    ),
                    dependency_id=dependency.id,
                )
            )
        direct_links = session.execute(
            select(EvidenceLink, DocPage)
            .outerjoin(
                DocPage,
                (DocPage.document_id == EvidenceLink.document_id)
                & (DocPage.page_no == EvidenceLink.page_no),
            )
            .where(EvidenceLink.dependency_id == dependency.id)
            .order_by(EvidenceLink.id)
        ).all()
        links_by_id = {link.id: (link, page) for link, page in direct_links}
        for member in memberships.for_dependency(dependency.id):
            links_by_id.setdefault(
                member.evidence_link.id,
                (
                    member.evidence_link,
                    event_pages_by_link.get(member.evidence_link.id),
                ),
            )
        links = [links_by_id[key] for key in sorted(links_by_id)]
        for link, page in links:
            citables.append(
                Citable(
                    ref=ref("E"),
                    kind="evidence",
                    text=(
                        f"{dependency.ref_code}, document {link.document_id} "
                        f"p.{link.page_no}: “{link.quote}”"
                    ),
                    quote=link.quote,
                    page_text=(page.text if page else "") or "",
                    text_source=page.text_source if page else None,
                )
            )

        assertions = session.scalars(
            select(Assertion)
            .where(Assertion.dependency_id == dependency.id)
            .order_by(Assertion.field_name, Assertion.id)
        ).all()
        for assertion in assertions:
            citables.append(
                Citable(
                    ref=ref("A"),
                    kind="assertion",
                    text=(
                        f"{dependency.ref_code}: {assertion.field_name} = "
                        f"{assertion.asserted_value!r}"
                    ),
                )
            )

        for exception in evaluation.for_dependency(dependency.id):
            x = ref("X")
            instance_refs[exception] = x
            if not project_scope:
                floor.append(x)
            days = (
                f" ({exception.quantity_days}d)"
                if exception.quantity_days is not None
                else ""
            )
            citables.append(
                Citable(
                    ref=x,
                    kind="exception",
                    text=(
                        f"{dependency.ref_code}: {exception.rule}{days}: "
                        f"{exception.detail}"
                    ),
                )
            )

    if project_scope:
        for facet in evaluation.facets():
            bucket_ref = ref("XB")
            covered_refs = tuple(
                instance_refs[exception] for exception in facet.exceptions
            )
            floor.append(bucket_ref)
            citables.append(
                Citable(
                    ref=bucket_ref,
                    kind="exception_bucket",
                    text=(
                        f"{facet.rule}: {facet.count} record(s); covers "
                        + ", ".join(f"[{covered}]" for covered in covered_refs)
                    ),
                    count=facet.count,
                    covers=covered_refs,
                )
            )

    return citables, tuple(floor), committed_dates


def assemble_native_citables(population, evaluation, publication, *, project_scope):
    """Citable native decisions and checked source cells, with the same rule floor."""
    citables = []
    floor = []
    for record in population.open_records:
        for name, field in sorted(record.fields.items()):
            source_refs = ", ".join(f"{source.filename} {source.locator}" for source in field.sources)
            citables.append(Citable(ref=f"A{len(citables)+1}", kind="assertion",
                text=f"{record.ref_code}: accepted {field_label(name)} = {accepted_field_text(field)}; Fact {field.fact_id}, decision {field.decision_id}, revision {field.revision_id}; {source_refs}",
                dependency_id=record.id))
        for source in record.checked_source_passages:
            citables.append(Citable(ref=f"E{len(citables)+1}", kind="evidence",
                text=f"{record.ref_code}: {source.filename} {source.locator}: {source.quote}",
                quote=source.quote, page_text=source.quote, text_source="cells", dependency_id=record.id))
    for statement in visible_native_statements(population, document_only=publication.document_only):
        for name, field in statement.fields.items():
            if not native_field_visible(field, document_only=publication.document_only):
                continue
            value = str(field.value)
            citables.append(Citable(ref=f"A{len(citables)+1}", kind="decision",
                text=f"Statement {statement.subject_key}: accepted {name} = {value}; Fact {field.fact_id}, "
                     f"Record Decision {field.decision_id}, revision {field.revision_id}, by {field.actor} on {field.decided_at}."))
        for source in statement.sources:
            if source.locator_validation_status != "valid" or (publication.document_only and source.document_id is None):
                continue
            citables.append(Citable(ref=f"E{len(citables)+1}", kind="evidence",
                text=f"{source.filename or source.source_class}: {source.locator}: {source.quote}",
                quote=source.quote, page_text=source.quote, text_source="cells"))
    for plan in population.follow_up_plans:
        citables.append(Citable(ref=f"A{len(citables)+1}", kind="decision",
            text=f"Follow-up Plan {plan.plan_id} for Proposed Delta {plan.delta_id}: {plan.open_question}. "
                 f"Responsible: {plan.responsible_principal or plan.responsible_organization}; "
                 f"return date {plan.return_date or 'not yet known'}; decided by {plan.recorded_by} "
                 f"in revision {plan.revision_id}. The proposed value remains unaccepted."))
    if project_scope:
        for index, facet in enumerate(evaluation.facets(), 1):
            reference = f"XB{index}"
            floor.append(reference)
            citables.append(Citable(ref=reference, kind="exception_bucket",
                text=f"{format_exception_name(facet.rule)}: {facet.count} record(s)", count=facet.count,
                covers=tuple(str(item.dependency_id) for item in facet.exceptions)))
    else:
        for index, finding in enumerate(evaluation.found, 1):
            reference = f"X{index}"
            floor.append(reference)
            citables.append(Citable(ref=reference, kind="exception", text=finding.detail, dependency_id=finding.dependency_id))
    return citables, tuple(floor), publication.committed_dates


def _user_message(
    dependencies: list[Dependency],
    citables: list[Citable],
    committed_dates: dict[int, date | None],
    floor: tuple[str, ...],
) -> str:
    verbal_sources = {
        citable.dependency_id: citable
        for citable in citables
        if citable.kind == "verbal" and citable.dependency_id is not None
    }
    lines = []
    for dependency in dependencies:
        committed = str(committed_dates.get(dependency.id) or "—")
        if verbal := verbal_sources.get(dependency.id):
            committed = f"{committed} ({verbal.text}; cite [{verbal.ref}])"
        lines.append(f"Record {dependency.ref_code}: {dependency.title}.")
        lines.append(
            f" resolution strategy {dependency.resolution_strategy or 'none asserted'};"
            f" committed {committed};"
            f" needed {dependency.need_date or '—'}."
        )
    lines.append("")
    lines.append("You may cite ONLY these, by ref:")
    for citable in citables:
        lines.append(f"  [{citable.ref}] {citable.text}")
    floor_refs = ", ".join(floor)
    lines.append("")
    lines.append(
        "Every one of these must be cited by at least one sentence: "
        f"{floor_refs or '(none fired)'}."
    )
    return "\n".join(lines)


def _check(
    drafted: list[Sentence], by_ref: dict[str, Citable]
) -> tuple[list[Sentence], dict[str, int]]:
    """The mechanical half, and only it.

    Presence and existence: a sentence cites, its refs exist, and a cited
    evidence quote appears on its cited page — at the threshold that
    page's provenance earns, exactly (`cells`) or fuzzily (print). Whether
    the sentence's claim follows from its citation is the reviewer's work,
    the same citation-versus-truth split `verify.py` states for itself.

    Withheld loudly, per reason, never silently dropped and never shown
    unverified.
    """
    kept: list[Sentence] = []
    withheld: dict[str, int] = {}

    def withhold(reason: str) -> None:
        withheld[reason] = withheld.get(reason, 0) + 1

    for sentence in drafted:
        if not sentence.text:
            # "Never silently dropped" includes a sentence with no words:
            # the model emitted an item, and the count says so.
            withhold("empty")
            continue
        if not sentence.cites:
            withhold("uncited")
            continue
        cited = [by_ref.get(ref) for ref in sentence.cites]
        if any(citable is None for citable in cited):
            withhold("unknown citation")
            continue
        unbacked = [
            citable
            for citable in cited
            if citable.kind == "evidence"
            and not quote_appears_on(
                citable.quote or "",
                citable.page_text or "",
                threshold_for(citable.text_source),
            )
        ]
        if unbacked:
            withhold("quote not on page")
            continue
        kept.append(sentence)

    return kept, withheld


# The public seam for other Coordination Summary surfaces (#355): the same
# assembly, prompt composition, and mechanical checking this module's own
# briefings use, importable without reaching into private implementation.
assemble_citables = _assemble
compose_user_message = _user_message
check_sentences = _check


def render(briefing: Briefing) -> str:
    """The Briefing as text, stamps first — provenance before prose."""
    lines = [
        f"{label('coordination_summary')} — {briefing.ref_code}",
        f"  drafted by {briefing.model or '—'} at {briefing.prompt_version}; "
        f"constraint alerts checked {briefing.evaluated_at} under ruleset "
        f"{briefing.ruleset_version}",
        "  AI draft from the project record; no new project decisions",
        "",
    ]

    if briefing.refused:
        lines.append(f"REFUSED: {briefing.refusal_reason}")
        lines.append(
            "The required constraint-alert coverage floor is mandatory (ADR-0011); "
            "a draft that omits an alert or its complete group is not shown."
        )
    else:
        for sentence in briefing.sentences:
            lines.append(f"{sentence.text}  [{', '.join(sentence.cites)}]")

    for reason, count in sorted(briefing.withheld.items()):
        plural = "sentence" if count == 1 else "sentences"
        lines.append("")
        lines.append(f"{count} {plural} withheld: {reason}")

    lines.append("")
    lines.append("Source references:")
    for citable in briefing.citables:
        lines.append(
            f"  [{citable.ref}] {provenance_label(citable.kind)}: {citable.text}"
        )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """One record or the whole project, by the same command.

    `uv run python -m corridor.briefing <slug> <ref_code>` briefs one
    record; `<slug>` alone briefs the project — one model call either way.
    """
    import sys

    from corridor.db import WorkerSession as SessionFactory
    from corridor.llm import OpenAIClient
    from corridor.models import Project

    if not argv:
        print(
            "usage: python -m corridor.briefing <slug> [<ref_code>]",
            file=sys.stderr,
        )
        return 2

    slug, ref_code = argv[0], argv[1] if len(argv) > 1 else None
    with SessionFactory() as session:
        project = session.scalars(
            select(Project).where(Project.slug == slug)
        ).first()
        if project is None:
            print(f"no project {slug!r}", file=sys.stderr)
            return 1

        client = OpenAIClient()
        try:
            if ref_code is None:
                print(render(brief_project(session, project.id, client=client)))
            else:
                dependency = session.scalars(
                    select(Dependency).where(
                        Dependency.project_id == project.id,
                        Dependency.ref_code == ref_code,
                    )
                ).first()
                if dependency is None:
                    print(f"no record {ref_code!r} in {slug}", file=sys.stderr)
                    return 1
                print(render(brief(session, dependency.id, client=client)))
        finally:
            client.close()
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
