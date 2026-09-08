"""Source-authored geometry across inventory, rendering and routed pixel crops.

Earlier stage tests proved inventory boxes, render transforms and native
locators separately. These fixtures join those public seams and discriminate
wrong pixels from an internally consistent inverse. They qualify this geometry
handoff only; they neither evaluate OCR nor resolve the corpus raster outliers.
"""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw
import pytest

from corridor.config import settings
from corridor.models import SourceSegment
from corridor.page_inventory import (
    READER_COORDINATE_FRAME,
    PdfRect,
    read_page_facts,
    reader_page_inventories,
    route_reader_page,
)
from corridor.reader_segments import (
    native_segment_pdf_boxes,
    native_segment_render_boxes,
    native_segment_values,
)
from corridor.render_profiles import (
    DEFAULT_WORKER_PROJECT,
    LEGACY_RASTERIZER,
    RenderDerivative,
    crop_routed_region,
    load_render_profile_bundle,
    render_page_derivative,
    render_profile_identity,
    worker_environment,
)
from corridor.token_layers import read_native_pdf, render_point_to_pdf
from pdf_fixture_support import PdfFixture


MEDIA_HEIGHT = 420
CROP = (37, 29, 453, 371)
CROP_WIDTH, CROP_HEIGHT = 416, 342
COLOURS = {
    "red": (225, 30, 30),
    "blue": (25, 55, 230),
    "yellow": (240, 190, 20),
    "cyan": (15, 200, 200),
}
IMAGE_ANCHORS = {"red": (60, 40), "blue": (282, 22),
                 "yellow": (80, 176), "cyan": (180, 120)}
MAGENTA = (230, 20, 210)


def _original_point(point, skew=0):
    """Authored crop-relative points to the PDF operators' original frame.

    Deliberately independent of reader geometry and derivative metadata.
    """
    x, y = CROP[0] + point[0], MEDIA_HEIGHT - CROP[1] - point[1]
    cosine, sine = math.cos(math.radians(skew)), math.sin(math.radians(skew))
    return cosine * x - sine * y, sine * x + cosine * y


def _display_point(point, rotation):
    x, y = point[0] - CROP[0], MEDIA_HEIGHT - CROP[1] - point[1]
    return {
        0: (x, y),
        90: (CROP_HEIGHT - y, x),
        180: (CROP_WIDTH - x, CROP_HEIGHT - y),
        270: (y, CROP_WIDTH - x),
    }[rotation]


def _bounds(points):
    points = tuple(points)
    return (min(x for x, _ in points), min(y for _, y in points),
            max(x for x, _ in points), max(y for _, y in points))


def _box_tuple(box):
    return box.x0, box.y0, box.x1, box.y1


@dataclass(frozen=True)
class AuthoredPage:
    path: Path
    rotation: int
    image_display_box: tuple[float, float, float, float]
    source_anchors: dict[str, tuple[float, float]]
    source_line_points: tuple[tuple[float, float], tuple[float, float]]
    outside_source_point: tuple[float, float] | None


