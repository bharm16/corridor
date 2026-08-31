"""Freeze PDF extraction truth and compare engines without granting authority.

This module owns the Stage 0 experiment contract: independently checked labels,
document-family splits, PDF-coordinate geometry, and comparable measurements.  It
does not ingest documents, run an extraction engine, or append anything to the
Project Record.  Keeping that boundary here prevents a challenger engine from
quietly redefining the denominator it is supposed to meet.

The earlier evaluator scored final Candidate rows and a machine-authored CSV.
That approach could not isolate page, table, cell, or citation failures and it
shared extraction machinery with its reference.  This contract replaces that
coupled ceiling with independently checked layers in document coordinates.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    model_validator,
)
from shapely.geometry import Polygon as ShapelyPolygon


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ContractModel(BaseModel):
    """Reject undeclared experiment fields so two receipts mean the same thing."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Split(StrEnum):
    development = "development"
    regression = "regression"
    holdout = "holdout"


class Point(ContractModel):
    """One fixed-point PDF coordinate, in thousandths of a PDF point."""

    x: int = Field(strict=True)
    y: int = Field(strict=True)


class Polygon(ContractModel):
    """A document-coordinate polygon; render pixels never enter the contract."""

    points: tuple[Point, ...] = Field(min_length=3)

    @model_validator(mode="after")
    def has_area(self) -> "Polygon":
        if ShapelyPolygon([(point.x, point.y) for point in self.points]).area <= 0:
            raise ValueError("polygon must have non-zero area")
        return self

    def as_shapely(self) -> ShapelyPolygon:
        return ShapelyPolygon([(point.x, point.y) for point in self.points])


class CellGold(ContractModel):
    cell_id: NonEmpty
    row_id: NonEmpty
    column_id: NonEmpty
    polygon: Polygon
    visible_text: str
    row_span: int = Field(default=1, ge=1)
    column_span: int = Field(default=1, ge=1)
    header_cell_ids: tuple[str, ...] = ()
    canonical_mapping: str | None = None
    state: Literal["confirmed", "unreadable", "unconfirmed"] = "confirmed"


class ProposalCaseGold(ContractModel):
    """A source-citation opportunity, including cases that must abstain."""

    case_id: NonEmpty
    expected_value: str | None = None
    allowed_source_cell_ids: tuple[str, ...] = ()
    must_abstain: bool = False

    @model_validator(mode="after")
    def source_rule_is_complete(self) -> "ProposalCaseGold":
        if self.must_abstain:
            return self
        if self.expected_value is None or not self.allowed_source_cell_ids:
            raise ValueError(
                "a non-abstaining proposal case needs a value and source cells"
            )
        return self


class TableGold(ContractModel):
    table_id: NonEmpty
    polygon: Polygon
    row_ids: tuple[str, ...]
    column_ids: tuple[str, ...]
    row_polygons: tuple[Polygon, ...] = ()
    column_polygons: tuple[Polygon, ...] = ()
    row_dispositions: dict[
        str, Literal["header", "active", "retired", "blank", "unreadable"]
    ] = Field(default_factory=dict)
    cells: tuple[CellGold, ...] = ()

    @model_validator(mode="after")
    def cell_topology_is_closed(self) -> "TableGold":
        row_ids = set(self.row_ids)
        column_ids = set(self.column_ids)
        if self.row_polygons and len(self.row_polygons) != len(self.row_ids):
            raise ValueError("row polygons must align one-to-one with row ids")
        if self.column_polygons and len(self.column_polygons) != len(self.column_ids):
            raise ValueError("column polygons must align one-to-one with column ids")
        if set(self.row_dispositions) != row_ids:
            raise ValueError("every declared row needs exactly one disposition")
        cell_ids = {cell.cell_id for cell in self.cells}
        if len(cell_ids) != len(self.cells):
            raise ValueError("cell ids must be unique within a table")
        for cell in self.cells:
            if cell.row_id not in row_ids or cell.column_id not in column_ids:
                raise ValueError("every cell must name a declared row and column")
            unknown_headers = set(cell.header_cell_ids) - cell_ids
            if unknown_headers:
                raise ValueError(
                    f"cell {cell.cell_id} names unknown header cells: "
                    f"{sorted(unknown_headers)}"
                )
        return self


