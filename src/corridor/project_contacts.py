"""Import and resolve explicit project contacts without changing the record (#562).

Previously a free-text accepted contact cell counted as a resolved recipient,
even without a person, channel or address. This module owns the two declared
input adapters, immutable import/correction history and cutoff-based resolution.
It never sends anything or grants accepted-record authority to a contact source.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, replace
import csv
from datetime import date, datetime
from hashlib import sha256
from io import StringIO
import json
import re
from corridor.contact_mapping import CONTACT_FIELDS
from sqlalchemy import bindparam, cast, func, select
from sqlalchemy.dialects.postgresql import JSONB
from corridor.models import Dependency, Document, ExternalOrg, ProjectContact, ProjectContactImport, SourceSegment
from corridor.source_delivery import require_stored_envelope
from corridor.object_storage import content_store
from corridor.principals import require_human_principal


CONTACT_SCHEMA = "project-contacts-v1"
REQUIRED_FIELDS = CONTACT_FIELDS[:3]
MAX_CONTACT_BYTES = 2 * 1024 * 1024
MAX_CONTACT_ROWS = 10_000


class ContactImportRefused(ValueError):
    """A bound import or correction cannot satisfy its declared contract."""


@dataclass(frozen=True)
class ContactInput:
    source_contact_id: str
    organization_ref: str
    responsible_role: str
    person_name: str | None = None
    channel: str | None = None
    address: str | None = None
    effective_from: date | None = None
    effective_until: date | None = None
    external_system: str | None = None
    external_id: str | None = None

    def payload(self):
        return {key: value.isoformat() if isinstance(value, date) else value
                for key, value in asdict(self).items()}


@dataclass(frozen=True)
class ContactRow:
    row_number: int
    record: ContactInput | None
    reason: str | None
    raw_values: dict
    source_locators: dict


@dataclass(frozen=True)
class ContactRead:
    rows: tuple[ContactRow, ...]
    unknown_columns: tuple[str, ...]


def _record(values):
    if any(values.get(key) is not None and not isinstance(values[key], (str, date)) for key in CONTACT_FIELDS):
        raise ContactImportRefused("invalid_field_type")
    clean = {key: str(values.get(key) or "").strip() or None for key in CONTACT_FIELDS}
    if any(clean[key] is None for key in REQUIRED_FIELDS):
        raise ContactImportRefused("missing_required_identity")
    if clean["channel"] not in {None, "email", "phone"}:
        raise ContactImportRefused("unsupported_channel")
    address, channel = clean["address"], clean["channel"]
    if address:
        if channel is None:
            raise ContactImportRefused("address_requires_channel")
        valid = (re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", address) if channel == "email"
                 else re.fullmatch(r"\+?[0-9 ()-]+", address) and 7 <= len(re.sub(r"\D", "", address)) <= 15)
        if not valid:
            raise ContactImportRefused("invalid_address")
    for field in ("effective_from", "effective_until"):
        if clean[field]:
            try:
                clean[field] = date.fromisoformat(clean[field])
            except ValueError as exc:
                raise ContactImportRefused("invalid_effective_date") from exc
    if (clean["effective_from"] and clean["effective_until"]
            and clean["effective_from"] >= clean["effective_until"]):
        raise ContactImportRefused("invalid_effective_interval")
    return ContactInput(**clean)


def _account_rows(rows):
    groups = {}
    for row in rows:
        key = _source_contact_id(row)
        if key:
            groups.setdefault(key, []).append(_digest(row.record.payload() if row.record else row.raw_values))
    conflicts = {key for key, records in groups.items() if len(set(records)) > 1}
    seen = set()
    result = []
    for row in rows:
        key = _source_contact_id(row)
        if key in conflicts:
            row = replace(row, record=None, reason="conflicting_duplicate_identifier")
        elif key is not None and key in seen:
            row = replace(row, record=None, reason="duplicate_row")
        seen.add(key)
        result.append(row)
    return tuple(result)


def _source_contact_id(row):
    return str(row.raw_values.get("source_contact_id") or "").strip() or None


def read_contact_csv(raw: bytes) -> ContactRead:
    """Read the v1 CSV and account for every physical record, including refusals."""
    if len(raw) > MAX_CONTACT_BYTES:
        raise ContactImportRefused("contact input exceeds its byte budget")
    try:
        reader = csv.reader(StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
        headings = next(reader)
        if any(count > 1 for count in Counter(headings).values()) or not set(REQUIRED_FIELDS) <= set(headings):
            raise ContactImportRefused("contact CSV requires unique headings and all identity columns")
        rows = []
        previous_line = reader.line_num
        for row_number, cells in enumerate(reader, start=1):
            if row_number > MAX_CONTACT_ROWS:
                raise ContactImportRefused("contact input exceeds its row budget")
            start_line, end_line = previous_line + 1, reader.line_num
            previous_line = end_line
            values = dict(zip(headings, cells))
            locators = {heading: {"kind": "csv_field", "record": row_number,
                        "start_line": start_line, "end_line": end_line, "column": index + 1}
                        for index, heading in enumerate(headings)}
            record, reason = None, None
            if len(cells) != len(headings):
                reason = "malformed_column_count"
                values["cells"] = cells
            else:
                try:
                    record = _record(values)
                except ContactImportRefused as exc:
                    reason = str(exc)
            rows.append(ContactRow(row_number, record, reason, values, locators))
    except (UnicodeError, csv.Error, StopIteration) as exc:
        raise ContactImportRefused("contact CSV is not valid UTF-8 tabular data") from exc
    return ContactRead(_account_rows(rows), tuple(name for name in headings if name not in CONTACT_FIELDS))


def import_contact_csv(session, envelope, *, import_identity: str, source_family: str | None = None):
    """Capture a pre-bound delivery, source bytes and every row outcome atomically."""
    delivery = require_stored_envelope(session, envelope)
    raw = content_store().get(envelope.bytes_reference, sha256=envelope.content_digest)
    parsed = read_contact_csv(raw)
    with session.begin_nested():
        document = session.scalar(select(Document).where(
            Document.project_id == delivery.project_id, Document.sha256 == envelope.content_digest))
        if document is None:
            document = Document(project_id=delivery.project_id, sha256=envelope.content_digest,
                filename="project-contacts.csv", doc_type="other", pages=0, parse_status="parsed",
                numbering_scheme="project-unique", source_delivery_id=delivery.id)
            session.add(document)
            session.flush()
        return _append_import(session, document, delivery_id=delivery.id, customer=envelope.customer,
            family=source_family or envelope.external_identity, revision=envelope.external_version,
            key=import_identity, mapping={"schema_version": CONTACT_SCHEMA, "adapter": "csv"}, parsed=parsed)


def import_adopted_contacts(session, *, project_id: int, import_identity: str):
    """Read contacts only through the adopted workbook's registered mapping."""
    from corridor.baseline_adoption import adopted_baseline_source, effective_field_mapping_manifest
    from corridor.source_segments import spreadsheet_replay
    from corridor.storage import stored_file

    baseline = adopted_baseline_source(session, project_id)
    manifest = effective_field_mapping_manifest(session, project_id)
    if baseline is None or manifest is None or manifest.contact_mapping is None:
        raise ContactImportRefused("adopted workbook has no registered contact mapping")
    mapping = manifest.contact_mapping
    document = session.get_one(Document, baseline.document_id)
    path = stored_file(document)
    if path is None:
        raise ContactImportRefused("adopted contact source bytes are unavailable")
    segments = session.scalars(select(SourceSegment).where(
        SourceSegment.project_id == project_id, SourceSegment.document_id == document.id,
        SourceSegment.kind == "spreadsheet_cell", SourceSegment.sheet_name == mapping.sheet_name,
    ).order_by(SourceSegment.ordinal)).all()
    rows = {}
    with spreadsheet_replay(document, path) as replay:
        for segment in segments:
            match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", segment.cell_range)
            column, row = match.group(1), int(match.group(2))
            rows.setdefault(row, {})[column] = (replay(segment), segment)
    headings = {}
    for column, (heading, _) in rows.get(mapping.header_row, {}).items():
        headings.setdefault(heading, []).append(column)
    selected = {}
    for field, heading in mapping.columns:
        columns = headings.get(heading, [])
        if len(columns) != 1:
            raise ContactImportRefused(f"mapped contact heading is missing or ambiguous: {heading}")
        selected[field] = columns[0]
    parsed = []
    for row_number, cells in sorted(rows.items()):
        if row_number <= mapping.header_row:
            continue
        values, locators = {}, {}
        for field, column in selected.items():
            text_value, segment = cells.get(column, ("", None))
            # Native date cells retain midnight datetime strings; this is the
            # same day interpretation used by the workbook's ISO date parser.
            if field.startswith("effective_") and text_value.endswith(" 00:00:00"):
                text_value = text_value.removesuffix(" 00:00:00")
            values[field] = text_value
            locators[field] = {"kind": "spreadsheet_cell", "sheet_name": mapping.sheet_name,
                "cell_range": f"{column}{row_number}", "segment_id": segment.id if segment else None}
        if mapping.responsible_role is not None:
            values["responsible_role"] = mapping.responsible_role
            locators["responsible_role"] = {"kind": "registered_mapping", "mapping_sha256": manifest.content_sha256}
        try:
            record, reason = _record(values), None
        except ContactImportRefused as exc:
            record, reason = None, str(exc)
        parsed.append(ContactRow(row_number, record, reason, values, locators))
    unknown = tuple(heading for heading in headings if heading not in dict(mapping.columns).values())
    with session.begin_nested():
        return _append_import(session, document, delivery_id=None, customer=baseline.customer,
            family=f"adopted-ucm:{baseline.id}", revision=document.sha256, key=import_identity,
            mapping={"schema_version": CONTACT_SCHEMA, "adapter": "adopted_ucm",
                     "mapping_identity": manifest.identity, "mapping_version": manifest.version,
                     "mapping_sha256": manifest.content_sha256, "contact_mapping": mapping.as_payload()},
            parsed=ContactRead(_account_rows(parsed), unknown))


