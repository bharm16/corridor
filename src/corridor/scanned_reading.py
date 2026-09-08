"""Read the pages the inventory routed to Textract, as unconfirmed readings (#739, ADR-0094).

The incumbent scanned path sends every OCR-routed region to a local Tesseract
process and folds whatever comes back into the page string. ADR-0094 replaces
the engine and, with it, what a scanned value is allowed to claim: Textract has
not earned verified scanned-cell transcription — 695 of the 901 mismatched cells
in the clean-scan lane carried a mean word confidence of 95 or more — so a value
only Textract supplies is an **Unconfirmed reading**, flagged, never Ready, and
upgraded without ceremony the moment corroboration arrives (ADR-0064).

This module is the caller the adapter package was built for, and it is the only
production module that reaches it. Three rules shape it.

**The route is the only door.** A region is read here because a recorded Page
Inventory routing decision (#734) named Textract and marked it — an OCR or both
region, or the one structural trigger, a table region on a text-layer page whose
native reading recovered no cell text. `routed_textract_regions` is that rule and
nothing else consults the page: a semantic refusal in Tier 1 never appears on a
routing decision and so can never reach a call, and a decision naming the
incumbent engine routes nothing here at all.

**The check is the adapter's, not this module's.** Every call goes through
`corridor_pdf_reader.textract_adapter.boundary`, which matches the authorization
record against the request boundary on every field before it constructs a client
(#732). This module never builds a client, never names the service, and never
holds a credential; a refusal arrives as a Processing Failure with zero outbound
requests and is recorded with the engine, the configuration and the page scope.

**Three cases stay distinguishable.** Geometry is Textract's. Values come from
re-mapped native glyphs wherever the region has a usable native layer that gives
a cell a valid assignment and a locator — those take the ordinary
source-verification path — and from Textract's own words otherwise, which is an
Unconfirmed reading carrying both source and processing provenance. A readable
corroborating source verifies through that source's citation and keeps its
relation to the original unconfirmed reading; that upgrade is
`corridor.unreadable_cell_admission`'s and is untouched here.

There is no review screen. Nothing in this module offers a person a transcription
to confirm, and `tests/test_no_transcription_review_surface.py` holds that shut.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from sqlalchemy.orm import Session

from corridor.page_inventory import FIXED_POINT_SCALE, PageRoutingDecision, PdfRect
from corridor.token_layers import (
    READER_ENGINE,
    Token,
    TokenLayer,
    textract_token_layer,
)
from corridor.models import UnreadableCellResolution
from corridor.unreadable_cells import CellReadingRefused, current_resolution
from corridor.verify import normalize

# The adapter package, and only through its boundary. The rung underneath it —
# the imported client, its transport and its rasterizer — stays unreachable
# from production code: `tests/test_textract_adapter.py` allows this module the
# adapter and nothing below it, which is why the raster below is a protocol
# declared here rather than the rung's own dataclass imported by name.
from corridor_pdf_reader.textract_adapter.boundary import (
    AuthorizedTextract,
    TextractProcessingFailure,
    open_boundary,
)
from corridor_pdf_reader.textract_adapter.identity import RequestConfiguration
from corridor_pdf_reader.textract_adapter.records import PROVIDER_POSTURE, RequestBoundary
from corridor_pdf_reader.textract_adapter.rendering import rasterize_page


class PageRaster(Protocol):
    """What a page raster must offer the boundary: the bytes and the frame they are in."""

    png: bytes
    dpi: int
    mode: str

# ADR-0094's declared OCR provider. The routing decision names it; this module
# reads only for decisions that do.
TEXTRACT_ENGINE = "textract"


@dataclass(frozen=True)
class RoutedRegion:
    """One region of one recorded decision, and why Textract reads it."""

    region_id: str
    box: PdfRect
    reason: str
    # The structural trigger that routed a text-layer page's region, or None
    # when an ordinary OCR route did. Recorded so a receipt can say which rule
    # spent the call.
    trigger: Literal["table_region_without_table_read"] | None = None


def routed_textract_regions(routing: PageRoutingDecision) -> tuple[RoutedRegion, ...]:
    """The regions of one recorded decision that Textract reads, in record order.

    Everything this function knows comes off the decision. It does not look at
    the page, the inventory, or any later refusal: if a region is not on a
    Textract-naming decision as an OCR route or as the structural trigger, it is
    not read, and there is no other way into a call.
    """

    if routing.ocr_engine != TEXTRACT_ENGINE:
        return ()
    regions = [
        RoutedRegion(region_id=region.region_id, box=region.box, reason=region.reason)
        for region in routing.regions
        if region.mode in {"ocr", "both"}
    ]
    routed = {region.region_id for region in regions}
    regions.extend(
        RoutedRegion(
            region_id=trigger.region_id,
            box=trigger.box,
            reason=trigger.reason,
            trigger=trigger.trigger,
        )
        for trigger in routing.structural_triggers
        if trigger.region_id not in routed
    )
    return tuple(regions)


# --- the read ------------------------------------------------------------------


class ScannedRoutingRefused(RuntimeError):
    """A page was handed to this module that no recorded decision routed here.

    Raised before anything reaches the adapter, so a caller that lost its
    routing decision cannot spend a call by accident.
    """


@dataclass(frozen=True)
class ScannedProcessingFailure:
    """An OCR engine failure, in the shape ingest already records one.

    `engine` is the engine that actually failed — the provider, not the engine
    a route names — with the configuration the request carried and the page
    scope it covered, so a receipt can say what failed, under what settings,
    over which pages and regions. `outbound_requests` is how many requests left
    the process before the failure, which an authorization refusal makes zero
    by construction.
    """

    engine: str
    reason: str
    detail: str
    configuration: dict[str, Any]
    scope: dict[str, Any]
    outbound_requests: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "processing-failure",
            "engine": self.engine,
            "reason": self.reason,
            "detail": self.detail,
            "configuration": self.configuration,
            "scope": self.scope,
            "outbound_requests": self.outbound_requests,
        }


@dataclass(frozen=True)
class ScannedCellValue:
    """One cell of one routed region, and which of the three cases it is.

    `value_source` says where the characters came from and `state` says what
    may be claimed for them. They are two facts, not one: a value re-mapped
    from the document's own glyphs is on the ordinary source-verification path
    and carries a locator into the native reading, while a value only Textract
    supplies is an Unconfirmed reading — flagged, never Ready — however
    confident the provider was about it.
    """

    region_id: str
    table: int
    row: int
    column: int
    box: PdfRect
    value: str
    value_source: Literal["native_glyphs", "textract_words"]
    state: Literal["source_verified", "unconfirmed"]
    confidence: float | None
    locator: PdfRect | None
    provenance: dict[str, Any]


@dataclass(frozen=True)
class ScannedPageReading:
    """One page read through the authorized adapter, with everything it produced."""

    page_no: int
    regions: tuple[RoutedRegion, ...]
    token_layer: TokenLayer
    values: tuple[ScannedCellValue, ...]
    reading: dict[str, Any]
    provenance: dict[str, Any]

    @property
    def unconfirmed_readings(self) -> tuple[ScannedCellValue, ...]:
        return tuple(value for value in self.values if value.state == "unconfirmed")


def read_scanned_page(
    adapter: AuthorizedTextract,
    raster: PageRaster,
    *,
    routing: PageRoutingDecision,
    page_no: int,
    rendition_sha256: str,
    source_sha256: str,
    native_layer: TokenLayer | None = None,
    render_profile_id: str | None = None,
) -> ScannedPageReading:
    """Read one routed page through the adapter, and classify what came back.

    The routing decision is checked first and the call is refused outright when
    it routes nothing here, so the recorded decision is the only thing that can
    spend a request. The authorization check itself is the adapter's: this
    module hands it the raster and it either returns a reading or raises a
    Processing Failure, which `processing_failure` puts in the shape ingest
    records.
    """

    regions = routed_textract_regions(routing)
    if not regions:
        raise ScannedRoutingRefused(
            f"page {page_no}: no recorded routing decision sends a region to "
            f"{TEXTRACT_ENGINE} (engine {routing.ocr_engine!r}, mode "
            f"{routing.page_mode!r})"
        )
    reading = adapter.analyze_page(
        raster, rendition_sha256=rendition_sha256, page_number=page_no
    )
    provenance = reading.binding.as_dict()
    layer = textract_token_layer(
        reading.page,
        page_no=page_no,
        source_sha256=source_sha256,
        provenance=provenance,
        configuration=adapter.configuration.as_dict(),
        render_profile_id=render_profile_id,
    )
    values = classify_region_values(
        reading.page,
        regions=regions,
        native_layer=native_layer,
        provenance=provenance,
    )
    return ScannedPageReading(
        page_no=page_no,
        regions=regions,
        token_layer=layer,
        values=values,
        reading=reading.page,
        provenance=provenance,
    )


def processing_failure(
    failure: TextractProcessingFailure,
    *,
    page_no: int,
    regions: tuple[RoutedRegion, ...],
    rendition_sha256: str,
    configuration: dict[str, Any],
) -> ScannedProcessingFailure:
    """The adapter's refusal or failure, as the Processing Failure ingest records."""

    return ScannedProcessingFailure(
        engine=TEXTRACT_ENGINE,
        reason=failure.reason,
        detail=failure.detail or "; ".join(failure.mismatches) or failure.reason,
        configuration=dict(configuration),
        scope={
            "page_number": page_no,
            "rendition_sha256": rendition_sha256,
            "region_ids": [region.region_id for region in regions],
        },
        outbound_requests=failure.outbound_requests,
    )


