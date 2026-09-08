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

from dataclasses import dataclass
from typing import Literal

from corridor.page_inventory import PageRoutingDecision, PdfRect

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
