"""Immutable inputs to the explicit native pipeline experiment (#447).

The former engine switches could describe a reader while leaving its mapper,
source scope and deployment implicit. These small contracts bind those choices
without changing a default or granting record authority. JSON bytes, rather
than mutable nested dictionaries, are the retained identity boundary.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def content_digest(value: Any) -> str:
    return sha256(canonical_text(value).encode()).hexdigest()


class PipelineContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PipelineScope(PipelineContract):
    """A closed source allowlist in one deployment; no wildcard promotion."""

    deployment: str = Field(min_length=1)
    source_class: Literal["native_matrix"] = "native_matrix"
    source_sha256s: tuple[str, ...] = Field(min_length=1)
    corpus_version: str = Field(min_length=1)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    purpose: Literal["prospective_production", "synthetic_validation"] = "prospective_production"
    history: Literal["excluded_preserve_original", "compatibility_claimed"] = "excluded_preserve_original"
    excluded_classes: tuple[str, ...] = ("native_minutes", "scanned", "mixed_ocr", "other")

    @model_validator(mode="after")
    def closed_scope(self):
        if (len(set(self.source_sha256s)) != len(self.source_sha256s)
                or any(len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
                       for value in self.source_sha256s)):
            raise ValueError("scope needs distinct exact source SHA-256 identities")
        if self.source_sha256s != tuple(sorted(self.source_sha256s)):
            raise ValueError("scope source identities must be in canonical order")
        if self.excluded_classes != ("native_minutes", "scanned", "mixed_ocr", "other"):
            raise ValueError("this implementation supports only the complete native matrix chain")
        return self

    @property
    def identity(self) -> str:
        return content_digest(self.model_dump(mode="json"))


class ObservationPlan(PipelineContract):
    """Declared observation origin, checked against actual per-call receipts."""

    mode: Literal["retained_replay", "synthetic", "fresh_provider"]
    origin_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    description: str = Field(min_length=1)
    provider_posture_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    customer_authorization_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_permission: Literal["public", "synthetic", "customer"]
    qualification_policy_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class MetricRequirement(PipelineContract):
    """One predeclared numeric rule, retaining the originating metric contract."""

    name: str = Field(min_length=1)
    contract: Literal["paired_corpus", "pdf_gold", "render_geometry", "raster_source_effects", "history", "latency", "memory", "processing_cost"]
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rule: Literal["minimum_ratio", "maximum_value", "measured"]
    limit: float | None = Field(default=None, allow_inf_nan=False, strict=True)

    @model_validator(mode="after")
    def rule_limit(self):
        if (self.rule == "measured") != (self.limit is None):
            raise ValueError("a measured-only rule has no invented threshold; numeric rules need one")
        return self


class QualificationPolicy(PipelineContract):
    """A maintainer's frozen native gate criteria, registered before observation."""

    schema_version: Literal["native-matrix-qualification-policy-v1"] = "native-matrix-qualification-policy-v1"
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str = Field(min_length=1)
    criteria: tuple[MetricRequirement, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def complete_contracts(self):
        names = [item.name for item in self.criteria]
        if len(set(names)) != len(names):
            raise ValueError("policy repeats a metric")
        if not {"paired_corpus", "pdf_gold", "render_geometry", "raster_source_effects", "latency", "memory", "processing_cost"} <= {item.contract for item in self.criteria}:
            raise ValueError("policy must name every required native-chain evidence contract")
        return self

    @property
    def identity(self) -> str:
        return content_digest(self.model_dump(mode="json"))


class MeasuredEvidence(PipelineContract):
    """An external numeric observation, bound to the exact measured cohort.

    This is never an approval or a caller's pass flag. The qualification
    policy applies its own fixed rule to the measured value and denominator.
    The artifact digest names the retained measurement method and raw data.
    """

    name: str = Field(min_length=1)
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_sha256s: tuple[str, ...] = Field(min_length=1)
    value: float = Field(ge=0, allow_inf_nan=False, strict=True)
    denominator: int = Field(gt=0, strict=True)
    method: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    measurement: Literal["measured", "estimated"] = "measured"


class AcceptedEvidence(PipelineContract):
    """One named, reachable thing the maintainer actually read before deciding.

    A reference that cannot be followed is not evidence, so the name, the
    reachable reference and what it says are all required. ADR-0095 keeps
    these separate from the limits below: what was measured and what that
    measurement does not establish never merge into one sentence.
    """

    name: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    summary: str = Field(min_length=1)


class MaintainerAcceptance(PipelineContract):
    """ADR-0095's selection basis: a maintainer accepting measured evidence.

    This is not a gate result and carries no status, missing or failed member.
    An incomplete qualification stays incomplete in its own receipt; this
    record says something different, in its own words, with its own limits.
    """

    schema_version: Literal["maintainer-acceptance-v1"] = "maintainer-acceptance-v1"
    decision: str = Field(min_length=1)
    configuration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    implementation_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: tuple[AcceptedEvidence, ...] = Field(min_length=1)
    limits: tuple[str, ...] = Field(min_length=1)
    words: str = Field(min_length=1)
    accepted_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def distinct_and_stated(self):
        names = [item.name for item in self.evidence]
        if len(set(names)) != len(names):
            raise ValueError("acceptance repeats an evidence name")
        if any(not value.strip() for value in self.limits) or len(set(self.limits)) != len(self.limits):
            raise ValueError("every limit must be stated once, plainly")
        return self

    @property
    def identity(self) -> str:
        return content_digest(self.model_dump(mode="json"))
