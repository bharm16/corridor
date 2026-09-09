"""The explicit contact columns carried by a registered UCM mapping (#562).

Contact cells cannot be interpreted as person/address pairs by spelling or
punctuation. A mapping names each column's role; an unmapped value stays unknown.
This value has no database dependency and preserves older manifest identities.
"""

from dataclasses import dataclass


CONTACT_FIELDS = (
    "source_contact_id", "organization_ref", "responsible_role", "person_name",
    "channel", "address", "effective_from", "effective_until", "external_system", "external_id",
)


@dataclass(frozen=True)
class ContactMapping:
    sheet_name: str
    header_row: int
    columns: tuple[tuple[str, str], ...]
    responsible_role: str | None = None

    def __post_init__(self):
        fields = [field for field, _ in self.columns]
        if (not self.sheet_name.strip() or self.header_row < 1
                or len(fields) != len(set(fields)) or not set(fields) <= set(CONTACT_FIELDS)
                or any(not heading.strip() for _, heading in self.columns)):
            raise ValueError("invalid project-contacts-v1 column mapping")
        if self.responsible_role is not None and not self.responsible_role.strip():
            raise ValueError("contact role constant cannot be blank")

    def as_payload(self):
        return {"schema_version": "project-contacts-v1", "sheet_name": self.sheet_name,
                "header_row": self.header_row, "columns": [list(pair) for pair in sorted(self.columns)],
                "responsible_role": self.responsible_role}

    @classmethod
    def from_payload(cls, value):
        if set(value) != {"schema_version", "sheet_name", "header_row", "columns", "responsible_role"} or value["schema_version"] != "project-contacts-v1":
            raise ValueError("unsupported contact mapping declaration")
        return cls(value["sheet_name"], value["header_row"], tuple(tuple(pair) for pair in value["columns"]), value["responsible_role"])
