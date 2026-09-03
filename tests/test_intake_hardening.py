"""Tests for untrusted document intake hardening and security gates (#490)."""

import io
from uuid import uuid4
import zipfile
import pytest
from sqlalchemy.orm import Session

from corridor.db import engine
from corridor.intake_hardening import (
    CleanScanner,
    FakeMalwareScanner,
    HostileContentRefused,
    assert_can_process_richly,
    inspect_byte_gate,
    inspect_sandboxed_structure,
    quarantine_document,
    set_malware_scanner,
)
from corridor.models import Document, DocumentQuarantine, Project
from corridor.source_intake import IntakeRefused, validate_and_stage


@pytest.fixture
def session():
    connection = engine.connect()
    trans = connection.begin()
    s = Session(bind=connection)
    yield s
    s.close()
    trans.rollback()
    connection.close()


def _create_zip_with_members(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_byte_gate_enforces_magic_and_size() -> None:
    """Byte gate checks size limit and magic headers."""
    # Empty bytes
    with pytest.raises(HostileContentRefused, match="empty"):
        inspect_byte_gate(b"", "doc.pdf")

    # Size limit exceeded
    with pytest.raises(HostileContentRefused, match="size_limit_exceeded"):
        inspect_byte_gate(b"%PDF-1.4" + b"A" * 100, "doc.pdf", max_bytes=50)

    # Wrong PDF magic
    with pytest.raises(HostileContentRefused, match="magic_mismatch"):
        inspect_byte_gate(b"NOT_A_PDF_HEADER", "doc.pdf")

    # Wrong XLSX magic
    with pytest.raises(HostileContentRefused, match="magic_mismatch"):
        inspect_byte_gate(b"NOT_A_ZIP_HEADER", "workbook.xlsx")

    # Valid PDF and XLSX pass
    inspect_byte_gate(b"%PDF-1.5 test content", "doc.pdf")
    inspect_byte_gate(b"PK\x03\x04\x00\x00 test zip", "sheet.xlsx")


def test_malware_scan_seam_detects_threats() -> None:
    """Pluggable scanner detects infected signatures and refuses intake."""
    scanner = FakeMalwareScanner(
        infected_signatures={b"VIRUS_SIGNATURE_XYZ"},
        infected_filenames={"malicious.pdf"},
    )

    clean_bytes = b"%PDF-1.4 clean doc"
    inspect_byte_gate(clean_bytes, "clean.pdf", scanner=scanner)

    # Signature threat
    with pytest.raises(HostileContentRefused, match="malware_detected"):
        inspect_byte_gate(b"%PDF-1.4 VIRUS_SIGNATURE_XYZ payload", "doc.pdf", scanner=scanner)

    # Filename threat
    with pytest.raises(HostileContentRefused, match="malware_detected"):
        inspect_byte_gate(clean_bytes, "malicious.pdf", scanner=scanner)


def test_archive_path_traversal_refused() -> None:
    """Zip members with path traversal or absolute paths must be refused."""
    # Relative traversal ..
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../etc/passwd", b"root:x:0:0")
    with pytest.raises(HostileContentRefused, match="path_traversal"):
        inspect_sandboxed_structure(buf.getvalue(), "archive.xlsx")

    # Absolute path /
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w") as zf:
        zf.writestr("/root/.ssh/id_rsa", b"private key")
    with pytest.raises(HostileContentRefused, match="path_traversal"):
        inspect_sandboxed_structure(buf2.getvalue(), "archive.xlsx")

    # Windows drive path C:
    buf3 = io.BytesIO()
    with zipfile.ZipFile(buf3, "w") as zf:
        zf.writestr("C:/Windows/System32/evil.dll", b"evil")
    with pytest.raises(HostileContentRefused, match="path_traversal"):
        inspect_sandboxed_structure(buf3.getvalue(), "archive.xlsx")


def test_archive_duplicate_members_refused() -> None:
    """Duplicate member names inside an archive are refused."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/workbook.xml", b"<xml/>")
        zf.writestr("xl/workbook.xml", b"<xml_overwrite/>")

    with pytest.raises(HostileContentRefused, match="duplicate_member"):
        inspect_sandboxed_structure(buf.getvalue(), "matrix.xlsx")


def test_archive_symlink_refused() -> None:
    """Symlink entries inside archives are refused."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("symlink_entry")
        # 0o120000 indicates symlink in Unix file mode
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, b"/etc/shadow")

    with pytest.raises(HostileContentRefused, match="symlink_entry"):
        inspect_sandboxed_structure(buf.getvalue(), "matrix.xlsx")


def test_decompression_bomb_refused() -> None:
    """Decompression bombs exceeding byte ceilings or ratios are refused."""
    # Deflate-compressed 15 MiB of zeroes compresses to ~15 KiB (>1000:1 ratio)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zeroes.bin", b"\x00" * (15 * 1024 * 1024))

    with pytest.raises(HostileContentRefused, match="decompression"):
        inspect_sandboxed_structure(buf.getvalue(), "matrix.xlsx")


def test_xml_entity_bomb_refused() -> None:
    """XML entity bombs (Billion Laughs / quadratic blowup) are detected and refused."""
    xml_bomb = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE lolz ['
        b' <!ENTITY lol "lol">'
        b' <!ENTITY lol2 "&lol;&lol;">'
        b']>'
        b'<workbook>&lol2;</workbook>'
    )
    zip_bytes = _create_zip_with_members({"xl/workbook.xml": xml_bomb})
    with pytest.raises(HostileContentRefused, match="xml_entity_bomb"):
        inspect_sandboxed_structure(zip_bytes, "matrix.xlsx")


def test_rich_processing_gate_refuses_quarantined_documents(session) -> None:
    """Quarantined documents are refused by assert_can_process_richly."""
    project = Project(
        slug=f"sec-test-{uuid4().hex[:8]}",
        name="Security Test Project",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()

    doc = Document(
        project_id=project.id,
        filename="threat.pdf",
        sha256="1234567890abcdef" * 4,
        doc_type="matrix",
    )
    session.add(doc)
    session.flush()

    # Before quarantine: rich processing allowed
    assert_can_process_richly(session, doc.id)

    # Place in quarantine
    quarantine_document(session, doc.id, "Malware threat detected in stage 1")

    # After quarantine: rich processing refused
    with pytest.raises(HostileContentRefused, match="document_quarantined"):
        assert_can_process_richly(session, doc.id)


def test_validate_and_stage_integrates_security_gates() -> None:
    """validate_and_stage integrates byte gate and structural checks, raising IntakeRefused."""
    # Test malware detection through validate_and_stage
    scanner = FakeMalwareScanner(infected_filenames={"bad.pdf"})
    set_malware_scanner(scanner)
    try:
        with pytest.raises(IntakeRefused) as exc_info:
            validate_and_stage(b"%PDF-1.4 test", "bad.pdf")
        assert exc_info.value.reason == "malware_detected"
    finally:
        set_malware_scanner(CleanScanner())

    # Test XML bomb through validate_and_stage
    xml_bomb = b'<!DOCTYPE test [ <!ENTITY boom "boom"> ]><root/>'
    zip_bytes = _create_zip_with_members({"[Content_Types].xml": xml_bomb})
    with pytest.raises(IntakeRefused) as exc_info2:
        validate_and_stage(zip_bytes, "test.xlsx")
    assert exc_info2.value.reason == "xml_entity_bomb"
