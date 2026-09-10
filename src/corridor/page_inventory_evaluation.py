"""Measure Stage 1 page routing against independently checked labels.

The production router deliberately has no character-count fallback. This module
keeps the discarded short-text rule only as an experiment comparator so a
measurement can say whether the inventory reduced missed OCR and unnecessary
OCR. The earlier ingest tests proved examples but produced no comparable,
document-bound receipt; this evaluator is the durable boundary for that claim.

`main` is `make page-inventory-eval`. It was a 44-line module of its own whose
only job was to read two artifacts, call `evaluate_stage1` and write the
result; a separate module for that implied a second consumer that never
appeared, and left the receipt's shape one import away from the contract that
defines it. It still opens no source document and reruns no OCR, so recording
a measurement cannot accidentally spend a holdout.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    Field,
    StringConstraints,
    computed_field,
    model_validator,
)

from corridor.typed_output import ClosedModel


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
RETIRED_RULE_MAX_NATIVE_LENGTH = 49


class Stage1Model(ClosedModel):
    """Every Stage 1 routing receipt shape: declared fields only, and frozen."""


class RoutingCase(Stage1Model):
    document_sha256: Sha256
    page_number: int = Field(ge=1)
    page_class: str = Field(min_length=1)
    expected_ocr_needed: bool


class RoutingGoldSet(Stage1Model):
    schema_version: Literal["corridor.pdf-stage1-gold.v1"]
    dataset_version: str = Field(min_length=1)
    pdf_gold_dataset_version: str = Field(min_length=1)
    author: str = Field(min_length=1)
    checker: str = Field(min_length=1)
    cases: tuple[RoutingCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def cases_are_unique(self) -> "RoutingGoldSet":
        identities = [
            (case.document_sha256, case.page_number) for case in self.cases
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("Stage 1 gold document/page identities must be unique")
        return self


class RoutingObservation(Stage1Model):
    document_sha256: Sha256
    page_number: int = Field(ge=1)
    page_mode: Literal["native", "ocr", "both"]
    native_text_length: int = Field(ge=0)


class RoutingRun(Stage1Model):
    schema_version: Literal["corridor.pdf-stage1-run.v1"]
    router_version: str = Field(min_length=1)
    observations: tuple[RoutingObservation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def observations_are_unique(self) -> "RoutingRun":
        identities = [
            (observation.document_sha256, observation.page_number)
            for observation in self.observations
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("Stage 1 run document/page identities must be unique")
        return self


class ConfusionMatrix(Stage1Model):
    true_positive: int = 0
    true_negative: int = 0
    false_positive: int = 0
    false_negative: int = 0


class RoutingMetrics(Stage1Model):
    confusion: ConfusionMatrix

    @computed_field
    @property
    def false_ocr_not_needed_rate(self) -> float | None:
        actual_ocr = self.confusion.true_positive + self.confusion.false_negative
        return self.confusion.false_negative / actual_ocr if actual_ocr else None

    @computed_field
    @property
    def unnecessary_ocr_rate(self) -> float | None:
        actual_native = self.confusion.true_negative + self.confusion.false_positive
        return self.confusion.false_positive / actual_native if actual_native else None


class PageClassMetrics(Stage1Model):
    inventory_router: RoutingMetrics
    retired_character_rule: RoutingMetrics


class Stage1Evaluation(Stage1Model):
    schema_version: Literal["corridor.pdf-stage1-evaluation.v1"] = (
        "corridor.pdf-stage1-evaluation.v1"
    )
    dataset_version: str
    pdf_gold_dataset_version: str
    router_version: str
    cases: int
    inventory_router: RoutingMetrics
    retired_character_rule: RoutingMetrics
    by_page_class: dict[str, PageClassMetrics]


def _confusion(
    pairs: list[tuple[bool, bool]],
) -> ConfusionMatrix:
    return ConfusionMatrix(
        true_positive=sum(expected and predicted for expected, predicted in pairs),
        true_negative=sum(
            not expected and not predicted for expected, predicted in pairs
        ),
        false_positive=sum(
            not expected and predicted for expected, predicted in pairs
        ),
        false_negative=sum(
            expected and not predicted for expected, predicted in pairs
        ),
    )


def evaluate_stage1(gold: RoutingGoldSet, run: RoutingRun) -> Stage1Evaluation:
    """Score the same exact pages under the new and retired routers."""

    cases = {
        (case.document_sha256, case.page_number): case for case in gold.cases
    }
    observations = {
        (observation.document_sha256, observation.page_number): observation
        for observation in run.observations
    }
    if set(cases) != set(observations):
        missing = sorted(set(cases) - set(observations))
        extra = sorted(set(observations) - set(cases))
        raise ValueError(
            f"Stage 1 run must cover the gold set exactly; missing={missing}, "
            f"extra={extra}"
        )
    inventory_pairs: list[tuple[bool, bool]] = []
    retired_pairs: list[tuple[bool, bool]] = []
    inventory_by_class: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
    retired_by_class: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
    for identity, case in cases.items():
        observation = observations[identity]
        inventory_prediction = observation.page_mode in {"ocr", "both"}
        retired_prediction = (
            observation.native_text_length <= RETIRED_RULE_MAX_NATIVE_LENGTH
        )
        inventory_pair = (case.expected_ocr_needed, inventory_prediction)
        retired_pair = (case.expected_ocr_needed, retired_prediction)
        inventory_pairs.append(inventory_pair)
        retired_pairs.append(retired_pair)
        inventory_by_class[case.page_class].append(inventory_pair)
        retired_by_class[case.page_class].append(retired_pair)
    return Stage1Evaluation(
        dataset_version=gold.dataset_version,
        pdf_gold_dataset_version=gold.pdf_gold_dataset_version,
        router_version=run.router_version,
        cases=len(cases),
        inventory_router=RoutingMetrics(confusion=_confusion(inventory_pairs)),
        retired_character_rule=RoutingMetrics(confusion=_confusion(retired_pairs)),
        by_page_class={
            page_class: PageClassMetrics(
                inventory_router=RoutingMetrics(
                    confusion=_confusion(inventory_by_class[page_class])
                ),
                retired_character_rule=RoutingMetrics(
                    confusion=_confusion(retired_by_class[page_class])
                ),
            )
            for page_class in sorted(inventory_by_class)
        },
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="corridor-page-inventory-eval")
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Write the Stage 1 page-routing comparison receipt from explicit artifacts."""
    arguments = _parser().parse_args(argv)
    gold = RoutingGoldSet.model_validate_json(arguments.gold.read_text())
    run = RoutingRun.model_validate_json(arguments.run.read_text())
    report = evaluate_stage1(gold, run)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )
    print(arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
