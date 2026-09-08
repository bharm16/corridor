"""Purpose-specific rendering and replayable coordinate transforms."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image
import pytest
from sqlalchemy import select

from corridor.config import settings
from corridor.db import Session, engine
from corridor.models import (
    DocPage,
    Document,
    PageRenderDerivative,
    ProcessingArtifact,
    Project,
)
from corridor.render_profiles import (
    PageBox,
    RenderProfileMeasurement,
    load_render_profile_bundle,
    persist_render_derivative,
    regenerate_render_derivative,
    render_profile_identity,
    render_page_derivative,
    select_profile_dpis,
    selected_rasterizer,
)
from corridor.unreadable_cells import prepare_cell_detail_render

from pdf_fixture_support import PdfFixture


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
    fixture = PdfFixture()
    page = fixture.add_page(width=420, height=320, rotation=90, cropbox=(30, 20, 390, 300))
    page.text((60, 70), "Utility Owner")
    page.rect((55, 100, 340, 240))
    page.line((190, 100), (190, 240))
    page.line((55, 165), (340, 165))
    page.text((75, 140), "AT&T")
    page.text((215, 140), "UC-1")
    page.text((75, 210), "1149+00")
    page.text((215, 210), "1153+17")
    return fixture.save(path)


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
    assert derivative.media_box == PageBox(x0=0, y0=0, x1=420_000, y1=320_000)
    assert derivative.crop_box == PageBox(x0=30_000, y0=20_000, x1=390_000, y1=300_000)
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
    # A region crop passes through the same persistence seam, so it is Class B
    # by construction — no render path is exempt from TTL.
    crop_artifact = session.scalar(
        select(ProcessingArtifact).where(
            ProcessingArtifact.storage_path == stored.artifact_path
        )
    )
    assert crop_artifact is not None
    assert crop_artifact.kind == "page_render"
    assert crop_artifact.retention_class == "class_b"
    assert crop_artifact.content_sha256 == stored.artifact_sha256


# -- the PDFium raster path (#735) -----------------------------------------
#
# The replacement rasteriser is measured here on fixtures that declare their
# own geometry, never by demanding byte-identical PNGs from two independent
# rasterizers. `make render-rasterizer-compare` is where the corpus evidence
# with its recorded tolerances lives.

BLACK_BLOCK = (20, 30, 60, 70)


def geometry_pdf(path: Path, *, rotation: int, cropbox=(30, 40, 390, 300)) -> Path:
    """A page whose crop box is asymmetric in both axes, with a black block.

    The asymmetry matters: a crop box centred in its media box hides a
    vertical-convention error, because flipping it about the media box
    returns the same four numbers.
    """

    fixture = PdfFixture()
    page = fixture.add_page(width=420, height=320, rotation=rotation, cropbox=cropbox)
    page.image(BLACK_BLOCK, Image.new("L", (8, 8), 0))
    page.text((80, 120), "Utility Owner")
    page.rect((70, 100, 300, 200))
    return fixture.save(path)


def composed(derivative) -> tuple[float, ...]:
    matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for step in derivative.transforms:
        la, lb, lc, ld, le, lf = step.forward
        ra, rb, rc, rd, re, rf = matrix
        matrix = (
            la * ra + lc * rb,
            lb * ra + ld * rb,
            la * rc + lc * rd,
            lb * rc + ld * rd,
            la * re + lc * rf + le,
            lb * re + ld * rf + lf,
        )
    return matrix


def user_space_point(page_point, derivative) -> tuple[float, float]:
    """A fixture drawing point (crop-relative, y down) in PDF user space.

    The chain works in the thousandths of a point every Corridor page box is
    written in, so the point it is given is in those units too.
    """

    x, y = page_point
    origin = derivative.transforms[0].parameters["page_origin_y"]
    return (x * 1000 + derivative.crop_box.x0, origin - y * 1000)


def apply(matrix, point):
    a, b, c, d, e, f = matrix
    x, y = point
    return (a * x + c * y + e, b * x + d * y + f)


def test_the_replacement_rasterizer_is_off_by_default(tmp_path):
    """#447 owns selection. Merging #735 changes no production default."""

    assert settings.pdfium_render_worker is False
    assert selected_rasterizer() == "pymupdf"

    derivative = render_page_derivative(
        pdf_path=geometry_pdf(tmp_path / "default.pdf", rotation=0),
        page_number=1,
        profile_name="review",
        output_dir=tmp_path / "renders",
    )

    assert derivative.rasterizer == "pymupdf"


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_pdfium_render_reports_the_pages_own_geometry(tmp_path, rotation):
    pdf = geometry_pdf(tmp_path / f"rotated-{rotation}.pdf", rotation=rotation)

    derivative = render_page_derivative(
        pdf_path=pdf,
        page_number=1,
        profile_name="review",
        output_dir=tmp_path / "renders",
        rasterizer="pdfium",
    )

    assert derivative.rasterizer == "pdfium"
    assert derivative.rotation_degrees == rotation
    assert derivative.media_box == PageBox(x0=0, y0=0, x1=420_000, y1=320_000)
    assert derivative.crop_box == PageBox(
        x0=30_000, y0=40_000, x1=390_000, y1=300_000
    )
    crop_width, crop_height = 360, 260
    if rotation in {90, 270}:
        crop_width, crop_height = crop_height, crop_width
    assert abs(derivative.raster_width - round(crop_width * derivative.dpi / 72)) <= 1
    assert abs(derivative.raster_height - round(crop_height * derivative.dpi / 72)) <= 1
    assert [step.name for step in derivative.transforms] == [
        "pdf_user_to_page",
        "page_rotation",
        "page_to_clip",
        "clip_to_raster",
    ]
    assert derivative.max_round_trip_error <= derivative.measured_tolerance
    for step in derivative.transforms:
        point = (123_456.0, 78_901.0)
        restored = step.inverse_point(step.forward_point(point))
        assert abs(restored[0] - point[0]) <= derivative.measured_tolerance
        assert abs(restored[1] - point[1]) <= derivative.measured_tolerance


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_pdfium_transform_chain_lands_the_page_where_the_ink_is(tmp_path, rotation):
    """The chain is checked against pixels, not against itself.

    A self-consistent forward/inverse pair proves nothing about rotation or
    crop: both engines invert whatever they built. The fixture's black block
    is at a declared page-space box, so the chain must say which pixels it
    occupies, and those pixels must be the dark ones.
    """

    pdf = geometry_pdf(tmp_path / f"ink-{rotation}.pdf", rotation=rotation)

    derivative = render_page_derivative(
        pdf_path=pdf,
        page_number=1,
        profile_name="review",
        output_dir=tmp_path / "renders",
        rasterizer="pdfium",
    )

    matrix = composed(derivative)
    corners = [
        apply(matrix, user_space_point(point, derivative))
        for point in ((0, 0), (360, 0), (0, 260), (360, 260))
    ]
    assert sorted(round(x) for x, _ in corners) == sorted(
        [0, 0, derivative.raster_width, derivative.raster_width]
    )
    assert sorted(round(y) for _, y in corners) == sorted(
        [0, 0, derivative.raster_height, derivative.raster_height]
    )
    x0, y0, x1, y1 = BLACK_BLOCK
    block = [
        apply(matrix, user_space_point(point, derivative))
        for point in ((x0, y0), (x1, y1))
    ]
    left = round(min(point[0] for point in block))
    right = round(max(point[0] for point in block))
    top = round(min(point[1] for point in block))
    bottom = round(max(point[1] for point in block))
    image = Image.open(derivative.artifact_path).convert("L")
    inside = image.crop((left + 3, top + 3, right - 3, bottom - 3))

    assert max(inside.tobytes()) < 64
    assert min(image.crop((right + 6, top + 3, right + 26, bottom - 3)).tobytes()) > 200


