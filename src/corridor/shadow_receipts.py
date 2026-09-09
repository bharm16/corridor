"""Verify exact database-sealed shadow receipt bytes without reserializing JSON."""
from dataclasses import dataclass
from hashlib import sha256
import json

from sqlalchemy import text

from corridor.shadow_capabilities import ShadowRefused


def verified_shadow_payload(payload, payload_text, output_sha256):
    """Both the parsed value and retained bytes must describe the sealed object."""
    try:
        if sha256(payload_text.encode("utf-8")).hexdigest() != output_sha256:
            raise ValueError("digest differs")
        if json.loads(payload_text) != payload:
            raise ValueError("payload differs")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ShadowRefused("frozen output digest or payload mismatch") from exc
    return payload


@dataclass(frozen=True)
class FrozenShadowOutput:
    """Export the exact DB rendering alongside its verified parsed reading."""
    payload: dict
    payload_text: str
    output_sha256: str

    def __post_init__(self):
        verified_shadow_payload(self.payload, self.payload_text, self.output_sha256)


def read_shadow_run(session, identity):
    """One public frozen-output seam; no duplicated permanent JSON representation."""
    row = session.execute(text("select payload, payload::text as payload_text, output_sha256 from shadow_runs where identity=:identity"),
        {"identity": identity}).first()
    if row is None:
        raise ShadowRefused("shadow run does not exist")
    if row.payload.get("canonicalization") != "postgresql-jsonb-text-v1":
        raise ShadowRefused("unknown shadow output canonicalization")
    return FrozenShadowOutput(row.payload, row.payload_text, row.output_sha256)
