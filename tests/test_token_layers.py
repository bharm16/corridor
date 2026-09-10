"""Native and OCR token layers carry coordinates, origin, and engine pinning."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.models import Document, Project, TokenLayerManifest
from corridor.render_profiles import render_page_derivative
from corridor.token_layers import (
    EngineIdentity,
    NATIVE_INTEGRATION_VERSION,
    NATIVE_PROJECTION_MODULES,
    READER_ENGINE,
    READER_NATIVE_ADAPTER_VERSION,
    Token,
    TokenLayer,
    native_integration_digest,
    page_text_projection,
    persist_token_layer,
    read_native_token_layers,
    render_point_to_pdf,
)
from corridor_pdf_reader.execution import MEASURED_DPI, MEASURED_ENGINE
from corridor_pdf_reader.provenance import SOURCE_COMMIT

from pdf_fixture_support import PdfFixture, scan_image


def _native_fixture() -> PdfFixture:
    fixture = PdfFixture()
    page = fixture.add_page(width=420, height=320)
    page.text((60, 80), "Utility Owner AT&T")
    page.text((60, 140), "STA 1149+00")
    return fixture


# The engine identity a native layer written before #741 carries. It is
# constructed rather than produced, because the engine that produced it has
# left the product and its layers have not: a retained manifest still holds
# these exact words, and the code below has to keep working on them.
RETAINED_INCUMBENT_IDENTITY = EngineIdentity(
    origin="native",
    engine="pymupdf",
    engine_version="1.28.0",
    adapter_version="native-pymupdf-v1",
)


def _retained_incumbent_layer(source_sha256: str) -> TokenLayer:
    """One native layer as the retired engine's adapter wrote them."""

    return TokenLayer(
        page_no=1,
        origin="native",
        source_sha256=source_sha256,
        identity=RETAINED_INCUMBENT_IDENTITY,
        tokens=(
            Token(
                ordinal=0,
                origin="native",
                raw_text="Utility",
                normalized_text="utility",
                polygon_pdf={"x0": 60_000, "y0": 80_000, "x1": 96_000, "y1": 91_000},
                block=0,
                line=0,
            ),
        ),
        quality={"token_count": 1, "rotation_degrees": 0, "empty_word_tokens": 0},
    )


def _scanned_fixture() -> PdfFixture:
    scan = scan_image(595, 842, dpi=300, lines=(((72, 120), "CENTERPOINT ENERGY", 22),))
    fixture = PdfFixture()
    fixture.add_page().image((0, 0, 595, 842), scan)
    return fixture


def test_pdf_polygon_maps_inside_the_page_via_the_render_transform(tmp_path):
    fixture = _scanned_fixture()
    pdf = fixture.save(tmp_path / "scanned.pdf")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    _, _, page_width, page_height = fixture.pages[0].media_box
    derivative = render_page_derivative(
        pdf_path=pdf,
        page_number=1,
        profile_name="ocr_layout",
        output_dir=tmp_path / "renders",
    )
    # The render transform's PDF side is fixed-point thousandths, so a mid-raster
    # point inverts to within the page's bounds scaled by 1000.
    x, y = render_point_to_pdf(
        derivative, (derivative.raster_width / 2, derivative.raster_height / 2)
    )
    assert -1000 <= x <= (page_width + 1) * 1000
    assert -1000 <= y <= (page_height + 1) * 1000


def test_canonical_serialization_is_deterministic(tmp_path):
    pdf = _native_fixture().save(tmp_path / "native.pdf")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    (layer,) = read_native_token_layers(pdf, source_sha256=digest)
    reparsed = TokenLayer.model_validate_json(layer.canonical_bytes())
    assert reparsed.content_sha256 == layer.content_sha256
    assert reparsed.canonical_bytes() == layer.canonical_bytes()


# ---- the reader-backed native layer (#733) ---------------------------------