class PageGold(ContractModel):
    page_number: int = Field(ge=1)
    page_class: NonEmpty
    width_points: int = Field(strict=True, gt=0)
    height_points: int = Field(strict=True, gt=0)
    rotation_degrees: Literal[0, 90, 180, 270] = 0
    features: tuple[str, ...] = ()
    page_scoped_values: dict[str, str] = Field(default_factory=dict)
    tables: tuple[TableGold, ...] = ()
    proposal_cases: tuple[ProposalCaseGold, ...] = ()

    @model_validator(mode="after")
    def references_are_page_local(self) -> "PageGold":
        table_ids = {table.table_id for table in self.tables}
        if len(table_ids) != len(self.tables):
            raise ValueError("table ids must be unique within a page")
        cell_ids = {
            cell.cell_id for table in self.tables for cell in table.cells
        }
        cases = {case.case_id for case in self.proposal_cases}
        if len(cases) != len(self.proposal_cases):
            raise ValueError("proposal case ids must be unique within a page")
        for case in self.proposal_cases:
            unknown = set(case.allowed_source_cell_ids) - cell_ids
            if unknown:
                raise ValueError(
                    f"proposal case {case.case_id} names unknown cells: "
                    f"{sorted(unknown)}"
                )
        for table in self.tables:
            polygons = (
                table.polygon,
                *table.row_polygons,
                *table.column_polygons,
                *(cell.polygon for cell in table.cells),
            )
            for polygon in polygons:
                if any(
                    point.x < 0
                    or point.y < 0
                    or point.x > self.width_points
                    or point.y > self.height_points
                    for point in polygon.points
                ):
                    raise ValueError("PDF geometry must stay within the page bounds")
        return self


