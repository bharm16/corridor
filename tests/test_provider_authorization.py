"""One authorization check stands in front of both providers (ADR-0094).

The Textract adapter and the native model-provider adapter used to carry the
same check twice, and `tests/test_textract_adapter.py` and
`tests/test_native_provider_boundary.py` proved the same five properties twice.
Those properties are proved here once, through a table of the two adapters:
each row says how to build that adapter's customer authorization, experiment
scope and covered request, which posture it binds to, and how to open it
against a transport that fails the test on any outbound request. The adapter
files keep only the fields that are theirs.

The posture's approval is one rule over both adapters (ADR-0098, #808): a
customer authorization needs the posture's customer-processing approval, an
experiment scope needs its experimental approval, a proposed posture with
neither refuses every new live transmission with zero outbound requests, and
offline replay of a retained response never enters the check at all. The
tests here prove each of those through the same table. The test that pinned
the earlier per-adapter divergence (Textract gating customer records only,
the model provider every request) is retired with the divergence.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

import pytest

import test_native_provider_boundary as native
import test_textract_adapter as textract
from corridor import native_pipeline, provider_authorization
from corridor import native_provider_boundary as native_boundary
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
    TransmissionApproval,
)
from corridor_pdf_reader.textract_adapter import boundary as textract_boundary
from corridor_pdf_reader.textract_adapter import replay as textract_replay
from corridor_pdf_reader.textract_adapter.boundary import TextractProcessingFailure, open_boundary
from corridor_pdf_reader.textract_adapter.records import PROVIDER_POSTURE
from corridor_pdf_reader.textract_adapter.records import mismatches as textract_mismatches

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Adapter:
    """One provider adapter, as the shared check sees it.

    `accepted` is the posture with both approvals recorded and every customer
    field verified, so each test names exactly the field it is about;
    `unaccepted` is the same posture proposed, with neither approval.
    `replay` reads one retained response through the adapter's offline replay
    path, which must never reach the check or a transport.
    """

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
    replay: Callable[[Path], None]


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


def _replay_textract(tmp_path: Path) -> None:
    """The four retained fixtures through the normalizer, as CI replays them."""
    entries = textract_replay.replay_fixtures(passes=1)
    assert len(entries) == 4 and all(entry["readings"][0]["tables"] >= 1 for entry in entries)


def _replay_native(tmp_path: Path) -> None:
    """One retained answer through the sealed recorded client, as offline qualification replays it."""
    image = native._image(tmp_path)
    retained = {
        "system_sha256": sha256(b"system").hexdigest(),
        "schema_sha256": native_pipeline.content_digest(native.SCHEMA),
        "user": "page 1",
        "image_sha256s": [sha256(image.read_bytes()).hexdigest()],
        "answer": {"rows": ["r1"]},
    }
    client = native_pipeline.RecordedPipelineClient([retained])
    assert client.complete(system="system", user="page 1", schema=native.SCHEMA, images=[image]) == {"rows": ["r1"]}
    client.require_complete()


NATIVE_CUSTOMER_APPROVAL = TransmissionApproval(
    source_classes=POSTURE.permitted_source_classes, purposes=POSTURE.permitted_purposes,
    unverified=(), approved_by="a named maintainer, in this test only", approved_on="2026-09-10",
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
        replay=_replay_textract,
    ),
    "native-model-provider": Adapter(
        posture=POSTURE,
        accepted=replace(POSTURE, customer_processing="open", pdf_licensing="resolved",
                         customer_processing_approval=NATIVE_CUSTOMER_APPROVAL),
        unaccepted=replace(POSTURE, status="proposed", experimental_approval=None),
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
        replay=_replay_native,
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


def _approval_entries(mismatches: tuple[str, ...]) -> list[str]:
    return [entry for entry in mismatches if entry.startswith("posture-approval:")]


def test_a_customer_record_needs_the_customer_processing_approval_and_the_signed_authorization(adapter: Adapter, tmp_path: Path) -> None:
    """Rule 1 (ADR-0098): with both, no approval sentence; without the approval,
    the refusal names it, quotes the document's status, and sends nothing."""
    assert _approval_entries(adapter.mismatches(adapter.customer(), adapter.request(), adapter.accepted)) == []

    without = replace(adapter.accepted, customer_processing_approval=None)
    with pytest.raises(adapter.refusal) as caught:
        adapter.open(adapter.customer(), adapter.request(), without, tmp_path)
    assert _approval_entries(caught.value.mismatches) == [
        f"posture-approval: the posture is {without.status!r} and records no customer-processing approval; "
        "no customer page may be transmitted until the maintainer records one"
    ]
    assert caught.value.reason == "authorization-refused" and caught.value.outbound_requests == 0

    with pytest.raises(adapter.refusal) as absent:
        adapter.open(None, adapter.request(), adapter.accepted, tmp_path)
    assert _fields(absent.value.mismatches) == ["authorization-absent"], "an approval opens nothing without the signed record"