def test_the_reader_native_layer_carries_its_own_engine_identity(tmp_path):
    fixture = _native_fixture()
    pdf = fixture.save(tmp_path / "native.pdf")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()

    (layer,) = read_native_token_layers(pdf, source_sha256=digest)

    assert layer.origin == "native"
    assert layer.page_no == 1
    assert layer.source_sha256 == digest
    # A layer records what produced it, and the reader's identity is not the
    # incumbent's: origins stay distinct layers, engines stay distinct
    # identities (ADR-0073).
    assert layer.identity.engine == READER_ENGINE
    assert layer.identity.engine != "pymupdf"
    assert layer.identity.adapter_version == READER_NATIVE_ADAPTER_VERSION
    assert "PDFium" in layer.identity.engine_version
    assert layer.identity.configuration["source_commit"] == SOURCE_COMMIT
    assert layer.identity.configuration["reader_engine"] == MEASURED_ENGINE
    assert layer.identity.dpi == MEASURED_DPI

    declared = fixture.pages[0].expected_words
    assert [token.raw_text for token in layer.tokens] == [
        word.text for word in declared
    ]
    for token, word in zip(layer.tokens, declared, strict=True):
        assert token.origin == "native"
        assert token.confidence is None  # native readings are not estimates
        x0, y0, x1, y1 = word.fixed_point_box()
        # A token's box is the union of PDFium's advance boxes, so the word
        # starts exactly where the fixture placed it. Its right edge is the
        # reader's own advance for the last glyph, within a fifth of a point
        # of the AFM width the fixture wrote into the font, and the vertical
        # extents are the reader's substitute-face convention against the
        # fixture's AFM bounding box: within three points at 11 pt.
        assert token.polygon_pdf.x0 == x0
        assert abs(token.polygon_pdf.x1 - x1) <= 200
        assert abs(token.polygon_pdf.y0 - y0) <= 3_000
        assert abs(token.polygon_pdf.y1 - y1) <= 3_000
    assert layer.quality["token_count"] == len(layer.tokens) == len(declared)
    assert layer.quality["rotation_degrees"] == 0


def test_page_text_is_a_rebuildable_projection_over_the_reader_native_layer(
    tmp_path,
):
    """ADR-0073: `DocPage.text` stops being a winner and becomes a projection.

    Rebuildable means from the retained layer alone — the artifact carries
    every token, its line and its order, so the page string can be rebuilt
    without the PDF.
    """

    fixture = _native_fixture()
    pdf = fixture.save(tmp_path / "native.pdf")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()

    (layer,) = read_native_token_layers(pdf, source_sha256=digest)

    assert page_text_projection(layer) == fixture.pages[0].expected_text.rstrip("\n")
    # The projection is a function of the artifact, not of the reader run.
    rebuilt = TokenLayer.model_validate_json(layer.canonical_bytes())
    assert page_text_projection(rebuilt) == page_text_projection(layer)
    # It also reproduces the reader's own page text, so the projection adds no
    # reading of its own.
    assert layer.quality["projection_matches_reader_text"] is True


def test_the_projection_refuses_a_layer_it_did_not_produce(tmp_path):
    """A retained incumbent layer is readable and is still not a projection.

    Its page text was a separate call the layer cannot reproduce, and that was
    true before the engine left and is true after. The refusal has to survive
    the removal, because the layers it refuses did.
    """

    retained = _retained_incumbent_layer("0" * 64)

    with pytest.raises(ValueError):
        page_text_projection(retained)