class DocumentGold(ContractModel):
    document_sha256: Sha256
    document_family: NonEmpty
    split: Split
    source_title: NonEmpty
    author: str | None = None
    checker: str | None = None
    pages: tuple[PageGold, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def pages_are_unique(self) -> "DocumentGold":
        page_numbers = [page.page_number for page in self.pages]
        if len(page_numbers) != len(set(page_numbers)):
            raise ValueError("page numbers must be unique within a document")
        return self


class Adjudication(ContractModel):
    document_sha256: Sha256
    page_number: int = Field(ge=1)
    layer: NonEmpty
    item_id: NonEmpty
    first_labeler: NonEmpty
    second_labeler: NonEmpty
    adjudicator: NonEmpty
    resolution: NonEmpty


class AcceptanceThresholds(ContractModel):
    page_coverage_f1_min: float = Field(default=1.0, ge=0, le=1)
    page_class_accuracy_min: float = Field(default=0.98, ge=0, le=1)
    table_f1_min: float = Field(default=0.95, ge=0, le=1)
    row_f1_min: float = Field(default=0.95, ge=0, le=1)
    column_f1_min: float = Field(default=0.95, ge=0, le=1)
    cell_f1_min: float = Field(default=0.95, ge=0, le=1)
    cell_text_exact_min: float = Field(default=0.98, ge=0, le=1)
    row_disposition_exact_min: float = Field(default=0.98, ge=0, le=1)
    cell_span_exact_min: float = Field(default=0.98, ge=0, le=1)
    cell_topology_exact_min: float = Field(default=0.98, ge=0, le=1)
    header_relationships_exact_min: float = Field(default=0.98, ge=0, le=1)
    canonical_mapping_exact_min: float = Field(default=0.98, ge=0, le=1)
    cell_state_exact_min: float = Field(default=0.98, ge=0, le=1)
    page_scoped_values_f1_min: float = Field(default=0.98, ge=0, le=1)
    wrong_source_cited_proposals_max: int = Field(default=0, ge=0)
    failure_rate_max: float = Field(default=0.01, ge=0, le=1)
    abstention_rate_max: float = Field(default=0.10, ge=0, le=1)
    latency_ms_per_document_max: int = Field(default=120_000, gt=0)
    peak_memory_bytes_per_document_max: int = Field(
        default=2_147_483_648, gt=0
    )

class GoldSet(ContractModel):
    schema_version: Literal["corridor.pdf-gold.v1"]
    dataset_version: NonEmpty
    frozen_at: datetime
    frozen_by: NonEmpty
    checked_by: NonEmpty
    thresholds: AcceptanceThresholds = Field(default_factory=AcceptanceThresholds)
    documents: tuple[DocumentGold, ...] = Field(min_length=1)
    adjudications: tuple[Adjudication, ...] = ()

    @model_validator(mode="after")
    def split_by_document_family(self) -> "GoldSet":
        families: dict[str, set[Split]] = defaultdict(set)
        digests: set[str] = set()
        for document in self.documents:
            families[document.document_family].add(document.split)
            if document.document_sha256 in digests:
                raise ValueError("document digests must be unique")
            digests.add(document.document_sha256)
        leaking = sorted(
            family for family, splits in families.items() if len(splits) > 1
        )
        if leaking:
            raise ValueError(
                "document family appears in more than one split: "
                + ", ".join(leaking)
            )
        for adjudication in self.adjudications:
            if adjudication.document_sha256 not in digests:
                raise ValueError("adjudication must name a dataset document")
        return self


class CellPrediction(ContractModel):
    cell_id: NonEmpty
    row_id: NonEmpty
    column_id: NonEmpty
    polygon: Polygon
    visible_text: str
    row_span: int = Field(default=1, ge=1)
    column_span: int = Field(default=1, ge=1)
    header_cell_ids: tuple[str, ...] = ()
    canonical_mapping: str | None = None
    state: Literal["confirmed", "unreadable", "unconfirmed"] = "confirmed"


class TablePrediction(ContractModel):
    table_id: NonEmpty
    polygon: Polygon
    row_polygons: tuple[Polygon, ...] = ()
    column_polygons: tuple[Polygon, ...] = ()
    row_ids: tuple[str, ...] = ()
    column_ids: tuple[str, ...] = ()
    row_dispositions: dict[
        str, Literal["header", "active", "retired", "blank", "unreadable"]
    ] = Field(default_factory=dict)
    cells: tuple[CellPrediction, ...] = ()

    @model_validator(mode="after")
    def topology_is_closed(self) -> "TablePrediction":
        if len(self.row_ids) != len(self.row_polygons):
            raise ValueError("predicted row ids and polygons must align")
        if len(self.column_ids) != len(self.column_polygons):
            raise ValueError("predicted column ids and polygons must align")
        if set(self.row_dispositions) != set(self.row_ids):
            raise ValueError("every predicted row needs exactly one disposition")
        cell_ids = {cell.cell_id for cell in self.cells}
        if len(cell_ids) != len(self.cells):
            raise ValueError("predicted cell ids must be unique within a table")
        for cell in self.cells:
            if cell.row_id not in self.row_ids or cell.column_id not in self.column_ids:
                raise ValueError("predicted cells must name declared rows and columns")
            if set(cell.header_cell_ids) - cell_ids:
                raise ValueError("predicted cells must name declared header cells")
        return self


class ProposalPrediction(ContractModel):
    case_id: NonEmpty
    value: str | None = None
    source_cell_id: str | None = None
    abstained: bool = False

    @model_validator(mode="after")
    def emitted_or_abstained(self) -> "ProposalPrediction":
        if self.abstained and (self.value is not None or self.source_cell_id is not None):
            raise ValueError("an abstention cannot carry a value or source cell")
        if not self.abstained and (self.value is None or self.source_cell_id is None):
            raise ValueError("an emitted proposal needs a value and source cell")
        return self


class PagePrediction(ContractModel):
    page_number: int = Field(ge=1)
    page_class: NonEmpty
    abstained: bool
    page_scoped_values: dict[str, str] = Field(default_factory=dict)
    tables: tuple[TablePrediction, ...] = ()
    proposals: tuple[ProposalPrediction, ...] = ()

    @model_validator(mode="after")
    def ids_are_page_local(self) -> "PagePrediction":
        table_ids = [table.table_id for table in self.tables]
        if len(table_ids) != len(set(table_ids)):
            raise ValueError("predicted table ids must be unique within a page")
        cell_ids = [cell.cell_id for table in self.tables for cell in table.cells]
        if len(cell_ids) != len(set(cell_ids)):
            raise ValueError("predicted cell ids must be unique within a page")
        proposal_ids = [proposal.case_id for proposal in self.proposals]
        if len(proposal_ids) != len(set(proposal_ids)):
            raise ValueError("predicted proposal case ids must be unique within a page")
        return self


class DocumentPrediction(ContractModel):
    document_sha256: Sha256
    latency_ms: int = Field(ge=0)
    peak_memory_bytes: int = Field(ge=0)
    failed: bool = False
    failure_reason: str | None = None
    pages: tuple[PagePrediction, ...] = ()

    @model_validator(mode="after")
    def failure_is_explained(self) -> "DocumentPrediction":
        if self.failed and not self.failure_reason:
            raise ValueError("a failed document needs a failure reason")
        page_numbers = [page.page_number for page in self.pages]
        if len(page_numbers) != len(set(page_numbers)):
            raise ValueError("predicted page numbers must be unique within a document")
        return self


class EngineRun(ContractModel):
    schema_version: Literal["corridor.pdf-engine-run.v1"]
    engine: NonEmpty
    engine_version: NonEmpty
    configuration_sha256: Sha256
    documents: tuple[DocumentPrediction, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def documents_are_unique(self) -> "EngineRun":
        digests = [document.document_sha256 for document in self.documents]
        if len(digests) != len(set(digests)):
            raise ValueError("engine run document digests must be unique")
        return self


class BinaryCounts(ContractModel):
    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0

    @computed_field
    @property
    def precision(self) -> float:
        denominator = self.true_positive + self.false_positive
        return self.true_positive / denominator if denominator else 1.0

    @computed_field
    @property
    def recall(self) -> float:
        denominator = self.true_positive + self.false_negative
        return self.true_positive / denominator if denominator else 1.0

    @computed_field
    @property
    def f1(self) -> float:
        denominator = self.precision + self.recall
        return 2 * self.precision * self.recall / denominator if denominator else 0.0


class Accuracy(ContractModel):
    correct: int = 0
    total: int = 0

    @computed_field
    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 1.0


class AggregateMetrics(ContractModel):
    pages: int = 0
    documents: int = 0
    page_coverage: BinaryCounts = Field(default_factory=BinaryCounts)
    page_classification: Accuracy = Field(default_factory=Accuracy)
    tables: BinaryCounts = Field(default_factory=BinaryCounts)
    rows: BinaryCounts = Field(default_factory=BinaryCounts)
    columns: BinaryCounts = Field(default_factory=BinaryCounts)
    cells: BinaryCounts = Field(default_factory=BinaryCounts)
    cell_text_exact: Accuracy = Field(default_factory=Accuracy)
    row_disposition_exact: Accuracy = Field(default_factory=Accuracy)
    cell_span_exact: Accuracy = Field(default_factory=Accuracy)
    cell_topology_exact: Accuracy = Field(default_factory=Accuracy)
    header_relationships_exact: Accuracy = Field(default_factory=Accuracy)
    canonical_mapping_exact: Accuracy = Field(default_factory=Accuracy)
    cell_state_exact: Accuracy = Field(default_factory=Accuracy)
    page_scoped_values: BinaryCounts = Field(default_factory=BinaryCounts)
    failed_documents: int = 0
    abstained_pages: int = 0
    latency_ms: int = 0
    peak_memory_bytes: int = 0

    @computed_field
    @property
    def failure_rate(self) -> float:
        return self.failed_documents / self.documents if self.documents else 0.0

    @computed_field
    @property
    def abstention_rate(self) -> float:
        return self.abstained_pages / self.pages if self.pages else 0.0


class SourceCitationSafetyMetric(ContractModel):
    name: Literal["wrong_source_cited_proposals_avoided"] = (
        "wrong_source_cited_proposals_avoided"
    )
    opportunities: int
    avoided: int
    emitted: int

    @computed_field
    @property
    def rate(self) -> float:
        return self.avoided / self.opportunities if self.opportunities else 1.0


class EvaluationReport(ContractModel):
    schema_version: Literal["corridor.pdf-evaluation.v1"] = (
        "corridor.pdf-evaluation.v1"
    )
    dataset_version: NonEmpty
    engine: NonEmpty
    engine_version: NonEmpty
    configuration_sha256: Sha256
    overall: AggregateMetrics
    by_page_class: dict[str, AggregateMetrics]
    by_document: dict[str, AggregateMetrics]
    key_metric: SourceCitationSafetyMetric
    thresholds_met: dict[str, bool]
    passed: bool


def load_gold_set(path: Path | str) -> GoldSet:
    return GoldSet.model_validate_json(Path(path).read_text())


def load_engine_run(path: Path | str) -> EngineRun:
    return EngineRun.model_validate_json(Path(path).read_text())


def _iou(left: Polygon, right: Polygon) -> float:
    left_shape = left.as_shapely()
    right_shape = right.as_shapely()
    union = left_shape.union(right_shape).area
    return left_shape.intersection(right_shape).area / union if union else 0.0


def _hungarian_maximize(scores: list[list[float]]) -> list[tuple[int, int]]:
    """Return the maximum-weight one-to-one assignment for a rectangle."""

    if not scores or not scores[0]:
        return []
    rows = len(scores)
    columns = len(scores[0])
    size = max(rows, columns)
    padded = [
        [scores[row][column] if row < rows and column < columns else 0.0
         for column in range(size)]
        for row in range(size)
    ]
    costs = [[1.0 - score for score in row] for row in padded]
    u = [0.0] * (size + 1)
    v = [0.0] * (size + 1)
    p = [0] * (size + 1)
    way = [0] * (size + 1)
    for i in range(1, size + 1):
        p[0] = i
        j0 = 0
        minimum = [float("inf")] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = float("inf")
            j1 = 0
            for j in range(1, size + 1):
                if used[j]:
                    continue
                current = costs[i0 - 1][j - 1] - u[i0] - v[j]
                if current < minimum[j]:
                    minimum[j] = current
                    way[j] = j0
                if minimum[j] < delta:
                    delta = minimum[j]
                    j1 = j
            for j in range(size + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minimum[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    assignment = []
    for column in range(1, size + 1):
        row = p[column]
        if row and row <= rows and column <= columns:
            assignment.append((row - 1, column - 1))
    return assignment


def _match_polygons(
    gold: list[Polygon], predicted: list[Polygon], *, threshold: float = 0.5
) -> tuple[BinaryCounts, list[tuple[int, int]]]:
    if not gold or not predicted:
        return (
            BinaryCounts(
                true_positive=0,
                false_positive=len(predicted),
                false_negative=len(gold),
            ),
            [],
        )
    scores = [[_iou(gold_item, prediction) for prediction in predicted] for gold_item in gold]
    matches = [
        pair
        for pair in _hungarian_maximize(scores)
        if scores[pair[0]][pair[1]] >= threshold
    ]
    return (
        BinaryCounts(
            true_positive=len(matches),
            false_positive=len(predicted) - len(matches),
            false_negative=len(gold) - len(matches),
        ),
        matches,
    )


def _plus_counts(left: BinaryCounts, right: BinaryCounts) -> BinaryCounts:
    return BinaryCounts(
        true_positive=left.true_positive + right.true_positive,
        false_positive=left.false_positive + right.false_positive,
        false_negative=left.false_negative + right.false_negative,
    )


def _mapping_counts(gold: dict[str, str], predicted: dict[str, str]) -> BinaryCounts:
    gold_items = set(gold.items())
    predicted_items = set(predicted.items())
    return BinaryCounts(
        true_positive=len(gold_items & predicted_items),
        false_positive=len(predicted_items - gold_items),
        false_negative=len(gold_items - predicted_items),
    )


class _MutableAggregate:
    def __init__(self) -> None:
        self.pages = 0
        self.document_digests: set[str] = set()
        self.page_coverage = BinaryCounts()
        self.page_class_correct = 0
        self.page_class_total = 0
        self.tables = BinaryCounts()
        self.rows = BinaryCounts()
        self.columns = BinaryCounts()
        self.cells = BinaryCounts()
        self.cell_text_correct = 0
        self.cell_text_total = 0
        self.row_disposition_correct = 0
        self.row_disposition_total = 0
        self.cell_span_correct = 0
        self.cell_span_total = 0
        self.cell_topology_correct = 0
        self.cell_topology_total = 0
        self.header_relationships_correct = 0
        self.header_relationships_total = 0
        self.canonical_mapping_correct = 0
        self.canonical_mapping_total = 0
        self.cell_state_correct = 0
        self.cell_state_total = 0
        self.page_scoped_values = BinaryCounts()
        self.failed_documents: set[str] = set()
        self.abstained_pages = 0
        self.latency_by_document: dict[str, int] = {}
        self.memory_by_document: dict[str, int] = {}

    def add_document(self, digest: str, prediction: DocumentPrediction | None) -> None:
        self.document_digests.add(digest)
        if prediction is None or prediction.failed:
            self.failed_documents.add(digest)
        if prediction is not None:
            self.latency_by_document[digest] = prediction.latency_ms
            self.memory_by_document[digest] = prediction.peak_memory_bytes

    def freeze(self) -> AggregateMetrics:
        return AggregateMetrics(
            pages=self.pages,
            documents=len(self.document_digests),
            page_coverage=self.page_coverage,
            page_classification=Accuracy(
                correct=self.page_class_correct, total=self.page_class_total
            ),
            tables=self.tables,
            rows=self.rows,
            columns=self.columns,
            cells=self.cells,
            cell_text_exact=Accuracy(
                correct=self.cell_text_correct, total=self.cell_text_total
            ),
            row_disposition_exact=Accuracy(
                correct=self.row_disposition_correct,
                total=self.row_disposition_total,
            ),
            cell_span_exact=Accuracy(
                correct=self.cell_span_correct, total=self.cell_span_total
            ),
            cell_topology_exact=Accuracy(
                correct=self.cell_topology_correct,
                total=self.cell_topology_total,
            ),
            header_relationships_exact=Accuracy(
                correct=self.header_relationships_correct,
                total=self.header_relationships_total,
            ),
            canonical_mapping_exact=Accuracy(
                correct=self.canonical_mapping_correct,
                total=self.canonical_mapping_total,
            ),
            cell_state_exact=Accuracy(
                correct=self.cell_state_correct, total=self.cell_state_total
            ),
            page_scoped_values=self.page_scoped_values,
            failed_documents=len(self.failed_documents),
            abstained_pages=self.abstained_pages,
            latency_ms=sum(self.latency_by_document.values()),
            peak_memory_bytes=max(self.memory_by_document.values(), default=0),
        )


def _add_page(
    aggregate: _MutableAggregate,
    gold_page: PageGold,
    predicted_page: PagePrediction | None,
) -> None:
    aggregate.pages += 1
    aggregate.page_coverage = _plus_counts(
        aggregate.page_coverage,
        BinaryCounts(
            true_positive=1 if predicted_page is not None else 0,
            false_negative=1 if predicted_page is None else 0,
        ),
    )
    aggregate.page_class_total += 1
    if predicted_page is not None and predicted_page.page_class == gold_page.page_class:
        aggregate.page_class_correct += 1
    if predicted_page is None or predicted_page.abstained:
        aggregate.abstained_pages += 1
    aggregate.page_scoped_values = _plus_counts(
        aggregate.page_scoped_values,
        _mapping_counts(
            gold_page.page_scoped_values,
            predicted_page.page_scoped_values if predicted_page else {},
        ),
    )
    predicted_tables = list(predicted_page.tables) if predicted_page else []
    table_counts, table_matches = _match_polygons(
        [table.polygon for table in gold_page.tables],
        [table.polygon for table in predicted_tables],
    )
    aggregate.tables = _plus_counts(aggregate.tables, table_counts)
    for gold_index, predicted_index in table_matches:
        gold_table = gold_page.tables[gold_index]
        predicted_table = predicted_tables[predicted_index]
        row_counts, row_matches = _match_polygons(
            list(gold_table.row_polygons), list(predicted_table.row_polygons)
        )
        aggregate.rows = _plus_counts(aggregate.rows, row_counts)
        row_id_map = {
            predicted_table.row_ids[predicted_row]: gold_table.row_ids[gold_row]
            for gold_row, predicted_row in row_matches
        }
        for gold_row, predicted_row in row_matches:
            aggregate.row_disposition_total += 1
            if (
                gold_table.row_dispositions[gold_table.row_ids[gold_row]]
                == predicted_table.row_dispositions[
                    predicted_table.row_ids[predicted_row]
                ]
            ):
                aggregate.row_disposition_correct += 1
        column_counts, column_matches = _match_polygons(
            list(gold_table.column_polygons), list(predicted_table.column_polygons)
        )
        aggregate.columns = _plus_counts(aggregate.columns, column_counts)
        column_id_map = {
            predicted_table.column_ids[predicted_column]: (
                gold_table.column_ids[gold_column]
            )
            for gold_column, predicted_column in column_matches
        }
        cell_counts, cell_matches = _match_polygons(
            [cell.polygon for cell in gold_table.cells],
            [cell.polygon for cell in predicted_table.cells],
        )
        aggregate.cells = _plus_counts(aggregate.cells, cell_counts)
        cell_id_map = {
            predicted_table.cells[predicted_cell].cell_id: (
                gold_table.cells[gold_cell].cell_id
            )
            for gold_cell, predicted_cell in cell_matches
        }
        for gold_cell_index, predicted_cell_index in cell_matches:
            gold_cell = gold_table.cells[gold_cell_index]
            predicted_cell = predicted_table.cells[predicted_cell_index]
            aggregate.cell_text_total += 1
            if gold_cell.visible_text == predicted_cell.visible_text:
                aggregate.cell_text_correct += 1
            aggregate.cell_span_total += 1
            if (gold_cell.row_span, gold_cell.column_span) == (
                predicted_cell.row_span,
                predicted_cell.column_span,
            ):
                aggregate.cell_span_correct += 1
            aggregate.cell_topology_total += 1
            if (
                row_id_map.get(predicted_cell.row_id) == gold_cell.row_id
                and column_id_map.get(predicted_cell.column_id)
                == gold_cell.column_id
            ):
                aggregate.cell_topology_correct += 1
            aggregate.header_relationships_total += 1
            translated_headers = {
                cell_id_map.get(header_id) for header_id in predicted_cell.header_cell_ids
            }
            if translated_headers == set(gold_cell.header_cell_ids):
                aggregate.header_relationships_correct += 1
            aggregate.canonical_mapping_total += 1
            if gold_cell.canonical_mapping == predicted_cell.canonical_mapping:
                aggregate.canonical_mapping_correct += 1
            aggregate.cell_state_total += 1
            if gold_cell.state == predicted_cell.state:
                aggregate.cell_state_correct += 1
    matched_gold = {gold_index for gold_index, _ in table_matches}
    matched_predicted = {predicted_index for _, predicted_index in table_matches}
    for index, table in enumerate(gold_page.tables):
        if index in matched_gold:
            continue
        aggregate.rows = _plus_counts(
            aggregate.rows,
            BinaryCounts(false_negative=len(table.row_polygons)),
        )
        aggregate.columns = _plus_counts(
            aggregate.columns,
            BinaryCounts(false_negative=len(table.column_polygons)),
        )
        aggregate.cells = _plus_counts(
            aggregate.cells,
            BinaryCounts(false_negative=len(table.cells)),
        )
    for index, table in enumerate(predicted_tables):
        if index in matched_predicted:
            continue
        aggregate.rows = _plus_counts(
            aggregate.rows,
            BinaryCounts(false_positive=len(table.row_polygons)),
        )
        aggregate.columns = _plus_counts(
            aggregate.columns,
            BinaryCounts(false_positive=len(table.column_polygons)),
        )
        aggregate.cells = _plus_counts(
            aggregate.cells,
            BinaryCounts(false_positive=len(table.cells)),
        )


def _add_extra_page(
    aggregate: _MutableAggregate, predicted_page: PagePrediction
) -> None:
    aggregate.pages += 1
    aggregate.page_coverage = _plus_counts(
        aggregate.page_coverage, BinaryCounts(false_positive=1)
    )
    aggregate.page_class_total += 1
    if predicted_page.abstained:
        aggregate.abstained_pages += 1
    aggregate.page_scoped_values = _plus_counts(
        aggregate.page_scoped_values,
        BinaryCounts(false_positive=len(predicted_page.page_scoped_values)),
    )
    aggregate.tables = _plus_counts(
        aggregate.tables, BinaryCounts(false_positive=len(predicted_page.tables))
    )
    for table in predicted_page.tables:
        aggregate.rows = _plus_counts(
            aggregate.rows, BinaryCounts(false_positive=len(table.row_polygons))
        )
        aggregate.columns = _plus_counts(
            aggregate.columns,
            BinaryCounts(false_positive=len(table.column_polygons)),
        )
        aggregate.cells = _plus_counts(
            aggregate.cells, BinaryCounts(false_positive=len(table.cells))
        )


def _predicted_to_gold_cell_ids(
    gold_page: PageGold, predicted_page: PagePrediction | None
) -> dict[str, str]:
    if predicted_page is None:
        return {}
    gold_cells = [cell for table in gold_page.tables for cell in table.cells]
    predicted_cells = [
        cell for table in predicted_page.tables for cell in table.cells
    ]
    _, matches = _match_polygons(
        [cell.polygon for cell in gold_cells],
        [cell.polygon for cell in predicted_cells],
    )
    return {
        predicted_cells[predicted_index].cell_id: gold_cells[gold_index].cell_id
        for gold_index, predicted_index in matches
    }


def _source_citation_metric(
    gold: GoldSet, predictions: dict[str, DocumentPrediction]
) -> SourceCitationSafetyMetric:
    opportunities = avoided = emitted = 0
    for document in gold.documents:
        predicted_document = predictions.get(document.document_sha256)
        predicted_pages = {
            page.page_number: page
            for page in predicted_document.pages
        } if predicted_document else {}
        for page in document.pages:
            predicted_page = predicted_pages.get(page.page_number)
            cell_id_map = _predicted_to_gold_cell_ids(page, predicted_page)
            cases = {
                proposal.case_id: proposal
                for proposal in predicted_page.proposals
            } if predicted_page else {}
            for case in page.proposal_cases:
                opportunities += 1
                proposal = cases.get(case.case_id)
                wrong = bool(
                    proposal
                    and not proposal.abstained
                    and (
                        case.must_abstain
                        or proposal.value != case.expected_value
                        or cell_id_map.get(proposal.source_cell_id)
                        not in case.allowed_source_cell_ids
                    )
                )
                if wrong:
                    emitted += 1
                else:
                    avoided += 1
    return SourceCitationSafetyMetric(
        opportunities=opportunities, avoided=avoided, emitted=emitted
    )


def evaluate(gold: GoldSet, run: EngineRun) -> EvaluationReport:
    """Compare one frozen engine run at every independent label layer."""

    gold_digests = {document.document_sha256 for document in gold.documents}
    unknown = {
        document.document_sha256 for document in run.documents
    } - gold_digests
    if unknown:
        raise ValueError(f"engine run names documents outside the gold set: {sorted(unknown)}")
    predictions = {document.document_sha256: document for document in run.documents}
    overall = _MutableAggregate()
    by_class: dict[str, _MutableAggregate] = defaultdict(_MutableAggregate)
    by_document: dict[str, _MutableAggregate] = defaultdict(_MutableAggregate)
    for document in gold.documents:
        predicted_document = predictions.get(document.document_sha256)
        predicted_pages = {
            page.page_number: page for page in predicted_document.pages
        } if predicted_document else {}
        overall.add_document(document.document_sha256, predicted_document)
        by_document[document.document_sha256].add_document(
            document.document_sha256, predicted_document
        )
        for page in document.pages:
            predicted_page = predicted_pages.get(page.page_number)
            page_class_aggregate = by_class[page.page_class]
            page_class_aggregate.add_document(
                document.document_sha256, predicted_document
            )
            _add_page(overall, page, predicted_page)
            _add_page(page_class_aggregate, page, predicted_page)
            _add_page(by_document[document.document_sha256], page, predicted_page)
        gold_page_numbers = {page.page_number for page in document.pages}
        for extra_page_number in sorted(set(predicted_pages) - gold_page_numbers):
            extra_page = predicted_pages[extra_page_number]
            page_class_aggregate = by_class[extra_page.page_class]
            page_class_aggregate.add_document(
                document.document_sha256, predicted_document
            )
            _add_extra_page(overall, extra_page)
            _add_extra_page(page_class_aggregate, extra_page)
            _add_extra_page(by_document[document.document_sha256], extra_page)
    frozen_overall = overall.freeze()
    key_metric = _source_citation_metric(gold, predictions)
    thresholds = gold.thresholds
    average_latency = (
        frozen_overall.latency_ms / frozen_overall.documents
        if frozen_overall.documents else 0
    )
    thresholds_met = {
        "page_coverage": (
            frozen_overall.page_coverage.f1 >= thresholds.page_coverage_f1_min
        ),
        "page_class_accuracy": (
            frozen_overall.page_classification.accuracy
            >= thresholds.page_class_accuracy_min
        ),
        "table_f1": frozen_overall.tables.f1 >= thresholds.table_f1_min,
        "row_f1": frozen_overall.rows.f1 >= thresholds.row_f1_min,
        "column_f1": frozen_overall.columns.f1 >= thresholds.column_f1_min,
        "cell_f1": frozen_overall.cells.f1 >= thresholds.cell_f1_min,
        "cell_text_exact": (
            frozen_overall.cell_text_exact.accuracy
            >= thresholds.cell_text_exact_min
        ),
        "row_disposition_exact": (
            frozen_overall.row_disposition_exact.accuracy
            >= thresholds.row_disposition_exact_min
        ),
        "cell_span_exact": (
            frozen_overall.cell_span_exact.accuracy
            >= thresholds.cell_span_exact_min
        ),
        "cell_topology_exact": (
            frozen_overall.cell_topology_exact.accuracy
            >= thresholds.cell_topology_exact_min
        ),
        "header_relationships_exact": (
            frozen_overall.header_relationships_exact.accuracy
            >= thresholds.header_relationships_exact_min
        ),
        "canonical_mapping_exact": (
            frozen_overall.canonical_mapping_exact.accuracy
            >= thresholds.canonical_mapping_exact_min
        ),
        "cell_state_exact": (
            frozen_overall.cell_state_exact.accuracy
            >= thresholds.cell_state_exact_min
        ),
        "page_scoped_values": (
            frozen_overall.page_scoped_values.f1
            >= thresholds.page_scoped_values_f1_min
        ),
        "wrong_source_cited_proposals": (
            key_metric.emitted <= thresholds.wrong_source_cited_proposals_max
        ),
        "failure_rate": frozen_overall.failure_rate <= thresholds.failure_rate_max,
        "abstention_rate": (
            frozen_overall.abstention_rate <= thresholds.abstention_rate_max
        ),
        "latency": average_latency <= thresholds.latency_ms_per_document_max,
        "memory": (
            frozen_overall.peak_memory_bytes
            <= thresholds.peak_memory_bytes_per_document_max
        ),
    }
    return EvaluationReport(
        dataset_version=gold.dataset_version,
        engine=run.engine,
        engine_version=run.engine_version,
        configuration_sha256=run.configuration_sha256,
        overall=frozen_overall,
        by_page_class={name: value.freeze() for name, value in sorted(by_class.items())},
        by_document={
            digest: value.freeze() for digest, value in sorted(by_document.items())
        },
        key_metric=key_metric,
        thresholds_met=thresholds_met,
        passed=all(thresholds_met.values()),
    )