def _organization_registry(session, project_id):
    from corridor.delta_generation import accepted_values
    from corridor.operating_mode import is_adopted_baseline

    normalize = lambda text: " ".join(str(text).split()).casefold()
    accepted = {normalize(value) for (_, field), value in accepted_values(session, project_id).items()
                if field == "external_org" and isinstance(value, str)}
    associated = set() if is_adopted_baseline(session, project_id) else set(
        session.scalars(select(Dependency.external_org_id).where(Dependency.project_id == project_id)).all())
    result = {}
    for organization in session.scalars(select(ExternalOrg).order_by(ExternalOrg.id)):
        aliases = {normalize(name) for name in (organization.name, *(organization.aliases or []))}
        if organization.id in associated or aliases & accepted:
            for name in (*aliases, f"external_org:{organization.id}"):
                result.setdefault(name, set()).add(organization.id)
    return result


def _organization(registry, reference):
    found = registry.get(" ".join(reference.split()).casefold(), set())
    return (next(iter(found)), None) if len(found) == 1 else (
        None, "ambiguous_organization" if found else "unknown_organization")


def _append_import(session, document, *, delivery_id, customer, family, revision, key, mapping, parsed):
    if not key.strip():
        raise ContactImportRefused("contact import needs a stable identity")
    registry = _organization_registry(session, document.project_id)
    rows = []
    refused_ids = set()
    for row in parsed.rows:
        org_id, unresolved = _organization(registry, row.record.organization_ref) if row.record else (None, None)
        refusal = None
        source_id = _source_contact_id(row)
        if row.record is None and source_id and row.reason != "duplicate_row" and source_id not in refused_ids:
            # Keep an identifiable refusal in occurrence order, so a bad newer
            # row cannot silently revive the previous recipient. Raw input is
            # retained separately; these empty fields are not repaired values.
            refusal = ContactInput(source_id, str(row.raw_values.get("organization_ref") or "").strip(),
                                   str(row.raw_values.get("responsible_role") or "").strip()).payload()
            org_id, _ = _organization(registry, refusal["organization_ref"])
            unresolved = "refused_contact_revision"
            refused_ids.add(source_id)
        rows.append({"row_number": row.row_number, "record": row.record.payload() if row.record else None,
            "refused_record": refusal,
            "reason": row.reason, "raw_values": row.raw_values, "source_locators": row.source_locators,
            "organization_id": org_id, "unresolved_reason": unresolved})
    accounting = {"schema_version": CONTACT_SCHEMA, "rows": rows, "unknown_columns": list(parsed.unknown_columns)}
    # Registry resolution is an observation retained on the first import. It
    # is not part of source identity and cannot turn a replay into a new import.
    digest = _digest({"document": document.sha256, "mapping": mapping,
                      "family": family, "revision": revision, "schema": CONTACT_SCHEMA})
    import_id = session.scalar(select(func.append_project_contact_import(
        document.project_id, document.id, delivery_id, customer, family, revision, key,
        digest, _json(mapping), _json(accounting))))
    return session.get_one(ProjectContactImport, int(import_id))


