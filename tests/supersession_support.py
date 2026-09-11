"""One registered Document Supersession, and the stages that reach it.

A Supersession scenario is always the same chain of domain acts: a source
document that carries the declaration, a predecessor and a successor revision
with pages, Extracted Proposals read from those revisions, completed Extraction
Runs that own them, a declared Active Run, and a registered
``SupersessionDeclaration`` linking the pair.

That chain was written twice, independently. ``test_supersession_review`` built
it in 125 lines under ``REV-A``/``REV-B``; ``test_web`` built it in 154 lines
under ``RID-100``/``RID-101``. Both returned an untyped dict, so every consumer
spelled the stage it wanted as a key string -- 418 of them in one module -- and
a stage the builder did not put in the dict failed at the reading line rather
than at construction. Neither dict could gain a stage without every consumer
learning a new string.

The chain is a dataclass here instead. A stage is an attribute, so a consumer
that wants one asks for it by name; a stage that was never built is ``None``
rather than a missing key, and a name that does not exist is an
``AttributeError`` at the reading line rather than a ``KeyError``. Adding a
stage to the chain adds an attribute, and nothing that does not want it changes.

The chain is built in stages rather than by one call with a flag per stage,
because the two modules genuinely need different orders: the review module
accepts the predecessor's row before the successor is registered, the web
module registers first and then accepts under an explicit historical override.
Each stage returns a new chain naming what it produced, so the caller writes
the order it means.

**Extracted Proposal payloads are not built here.** Each module builds its own
matrix rows -- different organizations, different fields, different prompt
versions -- and passes the Proposals it built to ``extracted``. What is settled
here is the chain, not the payload.

**Every run this module records is unsealed.** ``record_extraction_run``
requires exact extractor configuration and token usage unless the caller passes
``allow_unsealed_legacy=True``, and no Supersession consumer builds a sealed
configuration: a synthetic revision pair has no real prompt bytes to seal. The
flag is passed in exactly one place below so the decision is visible once
instead of being an accident of which builder a test started from.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.adjudicate import accept_candidate
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.ledger import mark_satisfies
from corridor.models import (
    Candidate,
    Dependency,
    DocPage,
    Document,
    EvidenceLink,
    ExtractionRun,
    Project,
)
from corridor.principals import HumanPrincipal
from corridor.supersession import SupersessionDeclaration, register_supersessions

@dataclass(frozen=True)
class SupersededChain:
    """One revision pair, the source that declares it, and every stage built.

    A stage that has not been built yet is ``None``. The building methods
    return a new chain naming what they produced rather than mutating this
    one, so a test reads the stage it asked for and nothing else silently
    appears.
    """

    session: Session
    project: Project
    index: Document
    predecessor: Document
    successor: Document
    declaration: SupersessionDeclaration
    # The human whose acts the remaining stages are recorded under. A chain
    # that only declares a revision pair performs no human act and names none;
    # the act that needs one refuses a chain without it.
    principal: HumanPrincipal | None = None
    predecessor_proposals: tuple[Candidate, ...] = ()
    predecessor_run: ExtractionRun | None = None
    successor_proposals: tuple[Candidate, ...] = ()
    successor_run: ExtractionRun | None = None
    dependency: Dependency | None = None
    old_evidence: EvidenceLink | None = None

    @property
    def predecessor_proposal(self) -> Candidate:
        """The predecessor's first Extracted Proposal."""

        return self.predecessor_proposals[0]

    @property
    def successor_proposal(self) -> Candidate:
        """The successor's first Extracted Proposal."""

        return self.successor_proposals[0]

    def register(self) -> tuple[Document, ...]:
        """Register the declared edge between the two revisions."""

        registered = register_supersessions(
            self.session, [self.declaration], project_id=self.project.id
        )
        self.session.flush()
        return registered

    def extracted(
        self,
        document: Document,
        *proposals: Candidate,
        active: bool = True,
        outcome: str = "completed",
        page_errors: int = 0,
        error_detail: str | None = None,
        prompt_version: str | None = None,
        model: str | None = None,
        schema_version: str | None = None,
    ) -> SupersededChain:
        """Record one terminal Extraction Run over `document` and name it.

        `document` must be this chain's predecessor or successor, so the run
        lands on the attribute a consumer reads. Lineage defaults to what the
        Proposals themselves carry, because ``record_extraction_run`` refuses
        a run whose prompt version or model disagrees with its Candidates.
        """

        if document is not self.predecessor and document is not self.successor:
            raise ValueError(
                "an Extraction Run belongs to one of this chain's revisions"
            )
        run = active_run(
            self.session,
            document,
            *proposals,
            principal=self.principal,
            active=active,
            outcome=outcome,
            page_errors=page_errors,
            error_detail=error_detail,
            prompt_version=prompt_version,
            model=model,
            schema_version=schema_version,
        )
        if document is self.predecessor:
            return replace(
                self, predecessor_proposals=proposals, predecessor_run=run
            )
        return replace(self, successor_proposals=proposals, successor_run=run)

    def activate_successor(self) -> ExtractionRun:
        """Declare the successor's recorded run the operative reading."""

        if self.successor_run is None:
            raise ValueError("the successor has no Extraction Run to declare active")
        run = declare_active_run(
            self.session,
            self.successor.id,
            self.successor_run.id,
            principal=self.principal,
        )
        self.session.flush()
        return run

    def accepted(
        self,
        proposal: Candidate | None = None,
        *,
        satisfying: bool = False,
        historical: bool = False,
    ) -> SupersededChain:
        """Accept one predecessor Proposal into the record and name what it wrote.

        `historical` is the explicit exact-document override an already
        superseded predecessor needs; before registration the predecessor is
        still current and no override applies.
        """

        if proposal is None:
            proposal = self.predecessor_proposal
        dependency = accept_candidate(
            self.session,
            proposal,
            principal=self.principal,
            historical_document_id=self.predecessor.id if historical else None,
        )
        old_evidence = self.session.scalars(
            select(EvidenceLink)
            .where(EvidenceLink.dependency_id == dependency.id)
            .order_by(EvidenceLink.id)
        ).first()
        assert old_evidence is not None
        if satisfying:
            mark_satisfies(
                self.session,
                dependency.id,
                old_evidence.id,
                principal=self.principal,
            )
        self.session.flush()
        return replace(self, dependency=dependency, old_evidence=old_evidence)


