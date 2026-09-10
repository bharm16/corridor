"""One authorization check stands in front of both providers (ADR-0094).

The Textract adapter and the native model-provider adapter used to carry the
same check twice, and `tests/test_textract_adapter.py` and
`tests/test_native_provider_boundary.py` proved the same five properties twice.
Those properties are proved here once, through a table of the two adapters:
each row says how to build that adapter's customer authorization, experiment
scope and covered request, which posture it binds to, and how to open it
against a transport that fails the test on any outbound request. The adapter
files keep only the fields that are theirs.

The posture-status rule is the one place the two adapters differ on a shared
field, and the difference is declared in `provider_authorization` rather than
unified; the last test pins that declaration so a change to either rule is a
visible decision.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

import pytest

import test_native_provider_boundary as native
import test_textract_adapter as textract
from corridor.native_provider_boundary import (
    POSTURE,
    Budget,
    NativeProviderRefused,
    open_native_provider_boundary,
)
from corridor.native_provider_boundary import mismatches as native_mismatches
from corridor.provider_authorization import (
    CUSTOMER_STAGES,
    EXPERIMENT_STAGE,
    OutboundCounts,
    ProviderRefused,
)
from corridor_pdf_reader.textract_adapter.boundary import TextractProcessingFailure, open_boundary
from corridor_pdf_reader.textract_adapter.records import PROVIDER_POSTURE
from corridor_pdf_reader.textract_adapter.records import mismatches as textract_mismatches

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Adapter:
    """One provider adapter, as the shared check sees it."""

    posture: Any
    accepted: Any
    unaccepted: Any
    refusal: type[ProviderRefused]
    refusal_kind: str
    customer: Callable[..., Any]
    experiment: Callable[..., Any]
    request: Callable[..., Any]
    experiment_request: Callable[..., Any]
    other_purpose: str
    open: Callable[[Any, Any, Any, Path], Any]
    mismatches: Callable[[Any, Any, Any], tuple[str, ...]]
    status_gates_every_request: bool


def _open_textract(record: Any, request: Any, posture: Any, tmp_path: Path) -> Any:
    return open_boundary(
        record, request, extraction_run="run-1", cache_root=tmp_path / "cache",
        service=textract.FailingService(), posture=posture,
    )


def _open_native(record: Any, request: Any, posture: Any, tmp_path: Path) -> Any:
    return open_native_provider_boundary(
        record, request, transport=native.ForbiddenTransport(),
        budget=Budget(max_calls=2, max_pages=2, max_total_tokens=10_000),
        source_sha256s=frozenset({native.SOURCE}), campaign="shared-check", posture=posture,
    )


ADAPTERS = {
    "textract": Adapter(
        posture=PROVIDER_POSTURE,
        accepted=textract.ACCEPTED_POSTURE,
        unaccepted=PROVIDER_POSTURE,
        refusal=TextractProcessingFailure,
        refusal_kind="processing-failure",
        customer=textract.authorization,
        experiment=textract.experiment,
        request=textract.request,
        experiment_request=textract.experiment_request,
        other_purpose="image-region-reading",
        open=_open_textract,
        mismatches=textract_mismatches,
        status_gates_every_request=False,
    ),
    "native-model-provider": Adapter(
        posture=POSTURE,
        accepted=POSTURE,
        unaccepted=replace(POSTURE, status="proposed"),
        refusal=NativeProviderRefused,
        refusal_kind="native-provider-refusal",
        customer=native._customer,
        experiment=native._experiment,
        request=lambda **overrides: native._request(**{"stage": "shadow", **overrides}),
        experiment_request=native._request,
        other_purpose="extraction-measurement",
        open=_open_native,
        mismatches=lambda record, request, posture: native_mismatches(
            record, request, source_sha256s=frozenset({native.SOURCE}),
            budget=Budget(max_calls=2, max_pages=2, max_total_tokens=10_000), posture=posture,
        ),
        status_gates_every_request=True,
    ),
}


def _fields(mismatches: tuple[str, ...]) -> list[str]:
    return [entry.split(":")[0] for entry in mismatches]


def _shared(mismatches: tuple[str, ...], *names: str) -> list[str]:
    """The named fields in the order the refusal listed them, each once."""
    seen: list[str] = []
    for field in _fields(mismatches):
        if field in names and field not in seen:
            seen.append(field)
    return seen


@pytest.fixture(params=sorted(ADAPTERS))
def adapter(request: pytest.FixtureRequest) -> Adapter:
    return ADAPTERS[request.param]


def test_an_absent_record_is_refused_with_zero_outbound_requests(adapter: Adapter, tmp_path: Path) -> None:
    request = adapter.request()

    with pytest.raises(adapter.refusal) as caught:
        adapter.open(None, request, adapter.accepted, tmp_path)

    refused = caught.value
    assert isinstance(refused, ProviderRefused)
    assert refused.reason == "authorization-absent"
    assert refused.outbound_requests == 0
    assert _fields(refused.mismatches) == ["authorization-absent"]
    assert str(refused) == f"authorization-absent: {refused.mismatches[0]}"
    record = refused.record()
    assert record["kind"] == adapter.refusal_kind
    assert set(record) == {"kind", "reason", "detail", "mismatches", "outbound_requests", "request", "recorded_at"}
    assert record["request"] == request.as_dict() and record["mismatches"] == list(refused.mismatches)
    assert record["outbound_requests"] == 0 and record["detail"] == ""


def test_a_refusal_names_every_failing_field_not_the_first(adapter: Adapter, tmp_path: Path) -> None:
    request = adapter.request(project="project-9", source_class="email", stage="authoritative")

    with pytest.raises(adapter.refusal) as caught:
        adapter.open(adapter.customer(), request, adapter.accepted, tmp_path)

    found = caught.value.mismatches
    assert _shared(found, "source-class", "project", "stage") == ["source-class", "project", "stage"]
    project = next(entry for entry in found if entry.startswith("project:"))
    assert "project-9" in project and "record" in project
    assert caught.value.reason == "authorization-refused"
    assert caught.value.outbound_requests == 0


def test_a_purpose_outside_the_posture_is_named_even_when_the_record_permits_it(adapter: Adapter, tmp_path: Path) -> None:
    """The posture's `permitted_purposes` is checked on its own: a customer
    record cannot widen it by listing a purpose the posture never named."""
    purpose = "minutes-prose-extraction"
    assert purpose not in adapter.posture.permitted_purposes

    with pytest.raises(adapter.refusal) as caught:
        adapter.open(adapter.customer(purposes=frozenset({purpose})), adapter.request(purpose=purpose), adapter.accepted, tmp_path)

    entries = [entry for entry in caught.value.mismatches if entry.startswith("purpose:")]
    assert len(entries) == 1 and f"{purpose!r} is not a purpose the posture permits" in entries[0]
    assert caught.value.outbound_requests == 0


def test_a_customer_record_answering_an_experiment_request_names_every_disagreeing_field(adapter: Adapter, tmp_path: Path) -> None:
    request = adapter.experiment_request(project="project-9", source_class="email", purpose=adapter.other_purpose)

    with pytest.raises(adapter.refusal) as caught:
        adapter.open(adapter.customer(), request, adapter.accepted, tmp_path)

    found = caught.value.mismatches
    assert _shared(found, "project", "source-class", "purpose", "stage") == ["source-class", "project", "purpose", "stage"]
    for field in ("source-class", "project", "purpose", "stage"):
        assert any(entry.startswith(f"{field}:") and "record" in entry for entry in found), field
    assert caught.value.outbound_requests == 0


def test_a_record_that_is_not_an_authorization_record_is_refused(adapter: Adapter) -> None:
    found = adapter.mismatches({"record_id": "auth-0001"}, adapter.request(), adapter.accepted)

    assert _fields(found) == ["record-kind"]
    assert "dict is not an authorization record" in found[0]


def test_the_record_covers_the_request_on_project_purpose_and_source_class(adapter: Adapter) -> None:
    covered = adapter.experiment_request()
    assert adapter.mismatches(adapter.experiment(), covered, adapter.accepted) == ()

    by_purpose = adapter.mismatches(adapter.customer(), adapter.request(purpose=adapter.other_purpose), adapter.accepted)
    assert "purpose" in _fields(by_purpose)
    assert any(entry.startswith("purpose:") and "is not permitted by record" in entry for entry in by_purpose)

    another_dataset = adapter.mismatches(adapter.experiment(), adapter.experiment_request(project="project-1"), adapter.accepted)
    assert _fields(another_dataset) == ["project"] and "is not the dataset of experiment scope" in another_dataset[0]

    another_purpose = adapter.mismatches(adapter.experiment(purpose="something-else"), covered, adapter.accepted)
    assert "purpose" in _fields(another_purpose)
    assert any("is not the purpose of experiment scope" in entry for entry in another_purpose)


def test_an_experiment_scope_cannot_authorize_a_customer_stage(adapter: Adapter, tmp_path: Path) -> None:
    for stage in CUSTOMER_STAGES:
        with pytest.raises(adapter.refusal) as caught:
            adapter.open(adapter.experiment(), adapter.experiment_request(stage=stage), adapter.accepted, tmp_path)
        assert _fields(caught.value.mismatches) == ["stage"]
        assert caught.value.mismatches[0] == (
            f"stage: {stage!r} cannot be authorized by an experiment scope, which covers {EXPERIMENT_STAGE!r} only"
        )
        assert caught.value.outbound_requests == 0


def test_a_customer_authorization_cannot_authorize_the_experiment_stage(adapter: Adapter) -> None:
    """A customer record that lists `experiment` among its stages does not make
    it a customer stage: an experiment scope is the only record for that stage."""
    record = adapter.customer(stages=frozenset({EXPERIMENT_STAGE, "shadow"}))

    found = adapter.mismatches(record, adapter.request(stage=EXPERIMENT_STAGE), adapter.accepted)

    assert "stage" in _fields(found)
    assert any(entry.startswith(f"stage: {EXPERIMENT_STAGE!r} is not authorized by record") for entry in found)


def test_the_posture_is_bound_to_its_document_by_digest(adapter: Adapter, tmp_path: Path) -> None:
    posture = adapter.posture
    assert sha256((REPO_ROOT / posture.document).read_bytes()).hexdigest() == posture.digest

    old_digest = adapter.mismatches(adapter.customer(posture_digest="0" * 64), adapter.request(), adapter.accepted)
    assert "posture-digest" in _fields(old_digest) and "posture-identity" not in _fields(old_digest)
    assert any(entry.startswith("posture-digest: record") and "'000000000000'" in entry for entry in old_digest)

    other_record = adapter.mismatches(adapter.customer(posture_identity="some-other-posture"), adapter.request(), adapter.accepted)
    assert "posture-identity" in _fields(other_record)

    with pytest.raises(adapter.refusal) as caught:
        adapter.open(adapter.experiment(), adapter.experiment_request(posture_identity="some-other-posture"), adapter.accepted, tmp_path)
    assert _fields(caught.value.mismatches) == ["posture-identity"]
    assert caught.value.mismatches[0].startswith("posture-identity: request names 'some-other-posture'")
    assert caught.value.outbound_requests == 0


def test_posture_status_gates_customer_records_for_textract_and_every_request_for_the_model_provider(adapter: Adapter) -> None:
    """The declared divergence (`provider_authorization.PostureStatusRule`).

    Textract refuses a customer record while its posture is not `accepted` and
    lets an experiment scope through; the model provider refuses every request
    while its posture is not `approved`. Neither rule is silently the other's."""
    customer = adapter.mismatches(adapter.customer(), adapter.request(), adapter.unaccepted)
    assert "posture-status" in _fields(customer)
    assert any(entry.startswith(f"posture-status: the posture is {adapter.unaccepted.status!r}; ") for entry in customer)

    experiment = adapter.mismatches(adapter.experiment(), adapter.experiment_request(), adapter.unaccepted)
    assert ("posture-status" in _fields(experiment)) is adapter.status_gates_every_request

    assert "posture-status" not in _fields(adapter.mismatches(adapter.experiment(), adapter.experiment_request(), adapter.accepted))


def test_outbound_requests_are_calls_plus_retries_plus_failed_attempts() -> None:
    counts = OutboundCounts()
    assert counts.outbound_requests == 0
    counts.calls += 2
    counts.retries += 3
    counts.failed_attempts += 1
    assert counts.outbound_requests == 6
    assert counts.counted() == {"calls": 2, "retries": 3, "failed_attempts": 1, "outbound_requests": 6}
