"""The one live check the production path makes: is this pipeline selected?

`pipeline_qualification` records measurements and settles selection: a gate, a
maintainer's acceptance, a policy, a scoped selection - thirteen functions, all
of them operator maintenance acts driven from a CLI by an attributable human.
Exactly one of them runs while a Document is being read, and reads nothing but
what was already decided. Keeping it in the same module meant the production
extraction route imported the recording commands to reach the readback, so an
accidental call to one of them was an import away from the fact path.

So the readback lives here, with the refusal type it raises and the receipt
reader both sides need, and nothing that writes. ADR-0095 is untouched by the
split: an acceptance is not a passing gate and is never recorded as one, and
this readback still refuses on either basis exactly as the selection command
does - a gate that is not complete and passing, or an acceptance that is not
the one the selection names.
"""

from __future__ import annotations

from hashlib import sha256
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import (
    Document, PipelineAcceptance, PipelineComparison, PipelineConfiguration,
    PipelineObservation, PipelineQualification, PipelineSelection, Project,
)
from corridor.pipeline_contracts import PipelineScope, canonical_text


class PipelineQualificationRefused(ValueError):
    """Missing, stale, mismatched or unauthorized qualification evidence."""


def pipeline_receipt(row) -> dict:
    record = json.loads(row.receipt_text)
    if (sha256(row.receipt_text.encode()).hexdigest() != row.receipt_sha256
            or record["project_id"] != row.project_id
            or record["configuration_sha256"] != row.configuration_sha256
            or record["scope_sha256"] != row.scope_sha256):
        raise PipelineQualificationRefused("pipeline receipt bytes or scope binding differ")
    if isinstance(row, (PipelineObservation, PipelineQualification, PipelineAcceptance, PipelineSelection)):
        try:
            scope = PipelineScope.model_validate(record["scope"])
        except (KeyError, ValueError) as exc:
            raise PipelineQualificationRefused("pipeline receipt has no valid declared scope") from exc
        if scope.identity != row.scope_sha256:
            raise PipelineQualificationRefused("parsed pipeline scope differs from its stored digest")
        if "scope_text" in record and record["scope_text"] != canonical_text(scope.model_dump(mode="json")):
            raise PipelineQualificationRefused("pipeline scope bytes differ from the parsed scope")
    if isinstance(row, PipelineQualification):
        if (record.get("status") != row.status
                or any(not isinstance(record.get(name), list) or any(not isinstance(value, str) for value in record[name])
                       for name in ("missing", "failed"))
                or (row.status == "passed" and (record["missing"] or record["failed"]))):
            raise PipelineQualificationRefused("pipeline gate requires typed missing/failed evidence lists and its exact status")
    if isinstance(row, PipelineAcceptance):
        # ADR-0095. The two bases must never read alike, so an acceptance may
        # not carry a gate's vocabulary, and every field the decision requires
        # is read back from the bytes rather than trusted from the writer.
        if (record.get("schema") != "pipeline-acceptance-v1"
                or record.get("basis") != "maintainer_acceptance"
                or {"status", "missing", "failed"} & set(record)):
            raise PipelineQualificationRefused("a maintainer acceptance is never recorded as a qualification gate")
        if (record.get("actor") != row.actor
                or record.get("implementation_revision") != row.implementation_revision
                or not str(record.get("words", "")).strip()
                or not str(record.get("decision", "")).strip()
                or not str(record.get("accepted_at", "")).strip()
                or not isinstance(record.get("limits"), list) or not record["limits"]
                or any(not isinstance(value, str) or not value.strip() for value in record["limits"])
                or not isinstance(record.get("evidence"), list) or not record["evidence"]
                or any(not isinstance(item, dict)
                       or any(not str(item.get(name, "")).strip() for name in ("name", "reference", "summary"))
                       for item in record["evidence"])):
            raise PipelineQualificationRefused(
                "a maintainer acceptance needs its attributable principal, its own words, named reachable evidence and stated limits")
    if isinstance(row, PipelineComparison) and (record.get("kind") != row.kind or type(record.get("passed")) is not bool):
        raise PipelineQualificationRefused("pipeline comparison kind or result is malformed")
    return record


def selected_pipeline_configuration(
    session: Session, document: Document, *, deployment: str,
    configuration_sha256: str,
) -> PipelineScope:
    """Require the selected source/deployment/configuration before future work."""
    selection = session.scalar(select(PipelineSelection).where(
        PipelineSelection.project_id == document.project_id, PipelineSelection.deployment == deployment,
    ).order_by(PipelineSelection.id.desc()).limit(1))
    if selection is None or not selection.enabled:
        raise PipelineQualificationRefused("no enabled qualified pipeline selection for this deployment")
    record = pipeline_receipt(selection)
    # The readback refuses on either basis exactly as the selection command
    # does: a gate that is not passing, or an acceptance that is not the one
    # this selection names. What an acceptance settles is whether the
    # configuration is good enough, never who may run it, on what, or what it
    # may write, so every other check below is unchanged (ADR-0095).
    basis = record.get("basis")
    if basis == "qualification" and selection.qualification_id is not None and selection.acceptance_id is None:
        basis_row: PipelineQualification | PipelineAcceptance = session.get_one(PipelineQualification, selection.qualification_id)
        basis_record = pipeline_receipt(basis_row)
        if (basis_row.status != "passed" or basis_record["status"] != "passed"
                or basis_row.receipt_sha256 != record["qualification_sha256"]):
            raise PipelineQualificationRefused("selected pipeline stands on a gate that is not complete and passing")
    elif basis == "maintainer_acceptance" and selection.acceptance_id is not None and selection.qualification_id is None:
        basis_row = session.get_one(PipelineAcceptance, selection.acceptance_id)
        basis_record = pipeline_receipt(basis_row)
        if basis_row.receipt_sha256 != record["acceptance_sha256"]:
            raise PipelineQualificationRefused("selected pipeline stands on a different acceptance than the one it names")
    else:
        raise PipelineQualificationRefused("selected pipeline does not name exactly one recorded basis")
    scope = PipelineScope.model_validate(record["scope"])
    if (record["scope"] != basis_record["scope"] or basis_row.scope_sha256 != selection.scope_sha256
            or basis_row.project_id != selection.project_id
            or basis_row.configuration_sha256 != selection.configuration_sha256
            or selection.configuration_sha256 != configuration_sha256
            or document.sha256 not in scope.source_sha256s or document.doc_type != "matrix"
            or (scope.purpose == "synthetic_validation" and not session.get_one(Project, document.project_id).is_synthetic)):
        raise PipelineQualificationRefused("selected pipeline does not qualify this exact source/configuration/class")
    configuration = session.get_one(PipelineConfiguration, configuration_sha256)
    if sha256(configuration.configuration_text.encode()).hexdigest() != configuration_sha256:
        raise PipelineQualificationRefused("selected configuration bytes differ")
    return scope