def test_a_reader_layer_is_written_beside_the_earlier_layer_not_over_it(tmp_path):
    """Re-ingest versions Class B artifacts; it never rewrites a history.

    A cited reading is promoted into a Source Segment carrying its own exact
    text and digest (ADR-0068), and an earlier token layer keeps its manifest,
    its artifact bytes and its own retention clock (ADR-0072).
    """

    fixture = _native_fixture()
    pdf = fixture.save(tmp_path / "native.pdf")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    legacy = _retained_incumbent_layer(digest)
    (replacement,) = read_native_token_layers(pdf, source_sha256=digest)

    assert replacement.content_sha256 != legacy.content_sha256

    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        project = Project(
            slug="token-layer-versions", name="Token layer versions", is_synthetic=True
        )
        session.add(project)
        session.flush()
        document_row = Document(
            project_id=project.id,
            sha256=digest,
            filename="native.pdf",
            doc_type="matrix",
            pages=1,
            parse_status="parsed",
        )
        session.add(document_row)
        session.flush()

        first = persist_token_layer(
            session, document_row.id, legacy, output_dir=tmp_path / "layers"
        )
        first_bytes = Path(first.artifact_path).read_bytes()
        second = persist_token_layer(
            session, document_row.id, replacement, output_dir=tmp_path / "layers"
        )

        manifests = session.scalars(
            select(TokenLayerManifest).where(
                TokenLayerManifest.document_id == document_row.id
            )
        ).all()
        assert len(manifests) == 2
        assert {m.origin for m in manifests} == {"native"}
        assert {m.page_no for m in manifests} == {1}
        assert first.artifact_path != second.artifact_path
        assert Path(first.artifact_path).read_bytes() == first_bytes
        assert first.engine_json["engine"] == "pymupdf"
        assert second.engine_json["engine"] == READER_ENGINE
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def test_the_native_assembly_digest_is_declared_and_not_hashed_source_text(
    monkeypatch,
):
    """A comment may not retire a customer's citation.

    ``integration_sha256`` is recorded on every ``pdf_span`` and ``pdf_cell``
    reading and has to match on replay, so whatever it is computed from decides
    which edits make retained citations unreplayable. It used to be
    ``inspect.getsource`` of ten functions in ``token_layers``, which is the
    source text as written: a reflow or a corrected comment changed it and the
    Source Passage Check then reported *Cited location cannot be re-read* for
    every retained native citation, over a change that altered no behaviour.
    So the digest is now exactly a declared version for this module's half plus
    the bytes of the two files that own the segment and prose projections, and
    this test is what says so -- the expected value is recomputed here from
    those three inputs and nothing else.
    """

    root = Path(page_text_projection.__code__.co_filename).parent
    expected_inputs = {
        "native_integration_version": NATIVE_INTEGRATION_VERSION,
        "native_modules": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in NATIVE_PROJECTION_MODULES
        },
    }
    # The exact declared input set, named here so a new dependency of the
    # projection cannot join it unhashed and an unrelated module cannot join it
    # at all.
    assert NATIVE_PROJECTION_MODULES == (
        "native_segment_projection.py",
        "prose_spans.py",
    )
    canonical = json.dumps(
        expected_inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert native_integration_digest() == hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()

    # The declared half is load-bearing: a behaviour change to the page-text
    # projection is pinned by bumping it, and the digest must move when it does.
    before = native_integration_digest()
    monkeypatch.setattr(
        "corridor.token_layers.NATIVE_INTEGRATION_VERSION", "native-integration-v2"
    )
    assert native_integration_digest() != before


def test_the_native_assembly_digest_follows_the_projection_modules(monkeypatch):
    """The two files that turn a reading into segments are still pinned by bytes."""

    original = Path.read_bytes
    read = []

    def watched(self):
        read.append(self.name)
        content = original(self)
        if self.name == "prose_spans.py":
            return content + b"\n# a projection change\n"
        return content

    monkeypatch.setattr(Path, "read_bytes", watched)
    changed = native_integration_digest()
    monkeypatch.undo()

    assert changed != native_integration_digest()
    # Only the two projection files are read. This module's own bytes stay out:
    # it also carries the OCR layers, the Textract reading and retention, and a
    # change to any of those must not invalidate a native locator.
    assert sorted(read) == ["native_segment_projection.py", "prose_spans.py"]


def test_editing_the_replay_module_does_not_retire_a_retained_citation(monkeypatch):
    """A refusal message or docstring in ``reader_segments`` is not the projection.

    The digest hashed the whole of ``reader_segments.py``, which also holds
    ``replay_native_segment``, ``replay_native_segments`` and
    ``append_native_segments`` with their long docstrings. Correcting one of
    those words changed ``integration_sha256``, and every retained
    ``pdf_span``/``pdf_cell`` then reported *Cited location cannot be re-read* --
    exactly the regression ``native_integration_digest`` was written to remove,
    reintroduced one file up. What a retained reading must reproduce is the
    projection, so the projection is its own module and the replay and append
    seams that read it are not hashed.
    """

    root = Path(page_text_projection.__code__.co_filename).parent
    before = native_integration_digest()
    edited = (
        (root / "reader_segments.py")
        .read_bytes()
        .replace(b"Append one explicit reading", b"Append one stated reading")
    )
    assert b"Append one stated reading" in edited
    original = Path.read_bytes
    read = []

    def watched(self):
        read.append(self.name)
        return edited if self.name == "reader_segments.py" else original(self)

    monkeypatch.setattr(Path, "read_bytes", watched)
    assert native_integration_digest() == before
    assert "reader_segments.py" not in read