def superseded_chain(
    session: Session,
    project: Project,
    *,
    principal: HumanPrincipal | None = None,
    predecessor_registry_id: str = "REV-A",
    successor_registry_id: str = "REV-B",
    index_registry_id: str = "INDEX",
    predecessor_text: str = "",
    successor_text: str = "",
    index_text: str | None = None,
    replacement_date: date = date(2026, 8, 1),
    source_page: int = 1,
    predecessor_date: date | None = None,
    successor_date: date | None = None,
    register: bool = True,
) -> SupersededChain:
    """Build one revision pair and the source document that declares the edge.

    Nothing is extracted or accepted here: an Extraction Run and an accepted
    row are separate stages, because the two consuming modules build them in
    different orders and one of them wants a predecessor with no successor run
    at all.
    """

    index = _revision(
        session,
        project,
        registry_id=index_registry_id,
        doc_type="other",
        page_no=source_page,
        text=(
            index_text
            if index_text is not None
            else (
                f"{predecessor_registry_id} superseded by "
                f"{successor_registry_id} on {replacement_date.isoformat()}"
            )
        ),
    )
    predecessor = _revision(
        session,
        project,
        registry_id=predecessor_registry_id,
        text=predecessor_text,
        doc_date=predecessor_date,
    )
    successor = _revision(
        session,
        project,
        registry_id=successor_registry_id,
        text=successor_text,
        doc_date=successor_date,
    )
    chain = SupersededChain(
        session=session,
        project=project,
        principal=principal,
        index=index,
        predecessor=predecessor,
        successor=successor,
        declaration=SupersessionDeclaration(
            predecessor_registry_id=predecessor.registry_id,
            successor_registry_id=successor.registry_id,
            replacement_date=replacement_date,
            source_registry_id=index.registry_id,
            source_page=source_page,
        ),
    )
    if register:
        chain.register()
    return chain


def active_run(
    session: Session,
    document: Document,
    *proposals: Candidate,
    principal: HumanPrincipal | None,
    active: bool = True,
    outcome: str = "completed",
    page_errors: int = 0,
    error_detail: str | None = None,
    prompt_version: str | None = None,
    model: str | None = None,
    schema_version: str | None = None,
) -> ExtractionRun:
    """Record one terminal Extraction Run over a revision and declare it operative.

    Lineage defaults to what the Proposals themselves carry, because
    ``record_extraction_run`` refuses a run whose prompt version or model
    disagrees with its Candidates.

    This is the one place a Supersession scenario waives sealed lineage; see
    the module docstring for why every one of them waives it.
    """

    if prompt_version is None:
        if not proposals:
            raise ValueError("a run with no Proposals must state its prompt_version")
        prompt_version = proposals[0].prompt_version
    if model is None and proposals:
        model = proposals[0].model
    run = record_extraction_run(
        session,
        document,
        prompt_version=prompt_version,
        candidate_count=len(proposals),
        page_errors=page_errors,
        outcome=outcome,
        candidates=proposals,
        model=model,
        schema_version=schema_version,
        error_detail=error_detail,
        allow_unsealed_legacy=True,
    )
    if active:
        declare_active_run(session, document.id, run.id, principal=principal)
    session.flush()
    return run


def _revision(
    session: Session,
    project: Project,
    *,
    registry_id: str,
    text: str,
    doc_type: str = "matrix",
    page_no: int = 1,
    doc_date: date | None = None,
) -> Document:
    """One registered revision of the project's matrix, with its cited page."""

    document = Document(
        project_id=project.id,
        registry_id=registry_id,
        sha256=sha256(f"{project.id}:{registry_id}".encode()).hexdigest(),
        filename=f"{registry_id.lower()}.pdf",
        doc_type=doc_type,
        parse_status="parsed",
        doc_date=doc_date,
        pages=page_no,
    )
    session.add(document)
    session.flush()
    session.add(
        DocPage(
            document_id=document.id,
            page_no=page_no,
            text=text,
            image_path=f"/tmp/{registry_id.lower()}-p{page_no}.png",
        )
    )
    session.flush()
    return document