def test_pdfium_and_the_legacy_raster_agree_within_recorded_tolerance(tmp_path):
    """Two independent rasterizers, compared by a stated measure.

    Byte equality is not the criterion and is not asserted: anti-aliasing,
    hinting and rounding differ between PDFium and MuPDF. The tolerances are
    the fixture-scale form of the corpus receipt's
    (`artifacts/render-rasterizer-comparison/`).
    """

    pdf = geometry_pdf(tmp_path / "compare.pdf", rotation=90)
    legacy, replacement = (
        render_page_derivative(
            pdf_path=pdf,
            page_number=1,
            profile_name="review",
            output_dir=tmp_path / "renders",
            rasterizer=name,
        )
        for name in ("pymupdf", "pdfium")
    )

    assert legacy.artifact_sha256 != replacement.artifact_sha256
    assert abs(legacy.raster_width - replacement.raster_width) <= 1
    assert abs(legacy.raster_height - replacement.raster_height) <= 1
    assert legacy.crop_box == replacement.crop_box
    assert legacy.media_box == replacement.media_box
    left = Image.open(legacy.artifact_path).convert("L")
    right = Image.open(replacement.artifact_path).convert("L")
    size = (min(left.width, right.width), min(left.height, right.height))
    a = left.crop((0, 0, *size)).tobytes()
    b = right.crop((0, 0, *size)).tobytes()
    differences = [abs(one - two) for one, two in zip(a, b, strict=True)]
    ink = [(one < 128, two < 128) for one, two in zip(a, b, strict=True)]
    intersection = sum(1 for one, two in ink if one and two)
    union = sum(1 for one, two in ink if one or two)

    assert sum(differences) / len(differences) / 255 <= 0.02
    assert sum(1 for value in differences if value > 32) / len(differences) <= 0.02
    assert intersection / union >= 0.90


