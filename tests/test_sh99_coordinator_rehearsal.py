"""Public acceptance-bundle coverage for the bounded SH 99 coordinator rehearsal."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import corridor.sh99_coordinator_rehearsal as rehearsal
from corridor.sh99_coordinator_rehearsal import (
    BUNDLE_SCHEMA_VERSION,
    BUNDLE_FILES,
    LEGACY_BUNDLE_SCHEMA_VERSION,
    CoordinatorRehearsalCapture,
    CorruptSH99CoordinatorRehearsalBundle,
    SH99CoordinatorRehearsalConfig,
    _JourneyProgress,
    _candidate_source_attribution_counts,
    _fetch_evidence_page_image,
    _candidate_7587_work_facts,
    _equistar_form,
    _find_statement_screen,
    _kinder_morgan_form,
    _require_direct_migration_successor,
    _require_report_pdf_contents,
    _scenario_input_receipt,
    _run_ordinary_interface_journey,
    _supporting_evidence_selection,
    publish_coordinator_rehearsal_bundle,
    verify_coordinator_rehearsal_bundle,
)

from pdf_fixture_support import PdfFixture


EQUISTAR_QUOTE = (
    "Equistar to provide a chain of title on the ROW agreement that is in "
    "DOW’s name (Due date of 01/2025)."
)


def _write_frozen_v2_bundle(bundle_dir: Path) -> str:
    """Write the old four-file shape without using the current publisher."""

    canonical = {
        "claim_boundary": {
            "customer_usability_validation": False,
            "internal_workflow_rehearsal": True,
            "provisional_targets": True,
        },
        "inputs": {"source_revision": "a" * 40},
        "operations": {"elapsed_seconds": 1.0},
        "coordinator": {
            "elapsed_seconds": 2.0,
            "interactions": [],
            "retries": [],
            "scenario_timings": {},
        },
        "outcome": {
            "assistance": [],
            "deviations": [],
            "errors": ["immutable #256 failure"],
            "released_pdf": None,
            "status": "failed",
            "unqualified_pass": False,
        },
        "verification": {"valid": False},
    }
    exports = {
        "receipt.json": {
            "schema_version": "corridor.sh99-coordinator-rehearsal-bundle.v2",
            **canonical,
        },
        "canonical-content.json": canonical,
        "environment.json": {
            "schema_version": "corridor.sh99-coordinator-rehearsal-bundle.v2",
            "rehearsed_at": "2026-08-13T00:00:00+00:00",
            "source_revision": "a" * 40,
            "source_migration_head": "e255a7c4d9e2",
            "details": {},
        },
        "released-report.pdf": b"",
    }
    bundle_dir.mkdir()
    files = {}
    for name in BUNDLE_FILES:
        value = exports[name]
        payload = (
            value
            if isinstance(value, bytes)
            else json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode()
            + b"\n"
        )
        (bundle_dir / name).write_bytes(payload)
        files[name] = {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    canonical_sha = hashlib.sha256(
        json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()
    manifest = {
        "schema_version": "corridor.sh99-coordinator-rehearsal-bundle.v2",
        "created_at": "2026-08-13T00:00:00+00:00",
        "canonical_content_sha256": canonical_sha,
        "files": files,
    }
    manifest_bytes = (
        json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        + b"\n"
    )
    (bundle_dir / "manifest.json").write_bytes(manifest_bytes)
    return hashlib.sha256(manifest_bytes).hexdigest()


def _regenerate_bundle_manifest(bundle_dir: Path) -> str:
    """Regenerate every public integrity identity after an adversarial edit."""

    canonical = json.loads((bundle_dir / "canonical-content.json").read_text())
    receipt = {"schema_version": BUNDLE_SCHEMA_VERSION, **canonical}
    canonical_bytes = (
        json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        + b"\n"
    )
    receipt_bytes = (
        json.dumps(
            receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        + b"\n"
    )
    (bundle_dir / "canonical-content.json").write_bytes(canonical_bytes)
    (bundle_dir / "receipt.json").write_bytes(receipt_bytes)
    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    manifest["canonical_content_sha256"] = hashlib.sha256(
        canonical_bytes.removesuffix(b"\n")
    ).hexdigest()
    manifest["files"] = {
        name: {
            "bytes": (bundle_dir / name).stat().st_size,
            "sha256": hashlib.sha256((bundle_dir / name).read_bytes()).hexdigest(),
        }
        for name in BUNDLE_FILES
    }
    manifest_bytes = (
        json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        + b"\n"
    )
    (bundle_dir / "manifest.json").write_bytes(manifest_bytes)
    return hashlib.sha256(manifest_bytes).hexdigest()


def _v3_inputs() -> dict:
    return {
        "source_revision": "a" * 40,
        "source_snapshot_sha256": "b" * 64,
        "source_dump_sha256": "d" * 64,
        "source_migration_head": "e255a7c4d9e2",
        "target_migration_head": "f255b7c4d9e3",
        "environment_details": {"python_version": "3.12.0"},
        "shared_admission_receipt": {"sha256": "c" * 64},
        "approved_shared_state_receipt": "https://example.test/approval",
        "corpus_inputs": [],
        "active_runs": [],
        "policy_identities": [],
        "report_publication": {
            "ruleset_version": "v0.4",
            "provenance_mode": "all-supported-sources",
            "evaluation_thresholds": {
                "stale_days": 14,
                "due_soon_days": 30,
                "action_due_soon_days": 7,
            },
        },
        "seeded_coordinator": {
            "subject": "local:sh99-coordinator",
            "display_name": "SH 99 Coordinator",
        },
        "scenario_candidates": {"7296": 7296, "7129": 7129, "7587": 7587},
        "scenario_source_receipts": {
            candidate_id: {
                "candidate_id": int(candidate_id),
                "document_id": int(candidate_id) + 100,
                "document_name": f"source-{candidate_id}.pdf",
                "document_date": "2025-01-01",
                "document_sha256": "e" * 64,
                "page": 1,
                "quote": f"source quote {candidate_id}",
                "text_source": "text_layer",
                "page_text_sha256": "f" * 64,
                "page_image": {
                    "url": f"/page-image/{int(candidate_id) + 100}/1",
                    "bytes": 9,
                    "sha256": "1" * 64,
                },
            }
            for candidate_id in ("7296", "7129", "7587")
        },
    }


def _v3_operations(**overrides) -> dict:
    values = {
        "elapsed_seconds": 1.0,
        "backfill_elapsed_seconds": 291.0,
        "source_database_mutated": False,
        "source_migration_head_before": "e255a7c4d9e2",
        "source_migration_head_after": "e255a7c4d9e2",
        "source_state_sha256_before": "b" * 64,
        "source_state_sha256_after": "b" * 64,
        "source_scenario_receipts_sha256_before": "2" * 64,
        "source_scenario_receipts_sha256_after": "2" * 64,
        "clone_upgrade": {
            "database_name": "corridor_sh99_coordinator_rehearsal_test",
            "from_revision": "e255a7c4d9e2",
            "to_revision": "f255b7c4d9e3",
            "verified_revision": "f255b7c4d9e3",
        },
    }
    values.update(overrides)
    return values


def _release_facts(pdf_bytes: bytes) -> dict:
    return {
        "release_id": 81,
        "artifact_name": "sh99.pdf",
        "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "evaluated_on": "2026-08-13",
        "ruleset_version": "v0.4",
        "provenance_mode": "all-supported-sources",
        "released_by": "local:sh99-coordinator",
        "released_by_display": "SH 99 Coordinator",
        "released_at": "2026-08-13T00:00:00+00:00",
        "record_context": {"party_statements": []},
        "evaluation_context": {
            "evaluated_on": "2026-08-13",
            "ruleset_version": "v0.4",
            "thresholds": {
                "stale_days": 14,
                "due_soon_days": 30,
                "action_due_soon_days": 7,
            },
        },
    }


def _complete_verification(pdf_bytes: bytes) -> dict:
    """Return the independently checked facts needed to earn a v3 pass."""

    image_receipt = {
        "url": "/page-image/1435/2",
        "document_name": "source.pdf",
        "registered_page": 2,
        "sha256": "3" * 64,
        "bytes": 128,
    }
    return {
        "valid": True,
        "candidate_7296": {
            "state": "accepted",
            "event_type": "committed_date_change",
            "direction": "later",
            "scope": "unknown",
            "timings": {
                "previous": {
                    "text": "March 2026",
                    "precision": "month",
                    "start_date": "2026-03-01",
                    "end_date": "2026-03-31",
                },
                "new": {
                    "text": "May 16th",
                    "precision": "day",
                    "start_date": "2026-05-16",
                    "end_date": "2026-05-16",
                },
            },
            "milestone_impact": "not_yet_known",
        },
        "candidate_7129": {
            "state": "accepted",
            "event_type": "commitment",
            "scope": "unknown",
            "timing": {
                "text": "01/2025",
                "precision": "month",
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
            },
            "past_due": {
                "evaluated_on": "2025-02-01",
                "due_after": "2025-01-31",
                "not_past_due_on": "2025-01-31",
            },
        },
        "candidate_7587": {
            "state": "pending",
            "admission_outcome": "abstained",
            "statement_event_id": None,
            "coordination_receipt_count": 0,
            "forbidden_ledger_work_count": 0,
            "forbidden_past_due_count": 0,
            "report_context_count": 0,
            "released_pdf_count": 0,
        },
        "page_image_fetches": {
            "7296": image_receipt,
            "7129": {**image_receipt, "url": "/page-image/1453/2"},
        },
        "automated_scope_coverage": {
            "returncode": 0,
            "covered_scope_modes": ["selected", "all_active"],
        },
        "release": _release_facts(pdf_bytes),
    }


def _passing_capture(pdf_bytes: bytes) -> CoordinatorRehearsalCapture:
    release = _release_facts(pdf_bytes)
    return CoordinatorRehearsalCapture(
        inputs=_v3_inputs(),
        operations=_v3_operations(),
        coordinator={
            "elapsed_seconds": 30.0,
            "scenario_timings": {"7296": 12.0, "7129_and_release": 18.0},
            "interactions": [
                "save_kinder_morgan_statement",
                "save_equistar_statement",
                "open_external_report_release",
                "render_fixed_pdf_for_review",
                "review_fixed_pdf_preview",
                "download_fixed_pdf_for_review",
                "release_fixed_pdf",
                "retrieve_released_pdf",
                "reload_release_history",
            ],
            "retries": [],
        },
        outcome={
            "status": "passed",
            "assistance": [],
            "errors": [],
            "deviations": [],
            "released_pdf": {
                field: release[field]
                for field in ("release_id", "artifact_name", "sha256")
            },
        },
        verification=_complete_verification(pdf_bytes),
        released_pdf_bytes=pdf_bytes,
    )


class _ScreenClient:
    def __init__(self):
        self.seen = []

    def get(self, url):
        self.seen.append(url)
        return SimpleNamespace(status_code=200, text="<h1>Coordinate this statement</h1>")


def _work_card(*, href: str, party: str, quote: str, source: str) -> str:
    return f"""
      <section class="item">
        <h3>Extracted statement</h3>
        <p>Extracted context / affected party: {party}</p>
        <blockquote>{quote}</blockquote>
        <p class="source">{source}</p>
        <a class="action" href="{href}">Review extracted statement</a>
      </section>
    """


def test_visible_work_card_selection_uses_exact_source_context_without_candidate_identity():
    home = "".join(
        (
            "<main>",
            _work_card(
                href="/statements/sh99-grand-parkway/7396/coordinate",
                party="Equistar",
                quote=EQUISTAR_QUOTE,
                source="equistar-2025-02-12.pdf · 2025-02-12 · page 1",
            ),
            _work_card(
                href="/statements/sh99-grand-parkway/7129/coordinate",
                party="Equistar",
                quote=EQUISTAR_QUOTE,
                source="equistar-2024-12-04.pdf · 2024-12-04 · page 1",
            ),
            _work_card(
                href="/statements/sh99-grand-parkway/7296/coordinate",
                party="Kinder Morgan",
                quote="The March 2026 completion timeline seems unattainable.",
                source="kinder-morgan-2025-01-16.pdf · 2025-01-16 · page 1",
            ),
            '<section class="backlog" id="more-work">',
            _work_card(
                href="/statements/sh99-grand-parkway/9999/coordinate",
                party="Equistar",
                quote=EQUISTAR_QUOTE,
                source="equistar-2024-12-04.pdf · 2024-12-04 · page 1",
            ),
            "</section></main>",
        )
    )
    client = _ScreenClient()
    progress = _JourneyProgress.start()

    url, _screen = _find_statement_screen(
        client,
        home,
        party="Equistar",
        exact_quote=EQUISTAR_QUOTE,
        source_context=("equistar-2024-12-04.pdf", "2024-12-04"),
        progress=progress,
    )

    assert url == "/statements/sh99-grand-parkway/7129/coordinate"
    assert client.seen == [url]


def test_visible_work_card_ignores_a_second_non_review_action():
    card = _work_card(
        href="/statements/sh99-grand-parkway/browser-carried/coordinate",
        party="Equistar",
        quote=EQUISTAR_QUOTE,
        source="equistar-2024-12-04.pdf · 2024-12-04 · page 1",
    ).replace(
        "</section>",
        '<a class="action" href="/documents/source-page">View source page</a></section>',
    )
    client = _ScreenClient()

    url, _screen = _find_statement_screen(
        client,
        f"<main>{card}</main>",
        party="Equistar",
        exact_quote=EQUISTAR_QUOTE,
        source_context=("equistar-2024-12-04.pdf", "2024-12-04"),
        progress=_JourneyProgress.start(),
    )

    assert url == "/statements/sh99-grand-parkway/browser-carried/coordinate"
    assert client.seen == [url]


def test_visible_work_card_refuses_two_coordinate_review_actions():
    card = _work_card(
        href="/statements/sh99-grand-parkway/first-opaque/coordinate",
        party="Equistar",
        quote=EQUISTAR_QUOTE,
        source="equistar-2024-12-04.pdf · 2024-12-04 · page 1",
    ).replace(
        "</section>",
        (
            '<a class="action" '
            'href="/statements/sh99-grand-parkway/second-opaque/coordinate">'
            "Review extracted statement</a></section>"
        ),
    )
    client = _ScreenClient()

    with pytest.raises(RuntimeError, match="exactly one visible statement review action"):
        _find_statement_screen(
            client,
            f"<main>{card}</main>",
            party="Equistar",
            exact_quote=EQUISTAR_QUOTE,
            source_context=("equistar-2024-12-04.pdf", "2024-12-04"),
            progress=_JourneyProgress.start(),
        )

    assert client.seen == []


@pytest.mark.parametrize("matching_card_count", [0, 2])
def test_visible_work_card_selection_fails_closed_when_compound_match_is_not_unique(
    matching_card_count,
):
    exact = _work_card(
        href="/statements/sh99-grand-parkway/browser-carried/coordinate",
        party="Equistar",
        quote=EQUISTAR_QUOTE,
        source="equistar-2024-12-04.pdf · 2024-12-04 · page 1",
    )
    home = "<main>" + (exact * matching_card_count) + "</main>"
    client = _ScreenClient()

    with pytest.raises(
        RuntimeError,
        match="exactly one immediate statement card matching the visible party",
    ):
        _find_statement_screen(
            client,
            home,
            party="Equistar",
            exact_quote=EQUISTAR_QUOTE,
            source_context=("equistar-2024-12-04.pdf", "2024-12-04"),
            progress=_JourneyProgress.start(),
        )

    assert client.seen == []


def _guided_screen() -> str:
    return """
      <form method="post">
        <input type="hidden" name="expected_candidate_state" value="pending">
        <select name="affected_external_org_id">
          <option value="affected-party-option">Kinder Morgan Tejas Pipeline</option>
        </select>
        <select name="stated_external_org_id">
          <option value="stated-party-option">Kinder Morgan Tejas Pipeline</option>
        </select>
        <select name="internal_owner_roster_entry_id">
          <option value="7">SH 99 Coordinator</option>
        </select>
        <section class="evidence">
          <span class="muted">wrong-page.pdf · registered page 2</span>
          <span class="quote"><b>Candidate Evidence</b> — unrelated wording</span>
          <pre class="page-context">No supporting party wording here.</pre>
        </section>
        <section class="evidence">
          <span class="muted">Meeting Notes/Kinder Morgan/2025.01.16 GPB1 Kinder Morgan notes final.pdf · registered page 2</span>
          <span class="quote"><b>Candidate Evidence</b> — The March 2026 completion timeline seems unattainable. Propose extending to May 16th.</span>
          <img class="page-image" src="/page-image/1453/2">
          <pre class="page-context">Kinder Morgan Management Meeting Highlights</pre>
        </section>
        <input type="radio" name="supporting_page_index" value="0" disabled>
        <input type="radio" name="supporting_page_index" value="1">
      </form>
    """


def test_kinder_morgan_form_resolves_canonical_party_and_visible_enabled_evidence():
    form = _kinder_morgan_form(_guided_screen(), "SH 99 Coordinator")

    assert form["affected_external_org_id"] == "affected-party-option"
    assert form["stated_external_org_id"] == "stated-party-option"
    assert form["supporting_page_index"] == "1"
    assert form["supporting_quote"] == "Kinder Morgan Management Meeting Highlights"
    assert "supporting_document_id" not in form
    assert "supporting_page_no" not in form


def _party_fields(*options: tuple[str, str]):
    rendered_options = "".join(
        f'<option value="{value}">{label}</option>' for value, label in options
    )
    return rehearsal._ScreenFields(
        f'<select name="affected_external_org_id">{rendered_options}</select>'
    )


def test_external_party_option_prefers_one_normalized_exact_visible_match():
    fields = _party_fields(
        ("expanded-option", "Kinder Morgan Tejas Pipeline"),
        ("second-expanded-option", "Kinder Morgan Texas Pipeline"),
        ("exact-option", "  KINDER   MORGAN "),
    )

    assert (
        fields.external_party_option_value(
            "affected_external_org_id", "Kinder Morgan"
        )
        == "exact-option"
    )


def test_external_party_option_allows_one_whole_prefix_expansion():
    fields = _party_fields(
        ("canonical-option", "Kinder Morgan Tejas Pipeline"),
        ("unrelated-option", "Equistar"),
    )

    assert (
        fields.external_party_option_value(
            "affected_external_org_id", "Kinder Morgan"
        )
        == "canonical-option"
    )


def test_external_party_option_refuses_ambiguous_whole_prefix_expansions():
    fields = _party_fields(
        ("tejas-option", "Kinder Morgan Tejas Pipeline"),
        ("texas-option", "Kinder Morgan Texas Pipeline"),
    )

    with pytest.raises(ValueError, match="one unambiguous visible External Party"):
        fields.external_party_option_value(
            "affected_external_org_id", "Kinder Morgan"
        )


def test_external_party_option_refuses_a_nonmatch():
    fields = _party_fields(
        ("near-option", "Kinder Morganson Pipeline"),
        ("unrelated-option", "Equistar"),
    )

    with pytest.raises(ValueError, match="one unambiguous visible External Party"):
        fields.external_party_option_value(
            "affected_external_org_id", "Kinder Morgan"
        )


def test_generic_option_selection_remains_visible_exact_only():
    fields = rehearsal._ScreenFields(
        '<select name="internal_owner_roster_entry_id">'
        '<option value="coordinator-option">SH 99 Coordinator</option>'
        "</select>"
    )

    with pytest.raises(ValueError, match="does not offer"):
        fields.option_value("internal_owner_roster_entry_id", "SH 99")


def test_visible_supporting_evidence_fetches_the_ordinary_png_url():
    png_bytes = b"\x89PNG\r\n\x1a\nfixed-page-bytes"
    seen = []

    class Client:
        def get(self, url):
            seen.append(url)
            return SimpleNamespace(
                status_code=200,
                headers={"content-type": "image/png"},
                content=png_bytes,
            )

    evidence = _supporting_evidence_selection(
        _guided_screen(), exact_quote="Kinder Morgan Management Meeting Highlights"
    )
    receipt = _fetch_evidence_page_image(
        Client(), evidence, progress=_JourneyProgress.start()
    )

    assert seen == ["/page-image/1453/2"]
    assert receipt == {
        "bytes": len(png_bytes),
        "document_name": "Meeting Notes/Kinder Morgan/2025.01.16 GPB1 Kinder Morgan notes final.pdf",
        "registered_page": 2,
        "sha256": hashlib.sha256(png_bytes).hexdigest(),
        "url": "/page-image/1453/2",
    }


def _scenario_receipt_subject(*, image_path: str):
    quote = "Equistar to provide a chain of title in DOW’s name."
    page_text = f"Action Items:\n1. {quote}\n"
    document = SimpleNamespace(
        id=1435,
        filename="Meeting Notes/Equistar/2024.12.04 GPB1 Equistar notes final.pdf",
        doc_date="2024-12-04",
        sha256="4" * 64,
    )
    page = SimpleNamespace(
        document_id=1435,
        page_no=2,
        image_path=image_path,
        text=page_text,
        text_source="text_layer",
    )
    candidate = SimpleNamespace(
        id=7129,
        source_document_id=1435,
        payload_json={
            "citations": [
                {
                    "document_id": 1435,
                    "page": 2,
                    "quote": quote,
                    "verified": True,
                }
            ]
        },
    )
    return candidate, document, page, quote, page_text


def test_scenario_input_receipt_seals_exact_registered_page_and_image(
    monkeypatch, tmp_path
):
    checkout_root = tmp_path / "isolated-checkout"
    checkout_root.mkdir()
    monkeypatch.chdir(checkout_root)
    asset_root = tmp_path / "runtime-root"
    image_path = asset_root / "out/page-images/doc/0002.png"
    image_path.parent.mkdir(parents=True)
    image_bytes = b"\x89PNG\r\n\x1a\nsource-page"
    image_path.write_bytes(image_bytes)
    candidate, document, page, quote, page_text = _scenario_receipt_subject(
        image_path="out/page-images/doc/0002.png"
    )

    receipt = _scenario_input_receipt(
        candidate,
        document,
        page,
        asset_root=asset_root,
        expected_quote=quote,
    )

    assert receipt == {
        "candidate_id": 7129,
        "document_id": 1435,
        "document_name": document.filename,
        "document_date": "2024-12-04",
        "document_sha256": "4" * 64,
        "page": 2,
        "quote": quote,
        "text_source": "text_layer",
        "page_text_sha256": hashlib.sha256(page_text.encode()).hexdigest(),
        "page_image": {
            "url": "/page-image/1435/2",
            "bytes": len(image_bytes),
            "sha256": hashlib.sha256(image_bytes).hexdigest(),
        },
    }


def test_scenario_input_receipt_refuses_an_image_outside_the_runtime_asset_root(
    tmp_path,
):
    asset_root = tmp_path / "runtime-root"
    asset_root.mkdir()
    outside_image = tmp_path / "outside.png"
    outside_image.write_bytes(b"\x89PNG\r\n\x1a\noutside")
    candidate, document, page, quote, _page_text = _scenario_receipt_subject(
        image_path="../outside.png"
    )

    with pytest.raises(ValueError, match="outside the runtime asset root"):
        _scenario_input_receipt(
            candidate,
            document,
            page,
            asset_root=asset_root,
            expected_quote=quote,
        )


def _equistar_screen() -> str:
    return f"""
      <form method="post">
        <input type="hidden" name="expected_candidate_state" value="pending">
        <select name="affected_external_org_id"><option value="42">Equistar</option></select>
        <select name="stated_external_org_id"><option value="42">Equistar</option></select>
        <select name="internal_owner_roster_entry_id"><option value="7">SH 99 Coordinator</option></select>
        <section class="evidence">
          <span class="muted">Meeting Notes/Equistar/2024.12.04 GPB1 Equistar notes final.pdf · registered page 2</span>
          <span class="quote"><b>Candidate Evidence</b> — {EQUISTAR_QUOTE}</span>
          <img class="page-image" src="/page-image/1435/2">
          <pre class="page-context">{EQUISTAR_QUOTE}</pre>
        </section>
        <input type="radio" name="supporting_page_index" value="0">
      </form>
    """


def test_equistar_form_uses_the_visible_supporting_evidence_ordinal():
    form = _equistar_form(_equistar_screen(), "SH 99 Coordinator")

    assert form["supporting_page_index"] == "0"
    assert form["supporting_quote"] == EQUISTAR_QUOTE
    assert "supporting_document_id" not in form
    assert "supporting_page_no" not in form


@pytest.mark.parametrize("history_parse_failure", [False, True])
def test_ordinary_journey_retains_reviewed_bytes_immediately_after_release(
    monkeypatch, tmp_path, history_parse_failure
):
    pdf_bytes = b"%PDF-1.4\nfixed ordinary report\n"
    pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    png_7296 = b"\x89PNG\r\n\x1a\nkm-page"
    png_7129 = b"\x89PNG\r\n\x1a\nequistar-page"
    project_slug = "sh99-grand-parkway"
    km_home = (
        f'<a href="/reports/{project_slug}">Prepare External Report</a>'
        + _work_card(
            href=f"/statements/{project_slug}/7296/coordinate",
            party="Kinder Morgan",
            quote=(
                "The March 2026 completion timeline seems unattainable. "
                "Propose extending to May 16th."
            ),
            source="Meeting Notes/Kinder Morgan/2025.01.16 GPB1 Kinder Morgan notes final.pdf · 2025-01-16 · page 2",
        )
    )
    equistar_home = (
        f'<a href="/reports/{project_slug}">Prepare External Report</a>'
        + _work_card(
            href=f"/statements/{project_slug}/7396/coordinate",
            party="Equistar",
            quote=EQUISTAR_QUOTE,
            source="equistar-2025-02-12.pdf · 2025-02-12 · page 1",
        )
        + _work_card(
            href=f"/statements/{project_slug}/7129/coordinate",
            party="Equistar",
            quote=EQUISTAR_QUOTE,
            source="Meeting Notes/Equistar/2024.12.04 GPB1 Equistar notes final.pdf · 2024-12-04 · page 2",
        )
    )
    workspace = f"""
      <form method="post" action="/reports/{project_slug}/render">
        <input type="hidden" name="ordinary" value="1">
        <button type="submit">Render fixed PDF for review</button>
      </form>
    """
    review = f"""
      <section class="artifact">
        <h2>sh99-external-report.pdf</h2>
        <dl><dt>SHA-256 digest</dt><dd><code>{pdf_sha256}</code></dd></dl>
        <object data="/reports/{project_slug}/prepared/81/preview" type="application/pdf"></object>
        <a class="download" href="/reports/{project_slug}/prepared/81/download">Download this exact PDF</a>
        <form method="post" action="/reports/{project_slug}/prepared/81/release">
          <button type="submit">Release this exact PDF</button>
        </form>
      </section>
    """
    history = f"""
      <section class="release">
        <h3>sh99-external-report.pdf</h3>
        <dl>
          <dt>Released by</dt><dd>SH 99 Coordinator</dd>
          <dt>SHA-256 digest</dt><dd><code>{pdf_sha256}</code></dd>
        </dl>
        <a href="/reports/{project_slug}/releases/91/download">Download released PDF</a>
      </section>
    """

    class ScriptedClient:
        def __init__(self):
            self.work_reads = 0
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url):
            self.calls.append(("GET", url, None))
            if url == f"/work/{project_slug}":
                self.work_reads += 1
                return SimpleNamespace(
                    status_code=200,
                    text=km_home if self.work_reads == 1 else equistar_home,
                )
            if url.endswith("/7296/coordinate"):
                return SimpleNamespace(status_code=200, text=_guided_screen())
            if url.endswith("/7129/coordinate"):
                return SimpleNamespace(status_code=200, text=_equistar_screen())
            if url == "/page-image/1453/2":
                return SimpleNamespace(
                    status_code=200,
                    headers={"content-type": "image/png"},
                    content=png_7296,
                )
            if url == "/page-image/1435/2":
                return SimpleNamespace(
                    status_code=200,
                    headers={"content-type": "image/png"},
                    content=png_7129,
                )
            if url == f"/reports/{project_slug}":
                body = workspace if not any(call[0] == "POST_RELEASE" for call in self.calls) else history
                return SimpleNamespace(status_code=200, text=body)
            if url.endswith(("/preview", "/download")):
                return SimpleNamespace(
                    status_code=200,
                    headers={"content-type": "application/pdf"},
                    content=pdf_bytes,
                )
            raise AssertionError(f"unexpected GET {url}")

        def post(self, url, data=None, follow_redirects=True):
            if url.endswith(("/7296/coordinate", "/7129/coordinate")):
                assert "supporting_document_id" not in data
                assert "supporting_page_no" not in data
                if url.endswith("/7296/coordinate"):
                    assert data["supporting_page_index"] == "1"
                self.calls.append(("POST_STATEMENT", url, dict(data)))
                return SimpleNamespace(status_code=303)
            if url == f"/reports/{project_slug}/render":
                assert data == {"ordinary": "1"}
                self.calls.append(("POST_RENDER", url, dict(data)))
                return SimpleNamespace(status_code=201, text=review)
            if url == f"/reports/{project_slug}/prepared/81/release":
                assert not data
                self.calls.append(("POST_RELEASE", url, data))
                return SimpleNamespace(status_code=201, text=history)
            raise AssertionError(f"legacy or unexpected POST {url}")

    scripted = ScriptedClient()
    monkeypatch.setattr(rehearsal, "TestClient", lambda _app: scripted)
    if history_parse_failure:
        monkeypatch.setattr(
            rehearsal,
            "_matching_release_history",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ValueError("released history could not be parsed")
            ),
        )
    config = SH99CoordinatorRehearsalConfig(
        project_slug=project_slug,
        source_database_url="postgresql+psycopg://corridor:corridor@localhost:5433/source",
        postgres_admin_url="postgresql+psycopg://corridor:corridor@localhost:5433/admin",
        expected_clean_git_revision="a" * 40,
        expected_source_migration_head="e255a7c4d9e2",
        expected_target_migration_head="f255b7c4d9e3",
        shared_admission_receipt_path=tmp_path / "receipt.json",
        expected_shared_admission_receipt_sha256="b" * 64,
        approved_shared_state_receipt="https://example.test/approval",
        shared_backfill_elapsed_seconds=1,
        output_dir=tmp_path / "bundle",
    )

    if history_parse_failure:
        with pytest.raises(rehearsal._JourneyFailure) as exc_info:
            _run_ordinary_interface_journey(SimpleNamespace(), config)

        failure = exc_info.value
        assert failure.released_pdf_bytes == pdf_bytes
        assert failure.release_identity == {
            "artifact_name": "sh99-external-report.pdf",
            "sha256": pdf_sha256,
        }
        assert failure.interactions[-1] == "release_fixed_pdf"
        return

    result = _run_ordinary_interface_journey(SimpleNamespace(), config)

    assert result["assistance"] == []
    assert result["released_pdf"] == {
        "artifact_name": "sh99-external-report.pdf",
        "release_id": 91,
        "sha256": pdf_sha256,
    }
    assert result["released_pdf_bytes"] == pdf_bytes
    assert result["scenario_timings"]["7296"] <= 300
    assert result["scenario_timings"]["7129_and_release"] <= 180
    assert result["interactions"][-2:] == [
        "retrieve_released_pdf",
        "reload_release_history",
    ]
    assert not any(call[1] == f"/reports/{project_slug}/release" for call in scripted.calls)


def test_verify_preserves_the_literal_frozen_v2_bundle_contract(tmp_path):
    bundle_dir = tmp_path / "frozen-v2"
    manifest_sha256 = _write_frozen_v2_bundle(bundle_dir)

    verified = verify_coordinator_rehearsal_bundle(
        bundle_dir,
        expected_integrity_manifest_sha256=manifest_sha256,
    )

    assert LEGACY_BUNDLE_SCHEMA_VERSION == (
        "corridor.sh99-coordinator-rehearsal-bundle.v2"
    )
    assert verified.valid is True
    assert set(path.name for path in bundle_dir.iterdir()) == {
        "manifest.json",
        *BUNDLE_FILES,
    }


def test_v3_publication_refuses_a_caller_declared_pass_without_release_proof(tmp_path):
    with pytest.raises(ValueError, match="declared outcome status"):
        publish_coordinator_rehearsal_bundle(
            tmp_path / "forged-pass",
            CoordinatorRehearsalCapture(
                inputs=_v3_inputs(),
                operations=_v3_operations(),
                coordinator={
                    "elapsed_seconds": 10.0,
                    "scenario_timings": {"7296": 4.0, "7129_and_release": 5.0},
                    "interactions": [],
                    "retries": [],
                },
                outcome={
                    "status": "passed",
                    "assistance": [],
                    "errors": [],
                    "deviations": [],
                    "released_pdf": None,
                },
                verification={"valid": True},
            ),
        )


def test_v3_publication_refuses_boolean_valid_without_semantic_verification(tmp_path):
    pdf_bytes = b"%PDF-1.4\nrelease without semantic proof\n"
    release = _release_facts(pdf_bytes)

    with pytest.raises(ValueError, match="declared outcome status"):
        publish_coordinator_rehearsal_bundle(
            tmp_path / "forged-semantic-pass",
            CoordinatorRehearsalCapture(
                inputs=_v3_inputs(),
                operations=_v3_operations(),
                coordinator={
                    "elapsed_seconds": 10.0,
                    "scenario_timings": {"7296": 4.0, "7129_and_release": 5.0},
                    "interactions": [
                        "save_kinder_morgan_statement",
                        "save_equistar_statement",
                        "render_fixed_pdf_for_review",
                        "release_fixed_pdf",
                        "retrieve_released_pdf",
                        "reload_release_history",
                    ],
                    "retries": [],
                },
                outcome={
                    "status": "passed",
                    "assistance": [],
                    "errors": [],
                    "deviations": [],
                    "released_pdf": {
                        field: release[field]
                        for field in ("release_id", "artifact_name", "sha256")
                    },
                },
                verification={"valid": True, "release": release},
                released_pdf_bytes=pdf_bytes,
            ),
        )


def test_v3_publication_derives_an_unqualified_pass_from_complete_evidence(tmp_path):
    pdf_bytes = b"%PDF-1.4\ncomplete successor release\n"
    summary = publish_coordinator_rehearsal_bundle(
        tmp_path / "passed",
        _passing_capture(pdf_bytes),
    )

    receipt = json.loads((summary.bundle_dir / "receipt.json").read_text())
    assert receipt["outcome"]["status"] == "passed"
    assert receipt["outcome"]["unqualified_pass"] is True


@pytest.mark.parametrize(
    ("case", "mutations"),
    (
        (
            "release ruleset",
            {
                ("verification", "release", "ruleset_version"): "forged-v9",
                (
                    "verification",
                    "release",
                    "evaluation_context",
                    "ruleset_version",
                ): "forged-v9",
            },
        ),
        (
            "release provenance",
            {
                ("verification", "release", "provenance_mode"): "document-only",
            },
        ),
        (
            "release raw actor",
            {
                ("verification", "release", "released_by"): "local:forged",
            },
        ),
        (
            "release display actor",
            {
                (
                    "verification",
                    "release",
                    "released_by_display",
                ): "Forged Coordinator",
            },
        ),
        (
            "evaluation date",
            {
                (
                    "verification",
                    "release",
                    "evaluation_context",
                    "evaluated_on",
                ): "2026-08-12",
            },
        ),
        (
            "evaluation ruleset",
            {
                (
                    "verification",
                    "release",
                    "evaluation_context",
                    "ruleset_version",
                ): "forged-v9",
            },
        ),
        (
            "timezone-naive release",
            {
                (
                    "verification",
                    "release",
                    "released_at",
                ): "2026-08-13T00:00:00",
            },
        ),
        (
            "stale threshold",
            {
                (
                    "verification",
                    "release",
                    "evaluation_context",
                    "thresholds",
                    "stale_days",
                ): 15,
            },
        ),
        (
            "due-soon threshold",
            {
                (
                    "verification",
                    "release",
                    "evaluation_context",
                    "thresholds",
                    "due_soon_days",
                ): 31,
            },
        ),
        (
            "action threshold",
            {
                (
                    "verification",
                    "release",
                    "evaluation_context",
                    "thresholds",
                    "action_due_soon_days",
                ): 8,
            },
        ),
        (
            "unsealed threshold",
            {
                (
                    "verification",
                    "release",
                    "evaluation_context",
                    "thresholds",
                    "unsealed_days",
                ): 99,
            },
        ),
        (
            "rehashed stale threshold policy",
            {
                (
                    "inputs",
                    "report_publication",
                    "evaluation_thresholds",
                    "stale_days",
                ): 15,
                (
                    "verification",
                    "release",
                    "evaluation_context",
                    "thresholds",
                    "stale_days",
                ): 15,
            },
        ),
        (
            "rehashed unsealed threshold policy",
            {
                (
                    "inputs",
                    "report_publication",
                    "evaluation_thresholds",
                    "unsealed_days",
                ): 99,
                (
                    "verification",
                    "release",
                    "evaluation_context",
                    "thresholds",
                    "unsealed_days",
                ): 99,
            },
        ),
    ),
)
def test_v3_verifier_refuses_rehashed_release_metadata_mismatch(
    tmp_path, case, mutations
):
    pdf_bytes = b"%PDF-1.4\nrelease metadata binding\n"
    summary = publish_coordinator_rehearsal_bundle(
        tmp_path / case.replace(" ", "-"),
        _passing_capture(pdf_bytes),
    )
    canonical_path = summary.bundle_dir / "canonical-content.json"
    canonical = json.loads(canonical_path.read_text())
    for path, value in mutations.items():
        target = canonical
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    canonical_path.write_text(
        json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        + "\n"
    )
    regenerated_manifest_sha256 = _regenerate_bundle_manifest(summary.bundle_dir)

    with pytest.raises(
        CorruptSH99CoordinatorRehearsalBundle,
        match="v3 coordinator receipt shape is absent or invalid",
    ):
        verify_coordinator_rehearsal_bundle(
            summary.bundle_dir,
            expected_integrity_manifest_sha256=regenerated_manifest_sha256,
        )


def test_clone_upgrade_refuses_a_pair_that_is_not_a_direct_successor():
    """#548 consolidated the chain, so there is no successor pair to accept.

    The accepting half of this test named the builder and marker #423 left
    behind; both are retired, and the executable graph is now one baseline
    with no edges. The refusal is what still guards a live path, so it stays
    and the acceptance returns when the next migration adds an edge.
    """

    repo_root = Path(__file__).resolve().parents[1]

    with pytest.raises(ValueError, match="direct predecessor"):
        _require_direct_migration_successor(
            repo_root,
            source_revision="a1c4e7b0d2f3",
            target_revision="a1c4e7b0d2f3",
        )


def test_candidate_7587_pending_source_card_is_not_ledger_derived_work():
    work_list = SimpleNamespace(
        immediate=(
            SimpleNamespace(
                kind="candidate",
                candidate_id=7587,
                source_candidate_id=None,
                statement_event_id=None,
                past_due=None,
            ),
        ),
        backlog=(),
        candidate_backlog=(
            SimpleNamespace(
                kind="candidate",
                candidate_id=7587,
                source_candidate_id=None,
                statement_event_id=None,
                past_due=None,
            ),
        ),
    )

    facts = _candidate_7587_work_facts(work_list)

    assert facts == {
        "pending_candidate_card_count": 2,
        "forbidden_ledger_work_count": 0,
        "forbidden_past_due_count": 0,
    }


def test_candidate_7587_attribution_counts_only_its_exact_source_signature():
    invitation = (
        "Air Products will be invited to the TxDOT-Utility Owners-DB Proposers "
        "Workshop on May 8th, 2025. At this event, the DB Contractor PUAA process "
        "will be explained."
    )
    source = {
        "document_id": 1758,
        "document_name": (
            "Meeting Notes/Air Products/2025.04.14 GPB1 Air Products notes final.pdf"
        ),
        "page": 1,
        "quote": invitation,
    }
    candidate_description = (
        "Air Products will be invited to the TxDOT-Utility Owners-DB Proposers "
        "Workshop, where the DB Contractor PUAA process will be explained."
    )
    published_paraphrase = (
        "Air Products expects an invitation to the proposer workshop and a PUAA "
        "process briefing."
    )
    legitimate_air_products = {
        "external_party": "Air Products",
        "supported_statement": "Air Products confirmed a separate crossing review.",
        "source_context": (
            "Meeting Notes/Air Products/2025.04.14 GPB1 Air Products notes final.pdf "
            "· page 3 · “Air Products confirmed a separate crossing review.”"
        ),
    }
    context = {"party_statement_display": [legitimate_air_products]}
    legitimate_pdf = (
        "External Party commitments Air Products [D1758 p.3] "
        "Air Products confirmed a separate crossing review. [D1758 p.3]"
    )

    assert _candidate_source_attribution_counts(
        context,
        legitimate_pdf,
        context_external_org="Air Products",
        candidate_description=candidate_description,
        source_receipt=source,
    ) == {"report_context_count": 0, "released_pdf_count": 0}

    context["party_statement_display"].append(
        {
            "external_party": "Air Products",
            "supported_statement": published_paraphrase,
            "source_context": (
                f"{source['document_name']} · page 1 · “{invitation}”"
            ),
        }
    )
    contaminated_pdf = (
        f"{legitimate_pdf}\nAir Products   [D1758 p.1]\n"
        f"{published_paraphrase}\n[D1758 p.1]"
    )

    assert _candidate_source_attribution_counts(
        context,
        contaminated_pdf,
        context_external_org="Air Products",
        candidate_description=candidate_description,
        source_receipt=source,
    ) == {"report_context_count": 1, "released_pdf_count": 1}


def test_assisted_rehearsal_is_sealed_but_never_called_an_unqualified_pass(tmp_path):
    """A release control needing a technical identifier is an honest failed rehearsal."""

    summary = publish_coordinator_rehearsal_bundle(
        tmp_path / "bundle",
        CoordinatorRehearsalCapture(
            inputs=_v3_inputs(),
            operations=_v3_operations(elapsed_seconds=17.5),
            coordinator={
                "elapsed_seconds": 42.0,
                "scenario_timings": {"7296": 20.0, "7129_and_release": 22.0},
                "interactions": ["coordinator_home", "render_report", "release_report"],
                "retries": [],
            },
            outcome={
                "status": "failed",
                "assistance": ["release required an artifact identity not exposed by a coordinator screen"],
                "errors": [],
                "deviations": [],
                "released_pdf": {
                    "sha256": "f0a9624cf25cbb23e2d237e385e3bebc363ae45d1f99e93416f466ebbd451737",
                    "release_id": 81,
                    "artifact_name": "sh99.pdf",
                },
            },
            verification={
                "candidate_7587": {"admission_outcome": "abstained"},
                "release": {
                    "release_id": 81,
                    "artifact_name": "sh99.pdf",
                    "sha256": "f0a9624cf25cbb23e2d237e385e3bebc363ae45d1f99e93416f466ebbd451737",
                    "evaluated_on": "2026-08-13",
                    "ruleset_version": "v0.4",
                    "provenance_mode": "all-supported-sources",
                    "released_by": "local:sh99-coordinator",
                    "released_by_display": "SH 99 Coordinator",
                    "released_at": "2026-08-13T00:00:00+00:00",
                    "record_context": {"party_statements": []},
                    "evaluation_context": {
                        "evaluated_on": "2026-08-13",
                        "ruleset_version": "v0.4",
                        "thresholds": {
                            "stale_days": 14,
                            "due_soon_days": 30,
                            "action_due_soon_days": 7,
                        },
                    },
                },
            },
            released_pdf_bytes=b"%PDF-1.4\nfixed sh99 report\n",
        ),
    )

    verified = verify_coordinator_rehearsal_bundle(
        summary.bundle_dir,
        expected_integrity_manifest_sha256=summary.integrity_manifest_sha256,
    )
    receipt = json.loads((summary.bundle_dir / "receipt.json").read_text())

    assert verified.valid is True
    assert receipt["schema_version"] == BUNDLE_SCHEMA_VERSION
    assert receipt["outcome"]["status"] == "failed"
    assert receipt["outcome"]["unqualified_pass"] is False
    assert receipt["claim_boundary"] == {
        "internal_workflow_rehearsal": True,
        "customer_usability_validation": False,
        "provisional_targets": True,
    }
    assert receipt["operations"]["elapsed_seconds"] == 17.5
    assert receipt["coordinator"]["elapsed_seconds"] == 42.0
    assert receipt["outcome"]["released_pdf"]["sha256"] == (
        "f0a9624cf25cbb23e2d237e385e3bebc363ae45d1f99e93416f466ebbd451737"
    )
    assert (summary.bundle_dir / "released-report.pdf").read_bytes() == (
        b"%PDF-1.4\nfixed sh99 report\n"
    )


def test_late_semantic_failure_still_seals_the_exact_durable_release(tmp_path):
    pdf_bytes = b"%PDF-1.4\nreviewed and released before verifier failure\n"
    release = _release_facts(pdf_bytes)
    error = "Candidate 7587 exact invitation appeared in released report context"

    summary = publish_coordinator_rehearsal_bundle(
        tmp_path / "late-failure",
        CoordinatorRehearsalCapture(
            inputs=_v3_inputs(),
            operations=_v3_operations(),
            coordinator={
                "elapsed_seconds": 20.0,
                "scenario_timings": {"7296": 8.0, "7129_and_release": 12.0},
                "interactions": ["release_fixed_pdf", "retrieve_released_pdf"],
                "retries": [],
            },
            outcome={
                "status": "failed",
                "assistance": [],
                "errors": [f"post-rehearsal verification: {error}"],
                "deviations": [{"kind": "error", "detail": error}],
                "released_pdf": {
                    field: release[field]
                    for field in ("release_id", "artifact_name", "sha256")
                },
            },
            verification={"valid": False, "error": error, "release": release},
            released_pdf_bytes=pdf_bytes,
        ),
    )

    verified = verify_coordinator_rehearsal_bundle(
        summary.bundle_dir,
        expected_integrity_manifest_sha256=summary.integrity_manifest_sha256,
    )
    receipt = json.loads((summary.bundle_dir / "receipt.json").read_text())

    assert verified.valid is True
    assert receipt["outcome"]["status"] == "failed"
    assert receipt["outcome"]["unqualified_pass"] is False
    assert receipt["verification"]["release"] == release
    assert (summary.bundle_dir / "released-report.pdf").read_bytes() == pdf_bytes


def test_verify_refuses_tampered_fixed_pdf_bytes(tmp_path):
    """A byte change after publication cannot retain a valid release receipt."""

    summary = publish_coordinator_rehearsal_bundle(
        tmp_path / "bundle",
        CoordinatorRehearsalCapture(
            inputs=_v3_inputs(),
            operations=_v3_operations(),
            coordinator={
                "elapsed_seconds": 1.0,
                "scenario_timings": {},
                "interactions": [],
                "retries": [],
            },
            outcome={
                "status": "failed",
                "assistance": [],
                "errors": ["coordinator home did not distinguish a statement"],
                "deviations": [],
                "released_pdf": None,
            },
            verification={"valid": False},
        ),
    )

    (summary.bundle_dir / "released-report.pdf").write_bytes(b"not a PDF")

    try:
        verify_coordinator_rehearsal_bundle(
            summary.bundle_dir,
            expected_integrity_manifest_sha256=summary.integrity_manifest_sha256,
        )
    except CorruptSH99CoordinatorRehearsalBundle as exc:
        assert "released-report.pdf does not match its digest" in str(exc)
    else:
        raise AssertionError("tampered PDF passed bundle verification")


@pytest.mark.parametrize("label_version", ["legacy", "current"])
def test_retained_pdf_check_requires_each_party_report_field(label_version):
    """The byte-pinned PDF must also retain the fields named by its receipt."""

    statement = SimpleNamespace(
        event=SimpleNamespace(
            event_type="committed_date_change",
            timing_direction="later",
            stated_party="Kinder Morgan",
            description="Kinder Morgan moved completion to May 16th.",
        ),
        timings=(
            SimpleNamespace(text="March 2026", precision="month"),
            SimpleNamespace(text="May 16th", precision="day"),
        ),
        plan=SimpleNamespace(
            internal_owner="SH 99 Coordinator",
            next_action="Confirm the revised plan",
            action_due_date=None,
            next_action_decision=object(),
            milestone_impact="not_yet_known",
        ),
    )
    text = "\n".join(
        (
            "External Party commitments",
            "External Party",
            "Supported statement",
            "Timing",
            "Timing precision",
            "Statement type",
            "Commitment Scope",
            "Open / past-due status",
            "Internal Owner",
            "Next Action",
            "Action Due",
            "Milestone Impact",
            "Scope not yet known",
            "Kinder Morgan",
            "Kinder Morgan moved completion to May 16th.",
            "Committed Date Change · later",
            "March 2026",
            "May 16th",
            "month",
            "day",
            "SH 99 Coordinator",
            "Confirm the revised plan",
            "Date not yet known",
            "Not yet known",
        )
    )
    if label_version == "current":
        current_labels = {
            "External Party commitments": "Organization commitments",
            "External Party": "Organization",
            "Commitment Scope": "Applies to",
            "Internal Owner": "Assigned to",
            "Next Action": "Next action",
            "Action Due": "Action due date",
            "Milestone Impact": "Effect on key dates",
            "Scope not yet known": "Not yet known",
            "Committed Date Change · later": "Change to promised timing · later",
        }
        text = "\n".join(current_labels.get(line, line) for line in text.splitlines())
    _require_report_pdf_contents(_report_pdf(text), (statement,))
    incomplete = PdfFixture()
    incomplete.add_page().text((36, 36), "External Party commitments", fontsize=9)
    with pytest.raises(ValueError, match="omits required Report fields"):
        _require_report_pdf_contents(incomplete.tobytes(), (statement,))

    # A partial terminology migration cannot mix columns from different profiles.
    old, new = "Milestone Impact", "Effect on key dates"
    mixed_text = text.replace(old, new) if label_version == "legacy" else text.replace(new, old)
    with pytest.raises(ValueError, match="omits required Report fields"):
        _require_report_pdf_contents(_report_pdf(mixed_text), (statement,))

    # Both label profiles still require the source statement and project action.
    for missing_fact in (statement.event.description, statement.plan.next_action):
        with pytest.raises(ValueError, match="omits required Report fields"):
            _require_report_pdf_contents(
                _report_pdf(text.replace(missing_fact, "")), (statement,)
            )


def _report_pdf(text: str) -> bytes:
    """A one-page PDF carrying ``text`` as a released report would."""

    fixture = PdfFixture()
    fixture.add_page().text_box((36, 36, 559, 806), text, fontsize=9)
    return fixture.tobytes()