def test_an_experiment_scope_needs_the_experimental_approval_and_the_recorded_scope(adapter: Adapter, tmp_path: Path) -> None:
    """Rule 2 (ADR-0098): the experimental approval, not the customer one, and
    only for what the approval names."""
    assert adapter.mismatches(adapter.experiment(), adapter.experiment_request(), adapter.accepted) == ()
    experiment_only = replace(adapter.accepted, customer_processing_approval=None)
    assert adapter.mismatches(adapter.experiment(), adapter.experiment_request(), experiment_only) == ()

    without = replace(adapter.accepted, experimental_approval=None)
    with pytest.raises(adapter.refusal) as caught:
        adapter.open(adapter.experiment(), adapter.experiment_request(), without, tmp_path)
    assert _approval_entries(caught.value.mismatches) == [
        f"posture-approval: the posture is {without.status!r} and records no experimental approval; "
        "no experiment page may be transmitted until the maintainer records one"
    ]
    assert caught.value.outbound_requests == 0

    approval = adapter.accepted.experimental_approval
    narrow = replace(adapter.accepted, experimental_approval=replace(approval, source_classes=("some-other-class",), purposes=("some-other-purpose",)))
    request = adapter.experiment_request()
    found = _approval_entries(adapter.mismatches(adapter.experiment(), request, narrow))
    assert found == [
        f"posture-approval: the experimental approval permits source classes (some-other-class), not {request.source_class!r}",
        f"posture-approval: the experimental approval permits purposes (some-other-purpose), not {request.purpose!r}",
    ]

    with pytest.raises(adapter.refusal) as absent:
        adapter.open(None, adapter.experiment_request(), adapter.accepted, tmp_path)
    assert _fields(absent.value.mismatches) == ["authorization-absent"], "an approval opens nothing without the recorded scope"


def test_an_experimental_approval_never_authorizes_a_customer_stage(adapter: Adapter, tmp_path: Path) -> None:
    """An experimental approval is read for an experiment scope only. A customer
    record under a posture that records only that approval is refused for the
    missing customer-processing approval, and an experiment scope asked for a
    customer stage is refused on the stage whatever the posture approves."""
    experiment_only = replace(adapter.accepted, customer_processing_approval=None)

    with pytest.raises(adapter.refusal) as caught:
        adapter.open(adapter.customer(), adapter.request(), experiment_only, tmp_path)
    assert any("records no customer-processing approval" in entry for entry in caught.value.mismatches)
    assert caught.value.outbound_requests == 0

    for stage in CUSTOMER_STAGES:
        with pytest.raises(adapter.refusal) as staged:
            adapter.open(adapter.experiment(), adapter.experiment_request(stage=stage), experiment_only, tmp_path)
        assert _fields(staged.value.mismatches) == ["stage"]
        assert staged.value.outbound_requests == 0


def test_replay_of_a_retained_response_is_not_gated_and_makes_zero_outbound_requests(adapter: Adapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 3 (ADR-0098): offline replay is not a transmission. It never enters
    the check and never constructs a client or a transport, so no approval is
    consulted and nothing can leave the process."""
    def never(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("a replay path reached the authorization check or a live transport")

    monkeypatch.setattr(provider_authorization.AuthorizationCheck, "mismatches", never)
    monkeypatch.setattr(provider_authorization.AuthorizationCheck, "authorized", never)
    monkeypatch.setattr(textract_boundary, "live_service", never)
    monkeypatch.setattr(textract_boundary, "open_boundary", never)
    monkeypatch.setattr(native_boundary, "live_transport", never)
    monkeypatch.setattr(native_boundary, "open_native_provider_boundary", never)

    adapter.replay(tmp_path)


def test_a_proposed_posture_refuses_a_live_experiment_with_zero_outbound_requests(adapter: Adapter, tmp_path: Path) -> None:
    """Rule 4 (ADR-0098): the recorded Textract posture is proposed with no
    approval, and the model provider's would be if its approval were withdrawn;
    a live experiment under either refuses, naming the missing approval, with
    the transport untouched and nothing on disk."""
    proposed = adapter.unaccepted
    assert proposed.status == "proposed" and proposed.experimental_approval is None and proposed.customer_processing_approval is None

    with pytest.raises(adapter.refusal) as caught:
        adapter.open(adapter.experiment(), adapter.experiment_request(), proposed, tmp_path)

    assert _fields(caught.value.mismatches) == ["posture-approval"]
    assert caught.value.mismatches[0] == (
        "posture-approval: the posture is 'proposed' and records no experimental approval; "
        "no experiment page may be transmitted until the maintainer records one"
    )
    assert caught.value.reason == "authorization-refused" and caught.value.outbound_requests == 0
    assert not (tmp_path / "cache").exists()

    with pytest.raises(adapter.refusal) as customer:
        adapter.open(adapter.customer(), adapter.request(), proposed, tmp_path)
    assert "posture-approval" in _fields(customer.value.mismatches) and customer.value.outbound_requests == 0


def test_outbound_requests_are_calls_plus_retries_plus_failed_attempts() -> None:
    counts = OutboundCounts()
    assert counts.outbound_requests == 0
    counts.calls += 2
    counts.retries += 3
    counts.failed_attempts += 1
    assert counts.outbound_requests == 6
    assert counts.counted() == {"calls": 2, "retries": 3, "failed_attempts": 1, "outbound_requests": 6}