def _author_page(tmp_path, *, rotation, clipped=False, skew=0):
    fixture = PdfFixture()
    page = fixture.add_page(
        width=500, height=MEDIA_HEIGHT, rotation=rotation, cropbox=CROP,
    )
    image_box = (310, 240, 480, 390) if clipped else (45, 90, 340, 295)
    x0, y0, x1, y1 = image_box
    image = Image.new("RGB", (300, 200), "white")
    draw = ImageDraw.Draw(image)
    for colour, (x, y) in IMAGE_ANCHORS.items():
        # Small marks with a white moat: a skipped deskew misses the colour,
        # unlike a large uniform block which tolerates the wrong transform.
        draw.rectangle((x - 1, y - 1, x + 1, y + 1), fill=COLOURS[colour])
    cosine, sine = math.cos(math.radians(skew)), math.sin(math.radians(skew))
    page._content.append(f"q {cosine} {sine} {-sine} {cosine} 0 0 cm\n")
    page.text((85, 35), "HEADER")
    page.image(image_box, image)
    if skew:
        for y in (120, 165, 205, 260):
            page.line((65, y), (320, y))
    page._content.append("Q\n")
    image_corners = tuple(
        _original_point(point, skew)
        for point in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    )
    display_box = _bounds(_display_point(p, rotation) for p in image_corners)
    anchors = {
        colour: _original_point(
            (x0 + x / 300 * (x1 - x0), y0 + y / 200 * (y1 - y0)), skew,
        )
        for colour, (x, y) in IMAGE_ANCHORS.items()
    }
    outside = None
    if skew:
        unrotated = _bounds(_display_point(p, 0) for p in image_corners)
        outside = _original_point((unrotated[0] - 2, (unrotated[1] + unrotated[3]) / 2))
        # This vector mark is outside image permission, but inside the AABB
        # created by deskew. It must exist in the full render and be masked in
        # the routed crop; it is not another embedded-image routing region.
        x, y = outside
        red, green, blue = (component / 255 for component in MAGENTA)
        page._content.append(
            f"q {red} {green} {blue} rg {x - .6} {y - .6} 1.2 1.2 re f Q\n"
        )
    return AuthoredPage(
        fixture.save(tmp_path / "authored.pdf"), rotation, display_box, anchors,
        (_original_point((100, 120), skew), _original_point((280, 120), skew)), outside,
    )


def _render_point(derivative, source_point):
    point = tuple(coordinate * 1000 for coordinate in source_point)
    for step in derivative.transforms:
        point = step.forward_point(point)
    return point


def _has_colour(image, point, colour):
    x, y = (math.floor(value) for value in point)
    for row in range(max(0, y - 1), min(image.height, y + 2)):
        for column in range(max(0, x - 1), min(image.width, x + 2)):
            if all(abs(a - b) < 45 for a, b in zip(image.getpixel((column, row)), colour)):
                return True
    return False


def _line_alignment(image, derivative, authored):
    """Measure actual black pixels at two authored positions on one rule.

    The fixture's displayed frame and actual raster preprocessing locate the
    rule, but cannot answer whether its pixels are level. Two separated pixel
    centres determine that independently. Historical legacy source-frame
    metadata stays untouched; this helper does not validate its locators.
    The authored page rotation decides horizontal versus vertical, since a
    nearest-axis assertion could hide a wrong quarter turn. PDFium's complete
    source transform is checked separately against native glyphs and colours.
    """
    pixels = []
    vertical = authored.rotation in (90, 270)
    for source in authored.source_line_points:
        point = tuple(value * 1000 for value in _display_point(source, authored.rotation))
        for step in derivative.transforms[2:]:
            point = step.forward_point(point)
        x, y = point
        fixed, expected = (round(y), x) if vertical else (round(x), y)
        ink = []
        for cross in range(math.floor(expected) - 15, math.ceil(expected) + 16):
            point = (cross, fixed) if vertical else (fixed, cross)
            if max(image.getpixel(point)) < 70:
                ink.append(cross)
        assert ink, (source, x, y)
        centre = sum(ink) / len(ink)
        pixels.append((centre, fixed) if vertical else (fixed, centre))
    dx, dy = pixels[1][0] - pixels[0][0], pixels[1][1] - pixels[0][1]
    deviation = math.degrees(math.atan2(dx, abs(dy)) if vertical else math.atan2(dy, abs(dx)))
    return {"pixel_anchors": pixels, "axis_deviation_degrees": deviation}


