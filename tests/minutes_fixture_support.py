"""Synthetic adopted project and exact PDF fixtures for the minutes public seam."""

from io import BytesIO
from uuid import uuid4

from openpyxl import Workbook
from sqlalchemy import select

from corridor.baseline_adoption import preview_baseline_adoption, adopt_baseline
from corridor.ingest import ingest_document
from corridor.models import ExternalOrg, Project
from corridor.principals import HumanPrincipal
from corridor.source_intake import validate_and_stage
from pdf_fixture_support import PdfFixture


def adopted_project(session):
    project = Project(slug=f"minutes-{uuid4().hex[:10]}", name="Minutes fixture",
                      is_synthetic=True, project_side_parties=["Project Team"])
    organization = session.scalar(select(ExternalOrg).where(ExternalOrg.name == "Utility A"))
    if organization is None:
        organization = ExternalOrg(name="Utility A")
        session.add(organization)
    session.add(project)
    session.flush()
    workbook = Workbook()
    workbook.active.title = "Utility Conflicts"
    workbook.active.append(["Utility Conflict ID", "Utility Owner", "Utility Type", "Utility Conflict Description"])
    workbook.active.append(["UC-1", "Utility A", "Water", "Relocate main"])
    stream = BytesIO()
    workbook.save(stream)
    staged = validate_and_stage(stream.getvalue(), "baseline.xlsx")
    preview = preview_baseline_adoption(session, project=project, staged=staged,
        customer="synthetic", source_identity="fixture-baseline")
    adopt_baseline(session, preview=preview, principal=HumanPrincipal("local:minutes-owner"), idempotency_key="adopt")
    return project


def minutes_document(session, project, text):
    pdf = PdfFixture()
    pdf.add_page().text((45, 50), text)
    staged = validate_and_stage(pdf.tobytes(), "minutes.pdf")
    return ingest_document(session, project_id=project.id, path=staged.stored_path,
        doc_type="minutes", filename="minutes.pdf", images_dir=staged.stored_path.parent / "images")


class MinutesClient:
    """Inject only the provider boundary; references come from the real catalog."""
    model = "fixture"

    def __init__(self, kind="commitment", scope=True):
        self.kind, self.scope = kind, scope

    def complete(self, *, system, user, schema):
        import json
        catalog = json.loads(user)
        statements = []
        for source in catalog["segments"]:
            if source["role"] not in {"action_item", "body"}:
                continue
            organizations = [org for org in catalog["organizations"] if org["name"] in source["text"]]
            organization_id = organizations[0]["id"] if organizations else catalog["organizations"][0]["id"]
            timing_refs = [item["ref"] for item in catalog["timings"] if item["segment_id"] == source["id"]]
            timing = (timing_refs[-1] if self.kind == "timing_change" else timing_refs[0]) if timing_refs else None
            predecessors = catalog["predecessors"]
            statements.append({"kind": self.kind, "wording_segment_id": source["id"],
                "attribution_segment_id": source["attribution_segment_id"], "organization_id": organization_id,
                "person_id": None, "timing_ref": timing,
                "predecessor_ref": predecessors[0]["ref"] if predecessors and self.kind in {"timing_change", "completion_report"} else None,
                "scope": [{"subject_ref": subject["ref"], "segment_id": source["id"]}
                          for subject in catalog["subjects"] if self.scope and subject["reference"] in source["text"]]})
        return {"read_segment_ids": [source["id"] for source in catalog["segments"]], "statements": statements}
