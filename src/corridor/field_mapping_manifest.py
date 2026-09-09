"""The registered mapping revision a template's columns are read through (#597).

#495 refused a successor template by recomputing a digest over the template's
own columns: which canonical field each heading carries, which of those are
material, and each column's declared drop-down.  That digest catches an added
column, a moved canonical field, and a narrowed vocabulary, and it cannot catch
the one change that matters most — a form that stops carrying *Start Station*
and *End Station* as two values and starts carrying one combined range in the
same two columns, or the reverse.  Every heading is identical, every drop-down
is identical, so the digest is identical and the render is silently wrong:
accepted values land in columns that no longer mean what the mapping believes.

**No digest over the template can catch it, because the template does not say
it.**  So the authority moves off the bytes entirely.  A *mapping revision* is
a declared, versioned manifest: for each mapping it records the exact source
columns, the canonical fields they carry, the cardinality, the parsing,
formatting and composition rule identities, the delimiter or range semantics,
the unit and timing precision, the controlled vocabulary, blank handling,
materiality, and retirement handling.  A successor template that combines a
value **must register a different mapping revision**; it cannot keep using the
old one because its headings did not move.

**Shape never authorizes, and may refuse.**  `RANGE_SEPARATORS` below is a
shape check, and it exists only to stop a render: a populated cell under a
declared range endpoint carrying ` - ` is not the single value the manifest
declares, so the renderer refuses rather than deciding what the cell means.
Nothing here ever reads a separator and concludes that the form changed
meaning — that conclusion is a person's, recorded as a registration.

**A populated example proves the manifest.**  A declared composition is a claim
about values, so `prove_manifest` runs the claim over a representative
populated example and requires the round trip: source cells decompose to the
canonical values, and those values compose back to the same cells.  A
declaration that cannot reproduce the customer's own example is refused before
anyone registers it.

Nothing here touches the database, calls a model, or reads a clock.  It holds
the declaration and the rules that check it; `baseline_adoption` registers one
(`register_baseline_format`) and `workbook_render` renders through it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
import json

from corridor.baseline_workbook import (
    MATERIAL_FIELDS,
    BaselineRow,
    OperationsReading,
)
from corridor.fact_types import FACT_TYPE_CONTRACTS
from corridor.contact_mapping import ContactMapping


# The shape of the manifest itself, apart from the version of any one mapping
# revision.  A manifest written under a later schema records different things
# and is not comparable with this one, so the version travels in the digest.
MANIFEST_SCHEMA_VERSION = "field-mapping-manifest-v1"

# The mapping revision a project starts from, named as its own identity because
# a later revision must be registrable without re-adopting anything: the
# accepted values already exist and a mapping change cannot move them.
FIELD_MAPPING_IDENTITY = "ucm-published-column-headings"
FIELD_MAPPING_VERSION = "v1"

# --- Released rule identities ----------------------------------------------
#
# A manifest may name only a rule that ships with the renderer.  An identity
# nobody implemented is a mapping nobody can render, and accepting one would
# put the refusal off until the customer's issue.

# One source column carries one canonical field, the way the published UCM
# forms head one column per field (ADR-0005, ADR-0009).
ONE_VALUE_PER_COLUMN = "one_value_per_column_v1"

# One source column carries every canonical field of the mapping, joined by the
# declared delimiter; the mapping's remaining source columns are blank.
COMBINED_RANGE = "combined_range_v1"

# A mapping's declared parsing rule is not free text: it must be the released
# transformation the Fact contract already states for the field it carries, so
# a manifest cannot claim a parse the record does not perform.  The agreement
# is checked field by field in `prove_manifest`.

# How a canonical value is written back.  The renderer writes the accepted text
# exactly as the record holds it and reformats nothing.
FORMATTING_RULES = frozenset({"preserve_source_precision_v1"})

# What an absent canonical value means for the cells of one mapping.
ABSENT_IS_BLANK = "absent_is_blank_v1"
ALL_ABSENT_IS_UNKNOWN = "all_absent_is_unknown_v1"
BLANK_BEHAVIOURS = frozenset({ABSENT_IS_BLANK, ALL_ABSENT_IS_UNKNOWN})

# Unit and timing-precision semantics, where the field has any.
CALENDAR_DAY = "calendar_day_v1"
SOURCE_STATED_UNIT = "source_stated_unit_v1"
PRECISIONS = frozenset({CALENDAR_DAY, SOURCE_STATED_UNIT})

# Whether this mapping's field is the one a retired row's declared wording is
# written into (`workbook_render.RetirementMapping`).
NOT_A_RETIREMENT_FIELD = "not_a_retirement_field_v1"
CARRIES_RETIREMENT_WORDING = "carries_declared_retirement_wording_v1"
RETIREMENT_HANDLING = frozenset({NOT_A_RETIREMENT_FIELD, CARRIES_RETIREMENT_WORDING})

# The canonical fields that name one end of a range.  Declared vocabulary
# knowledge (`vocabulary.TEMPLATE_FIELDS` heads `Start Station`/`End Station`
# and `Start Offset`/`End Offset` as separate columns), not a reading of any
# file: it is why the default declaration for these fields carries range
# semantics at all, and why a combined value under one of them is refused.
RANGE_ENDPOINTS = (
    ("station_from", "station_to"),
    ("offset_from", "offset_to"),
)

# The delimiter the default declaration states for a range.  A form that writes
# its ranges another way registers a revision that says so.
DEFAULT_RANGE_DELIMITER = " - "

# Separators that make a populated cell something other than the one value a
# `one_value_per_column_v1` mapping declares.  Every one is spaced or a word:
# a bare hyphen is a negative offset and a hyphenated identifier as often as it
# is a range, and a check that refused `SR-BL` would be a false alarm rather
# than a fail-closed.  This list may only ever *refuse* (see the module
# docstring); it never decides that a form's meaning changed.
RANGE_SEPARATORS = (" - ", " – ", " — ", " to ", " thru ", " through ")

# The external-reference roles a customer mapping may declare.  The two are
# kept apart because they are different records: the utility-management
# system's own identifier for the conflict, and a link to a controlled
# document about it.
CONFLICT_RECORD_IDENTIFIER = "external_system_id"
DOCUMENT_LINK = "source_url"
EXTERNAL_REFERENCE_ROLES = frozenset({CONFLICT_RECORD_IDENTIFIER, DOCUMENT_LINK})


class MappingManifestRefused(ValueError):
    """This declaration is not a mapping revision anything can be rendered through."""


def declared_reference_headings(
    references: Sequence["ExternalReference"],
) -> dict[str, str]:
    """The declared headings, in the form `baseline_workbook` compares against."""

    return {
        " ".join(item.heading.split()).casefold(): item.role for item in references
    }


@dataclass(frozen=True)
class ExternalReference:
    """One printed heading that carries a reference out of this workbook.

    Declared, never matched by spelling: an exact heading is given a role
    because a customer's own form says it holds one, and a heading nobody
    declared stays a retained unknown column.
    """

    heading: str
    role: str


@dataclass(frozen=True)
class MaterialMapping:
    """One declared mapping between a form's columns and canonical fields."""

    source_columns: tuple[str, ...]
    target_fields: tuple[str, ...]
    composition: str
    parser: str = "trim_cell_text_v1"
    formatting: str = "preserve_source_precision_v1"
    delimiter: str | None = None
    precision: str | None = None
    controlled_vocabulary: tuple[str, ...] = ()
    vocabulary_reference: str | None = None
    blank_behaviour: str = ABSENT_IS_BLANK
    material: bool = False
    retirement: str = NOT_A_RETIREMENT_FIELD
    example: tuple[str, ...] = ()

    @property
    def cardinality(self) -> str:
        """How many source values carry how many canonical fields."""

        return _rule(self.composition).cardinality(self)

    @property
    def declared_columns(self) -> tuple[tuple[str, str], ...]:
        """The (heading, canonical field) pairs a conforming template prints."""

        return tuple(zip(self.source_columns, self.target_fields))

    def as_payload(self) -> dict[str, object]:
        return {
            "source_columns": list(self.source_columns),
            "target_fields": list(self.target_fields),
            "cardinality": self.cardinality,
            "composition": self.composition,
            "parser": self.parser,
            "formatting": self.formatting,
            "delimiter": self.delimiter,
            "precision": self.precision,
            "controlled_vocabulary": list(self.controlled_vocabulary),
            "vocabulary_reference": self.vocabulary_reference,
            "blank_behaviour": self.blank_behaviour,
            "material": self.material,
            "retirement": self.retirement,
            "example": list(self.example),
        }