def _inventory_render_and_header(authored, tmp_path, monkeypatch, profile="review"):
    digest = sha256(authored.path.read_bytes()).hexdigest()
    facts = read_page_facts(authored.path)
    inventory = reader_page_inventories(facts)[1]
    native = read_native_pdf(authored.path, source_sha256=digest)
    assert facts["pages"][0]["characters"] == native.pages[0]["characters"]
    value, = (v for v in native_segment_values(native) if v.exact_text == "HEADER")
    assert value.kind == "pdf_span"
    header = SourceSegment(document_id=1, project_id=1, **asdict(value))
    assert header.rendition_sha256 == digest
    assert header.reading_sha256 == native.reading_sha256
    assert inventory.native_glyph_count == len("HEADER")
    assert inventory.coordinate_frame == READER_COORDINATE_FRAME
    assert inventory.rotation_degrees == authored.rotation
    assert len(inventory.image_regions) == 1
    # Integer millipoints round the independently authored floating bounds.
    assert _box_tuple(inventory.image_regions[0].box) == pytest.approx(
        [v * 1000 for v in authored.image_display_box], abs=.55,
    )
    routing = route_reader_page(inventory)
    assert routing.page_mode == "both"
    assert routing.reason == "mixed_native_and_image_regions"
    assert len(routing.regions) == 2
    native_region, = (r for r in routing.regions if r.mode == "native")
    image_region, = (r for r in routing.regions if r.mode == "ocr")
    assert native_region.box == inventory.boxes.crop
    assert image_region.box == inventory.image_regions[0].box
    monkeypatch.setattr(settings, "pdfium_render_worker", True)
    derivative = render_page_derivative(
        pdf_path=authored.path, page_number=1, profile_name=profile,
        output_dir=tmp_path / "full",
    )
    assert derivative.rasterizer == "pdfium"
    assert derivative.parameters["preprocessing_version"] == "pdfium-deskew-v2"
    assert derivative.regenerable_from.source_sha256 == digest
    assert _box_tuple(derivative.crop_box) == tuple(v * 1000 for v in CROP)
    width, height = (CROP_HEIGHT, CROP_WIDTH) if authored.rotation % 180 else (CROP_WIDTH, CROP_HEIGHT)
    assert _box_tuple(inventory.boxes.crop) == (0, 0, width * 1000, height * 1000)
    (tmp_path / "inventory.json").write_text(inventory.model_dump_json(indent=2))
    (tmp_path / "full.json").write_text(derivative.model_dump_json(indent=2))
    with Image.open(derivative.artifact_path) as image:
        gray = image.convert("L")
        for box in native_segment_render_boxes(header, derivative):
            left, top, right, bottom = box
            assert 0 <= left < right <= gray.width and 0 <= top < bottom <= gray.height
            ink = gray.crop((math.floor(left), math.floor(top), math.ceil(right), math.ceil(bottom)))
            assert min(ink.tobytes()) < 80
    return inventory, image_region, derivative, header


def _assert_region_round_trip(crop, authored):
    visible = crop.visible_display_box
    expected_display = ((visible.x0, visible.y0), (visible.x1, visible.y0),
                        (visible.x1, visible.y1), (visible.x0, visible.y1))
    for source, display in zip(crop.source_polygon, expected_display, strict=True):
        assert _display_point(tuple(v / 1000 for v in source), authored.rotation) == pytest.approx(
            tuple(v / 1000 for v in display), abs=1e-7,
        )
        point = source
        for step in crop.derivative.transforms:
            point = step.forward_point(point)
        assert render_point_to_pdf(crop.derivative, point) == pytest.approx(source, abs=1e-7)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("clipped", [False, True], ids=["inside", "page-edge"])