# --- the three cases -----------------------------------------------------------


def classify_region_values(
    reading: dict[str, Any],
    *,
    regions: tuple[RoutedRegion, ...],
    native_layer: TokenLayer | None,
    provenance: dict[str, Any],
) -> tuple[ScannedCellValue, ...]:
    """Every routed region's cells, each as the case its evidence makes it.

    Geometry is Textract's throughout — the rows, columns and cell boxes are
    the ones it returned. What differs per cell is where the characters come
    from. Where the region has a usable native layer and that layer puts glyphs
    inside the cell, the value is the document's own text and the cell carries
    a locator back into the native reading, which is the ordinary
    source-verification path. Where it does not, the value is Textract's, and
    it is an Unconfirmed reading.

    A cell outside every routed region is not read here at all: on a mixed
    page the native regions keep their native values by the ordinary route, and
    borrowing Textract's opinion about them would be a reading nothing asked
    for.
    """

    usable = _usable_native_tokens(native_layer)
    values: list[ScannedCellValue] = []
    for table_no, table in enumerate(reading.get("tables") or ()):
        for cell in table.get("cells") or ():
            box = _fixed(cell["box"])
            region = _containing_region(box, regions)
            if region is None:
                continue
            native = _native_text_in(box, usable)
            if native is not None:
                text, locator = native
                values.append(
                    ScannedCellValue(
                        region_id=region.region_id,
                        table=table_no,
                        row=int(cell["row"]),
                        column=int(cell["column"]),
                        box=box,
                        value=text,
                        value_source="native_glyphs",
                        state="source_verified",
                        confidence=None,
                        locator=locator,
                        provenance=_cell_provenance(
                            provenance, region, source="native_glyphs"
                        ),
                    )
                )
                continue
            text = str(cell.get("text") or "").strip()
            if not text:
                continue
            values.append(
                ScannedCellValue(
                    region_id=region.region_id,
                    table=table_no,
                    row=int(cell["row"]),
                    column=int(cell["column"]),
                    box=box,
                    value=text,
                    value_source="textract_words",
                    state="unconfirmed",
                    confidence=_confidence(cell.get("confidence")),
                    locator=None,
                    provenance=_cell_provenance(
                        provenance, region, source="textract_words"
                    ),
                )
            )
    return tuple(values)