def correct_contact(session, *, project_id: int, contact_id: int, replacement: ContactInput,
                    principal, reason: str, idempotency_key: str):
    """Append an authenticated onboarding correction; stale predecessors refuse."""
    actor = require_human_principal(principal)
    if not isinstance(replacement, ContactInput):
        raise ContactImportRefused("correction requires a typed contact record")
    record = _record(replacement.payload())
    org, unresolved = _organization(_organization_registry(session, project_id), record.organization_ref)
    identifier = session.scalar(select(func.correct_project_contact(project_id, contact_id,
        _json(record.payload()), org, unresolved, actor.subject, reason, idempotency_key)))
    return session.get_one(ProjectContact, int(identifier))


@dataclass(frozen=True)
class ContactResolution:
    state: str
    reason: str | None
    person_name: str | None = None
    channel: str | None = None
    address: str | None = None
    record_ids: tuple[int, ...] = ()


def contact_history(session, *, project_id: int):
    """Every immutable source/correction occurrence, in recorded order."""
    return tuple(session.scalars(select(ProjectContact).where(ProjectContact.project_id == project_id)
                                .order_by(ProjectContact.id)).all())


def resolve_project_contacts(session, *, project_id: int, requests, as_of: datetime):
    """Resolve roles as of one cutoff, in one batch and without guessing an address."""
    if as_of.tzinfo is None:
        raise ContactImportRefused("contact resolution requires an aware cutoff")
    registry = _organization_registry(session, project_id)
    histories = session.execute(select(ProjectContact, ProjectContactImport.source_family)
        .join(ProjectContactImport, ProjectContactImport.id == ProjectContact.import_id)
        .where(ProjectContact.project_id == project_id, ProjectContact.recorded_at <= as_of)
        .order_by(ProjectContact.recorded_at, ProjectContact.id)).all()
    latest = {}
    occurrences = {}
    for row, family in histories:
        key = (family, row.source_contact_id)
        latest[key] = row
        occurrences.setdefault(key, []).append(row)
    results = {}
    for organization, role in requests:
        org_id, reason = _organization(registry, organization)
        def matches(row):
            return row.values_json["responsible_role"] == role and (
                (row.organization_id == org_id and org_id is not None)
                or row.values_json["organization_ref"].casefold() == organization.casefold())

        matching = [row for key, row in latest.items() if matches(row) or (
            row.unresolved_reason == "refused_contact_revision" and any(matches(previous) for previous in occurrences[key]))]
        if any(row.unresolved_reason == "refused_contact_revision" for row in matching):
            results[(organization, role)] = ContactResolution("unresolved_contact", "refused_contact_revision",
                                                             record_ids=tuple(row.id for row in matching))
            continue
        active = []
        for row in matching:
            value = _record(row.values_json)
            if ((value.effective_from is None or value.effective_from <= as_of.date())
                    and (value.effective_until is None or as_of.date() < value.effective_until)):
                active.append((row, value))
        complete = [(row, value) for row, value in active if row.organization_id is not None and value.person_name and value.channel and value.address
                    and row.unresolved_reason is None]
        identities = {(value.person_name, value.channel, value.address) for _, value in complete}
        if len(identities) == 1 and len(complete) == len(active):
            name, channel, address = next(iter(identities))
            result = ContactResolution("resolved", None, name, channel, address, tuple(row.id for row, _ in complete))
        else:
            reason = (reason if matching else None) or ("competing_contacts" if len(identities) > 1 else
                     "incomplete_contact" if active else "not_effective" if matching else "no_contact_record")
            result = ContactResolution("unresolved_contact", reason, record_ids=tuple(row.id for row in matching))
        results[(organization, role)] = result
    return results


def resolve_contact(session, *, project_id: int, organization_ref: str, responsible_role: str, as_of: datetime):
    key = (organization_ref, responsible_role)
    return resolve_project_contacts(session, project_id=project_id, requests=(key,), as_of=as_of)[key]


def _json(value):
    return cast(bindparam(None, json.dumps(value)), JSONB)


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