def test_inventory_to_selected_raster_crop_preserves_source_pixels_and_locations(
    tmp_path, monkeypatch, rotation, clipped,
):
    authored = _author_page(tmp_path, rotation=rotation, clipped=clipped)
    inventory, region, full, header = _inventory_render_and_header(authored, tmp_path, monkeypatch)
    original_bytes = full.artifact_path.read_bytes()
    crop = crop_routed_region(
        derivative=full, inventory=inventory, region=region, output_dir=tmp_path / "region",
    )
    crop.derivative.artifact_path.with_suffix(".json").write_text(crop.model_dump_json(indent=2))
    assert crop.mode == "ocr"
    assert crop.requested_display_box.model_dump() == region.box.model_dump()
    expected = (max(0, region.box.x0), max(0, region.box.y0),
                min(inventory.boxes.crop.x1, region.box.x1), min(inventory.boxes.crop.y1, region.box.y1))
    assert _box_tuple(crop.visible_display_box) == expected
    assert (crop.visible_display_box != crop.requested_display_box) is clipped
    assert crop.derivative.transforms[:-1] == full.transforms
    assert crop.derivative.transforms[-1].name == "routed_region_crop"
    _assert_region_round_trip(crop, authored)
    with Image.open(crop.derivative.artifact_path) as image:
        pixels = image.convert("RGB")
        assert pixels.size == (crop.pixel_crop[2] - crop.pixel_crop[0], crop.pixel_crop[3] - crop.pixel_crop[1])
        for colour, source_point in authored.source_anchors.items():
            projected = _render_point(crop.derivative, source_point)
            if clipped and colour in {"blue", "yellow"}:
                assert not (0 <= projected[0] < pixels.width and 0 <= projected[1] < pixels.height)
                assert not any(
                    all(abs(a - b) < 45 for a, b in zip(pixel, COLOURS[colour]))
                    for _, pixel in pixels.getcolors(pixels.width * pixels.height)
                )
            else:
                assert _has_colour(pixels, projected, COLOURS[colour]), (colour, projected)
                assert render_point_to_pdf(crop.derivative, projected) == pytest.approx(
                    tuple(v * 1000 for v in source_point), abs=1e-7,
                )
        # Native header glyph locators still refer to their own page pixels,
        # and lie wholly outside the crop granted to the image region.
        for left, top, right, bottom in native_segment_render_boxes(header, crop.derivative):
            assert right <= 0 or bottom <= 0 or left >= pixels.width or top >= pixels.height
    for source_box in native_segment_pdf_boxes(header):
        assert source_box[0] >= CROP[0] + 85
    assert full.artifact_path.read_bytes() == original_bytes
    retry = crop_routed_region(
        derivative=full, inventory=inventory, region=region, output_dir=tmp_path / "region",
    )
    assert retry == crop
    if not clipped:
        # Treating the displayed region as absolute media coordinates applies
        # the nonzero crop origin twice. The resulting actual crop must lose a
        # known source mark; an inverse-only test would accept it.
        shifted = region.model_copy(update={"box": PdfRect(
            x0=region.box.x0 - full.crop_box.x0,
            y0=region.box.y0 - full.crop_box.y0,
            x1=region.box.x1 - full.crop_box.x0,
            y1=region.box.y1 - full.crop_box.y0,
        )})
        misplaced = crop_routed_region(
            derivative=full, inventory=inventory, region=shifted,
            output_dir=tmp_path / "wrong-origin",
        )
        with Image.open(misplaced.derivative.artifact_path) as image:
            pixels = image.convert("RGB")
            assert any(
                not _has_colour(pixels, _render_point(misplaced.derivative, point), COLOURS[colour])
                for colour, point in authored.source_anchors.items()
            )
    # The nonzero media crop is not the reader's displayed page frame.
    wrong_frame = inventory.model_copy(update={
        "boxes": inventory.boxes.model_copy(update={"crop": PdfRect(**full.crop_box.model_dump())}),
    })
    with pytest.raises(ValueError, match="displayed bounds differ"):
        crop_routed_region(
            derivative=full, inventory=wrong_frame, region=region, output_dir=tmp_path / "wrong-frame",
        )