def _usable_native_tokens(layer: TokenLayer | None) -> tuple[Token, ...]:
    """The native tokens a Textract cell box may be re-mapped over, if any.

    Only the reader-backed native layer qualifies, and the reason is
    arithmetic rather than preference: its boxes are in the reader's displayed
    crop, top-left origin, which is the frame the adapter puts a Textract box
    in. The incumbent engine reports word boxes in its own page space, which
    differs on a rotated page, so re-mapping over it would silently mix two
    frames. A page with no usable layer has no native values, which is exactly
    the scanned case.
    """

    if layer is None or layer.origin != "native":
        return ()
    if layer.identity.engine != READER_ENGINE:
        return ()
    return layer.tokens


def _containing_region(
    box: PdfRect, regions: tuple[RoutedRegion, ...]
) -> RoutedRegion | None:
    centre = ((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)
    for region in regions:
        if (
            region.box.x0 <= centre[0] <= region.box.x1
            and region.box.y0 <= centre[1] <= region.box.y1
        ):
            return region
    return None


def _native_text_in(
    box: PdfRect, tokens: tuple[Token, ...]
) -> tuple[str, PdfRect] | None:
    """The document's own text inside one Textract cell, with its locator.

    A glyph belongs to the cell when its centre is inside the cell box, which
    is the measured lane A assignment. The locator is the union of the assigned
    tokens' own boxes — the region of the page the value is actually printed
    in, not the cell Textract drew around it.
    """

    inside = [
        token
        for token in tokens
        if box.x0 <= (token.polygon_pdf.x0 + token.polygon_pdf.x1) / 2 <= box.x1
        and box.y0 <= (token.polygon_pdf.y0 + token.polygon_pdf.y1) / 2 <= box.y1
    ]
    text = " ".join(token.raw_text for token in inside if token.raw_text.strip())
    if not text.strip():
        return None
    return text, PdfRect(
        x0=min(token.polygon_pdf.x0 for token in inside),
        y0=min(token.polygon_pdf.y0 for token in inside),
        x1=max(token.polygon_pdf.x1 for token in inside),
        y1=max(token.polygon_pdf.y1 for token in inside),
    )


def _cell_provenance(
    provenance: dict[str, Any], region: RoutedRegion, *, source: str
) -> dict[str, Any]:
    """Source provenance and processing provenance, kept apart and both recorded.

    Source provenance says which rendition, page and region the value is
    printed in. Processing provenance says which response produced it, under
    which authorization scope, and what the provider reported about itself.
    An Unconfirmed reading carries both because both are what a later
    corroboration is checked against.
    """

    return {
        "source": {
            "rendition_sha256": provenance.get("rendition_sha256"),
            "page_number": provenance.get("page_number"),
            "region_id": region.region_id,
            "routing_reason": region.reason,
            "structural_trigger": region.trigger,
        },
        "processing": {
            "engine": TEXTRACT_ENGINE,
            "value_source": source,
            "extraction_run": provenance.get("extraction_run"),
            "authorization_record_id": provenance.get("record_id"),
            "scope_digest": provenance.get("scope_digest"),
            "raster_sha256": provenance.get("raster_sha256"),
            "raw_response_digest": provenance.get("raw_response_digest"),
            "normalized_reading_digest": provenance.get("normalized_reading_digest"),
            "provider_model_version": provenance.get("model_version"),
            "request_identity": provenance.get("request_identity"),
            "normalization": provenance.get("normalization"),
        },
    }


def _fixed(box) -> PdfRect:
    """A reading's box, in points, as the inventory's fixed-point rectangle."""

    x0, y0, x1, y1 = (float(value) for value in box)
    return PdfRect(
        x0=round(x0 * FIXED_POINT_SCALE),
        y0=round(y0 * FIXED_POINT_SCALE),
        x1=round(x1 * FIXED_POINT_SCALE),
        y1=round(y1 * FIXED_POINT_SCALE),
    )


def _confidence(value: object) -> float | None:
    if value is None:
        return None
    return max(0.0, min(1.0, float(value) / 100.0))


# --- Tier 2: disagreement, which is not verification ----------------------------


@dataclass(frozen=True)
class TranscriptionCheck:
    """What comparing a Tier 2 transcription with the Textract reading found.

    Read the verdict carefully. `disagreed` is a real finding: the model wrote
    words the provider did not read on that page, which is a reason to distrust
    the transcription. `agreed` is not a finding at all. Both readings can be
    wrong in the same way, and the model was shown the same page the provider
    read; ADR-0094 lists "a model repeating words it was given from Textract"
    and "checking a model transcription against those same words" among the
    things that are explicitly *not* corroboration. This check can sink a row.
    It can never raise one.
    """

    transcribed_tokens: tuple[str, ...]
    reading_tokens: tuple[str, ...]
    agreed: tuple[str, ...]
    disagreed: tuple[str, ...]
    verdict: Literal["disagreement-detected", "no-disagreement-detected"]

    @property
    def disagrees(self) -> bool:
        return bool(self.disagreed)

    @property
    def corroborates(self) -> bool:
        """Never. Kept as a named answer so no caller has to infer it from `agreed`."""
        return False


def check_transcription_against_reading(
    transcribed: str, layer: TokenLayer
) -> TranscriptionCheck:
    """Compare a Tier 2 transcription's tokens with the Textract reading's.

    Both sides are normalized the way a token layer normalizes text, then split
    on whitespace, so the comparison is about characters rather than spacing. A
    transcribed token the reading does not hold anywhere on the page is a
    disagreement; everything else is silence.
    """

    if layer.identity.engine != TEXTRACT_ENGINE:
        raise ValueError(
            "a Tier 2 transcription is checked against the Textract reading of "
            f"the same page; this layer was read by {layer.identity.engine!r}"
        )
    reading_tokens = tuple(
        piece
        for token in layer.tokens
        for piece in token.normalized_text.split()
        if piece
    )
    transcribed_tokens = tuple(piece for piece in normalize(transcribed).split() if piece)
    held = set(reading_tokens)
    agreed = tuple(piece for piece in transcribed_tokens if piece in held)
    disagreed = tuple(piece for piece in transcribed_tokens if piece not in held)
    return TranscriptionCheck(
        transcribed_tokens=transcribed_tokens,
        reading_tokens=reading_tokens,
        agreed=agreed,
        disagreed=disagreed,
        verdict=(
            "disagreement-detected" if disagreed else "no-disagreement-detected"
        ),
    )


# --- an unconfirmed reading is ADR-0064's second state --------------------------

# What appended a scanned unconfirmed reading, recorded on the row so a later
# reader can tell it from a cell the reading harness worked.
UNCONFIRMED_READING_POLICY_VERSION = "scanned-textract-reading-v1"


def scanned_cell_key(value: ScannedCellValue, *, page_no: int) -> str:
    """A stable identity for one cell of one page's Textract reading.

    The page, the table and the cell's own row and column, which is everything
    the reading gives a cell. It is stable across re-reads of the same page
    because Textract's own row and column indices are, and it names no value,
    so a later reading of the same cell appends beside the earlier one rather
    than looking like a different cell.
    """

    return f"scan:p{page_no}:t{value.table}:r{value.row}:c{value.column}"


def record_unconfirmed_readings(
    session: Session,
    *,
    project_id: int,
    document_id: int,
    reading: ScannedPageReading,
) -> tuple[UnreadableCellResolution, ...]:
    """Append every Textract-only value of one page as an Unconfirmed reading.

    ADR-0094 says a value supplied only by Textract stays unconfirmed until the
    existing corroboration rules establish otherwise, and ADR-0064 already owns
    that state: flagged, never Ready, upgraded automatically the moment a
    corroborating source lands. So this appends into that class rather than
    inventing a second one, and everything downstream — `display_reading`,
    `contributes_to_ready`, the corroboration upgrade wired through
    `load_project` — applies to a scanned reading unchanged.

    Only the unconfirmed values are recorded. A cell whose characters came from
    the document's own glyphs is on the ordinary source-verification path and
    has no business in this class. Appending is idempotent per cell: a cell that
    already carries a resolution is left alone, so re-reading a page does not
    bury a corroboration under a fresh unconfirmed row.
    """

    appended: list[UnreadableCellResolution] = []
    for value in reading.unconfirmed_readings:
        cell_key = scanned_cell_key(value, page_no=reading.page_no)
        existing = current_resolution(
            session,
            document_id=document_id,
            page_no=reading.page_no,
            cell_key=cell_key,
        )
        if existing is not None:
            continue
        row = UnreadableCellResolution(
            project_id=project_id,
            document_id=document_id,
            page_no=reading.page_no,
            cell_key=cell_key,
            state="unconfirmed",
            value=value.value,
            origin="harness",
            policy_version=UNCONFIRMED_READING_POLICY_VERSION,
        )
        session.add(row)
        appended.append(row)
    if appended:
        session.flush(appended)
    return tuple(appended)


# --- ADR-0064's mechanical rescue, read by Textract ----------------------------


class TextractRescue:
    """ADR-0064's mechanical rescue with the provider in place of the local engine.

    The harness is unchanged: `rescue_page` still pins the page, calls one
    preprocessor, and lets the page leave the unreadable class when a usable
    text layer comes back. What changes is who reads. This satisfies
    `unreadable_cells.PagePreprocessor` and reads the pinned page through the
    same authorized boundary as every other scanned read, so a rescue is
    counted, bound and charged like one — there is no second, unchecked door
    for a repair pass.

    Two refusals happen here, at construction, before the harness is entered,
    because the harness may never widen its own scope:

    - a profile that has not declared the `textract` read identity may not have
      its pages read by Textract, however the caller was wired.

    The provider applies none of the profile's image operations, and this
    reader says so rather than letting the run credit the reading with a
    `deskew` nothing ran: `applied_ops` is empty, and `rescue_page` records it
    beside the declared chain.

    The bytes read are the page rendered under the boundary's own configuration,
    which is what the retained identity says was sent; the pinned page digest
    the harness hands in is checked against the pin this rescue was built for,
    so a rescue cannot quietly read a different page than the one the harness
    pinned.
    """

    def __init__(
        self,
        adapter: AuthorizedTextract,
        *,
        source: Path,
        page_number: int,
        rendition_sha256: str,
        page_image_sha256: str,
        read_identities: Sequence[str],
        rasterize: Callable[..., Any] | None = None,
    ) -> None:
        if TEXTRACT_ENGINE not in tuple(read_identities):
            raise CellReadingRefused(
                "undeclared_read_identity",
                f"the running profile did not declare the {TEXTRACT_ENGINE!r} read",
            )
        self.applied_ops: tuple[str, ...] = ()
        self.adapter = adapter
        self.source = Path(source)
        self.page_number = page_number
        self.rendition_sha256 = rendition_sha256
        self.page_image_sha256 = page_image_sha256
        self._rasterize = rasterize or rasterize_page
        self.reading: dict[str, Any] | None = None
        self.provenance: dict[str, Any] | None = None

    def rescue(
        self, *, image_sha256: str, image_path: str | None, ops: tuple[str, ...]
    ) -> str:
        if image_sha256 != self.page_image_sha256:
            raise CellReadingRefused(
                "stale_pinned_page",
                "the harness pinned a different page than this rescue was built for",
            )
        raster = self._rasterize(
            self.source, self.page_number, self.adapter.configuration
        )
        reading = self.adapter.analyze_page(
            raster,
            rendition_sha256=self.rendition_sha256,
            page_number=self.page_number,
        )
        self.reading = reading.page
        self.provenance = reading.binding.as_dict()
        return recovered_text(reading.page)


def recovered_text(reading: dict[str, Any]) -> str:
    """One page reading as a text layer: table cells in order, then what is outside.

    This is what the rescue offers the harness to decide whether the page has a
    usable text layer again. It is a projection over the reading, not a second
    reading, and it claims nothing about any individual value.
    """

    lines: list[str] = []
    for table in reading.get("tables") or ():
        for cell in table.get("cells") or ():
            text = str(cell.get("text") or "").strip()
            if text:
                lines.append(text)
    for item in reading.get("outside") or ():
        text = str(item.get("text") or "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


# --- ingest's handle on the route ----------------------------------------------

# What an ingest request claims about itself. The purpose and source class are
# the posture's own words for reading a scanned page; the region is the
# posture's, because a request naming another one is refused by the check
# rather than served from a second posture nobody accepted.
INGEST_SOURCE_CLASS = "scanned-pdf"
INGEST_PURPOSE = "scanned-page-reading"


@dataclass(frozen=True)
class ScannedReader:
    """Ingest's handle on the scanned route: an opened boundary, or the refusal instead.

    A refusal is held rather than raised because it is a per-region Processing
    Failure, not a failed document: the page still has its inventory, its
    routing decision, its renders and its native reading, and the record should
    say that Textract did not read it and why. What a refusal must never do is
    hand the region to the incumbent engine, which is why `adapter` and
    `refusal` are the only two states and there is no third.
    """

    adapter: AuthorizedTextract | None
    refusal: TextractProcessingFailure | None
    request: RequestBoundary

    @property
    def configuration(self) -> dict[str, Any]:
        if self.adapter is not None:
            return self.adapter.configuration.as_dict()
        return RequestConfiguration().as_dict()


def open_scanned_reader(
    project: str,
    *,
    extraction_run: str,
    cache_root: Path,
    record: object | None = None,
    service: Any = None,
    stage: str = "production",
    configuration: RequestConfiguration = RequestConfiguration(),
) -> ScannedReader:
    """Open the boundary ingest reads scanned pages through, or hold its refusal.

    Corridor has no source of a customer authorization record yet: the provider
    posture is proposed rather than accepted, its retention, opt-out and
    permissions are the maintainer's to verify (#732), and the customer-specific
    authorization is #522's. So the ordinary answer today is a refusal with zero
    outbound requests — which is the answer this route is supposed to give, and
    the seam a signed record will arrive at unchanged.

    The refusal is the adapter's, taken by calling the real check rather than
    anticipating it here, so a record that ever does arrive is matched on every
    field by the module that owns that job.
    """

    request = RequestBoundary(
        project=project,
        source_class=INGEST_SOURCE_CLASS,
        purpose=INGEST_PURPOSE,
        region=PROVIDER_POSTURE.region,
        posture_identity=PROVIDER_POSTURE.identity,
        stage=stage,
    )
    if record is not None and service is None:
        raise ValueError(
            "an authorized scanned read needs the service to read through; "
            "the boundary constructs the client, and the caller supplies it"
        )
    try:
        adapter = open_boundary(
            record,
            request,
            extraction_run=extraction_run,
            cache_root=cache_root,
            service=service,
            configuration=configuration,
        )
    except TextractProcessingFailure as refusal:
        return ScannedReader(adapter=None, refusal=refusal, request=request)
    return ScannedReader(adapter=adapter, refusal=None, request=request)


@dataclass(frozen=True)
class ScannedPageOutcome:
    """One routed page's answer: the reading, or the Processing Failure instead.

    Never both, and never neither. A page the decision routes nowhere does not
    produce an outcome at all — `read_routed_page` returns None for it, because
    an ordinary blank page and a clean native page are not scanned work that
    failed, they are scanned work that was never asked for.
    """

    reading: ScannedPageReading | None
    failure: ScannedProcessingFailure | None
    regions: tuple[RoutedRegion, ...]


def read_routed_page(
    scanned: ScannedReader,
    source: Path,
    *,
    page_no: int,
    routing: PageRoutingDecision,
    rendition_sha256: str,
    source_sha256: str,
    native_layer: TokenLayer | None = None,
    render_profile_id: str | None = None,
) -> ScannedPageOutcome | None:
    """Read one routed page through the reader's boundary, or record why not.

    This is the whole of what a caller needs, so a caller need not name the
    adapter to use it: the raster is made by the adapter's own rasterizer under
    the boundary's configuration — the bytes the retained identity says were
    sent — and a refused or failed read comes back as a Processing Failure
    rather than an exception, because the page still has its inventory, its
    routing decision, its renders and its native reading, and the record should
    say that Textract did not read it and why.
    """

    regions = routed_textract_regions(routing)
    if not regions:
        return None
    if scanned.adapter is None:
        assert scanned.refusal is not None
        return ScannedPageOutcome(
            None,
            processing_failure(
                scanned.refusal,
                page_no=page_no,
                regions=regions,
                rendition_sha256=rendition_sha256,
                configuration=scanned.configuration,
            ),
            regions,
        )
    try:
        reading = read_scanned_page(
            scanned.adapter,
            rasterize_page(source, page_no, scanned.adapter.configuration),
            routing=routing,
            page_no=page_no,
            rendition_sha256=rendition_sha256,
            source_sha256=source_sha256,
            native_layer=native_layer,
            render_profile_id=render_profile_id,
        )
    except TextractProcessingFailure as raised:
        return ScannedPageOutcome(
            None,
            processing_failure(
                raised,
                page_no=page_no,
                regions=regions,
                rendition_sha256=rendition_sha256,
                configuration=scanned.configuration,
            ),
            regions,
        )
    return ScannedPageOutcome(reading, None, regions)
