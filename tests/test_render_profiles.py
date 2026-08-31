"""Purpose-specific rendering and replayable coordinate transforms."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.models import DocPage, Document, PageRenderDerivative, Project
from corridor.render_profiles import (
    PageBox,
    RenderProfileMeasurement,
    load_render_profile_bundle,
    persist_render_derivative,
    regenerate_render_derivative,
    render_profile_identity,
    render_page_derivative,
    select_profile_dpis,
)
from corridor.unreadable_cells import prepare_cell_detail_render


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    value = Session(bind=connection)
    yield value
    value.close()
    transaction.rollback()
    connection.close()


def synthetic_pdf(path: Path) -> Path:
    document = pymupdf.open()
    page = document.new_page(width=420, height=320)
    page.set_cropbox(pymupdf.Rect(30, 20, 390, 300))
    page.set_rotation(90)
    page.insert_text((60, 70), "Utility Owner")
    page.draw_rect(pymupdf.Rect(55, 100, 340, 240))
    page.draw_line((190, 100), (190, 240))
    page.draw_line((55, 165), (340, 165))
    page.insert_text((75, 140), "AT&T")
    page.insert_text((215, 140), "UC-1")
    page.insert_text((75, 210), "1149+00")
    page.insert_text((215, 210), "1153+17")
    document.save(path)
    document.close()
    return path


def test_opencv_is_locked_only_in_the_render_worker():
    core_project = Path("pyproject.toml").read_text()
    core_lock = Path("uv.lock").read_text()
    worker_project = Path("workers/render/pyproject.toml").read_text()
    worker_lock = Path("workers/render/uv.lock").read_text()

    assert "opencv" not in core_project.lower()
    assert "opencv" not in core_lock.lower()
    assert "opencv-python-headless" in worker_project
    assert 'name = "opencv-python-headless"' in worker_lock


def test_profiles_are_selected_by_a_recorded_gold_measurement():
    bundle = load_render_profile_bundle()

    assert bundle.schema_version == "corridor.render-profiles.v1"
    assert bundle.pdf_gold_dataset_version == "2026-08-31.2"
    assert bundle.measurement_sha256 == hashlib.sha256(
        Path("gold/pdf/v1/render-profile-measurement.json").read_bytes()
    ).hexdigest()
    assert set(bundle.profiles) == {"review", "ocr_layout", "table_cv", "cell_detail"}
    assert bundle.profiles["review"].preprocessing == ()
    assert bundle.profiles["ocr_layout"].dpi > bundle.profiles["review"].dpi
    assert bundle.profiles["cell_detail"].requires_clip is True
    measurement = RenderProfileMeasurement.model_validate_json(
        Path("gold/pdf/v1/render-profile-measurement.json").read_text()
    )
    assert select_profile_dpis(measurement) == measurement.selected_dpis
    review = bundle.profiles["review"]
    changed = review.model_copy(
        update={"parameters": {**review.parameters, "format": "jpeg"}}
    )
    assert render_profile_identity(changed) != review.profile_id


def test_review_render_round_trips_rotated_cropped_page_without_preprocessing(
    tmp_path,
):
    pdf = synthetic_pdf(tmp_path / "rotated.pdf")

    derivative = render_page_derivative(
        pdf_path=pdf,
        page_number=1,
        profile_name="review",
        output_dir=tmp_path / "renders",
    )

    assert derivative.profile_name == "review"
    assert derivative.preprocessing == ()
    assert derivative.retention_class == "intermediary_processing"
    assert derivative.regenerable_from.source_sha256 == hashlib.sha256(
        pdf.read_bytes()
    ).hexdigest()
    assert derivative.artifact_path.exists()
    assert derivative.artifact_sha256 == hashlib.sha256(
        derivative.artifact_path.read_bytes()
    ).hexdigest()
    assert derivative.rotation_degrees == 90
    assert derivative.media_box != derivative.crop_box
    assert abs(
        derivative.raster_width
        - round(
            (derivative.crop_box.y1 - derivative.crop_box.y0)
            * derivative.dpi
            / 72_000
        )
    ) <= 1
    assert abs(
        derivative.raster_height
        - round(
            (derivative.crop_box.x1 - derivative.crop_box.x0)
            * derivative.dpi
            / 72_000
        )
    ) <= 1
    assert derivative.max_round_trip_error <= derivative.measured_tolerance
    assert [step.name for step in derivative.transforms] == [
        "pdf_user_to_page",
        "page_rotation",
        "page_to_clip",
        "clip_to_raster",
    ]
    for step in derivative.transforms:
        point = (123_456.0, 78_901.0)
        restored = step.inverse_point(step.forward_point(point))
        assert abs(restored[0] - point[0]) <= derivative.measured_tolerance
        assert abs(restored[1] - point[1]) <= derivative.measured_tolerance


def test_table_cv_is_a_separate_preprocessed_derivative(tmp_path):
    pdf = synthetic_pdf(tmp_path / "table.pdf")
    review = render_page_derivative(
        pdf_path=pdf,
        page_number=1,
        profile_name="review",
        output_dir=tmp_path / "renders",
    )
    table = render_page_derivative(
        pdf_path=pdf,
        page_number=1,
        profile_name="table_cv",
        output_dir=tmp_path / "renders",
    )

    assert table.artifact_path != review.artifact_path
    assert table.artifact_sha256 != review.artifact_sha256
    assert table.preprocessing == (
        "grayscale",
        "denoise",
        "adaptive_threshold",
        "line_morphology",
    )
    assert [step.name for step in table.processing_steps] == list(
        table.preprocessing
    )
    assert all(step.geometry_change is False for step in table.processing_steps)
    assert "deskew" in [step.name for step in table.transforms]
    assert table.max_round_trip_error <= table.measured_tolerance
    # Creating CV pixels never mutates or replaces the reviewer rendition.
    assert review.artifact_sha256 == hashlib.sha256(
        review.artifact_path.read_bytes()
    ).hexdigest()


def test_cell_detail_crop_has_its_own_clip_and_high_resolution_transform(tmp_path):
    pdf = synthetic_pdf(tmp_path / "cell.pdf")
    clip = PageBox(x0=60_000, y0=105_000, x1=185_000, y1=160_000)

    detail = render_page_derivative(
        pdf_path=pdf,
        page_number=1,
        profile_name="cell_detail",
        output_dir=tmp_path / "renders",
        clip_page_box=clip,
    )

    assert detail.clip_box == clip
    assert detail.dpi == 600
    assert abs(
        detail.raster_width - round((clip.x1 - clip.x0) * detail.dpi / 72_000)
    ) <= 1
    assert abs(
        detail.raster_height - round((clip.y1 - clip.y0) * detail.dpi / 72_000)
    ) <= 1
    assert detail.max_round_trip_error <= detail.measured_tolerance
    assert "page_to_clip" in [step.name for step in detail.transforms]


def test_cell_detail_rejects_a_clip_outside_the_rotated_page(tmp_path):
    pdf = synthetic_pdf(tmp_path / "overflow.pdf")

    with pytest.raises(RuntimeError, match="outside rotated page bounds"):
        render_page_derivative(
            pdf_path=pdf,
            page_number=1,
            profile_name="cell_detail",
            output_dir=tmp_path / "renders",
            # Rotated crop bounds are 280000 x 360000 fixed-point units.
            clip_page_box=PageBox(x0=0, y0=0, x1=280_001, y1=360_000),
        )


def test_derivative_manifest_is_persisted_idempotently_and_regenerates(
    session, tmp_path
):
    project = Project(slug="render-test", name="Render Test", is_synthetic=True)
    session.add(project)
    session.flush()
    pdf = synthetic_pdf(tmp_path / "source.pdf")
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
        filename=pdf.name,
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    derivative = render_page_derivative(
        pdf_path=pdf,
        page_number=1,
        profile_name="review",
        output_dir=tmp_path / "renders",
    )

    first = persist_render_derivative(session, document.id, derivative)
    second = persist_render_derivative(session, document.id, derivative)
    same_content_elsewhere = render_page_derivative(
        pdf_path=pdf,
        page_number=1,
        profile_name="review",
        output_dir=tmp_path / "same-content-elsewhere",
    )
    third = persist_render_derivative(session, document.id, same_content_elsewhere)
    session.flush()

    assert first.id == second.id == third.id
    [stored] = session.scalars(
        select(PageRenderDerivative).where(
            PageRenderDerivative.document_id == document.id
        )
    ).all()
    assert stored.retention_class == "intermediary_processing"
    assert stored.source_sha256 == document.sha256
    assert stored.manifest_json["transforms"]
    regenerated = regenerate_render_derivative(
        stored,
        pdf_path=pdf,
        output_dir=tmp_path / "regenerated",
    )
    assert regenerated.artifact_sha256 == stored.artifact_sha256


def test_unreadable_cell_path_persists_a_bounded_high_resolution_crop(
    session, tmp_path
):
    project = Project(slug="cell-detail-test", name="Cell Detail", is_synthetic=True)
    session.add(project)
    session.flush()
    pdf = synthetic_pdf(tmp_path / "cell-source.pdf")
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
        filename=pdf.name,
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    page = DocPage(
        document_id=document.id,
        page_no=1,
        text="",
        text_source="ocr",
        image_path=str(tmp_path / "review.png"),
    )
    session.add(page)
    session.flush()

    stored = prepare_cell_detail_render(
        session,
        document=document,
        page=page,
        cell_page_box=PageBox(x0=60_000, y0=105_000, x1=185_000, y1=160_000),
        source_path=pdf,
    )

    assert stored.profile_name == "cell_detail"
    assert stored.manifest_json["clip_box"] == {
        "x0": 60_000,
        "y0": 105_000,
        "x1": 185_000,
        "y1": 160_000,
    }
    assert stored.manifest_json["dpi"] == 600