@dataclass(frozen=True)
class FieldMappingManifest:
    """One registered mapping revision: the authority a template is read through."""

    identity: str
    version: str
    mappings: tuple[MaterialMapping, ...]
    external_references: tuple[ExternalReference, ...] = ()
    schema_version: str = MANIFEST_SCHEMA_VERSION
    contact_mapping: ContactMapping | None = None

    @property
    def revision(self) -> str:
        """How this mapping revision is named on a receipt and in a refusal."""

        return f"{self.identity} {self.version}"

    @property
    def external_reference_headings(self) -> dict[str, str]:
        """The declared heading-to-role map `baseline_workbook` reads with."""

        return declared_reference_headings(self.external_references)

    def mapping_for(self, field: str) -> MaterialMapping | None:
        for mapping in self.mappings:
            if field in mapping.target_fields:
                return mapping
        return None

    def as_payload(self) -> dict[str, object]:
        payload = {
            "schema_version": self.schema_version,
            "identity": self.identity,
            "version": self.version,
            "mappings": [
                mapping.as_payload()
                for mapping in sorted(
                    self.mappings, key=lambda item: item.target_fields
                )
            ],
            "external_references": [
                [item.heading, item.role]
                for item in sorted(
                    self.external_references, key=lambda item: (item.role, item.heading)
                )
            ],
        }
        if self.contact_mapping is not None:
            payload["contact_mapping"] = self.contact_mapping.as_payload()
        return payload

    @property
    def declaration_json(self) -> str:
        """The exact bytes this revision is digested as, and stored as (#610).

        The digest is taken over these bytes, so storing anything else beside
        the digest would store something the digest does not cover.  They are
        canonical — sorted keys, no separator padding — so the same declaration
        digests the same way on every machine that writes it.
        """

        return json.dumps(
            self.as_payload(), sort_keys=True, separators=(",", ":")
        )

    @property
    def content_sha256(self) -> str:
        return sha256(self.declaration_json.encode("utf-8")).hexdigest()


