"""Replay typed MIME parts from retained message bytes (#455).

The old email page flattened a message into its first plain-text body, losing
headers, quoted-history boundaries and signatures as evidence. These locators
address decoded MIME parts directly; no PDF reader or invented page is involved.
Raw MIME remains the Document rendition. Attachment values identify decoded
bytes by digest; the attachment Document owns interpretation of those bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from pathlib import Path
import re

from corridor.source_append import SegmentValues, append_source_segments
from corridor.source_segment_errors import SourceSegmentLocatorMismatch


MIME_SCHEME = "email-mime-v1"
MAX_PARTS = 200
MAX_TEXT_CHARACTERS = 400_000


@dataclass(frozen=True)
class MimeSegment:
    ordinal: int
    part_path: tuple[int, ...]
    section: str
    header_name: str | None
    header_index: int | None
    start: int
    end: int
    exact_text: str

    def locator(self, digest: str) -> dict:
        return {
            "scheme": MIME_SCHEME, "source_digest": digest,
            "part_path": list(self.part_path), "section": self.section,
            "header_name": self.header_name, "header_index": self.header_index,
        }


def read_mime_segments(raw: bytes) -> tuple[MimeSegment, ...]:
    """Decode bounded MIME, preserving every nonempty header and body range."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    spans: list[MimeSegment] = []
    parts = 0
    characters = 0
    draft = str(message.get("X-Unsent", "")).strip() == "1"

    def add(path, section, text, start=0, end=None, header_name=None, header_index=None):
        nonlocal characters
        end = len(text) if end is None else end
        value = text[start:end]
        if not value:
            return
        characters += len(value)
        if characters > MAX_TEXT_CHARACTERS:
            raise ValueError("MIME text exceeds the bounded reading budget")
        if (section == "body" and spans and value.strip() and spans[-1].exact_text.strip()
                and spans[-1].section == section and spans[-1].part_path == path
                and spans[-1].end == start):
            previous = spans[-1]
            spans[-1] = MimeSegment(previous.ordinal, path, section, None, None,
                                    previous.start, end, text[previous.start:end])
            return
        spans.append(MimeSegment(len(spans) + 1, path, section, header_name,
                                 header_index, start, end, value))

    def walk(part, path):
        nonlocal parts
        parts += 1
        if parts > MAX_PARTS or len(path) > 20:
            raise ValueError("MIME part count or nesting exceeds the reading budget")
        for index, (name, value) in enumerate(part.raw_items()):
            add(path, "header", value, header_name=name.lower(), header_index=index)
        if part.get_content_disposition() == "attachment" or part.get_filename():
            payload = part.get_payload(decode=True)
            if payload is None:
                # Encapsulated message parts cannot claim an invented byte identity.
                raise ValueError("encapsulated MIME attachment requires separately supplied bytes")
            add(path, "attachment", sha256(payload).hexdigest())
            return
        if part.is_multipart():
            for index, child in enumerate(part.iter_parts()):
                walk(child, (*path, index))
            return
        if part.get_content_maintype() != "text":
            payload = part.get_payload(decode=True) or b""
            add(path, "attachment", sha256(payload).hexdigest())
            return
        text = part.get_content()
        if not isinstance(text, str):
            raise ValueError("MIME text did not decode as text")
        if draft or part.get_content_type() != "text/plain":
            add(path, "draft" if draft else "html", text)
            return
        start = 0
        quoted_tail = False
        for line in text.splitlines(keepends=True):
            if line.rstrip("\r\n") == "-- ":
                add(path, "signature", text, start)
                break
            if re.match(r"^On .+wrote:\s*$|^-{2,}\s*Original Message\s*-{2,}", line):
                quoted_tail = True
            section = "quoted_history" if quoted_tail or line.lstrip().startswith(">") else "body"
            add(path, section, text, start, start + len(line))
            start += len(line)

    walk(message, ())
    if any(part.defects for part in message.walk()):
        raise ValueError("malformed MIME cannot supply exact source segments")
    return tuple(spans)


def append_email_segments(session, document, raw: bytes):
    """Append the exact rendition's typed locators through the scoped command."""
    if sha256(raw).hexdigest() != document.sha256:
        raise SourceSegmentLocatorMismatch("MIME bytes do not match the Document")
    return append_source_segments(
        session, project_id=document.project_id, document_id=document.id,
        recorded_verbal_origin_id=None,
        segments=tuple(SegmentValues(
            kind="email_span", exact_text=span.exact_text,
            content_sha256=sha256(span.exact_text.encode()).hexdigest(),
            ordinal=span.ordinal, start_offset=span.start, end_offset=span.end,
            location_json=span.locator(document.sha256),
        ) for span in read_mime_segments(raw)),
    )


def replay_email_segment(document, segment, path: Path) -> str:
    """Rebuild and compare the complete typed locator, not just its stored text."""
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != document.sha256:
        raise SourceSegmentLocatorMismatch("MIME rendition digest differs")
    spans = read_mime_segments(raw)
    if not 0 < segment.ordinal <= len(spans):
        raise SourceSegmentLocatorMismatch("MIME segment ordinal is absent")
    span = spans[segment.ordinal - 1]
    if (segment.location_json != span.locator(document.sha256)
            or (segment.start_offset, segment.end_offset) != (span.start, span.end)):
        raise SourceSegmentLocatorMismatch("MIME locator does not replay")
    return span.exact_text