def _colour_deskew_profile(monkeypatch):
    bundle = load_render_profile_bundle()
    # Keep colour after the real worker deskew. Table morphology deliberately
    # removes these marks, so this is a separately identified proof profile.
    profile = bundle.profiles["table_cv"].model_copy(update={"preprocessing": ()})
    profile = profile.model_copy(update={"profile_id": render_profile_identity(profile)})
    monkeypatch.setattr(
        "corridor.render_profiles.load_render_profile_bundle",
        lambda: bundle.model_copy(update={"profiles": {**bundle.profiles, "table_cv": profile}}),
    )
    return profile


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("skew", [-3, 3], ids=["minus-3", "plus-3"])
def test_routed_crop_uses_actual_deskew_and_masks_neighboring_source_ink(
    tmp_path, monkeypatch, rotation, skew,
):
    authored = _author_page(tmp_path, rotation=rotation, skew=skew)
    profile = _colour_deskew_profile(monkeypatch)
    inventory, region, full, header = _inventory_render_and_header(
        authored, tmp_path, monkeypatch, "table_cv",
    )
    deskew, = (step for step in full.transforms if step.name == "deskew")
    assert deskew.parameters["preprocessing_version"] == "pdfium-deskew-v2"
    assert 1 < abs(deskew.parameters["angle_degrees"]) < 5
    assert full.profile_id == render_profile_identity(profile)
    with Image.open(full.artifact_path) as image:
        alignment = _line_alignment(image.convert("RGB"), full, authored)
    (tmp_path / "alignment.json").write_text(json.dumps({
        "authored_tilt_degrees": skew, "rotation_degrees": rotation,
        "detected_skew_degrees": deskew.parameters["angle_degrees"], **alignment,
    }, indent=2))
    # The detector bins angles in tenths of a degree. Allow that quantization
    # plus one pixel over the 180-point authored baseline; the old wrong-sign
    # rotation leaves six degrees of tilt and fails this pixel measurement.
    assert abs(alignment["axis_deviation_degrees"]) < .2, alignment
    crop = crop_routed_region(
        derivative=full, inventory=inventory, region=region, output_dir=tmp_path / "region",
    )
    crop.derivative.artifact_path.with_suffix(".json").write_text(crop.model_dump_json(indent=2))
    _assert_region_round_trip(crop, authored)
    wrong = crop.derivative.model_copy(update={
        "transforms": tuple(step for step in crop.derivative.transforms if step.name != "deskew"),
    })
    with Image.open(crop.derivative.artifact_path) as image:
        pixels = image.convert("RGB")
        for colour, source_point in authored.source_anchors.items():
            assert _has_colour(pixels, _render_point(crop.derivative, source_point), COLOURS[colour])
        # Removing deskew must miss an actual small source mark, not only
        # change a matrix which could be consistently wrong in both directions.
        red_source = authored.source_anchors["red"]
        assert not _has_colour(pixels, _render_point(wrong, red_source), COLOURS["red"])
        assert authored.outside_source_point is not None
        outside = _render_point(crop.derivative, authored.outside_source_point)
        assert 1 <= outside[0] < pixels.width - 1 and 1 <= outside[1] < pixels.height - 1
        assert not _has_colour(pixels, outside, MAGENTA)
        x, y = (math.floor(v) for v in outside)
        assert pixels.getpixel((x, y)) == (255, 255, 255)
        for left, top, right, bottom in native_segment_render_boxes(header, crop.derivative):
            assert right <= 0 or bottom <= 0 or left >= pixels.width or top >= pixels.height
    with Image.open(full.artifact_path) as image:
        assert _has_colour(
            image.convert("RGB"), _render_point(full, authored.outside_source_point), MAGENTA,
        )


def _invoke_worker(tmp_path, request, name):
    """Exercise the public worker IPC, including historical omitted fields.

    The render worker environment is already installed by the test gate; this
    direct invocation cannot bootstrap or download another environment.
    """
    request_path = tmp_path / f"{name}.request.json"
    manifest_path = tmp_path / f"{name}.manifest.json"
    request_path.write_text(json.dumps({"requests": [request]}))
    completed = subprocess.run(
        [str(DEFAULT_WORKER_PROJECT / ".venv" / "bin" / "python"),
         str(DEFAULT_WORKER_PROJECT / "render_worker.py"),
         "--request", str(request_path), "--manifest", str(manifest_path)],
        cwd=DEFAULT_WORKER_PROJECT, env=worker_environment(),
        capture_output=True, text=True, check=False,
    )
    return completed, manifest_path