# --- Reading one back -------------------------------------------------------


def manifest_from_declaration(declaration: str) -> FieldMappingManifest:
    """The mapping revision one stored declaration records, or a refusal (#610).

    #597 registered a mapping revision by identity, version and digest alone,
    so reproducing a past render depended on whoever declared it still holding
    the declaration.  A digest nobody can resolve proves *that* a render used a
    revision and not *what* that revision said, which for an auditable record
    is the weaker half.

    The bytes are not trusted for being stored.  They are read back into a
    manifest and re-digested, and the result must be the digest of the bytes
    themselves: a stored declaration that reconstructs into something else —
    a mapping out of canonical order, a cardinality that disagrees with its
    composition rule, a key nobody writes — is refused rather than rendered
    through.
    """

    try:
        payload = json.loads(declaration)
    except ValueError as exc:
        raise MappingManifestRefused(
            f"the stored mapping revision is not readable: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise MappingManifestRefused(
            "a stored mapping revision is one declared manifest, and these "
            f"bytes read back as {type(payload).__name__}"
        )
    try:
        manifest = FieldMappingManifest(
            identity=payload["identity"],
            version=payload["version"],
            mappings=tuple(
                _mapping_from_payload(item) for item in payload["mappings"]
            ),
            external_references=tuple(
                ExternalReference(heading, role)
                for heading, role in payload["external_references"]
            ),
            schema_version=payload["schema_version"],
            contact_mapping=_contact_mapping(payload.get("contact_mapping")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MappingManifestRefused(
            f"the stored mapping revision does not declare {exc}"
        ) from exc

    stored = sha256(declaration.encode("utf-8")).hexdigest()
    if manifest.content_sha256 != stored:
        raise MappingManifestRefused(
            "the stored mapping revision does not reconstruct the declaration "
            f"it is digested as: the stored bytes digest to {stored} and what "
            f"they read back as digests to {manifest.content_sha256}"
        )
    return manifest


def _contact_mapping(value):
    return ContactMapping.from_payload(value) if value is not None else None


def _mapping_from_payload(payload: Mapping[str, object]) -> MaterialMapping:
    """One declared mapping, without the cardinality its rule derives."""

    return MaterialMapping(
        source_columns=tuple(payload["source_columns"]),
        target_fields=tuple(payload["target_fields"]),
        composition=payload["composition"],
        parser=payload["parser"],
        formatting=payload["formatting"],
        delimiter=payload["delimiter"],
        precision=payload["precision"],
        controlled_vocabulary=tuple(payload["controlled_vocabulary"]),
        vocabulary_reference=payload["vocabulary_reference"],
        blank_behaviour=payload["blank_behaviour"],
        material=payload["material"],
        retirement=payload["retirement"],
        example=tuple(payload["example"]),
    )


# --- The released composition rules -----------------------------------------


@dataclass(frozen=True)
class CompositionRule:
    """How one mapping's canonical values are distributed over its columns."""

    identity: str

    def cardinality(self, mapping: MaterialMapping) -> str:
        raise NotImplementedError

    def validate(self, mapping: MaterialMapping) -> None:
        """Refuse a declaration this rule cannot mean, before anything uses it."""

        if len(mapping.source_columns) != len(mapping.target_fields):
            raise MappingManifestRefused(
                f"{self.identity} maps {len(mapping.source_columns)} source "
                f"columns to {len(mapping.target_fields)} canonical fields; a "
                "mapping names one source column per canonical field, and says "
                "through its composition rule which of them carry a value"
            )

    def fields_at(self, mapping: MaterialMapping, index: int) -> tuple[str, ...]:
        raise NotImplementedError

    def compose(
        self, mapping: MaterialMapping, values: Sequence[str | None]
    ) -> tuple[str, ...]:
        raise NotImplementedError

    def decompose(
        self, mapping: MaterialMapping, cells: Sequence[str]
    ) -> tuple[str, ...]:
        raise NotImplementedError

    def refusal(
        self, mapping: MaterialMapping, cells: Sequence[str]
    ) -> str | None:
        raise NotImplementedError


class _OneValuePerColumn(CompositionRule):
    """Each column carries one canonical value, and no column carries two."""

    def cardinality(self, mapping: MaterialMapping) -> str:
        count = len(mapping.source_columns)
        return f"{count} → {count}"

    def fields_at(self, mapping: MaterialMapping, index: int) -> tuple[str, ...]:
        return (mapping.target_fields[index],)

    def compose(
        self, mapping: MaterialMapping, values: Sequence[str | None]
    ) -> tuple[str, ...]:
        _refuse_partial(mapping, values)
        return tuple("" if value is None else value for value in values)

    def decompose(
        self, mapping: MaterialMapping, cells: Sequence[str]
    ) -> tuple[str, ...]:
        return tuple(cells)

    def refusal(
        self, mapping: MaterialMapping, cells: Sequence[str]
    ) -> str | None:
        if mapping.delimiter is None:
            return None
        for heading, cell in zip(mapping.source_columns, cells):
            separator = _combining_separator(cell, mapping.delimiter)
            if separator is not None:
                return (
                    f"{heading!r} carries {cell!r}, which combines two values "
                    f"across {separator!r}, where the mapping declares one "
                    f"value per column ({mapping.composition}, cardinality "
                    f"{self.cardinality(mapping)})"
                )
        return None


class _CombinedRange(CompositionRule):
    """The first column carries every value of the mapping; the rest are blank."""

    def cardinality(self, mapping: MaterialMapping) -> str:
        return f"1 → {len(mapping.target_fields)}"

    def validate(self, mapping: MaterialMapping) -> None:
        super().validate(mapping)
        if len(mapping.target_fields) < 2:
            raise MappingManifestRefused(
                f"{self.identity} composes at least two canonical fields into "
                f"one column; {mapping.target_fields} is not a range"
            )
        if not mapping.delimiter:
            raise MappingManifestRefused(
                f"{self.identity} needs the delimiter its range is written "
                "with; a combined value with no declared delimiter cannot be "
                "read back into canonical values"
            )

    def fields_at(self, mapping: MaterialMapping, index: int) -> tuple[str, ...]:
        return mapping.target_fields if index == 0 else ()

    def compose(
        self, mapping: MaterialMapping, values: Sequence[str | None]
    ) -> tuple[str, ...]:
        _refuse_partial(mapping, values)
        if all(value is None for value in values):
            return tuple("" for _ in mapping.source_columns)
        combined = str(mapping.delimiter).join(
            "" if value is None else value for value in values
        )
        return (combined,) + tuple("" for _ in mapping.source_columns[1:])

    def decompose(
        self, mapping: MaterialMapping, cells: Sequence[str]
    ) -> tuple[str, ...]:
        return tuple(
            part.strip() for part in str(cells[0]).split(str(mapping.delimiter))
        )

    def refusal(
        self, mapping: MaterialMapping, cells: Sequence[str]
    ) -> str | None:
        combined = cells[0]
        trailing = [
            (heading, cell)
            for heading, cell in zip(mapping.source_columns[1:], cells[1:])
            if cell
        ]
        if trailing:
            heading, cell = trailing[0]
            return (
                f"{heading!r} carries {cell!r}, where the mapping declares the "
                f"whole range in {mapping.source_columns[0]!r} and this column "
                f"blank ({mapping.composition}, cardinality "
                f"{self.cardinality(mapping)})"
            )
        if not combined:
            return None
        if str(mapping.delimiter) not in combined:
            return (
                f"{mapping.source_columns[0]!r} carries {combined!r}, which is "
                f"one value where the mapping declares "
                f"{len(mapping.target_fields)} values across "
                f"{mapping.delimiter!r} ({mapping.composition}, cardinality "
                f"{self.cardinality(mapping)})"
            )
        if len(combined.split(str(mapping.delimiter))) != len(mapping.target_fields):
            return (
                f"{mapping.source_columns[0]!r} carries {combined!r}, which "
                f"does not split into the {len(mapping.target_fields)} values "
                f"the mapping declares ({mapping.composition})"
            )
        return None


COMPOSITION_RULES: dict[str, CompositionRule] = {
    ONE_VALUE_PER_COLUMN: _OneValuePerColumn(identity=ONE_VALUE_PER_COLUMN),
    COMBINED_RANGE: _CombinedRange(identity=COMBINED_RANGE),
}


def _rule(identity: str) -> CompositionRule:
    rule = COMPOSITION_RULES.get(identity)
    if rule is None:
        raise MappingManifestRefused(
            f"{identity!r} is not a released composition rule; a mapping "
            "revision may name only a rule this renderer implements"
        )
    return rule


def composition_rule(identity: str) -> CompositionRule:
    """The released rule one mapping names, or a refusal."""

    return _rule(identity)


def _combining_separator(cell: str, delimiter: str) -> str | None:
    for separator in (delimiter, *RANGE_SEPARATORS):
        if separator and separator in cell:
            return separator
    return None


def _refuse_partial(
    mapping: MaterialMapping, values: Sequence[str | None]
) -> None:
    if mapping.blank_behaviour != ALL_ABSENT_IS_UNKNOWN:
        return
    present = [value for value in values if value is not None]
    if present and len(present) != len(values):
        raise MappingManifestRefused(
            f"the accepted record holds {len(present)} of "
            f"{len(mapping.target_fields)} values for "
            f"{mapping.target_fields}, and the mapping declares that either "
            "every one is present or none is "
            f"({mapping.blank_behaviour})"
        )


# --- Declaring a manifest ---------------------------------------------------


@dataclass(frozen=True)
class MappingDeclaration:
    """What Corridor operations declares before a workbook is read.

    Operations may construct and technically validate a mapping; a person
    holding the project-coordination designation approves the material semantic
    change it amounts to (`baseline_adoption.register_baseline_format`).
    """

    identity: str = FIELD_MAPPING_IDENTITY
    version: str = FIELD_MAPPING_VERSION
    external_references: tuple[ExternalReference, ...] = ()
    mappings: tuple[MaterialMapping, ...] = ()
    contact_mapping: ContactMapping | None = None

    @property
    def external_reference_headings(self) -> dict[str, str]:
        return declared_reference_headings(self.external_references)


# The four headings #509 guessed at, kept only as a named synthetic profile.
#
# They were provisional: no customer form was read for them, and a production
# default that assigns meaning to a spelling is exactly what this ticket
# removes.  A fixture that wants an external reference declares this profile by
# name; a customer declares their own, read from their form's data dictionary
# (#561).
DEMO_EXTERNAL_REFERENCES = (
    ExternalReference("UCM Record ID", CONFLICT_RECORD_IDENTIFIER),
    ExternalReference("Document Control No.", CONFLICT_RECORD_IDENTIFIER),
    ExternalReference("Record URL", DOCUMENT_LINK),
    ExternalReference("Document Link", DOCUMENT_LINK),
)


def declared_field_mapping(
    reading: OperationsReading, declaration: MappingDeclaration | None = None
) -> FieldMappingManifest:
    """Construct the mapping revision this declaration amounts to, and prove it.

    The default for a column is one value per column, because that is what the
    canonical vocabulary means — a published UCM form heads one column per
    field (ADR-0005, ADR-0009) — and not because of anything read off this
    file.  A form that carries something else is declared explicitly in
    ``declaration.mappings`` and refuses this construction if its own populated
    example does not reproduce.
    """

    declaration = declaration or MappingDeclaration()
    declared = {
        field: mapping
        for mapping in declaration.mappings
        for field in mapping.target_fields
    }
    printed_headings = {column.heading for column in reading.column_mapping}
    vocabularies = _vocabularies_by_heading(reading)

    mappings: list[MaterialMapping] = []
    for mapping in declaration.mappings:
        mappings.append(_with_example(mapping, reading))
    for column in reading.column_mapping:
        if column.field in declared:
            continue
        mappings.append(
            _with_example(
                _default_mapping(column.heading, column.field, vocabularies),
                reading,
            )
        )

    manifest = FieldMappingManifest(
        identity=declaration.identity,
        version=declaration.version,
        mappings=tuple(mappings),
        external_references=tuple(declaration.external_references),
        contact_mapping=declaration.contact_mapping,
    )
    for mapping in manifest.mappings:
        for heading in mapping.source_columns:
            if heading not in printed_headings:
                raise MappingManifestRefused(
                    f"the declared mapping names {heading!r}, which this "
                    "workbook heads no canonical column for"
                )
    prove_manifest(manifest)
    return manifest


def _default_mapping(
    heading: str, field: str, vocabularies: Mapping[str, tuple[tuple[str, ...], str | None]]
) -> MaterialMapping:
    allowed, reference = vocabularies.get(heading, ((), None))
    contract = FACT_TYPE_CONTRACTS.get(field)
    parser = "trim_cell_text_v1" if contract is None else contract.transformation
    precision = (
        CALENDAR_DAY
        if contract is not None and contract.value_class == "date"
        else SOURCE_STATED_UNIT
    )
    return MaterialMapping(
        source_columns=(heading,),
        target_fields=(field,),
        composition=ONE_VALUE_PER_COLUMN,
        parser=parser,
        delimiter=(
            DEFAULT_RANGE_DELIMITER
            if any(field in pair for pair in RANGE_ENDPOINTS)
            else None
        ),
        precision=precision,
        controlled_vocabulary=allowed,
        vocabulary_reference=reference,
        material=field in MATERIAL_FIELDS,
        retirement=(
            CARRIES_RETIREMENT_WORDING
            if field == "marked_resolution"
            else NOT_A_RETIREMENT_FIELD
        ),
    )


def _with_example(
    mapping: MaterialMapping, reading: OperationsReading
) -> MaterialMapping:
    """Bind the first populated row that exercises this mapping as its example."""

    if mapping.example:
        return mapping
    for row in reading.rows:
        cells = tuple(exact_text(row, field) for field in mapping.target_fields)
        if any(cells):
            return replace(mapping, example=cells)
    return mapping


def exact_text(row: BaselineRow, field: str) -> str:
    """This row's exact cell text under one canonical field, typed or not.

    A combined range under `Start Station` may or may not satisfy the field's
    released transformation, and the conformance check must see the cell either
    way — a value reported as untypeable is still the value the column holds.
    """

    typed = row.value(field)
    if typed is not None:
        return typed
    for item in row.unsupported:
        if item.field == field:
            return item.exact_text
    return ""


def _vocabularies_by_heading(
    reading: OperationsReading,
) -> dict[str, tuple[tuple[str, ...], str | None]]:
    return {
        item.heading: (tuple(item.allowed_values), item.reference)
        for item in reading.controlled_vocabularies
    }


# --- Proving and conforming -------------------------------------------------


def prove_manifest(manifest: FieldMappingManifest) -> None:
    """Technically validate one declaration, or refuse it.

    Every rule identity is released, every parser agrees with the released
    transformation the Fact contract already states, no field or heading is
    claimed twice, and every declared composition reproduces its own populated
    representative example through a full round trip.
    """

    if manifest.schema_version != MANIFEST_SCHEMA_VERSION:
        raise MappingManifestRefused(
            f"{manifest.schema_version!r} is not the released mapping-manifest "
            f"schema {MANIFEST_SCHEMA_VERSION!r}"
        )
    if not manifest.identity.strip() or not manifest.version.strip():
        raise MappingManifestRefused(
            "a mapping revision is named by an identity and a version"
        )
    if not manifest.mappings:
        raise MappingManifestRefused(
            "a mapping revision declares at least one mapping"
        )

    seen_fields: set[str] = set()
    seen_headings: set[str] = set()
    for mapping in manifest.mappings:
        rule = _rule(mapping.composition)
        rule.validate(mapping)
        if mapping.formatting not in FORMATTING_RULES:
            raise MappingManifestRefused(
                f"{mapping.formatting!r} is not a released formatting rule"
            )
        if mapping.blank_behaviour not in BLANK_BEHAVIOURS:
            raise MappingManifestRefused(
                f"{mapping.blank_behaviour!r} is not a released blank behaviour"
            )
        if mapping.retirement not in RETIREMENT_HANDLING:
            raise MappingManifestRefused(
                f"{mapping.retirement!r} is not a released retirement handling"
            )
        if mapping.precision is not None and mapping.precision not in PRECISIONS:
            raise MappingManifestRefused(
                f"{mapping.precision!r} is not a released unit or timing precision"
            )
        for field in mapping.target_fields:
            if field in seen_fields:
                raise MappingManifestRefused(
                    f"{field!r} is declared by two mappings; one canonical "
                    "field is carried by one mapping"
                )
            seen_fields.add(field)
            contract = FACT_TYPE_CONTRACTS.get(field)
            if contract is None:
                raise MappingManifestRefused(
                    f"{field!r} is not a canonical Project Record field"
                )
            if contract.transformation != mapping.parser:
                raise MappingManifestRefused(
                    f"{field!r} is captured by {contract.transformation!r}, and "
                    f"the mapping declares {mapping.parser!r}"
                )
        for heading in mapping.source_columns:
            if heading in seen_headings:
                raise MappingManifestRefused(
                    f"{heading!r} is declared by two mappings"
                )
            seen_headings.add(heading)

    for item in manifest.external_references:
        if item.role not in EXTERNAL_REFERENCE_ROLES:
            raise MappingManifestRefused(
                f"{item.role!r} is not an external-reference role; a customer "
                "mapping distinguishes the conflict-record identifier from the "
                "document link"
            )
        if item.heading in seen_headings:
            raise MappingManifestRefused(
                f"{item.heading!r} carries a canonical field and cannot also "
                "carry an external reference"
            )

    _prove_examples(manifest)


def _prove_examples(manifest: FieldMappingManifest) -> None:
    """The round trip: cells decompose to values, and those compose back."""

    populated = False
    for mapping in manifest.mappings:
        rule = _rule(mapping.composition)
        if not any(mapping.example):
            if mapping.composition != ONE_VALUE_PER_COLUMN:
                raise MappingManifestRefused(
                    f"{mapping.composition} over {mapping.source_columns} is a "
                    "declared reading of the customer's values and is proved "
                    "by a populated representative example; this mapping "
                    "carries none"
                )
            continue
        populated = True
        if len(mapping.example) != len(mapping.source_columns):
            raise MappingManifestRefused(
                f"the example for {mapping.source_columns} has "
                f"{len(mapping.example)} cells"
            )
        refusal = rule.refusal(mapping, mapping.example)
        if refusal is not None:
            raise MappingManifestRefused(
                f"the representative example contradicts the declaration: {refusal}"
            )
        values = rule.decompose(mapping, mapping.example)
        if len(values) != len(mapping.target_fields):
            raise MappingManifestRefused(
                f"the example for {mapping.source_columns} reads back "
                f"{len(values)} values for {len(mapping.target_fields)} fields"
            )
        again = rule.compose(mapping, values)
        if tuple(again) != tuple(mapping.example):
            raise MappingManifestRefused(
                "the representative example does not survive a round trip "
                f"through {mapping.composition}: {tuple(mapping.example)} reads "
                f"as {values} and writes back as {tuple(again)}"
            )
    if not populated:
        raise MappingManifestRefused(
            "a mapping revision is proved by a populated representative "
            "example, and this one carries no populated values at all"
        )


def conformance_refusals(
    manifest: FieldMappingManifest, reading: OperationsReading
) -> tuple[str, ...]:
    """Why this template is not the mapping revision the project registered.

    Empty when it is.  Every refusal is stated against the manifest, never
    against the template's own spelling: the template is the thing being
    checked, and it does not get to say what it means.
    """

    refusals: list[str] = []
    declared = {
        (heading, field)
        for mapping in manifest.mappings
        for heading, field in mapping.declared_columns
    }
    printed = {
        (column.heading, column.field) for column in reading.column_mapping
    }
    for heading, field in sorted(printed - declared):
        refusals.append(
            f"{heading!r} carries {field!r} in this template, which the "
            f"approved mapping revision {manifest.revision} does not declare"
        )
    for heading, field in sorted(declared - printed):
        refusals.append(
            f"the approved mapping revision {manifest.revision} declares "
            f"{heading!r} carrying {field!r}, which this template does not head"
        )
    refusals.extend(_vocabulary_refusals(manifest, reading))
    refusals.extend(_material_refusals(manifest, reading))
    if refusals:
        return tuple(refusals)
    return tuple(_composition_refusals(manifest, reading))


def _vocabulary_refusals(
    manifest: FieldMappingManifest, reading: OperationsReading
) -> list[str]:
    printed = _vocabularies_by_heading(reading)
    refusals: list[str] = []
    for mapping in manifest.mappings:
        for heading in mapping.source_columns:
            allowed, reference = printed.get(heading, ((), None))
            declared = (mapping.controlled_vocabulary, mapping.vocabulary_reference)
            if (allowed, reference) != declared:
                refusals.append(
                    f"{heading!r} declares the controlled vocabulary "
                    f"{list(allowed)}, and the approved mapping revision "
                    f"{manifest.revision} declares "
                    f"{list(mapping.controlled_vocabulary)}"
                )
    return refusals


def _material_refusals(
    manifest: FieldMappingManifest, reading: OperationsReading
) -> list[str]:
    printed = {column.field: column.material for column in reading.column_mapping}
    refusals: list[str] = []
    for mapping in manifest.mappings:
        for field in mapping.target_fields:
            if field in printed and printed[field] != mapping.material:
                refusals.append(
                    f"{field!r} is material in this template and "
                    f"{'material' if mapping.material else 'not material'} in "
                    f"the approved mapping revision {manifest.revision}"
                )
    return refusals


def _composition_refusals(
    manifest: FieldMappingManifest, reading: OperationsReading
) -> list[str]:
    """The check no digest over the template's bytes can make.

    Every heading and every drop-down may be identical and the values still
    split or combine differently from the mapping the project approved.  So the
    template's own populated rows are read through the declared composition,
    and a row that cannot be what the manifest says refuses the render.
    """

    refusals: list[str] = []
    for row in reading.rows:
        for mapping in manifest.mappings:
            cells = tuple(exact_text(row, field) for field in mapping.target_fields)
            refusal = _rule(mapping.composition).refusal(mapping, cells)
            if refusal is not None:
                refusals.append(
                    f"{row.sheet_name}!{row.row_number}: {refusal}. A template "
                    "that splits or combines a mapped value carries a different "
                    "mapping revision, which a person holding the "
                    "project-coordination designation registers."
                )
                return refusals
    return refusals
