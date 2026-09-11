"""What changes when corpus pages are rendered by PDFium instead of MuPDF.

#735 replaces the render worker's rasterizer, and the honest question about a
replacement rasterizer is not whether it produces the same bytes - it cannot,
because anti-aliasing, hinting and pixel-grid rounding are engine decisions -
but whether it produces the same *page*: the same declared boxes, the same
rotation, the same raster within a pixel, the same affine chain, and ink in
the same places. So this runs both engines over the corpus under the measured
review, OCR-layout and table profiles and records each of those as a number
against a tolerance declared here, before the run, with every page that
exceeds one named and classified rather than averaged away.

It is an experiment, not a test: it reads the corpus content store, renders
hundreds of pages twice, and takes minutes (ADR-0008). CI runs the bounded
fixture tests in `tests/test_render_profiles.py` instead. Nothing it reports
selects an engine for production; #447 owns that act.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile

from PIL import Image, ImageChops, ImageFilter

from corridor.object_storage import content_key, content_store
from corridor.render_profiles import (
    DEFAULT_PROFILE_PATH,
    LEGACY_RASTERIZER,
    REPLACEMENT_RASTERIZER,
    RenderDerivative,
    load_render_profile_bundle,
    render_page_derivatives,
)

PROFILES = ("review", "ocr_layout", "table_cv")

# Ink is dark on the unprocessed and OCR profiles and bright on the table
# profile, whose preprocessing inverts and thresholds the page.
INK_IS_BRIGHT = {"table_cv": True}

# Declared before the run, on the question each measure actually answers.
#
# `ink_coverage_within_1px` is the gate on placement: the share of one
# engine's ink pixels lying within one pixel of the other's. A page rendered
# from the wrong box, at the wrong rotation, or off by a fraction of a point
# collapses it; antialiasing along a stroke's edge does not move it, because
# the neighbouring pixel is ink in both. It is measured in both directions, so
# ink one engine draws and the other does not counts against it.
#
# `mean_absolute_difference` is the gate on tone: how far the two rasters are
# apart per pixel over the whole page.
#
# `ink_iou` and `differing_pixel_fraction` are recorded but are not gates.
# Both binarize a difference and so measure antialiasing as much as placement:
# a 300-DPI glyph stroke is two or three pixels wide, and two independent
# rasterizers disagree along its whole boundary while drawing it in the same
# place. Gating on them would be gating on how each engine anti-aliases,
# which is the engine's own business and is exactly what the ticket declines
# to demand. They are in every row of the receipt, low values included.
TOLERANCES = {
    "raster_pixels": 1,
    "ink_coverage_within_1px": 0.98,
    "mean_absolute_difference": 0.02,
}

OBSERVED_ONLY = ("ink_iou", "differing_pixel_fraction")

DIFFERENCE_THRESHOLD = 32


def corpus_pdfs(root: Path) -> list[tuple[str, dict]]:
    """Every PDF the corpus manifests lock, by digest, deduplicated and sorted.

    The lock files are the corpus's own record of what was retrieved; reading
    them rather than listing the store keeps the selection reproducible and
    keeps this out of the store's directory layout.
    """

    found: dict[str, dict] = {}
    for lock in sorted(root.glob("*.lock.json")):
        for entry in json.loads(lock.read_text()).get("sources", {}).values():
            digest = entry.get("sha256")
            if not digest or not str(entry.get("local_path", "")).endswith(".pdf"):
                continue
            found.setdefault(
                digest,
                {
                    "sha256": digest,
                    "doc_type": entry.get("doc_type"),
                    "title": entry.get("title"),
                    "bytes": entry.get("bytes") or 0,
                    "manifest": lock.name,
                },
            )
    return sorted(found.items())


def _count(mask: Image.Image) -> int:
    return sum(mask.histogram()[255:])


def _dilate(mask: Image.Image) -> Image.Image:
    """The mask grown by one pixel in every direction."""

    return mask.convert("L").filter(ImageFilter.MaxFilter(3)).convert("1")


def _mask(image: Image.Image, *, bright: bool) -> Image.Image:
    threshold = 127
    return image.point(
        lambda value: 255 if (value > threshold if bright else value < 128) else 0
    ).convert("1")


def compare_images(left: Path, right: Path, *, bright_ink: bool) -> dict:
    """Three stated measures over the region both rasters cover."""

    one = Image.open(left).convert("L")
    two = Image.open(right).convert("L")
    box = (0, 0, min(one.width, two.width), min(one.height, two.height))
    one, two = one.crop(box), two.crop(box)
    pixels = one.width * one.height
    histogram = ImageChops.difference(one, two).histogram()
    absolute = sum(value * count for value, count in enumerate(histogram))
    differing = sum(histogram[DIFFERENCE_THRESHOLD + 1 :])
    ink_one, ink_two = _mask(one, bright=bright_ink), _mask(two, bright=bright_ink)
    intersection = _count(ImageChops.logical_and(ink_one, ink_two))
    union = _count(ImageChops.logical_or(ink_one, ink_two))
    one_pixels, two_pixels = _count(ink_one), _count(ink_two)
    near_one = _count(ImageChops.logical_and(ink_one, _dilate(ink_two)))
    near_two = _count(ImageChops.logical_and(ink_two, _dilate(ink_one)))
    return {
        "compared_pixels": pixels,
        "mean_absolute_difference": absolute / pixels / 255,
        "differing_pixel_fraction": differing / pixels,
        "ink_iou": 1.0 if union == 0 else intersection / union,
        "ink_pixels": {"legacy": one_pixels, "replacement": two_pixels},
        "ink_coverage_within_1px": {
            "legacy_covered_by_replacement": 1.0 if not one_pixels else near_one / one_pixels,
            "replacement_covered_by_legacy": 1.0 if not two_pixels else near_two / two_pixels,
        },
    }


def compare_derivatives(
    legacy: RenderDerivative, replacement: RenderDerivative
) -> dict:
    """One page, one profile, under both engines."""

    profile = legacy.profile_name
    geometry = {
        "media_box_equal": legacy.media_box == replacement.media_box,
        "crop_box_equal": legacy.crop_box == replacement.crop_box,
        "rotation_equal": legacy.rotation_degrees == replacement.rotation_degrees,
        "raster_width_delta": replacement.raster_width - legacy.raster_width,
        "raster_height_delta": replacement.raster_height - legacy.raster_height,
    }
    steps = {
        "names_equal": [step.name for step in legacy.transforms]
        == [step.name for step in replacement.transforms],
        "max_forward_delta": max(
            abs(one - two)
            for first, second in zip(legacy.transforms, replacement.transforms)
            for one, two in zip(first.forward, second.forward)
        ),
        "page_origin_y_delta": (
            replacement.transforms[0].parameters["page_origin_y"]
            - legacy.transforms[0].parameters["page_origin_y"]
        ),
    }
    image = compare_images(
        legacy.artifact_path,
        replacement.artifact_path,
        bright_ink=INK_IS_BRIGHT.get(profile, False),
    )
    reasons = []
    if not (
        geometry["media_box_equal"]
        and geometry["crop_box_equal"]
        and geometry["rotation_equal"]
    ):
        reasons.append("declared_boxes_or_rotation_differ")
    if max(
        abs(geometry["raster_width_delta"]), abs(geometry["raster_height_delta"])
    ) > TOLERANCES["raster_pixels"]:
        reasons.append("raster_dimensions_differ")
    if steps["page_origin_y_delta"] and steps["page_origin_y_delta"] != legacy.media_box.y0:
        reasons.append("page_origin_y_differs_beyond_the_media_box_origin")
    if not steps["names_equal"]:
        reasons.append("affine_chain_differs")
    if image["mean_absolute_difference"] > TOLERANCES["mean_absolute_difference"]:
        reasons.append("mean_absolute_difference_above_tolerance")
    if (
        min(image["ink_coverage_within_1px"].values())
        < TOLERANCES["ink_coverage_within_1px"]
    ):
        reasons.append("ink_placed_more_than_a_pixel_apart")
    return {
        "profile": profile,
        "profile_id": legacy.profile_id,
        "dpi": legacy.dpi,
        "geometry": geometry,
        "transforms": steps,
        "image": image,
        "within_tolerance": not reasons,
        "exceeded": reasons,
        # Explained, and still recorded as a difference: MuPDF's flipped crop
        # box cannot express a media box that does not start at y = 0, so the
        # legacy chain's flip constant is short by exactly that origin
        # (`workers/render/legacy_pymupdf.py`).
        "legacy_page_origin_shortfall": (
            steps["page_origin_y_delta"] == legacy.media_box.y0 != 0
        ),
    }


def run(
    *,
    output: Path,
    corpus_root: Path,
    page_number: int,
    limit: int,
    max_bytes: int,
    profiles: tuple[str, ...],
) -> dict:
    bundle = load_render_profile_bundle()
    selected = corpus_pdfs(corpus_root)
    store = content_store()
    documents: list[dict] = []
    # The engines run in the worker's environment, not this one, so their
    # versions are read off a manifest the worker wrote.
    versions: dict[str, dict[str, str]] = {}
    with tempfile.TemporaryDirectory(prefix="render-comparison-") as scratch:
        workspace = Path(scratch)
        compared = 0
        for digest, entry in selected:
            if limit and compared >= limit:
                break
            if entry["bytes"] > max_bytes:
                documents.append({**entry, "skipped": "larger than the byte bound"})
                continue
            key = content_key(digest, ".pdf")
            if not store.exists(key):
                documents.append({**entry, "skipped": "absent from the content store"})
                continue
            print(
                f"[{compared + 1}/{limit or len(selected)}] {digest[:12]} "
                f"{entry['bytes']} bytes",
                file=sys.stderr,
                flush=True,
            )
            source = store.stage(key, workspace / f"{digest}.pdf", sha256=digest)
            renders: dict[str, list[RenderDerivative]] = {}
            try:
                for engine in (LEGACY_RASTERIZER, REPLACEMENT_RASTERIZER):
                    renders[engine] = render_page_derivatives(
                        pdf_path=source,
                        page_number=page_number,
                        profile_names=profiles,
                        output_dir=workspace / engine,
                        rasterizer=engine,
                    )
                    versions[engine] = renders[engine][0].library_versions
            except Exception as failure:  # a page one engine refuses is evidence
                documents.append({**entry, "failed": f"{type(failure).__name__}: {failure}"})
                continue
            compared += 1
            documents.append(
                {
                    **entry,
                    "page_number": page_number,
                    "rotation_degrees": renders[LEGACY_RASTERIZER][0].rotation_degrees,
                    "profiles": [
                        compare_derivatives(legacy, replacement)
                        for legacy, replacement in zip(
                            renders[LEGACY_RASTERIZER], renders[REPLACEMENT_RASTERIZER]
                        )
                    ],
                }
            )
            for engine in renders:
                for derivative in renders[engine]:
                    derivative.artifact_path.unlink(missing_ok=True)
            source.unlink(missing_ok=True)
    receipt = {
        "schema_version": "corridor.render-rasterizer-comparison.v1",
        "issue": 735,
        "compared_at": datetime.now(timezone.utc).isoformat(),
        "configuration": {
            "legacy_rasterizer": LEGACY_RASTERIZER,
            "replacement_rasterizer": REPLACEMENT_RASTERIZER,
            "render_profile_bundle_sha256": hashlib.sha256(
                DEFAULT_PROFILE_PATH.read_bytes()
            ).hexdigest(),
            "pdf_gold_dataset_version": bundle.pdf_gold_dataset_version,
            "profile_ids": {
                name: bundle.profiles[name].profile_id for name in profiles
            },
            "page_number": page_number,
            "selection": (
                "every PDF locked by corpus/*.lock.json, by digest, at most "
                f"{max_bytes} bytes" + (f", first {limit}" if limit else "")
            ),
            "library_versions": versions,
        },
        "tolerances": TOLERANCES,
        "recorded_but_not_gated": OBSERVED_ONLY,
        "difference_threshold": DIFFERENCE_THRESHOLD,
        "documents": documents,
        "summary": summarize(documents),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt


def summarize(documents: list[dict]) -> dict:
    compared = [entry for entry in documents if entry.get("profiles")]
    rows = [row for entry in compared for row in entry["profiles"]]
    by_profile: dict[str, dict] = {}
    for row in rows:
        bucket = by_profile.setdefault(
            row["profile"],
            {
                "pages": 0,
                "within_tolerance": 0,
                "worst_mean_absolute_difference": 0.0,
                "worst_differing_pixel_fraction": 0.0,
                "lowest_ink_iou": 1.0,
                "lowest_ink_coverage_within_1px": 1.0,
                "max_raster_pixel_delta": 0,
            },
        )
        bucket["pages"] += 1
        bucket["within_tolerance"] += int(row["within_tolerance"])
        bucket["worst_mean_absolute_difference"] = max(
            bucket["worst_mean_absolute_difference"],
            row["image"]["mean_absolute_difference"],
        )
        bucket["worst_differing_pixel_fraction"] = max(
            bucket["worst_differing_pixel_fraction"],
            row["image"]["differing_pixel_fraction"],
        )
        bucket["lowest_ink_iou"] = min(bucket["lowest_ink_iou"], row["image"]["ink_iou"])
        bucket["lowest_ink_coverage_within_1px"] = min(
            bucket["lowest_ink_coverage_within_1px"],
            *row["image"]["ink_coverage_within_1px"].values(),
        )
        bucket["max_raster_pixel_delta"] = max(
            bucket["max_raster_pixel_delta"],
            abs(row["geometry"]["raster_width_delta"]),
            abs(row["geometry"]["raster_height_delta"]),
        )
    return {
        "documents_compared": len(compared),
        "documents_skipped": len([e for e in documents if e.get("skipped")]),
        "documents_failed": len([e for e in documents if e.get("failed")]),
        "pages_compared": len(rows),
        "identical_declared_geometry": sum(
            1
            for row in rows
            if row["geometry"]["media_box_equal"]
            and row["geometry"]["crop_box_equal"]
            and row["geometry"]["rotation_equal"]
        ),
        "legacy_page_origin_shortfall": sum(
            1 for row in rows if row["legacy_page_origin_shortfall"]
        ),
        "by_profile": by_profile,
        "outliers": [
            {
                "sha256": entry["sha256"],
                "title": entry["title"],
                "doc_type": entry["doc_type"],
                "rotation_degrees": entry["rotation_degrees"],
                "profile": row["profile"],
                "exceeded": row["exceeded"],
                "image": row["image"],
                "geometry": row["geometry"],
            }
            for entry in compared
            for row in entry["profiles"]
            if not row["within_tolerance"]
        ],
    }


_CONTRACT = """\
Render corpus pages under both rasterizers and record the comparison with
its declared tolerances (#735). An explicit experiment outside pytest and
CI; it reads the corpus content store and takes minutes:
  make render-rasterizer-compare ARGS="--output artifacts/render-rasterizer-comparison/735-corpus-render-comparison.json"
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=_CONTRACT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--corpus-root", type=Path, default=Path("corpus"))
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0, help="0 compares every document")
    parser.add_argument("--max-bytes", type=int, default=20_000_000)
    parser.add_argument("--profile", action="append", dest="profiles")
    arguments = parser.parse_args(argv)
    receipt = run(
        output=arguments.output,
        corpus_root=arguments.corpus_root,
        page_number=arguments.page,
        limit=arguments.limit,
        max_bytes=arguments.max_bytes,
        profiles=tuple(arguments.profiles or PROFILES),
    )
    print(json.dumps(receipt["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