def _worker_derivative(tmp_path, request, name):
    completed, manifest_path = _invoke_worker(tmp_path, request, name)
    assert completed.returncode == 0, completed.stderr
    manifest, = json.loads(manifest_path.read_text())["manifests"]
    return RenderDerivative.model_validate(manifest)


@pytest.mark.parametrize("rasterizer", [LEGACY_RASTERIZER, "pdfium"])
@pytest.mark.parametrize("skew", [-3, 3], ids=["minus-3", "plus-3"])
def test_unversioned_worker_pixels_keep_their_old_identity_and_direction(
    tmp_path, monkeypatch, rasterizer, skew,
):
    authored = _author_page(tmp_path, rotation=0, skew=skew)
    profile = _colour_deskew_profile(monkeypatch)
    request = {
        "pdf_path": str(authored.path), "page_number": 1,
        "profile": profile.model_dump(mode="json"), "rasterizer": rasterizer,
        "output_dir": str(tmp_path / "pixels"),
    }
    old_call = _worker_derivative(tmp_path, request, "unversioned")
    old_bytes = old_call.artifact_path.read_bytes()
    with Image.open(old_call.artifact_path) as image:
        historical = _line_alignment(image.convert("RGB"), old_call, authored)
        assert 5.5 < abs(historical["axis_deviation_degrees"]) < 6.5
    # Reconstruct the pre-version filename contract independently. Even the
    # rollback direction defect is preserved for these historical requests.
    identity = {"profile_id": profile.profile_id,
                "clip": {"x0": 0, "y0": 0, "x1": CROP_WIDTH * 1000, "y1": CROP_HEIGHT * 1000}}
    if rasterizer != LEGACY_RASTERIZER:
        identity["rasterizer"] = rasterizer
    suffix = sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    digest = sha256(authored.path.read_bytes()).hexdigest()
    assert old_call.artifact_path.name == f"{digest}-p0001-table_cv-{suffix}.png"
    assert old_call.parameters == profile.parameters
    old_step, = (step for step in old_call.transforms if step.name == "deskew")
    assert set(old_step.parameters) == {"angle_degrees"}
    if rasterizer == "pdfium":
        corrected = _worker_derivative(
            tmp_path, {**request, "preprocessing_version": "pdfium-deskew-v2"}, "versioned",
        )
        assert corrected.artifact_path != old_call.artifact_path
        assert corrected.artifact_sha256 != old_call.artifact_sha256
        assert old_call.artifact_path.read_bytes() == old_bytes
        assert corrected.parameters["preprocessing_version"] == "pdfium-deskew-v2"
        with Image.open(corrected.artifact_path) as image:
            assert abs(_line_alignment(image.convert("RGB"), corrected, authored)["axis_deviation_degrees"]) < .2
    retry = _worker_derivative(tmp_path, request, "unversioned-retry")
    assert retry == old_call
    assert retry.artifact_path.read_bytes() == old_bytes


@pytest.mark.parametrize("rasterizer,version,error", [
    ("pdfium", "unknown-v99", "unsupported render preprocessing version"),
    (LEGACY_RASTERIZER, "unknown-v99", "unsupported render preprocessing version"),
    (LEGACY_RASTERIZER, "pdfium-deskew-v2", "requires the PDFium rasterizer"),
])
def test_worker_refuses_unsupported_preprocessing_before_reading_source(
    tmp_path, rasterizer, version, error,
):
    completed, manifest_path = _invoke_worker(tmp_path, {
        "rasterizer": rasterizer, "preprocessing_version": version,
        "pdf_path": str(tmp_path / "does-not-exist.pdf"),
        "output_dir": str(tmp_path / "pixels"),
    }, "refused")
    assert completed.returncode != 0
    assert error in completed.stderr
    assert not manifest_path.exists()
    assert not (tmp_path / "pixels").exists()