def test_pdfium_cell_detail_clips_inside_the_rotated_page(tmp_path):
    pdf = geometry_pdf(tmp_path / "clip.pdf", rotation=90)
    clip = PageBox(x0=20_000, y0=30_000, x1=140_000, y1=110_000)

    detail = render_page_derivative(
        pdf_path=pdf,
        page_number=1,
        profile_name="cell_detail",
        output_dir=tmp_path / "renders",
        clip_page_box=clip,
        rasterizer="pdfium",
    )

    assert detail.clip_box == clip
    assert abs(detail.raster_width - round(120 * detail.dpi / 72)) <= 1
    assert abs(detail.raster_height - round(80 * detail.dpi / 72)) <= 1
    assert detail.transforms[2].parameters["box"] == clip.model_dump(mode="json")
    with pytest.raises(RuntimeError, match="outside rotated page bounds"):
        render_page_derivative(
            pdf_path=pdf,
            page_number=1,
            profile_name="cell_detail",
            output_dir=tmp_path / "renders",
            # The rotated crop is 260000 x 360000 fixed-point units.
            clip_page_box=PageBox(x0=0, y0=0, x1=260_001, y1=360_000),
            rasterizer="pdfium",
        )


def test_a_pdfium_render_is_a_new_derivative_beside_the_legacy_one(
    session, tmp_path
):
    """ADR-0072's manifest pattern: a new reading is a new identity, never an
    overwrite of the one already recorded."""

    project = Project(slug="raster-identity", name="Raster", is_synthetic=True)
    session.add(project)
    session.flush()
    pdf = geometry_pdf(tmp_path / "identity.pdf", rotation=0)
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
    legacy, replacement = (
        render_page_derivative(
            pdf_path=pdf,
            page_number=1,
            profile_name="review",
            output_dir=tmp_path / "renders",
            rasterizer=name,
        )
        for name in ("pymupdf", "pdfium")
    )

    first = persist_render_derivative(session, document.id, legacy)
    second = persist_render_derivative(session, document.id, replacement)
    session.flush()

    assert first.id != second.id
    assert first.artifact_sha256 != second.artifact_sha256
    assert first.profile_id == second.profile_id
    # The legacy engine keeps the identity every earlier derivative was
    # written under; only the replacement's key carries the engine.
    assert first.derivative_key == hashlib.sha256(
        json.dumps(
            {
                "document_id": document.id,
                "page_number": 1,
                "profile_id": legacy.profile_id,
                "clip_box": None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert second.manifest_json["rasterizer"] == "pdfium"
    regenerated = regenerate_render_derivative(
        second, pdf_path=pdf, output_dir=tmp_path / "regenerated"
    )
    # Regeneration replays the engine the derivative was made with, not the
    # engine this deployment currently selects.
    assert regenerated.rasterizer == "pdfium"
    assert regenerated.artifact_sha256 == second.artifact_sha256
