"""Append human analytical findings without rewriting frozen shadow evidence.

#499 requires causes, handling outcomes and human resolution of ambiguity. The
pure comparison deliberately stops at differences from a working reference.
These content-addressed review receipts add attributable analytical judgments,
retain their predecessor chain and keep unresolved questions out of reviewed
rates. They cannot resolve a Proposed Delta or change a Reference Dataset.
"""

from collections import defaultdict
from datetime import datetime
from hashlib import sha256
import os
from pathlib import Path
import tempfile

from corridor import digests
from corridor.principals import HumanPrincipal


CLASSIFICATIONS = frozenset({"matched", "corridor_only", "customer_only", "ambiguous"})


# Retained encoding: content-addressed private artifacts on disk are named by
# a digest computed with non-ASCII escaped.
canonical_json = digests.ascii_escaped_json
artifact_digest = digests.ascii_escaped_sha256


def retain_private_artifact(directory, payload):
    """Publish one owner-only content-addressed file; never replace existing bytes."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = canonical_json(payload)
    digest = sha256(body).hexdigest()
    target = directory / f"{digest}.json"
    fd, temporary = tempfile.mkstemp(prefix=".shadow-artifact-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.is_symlink() or target.read_bytes() != body:
                raise ValueError("retained artifact has conflicting bytes")
            if target.stat().st_mode & 0o077:
                raise ValueError("retained artifact is not private")
    finally:
        os.unlink(temporary)
    return {"sha256": digest, "path": str(target.resolve())}


def _time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("review time must include its timezone")
    return result


def _finding(comparison, subject, field):
    if comparison.get("schema_version") != "shadow-comparison-v1":
        raise ValueError("review requires the original frozen shadow comparison")
    findings = [f for f in comparison["findings"] if f["subject"] == subject and f["field"] == field]
    if len(findings) != 1:
        raise ValueError("review must name one exact original comparison finding")
    return findings[0]


def record_finding_review(comparison, note, *, previous=None):
    """Validate one imported human review, optionally superseding an earlier review.

    Identity is attributable input, not authentication or record-write authority.
    Corrections append another receipt and must name the exact predecessor.
    """
    finding = _finding(comparison, note["subject"], note["field"])
    comparison_sha, finding_sha = artifact_digest(comparison), artifact_digest(finding)
    if note.get("comparison_sha256") != comparison_sha or note.get("finding_sha256") != finding_sha:
        raise ValueError("review does not bind the exact original comparison and finding")
    HumanPrincipal(note["reviewer"])
    at = _time(note["reviewed_at"])
    if at < _time(comparison["successor"]["seen_at"]):
        raise ValueError("review cannot predate the frozen reference")
    classification = note["classification"]
    if classification not in CLASSIFICATIONS:
        raise ValueError("review classification must retain the comparison vocabulary")
    references = note.get("evidence_references")
    if (not note.get("review_id") or not note.get("reason") or not isinstance(references, list)
            or not references or any(not isinstance(r, str) or not r.strip() for r in references)):
        raise ValueError("review needs its identity, reason and retained evidence references")
    if classification == "matched" and (not finding["predictions"] or not finding["reference_changed"]):
        raise ValueError("a match requires both a native prediction and reference change")
    if classification == "corridor_only" and (not finding["predictions"] or not note.get("handling_outcome")):
        raise ValueError("a Corridor-only finding needs its prediction and handling outcome")
    if classification == "customer_only" and (not finding["reference_changed"] or not note.get("cause")):
        raise ValueError("a customer-only miss needs its reference change and cause")
    prior_sha = None
    if previous is not None:
        _validate_receipt(comparison, previous)
        if (previous["subject"], previous["field"]) != (note["subject"], note["field"]):
            raise ValueError("review predecessor belongs to another finding")
        if at <= _time(previous["reviewed_at"]) or note["review_id"] == previous["review_id"]:
            raise ValueError("a review correction needs a later distinct act")
        prior_sha = artifact_digest(previous)
    return {"schema_version": "shadow-finding-review-v1", "comparison_sha256": comparison_sha,
        "finding_sha256": finding_sha, "review_id": note["review_id"], "subject": note["subject"], "field": note["field"],
        "reviewer": note["reviewer"], "reviewed_at": at.isoformat(), "classification": classification,
        "reason": note["reason"], "evidence_references": references, "cause": note.get("cause"),
        "handling_outcome": note.get("handling_outcome"), "supersedes_sha256": prior_sha}


def _validate_receipt(comparison, receipt):
    if receipt.get("schema_version") != "shadow-finding-review-v1":
        raise ValueError("unknown human review receipt")
    reconstructed = record_finding_review(comparison, receipt)
    expected = {**receipt, "supersedes_sha256": None}
    if reconstructed != expected:
        raise ValueError("review receipt changes its validated analytical fields")


def reconcile_findings(comparison, reviews):
    """Retain originals and complete review chains; expose unresolved rates explicitly."""
    if comparison.get("schema_version") != "shadow-comparison-v1":
        raise ValueError("reconciliation requires the original frozen shadow comparison")
    keys = [(f["subject"], f["field"]) for f in comparison["findings"]]
    if len(set(keys)) != len(keys):
        raise ValueError("comparison repeats one original subject/field finding")
    by_sha, identities, successors = {}, {}, {}
    for receipt in reviews:
        _validate_receipt(comparison, receipt)
        digest = artifact_digest(receipt)
        if receipt["review_id"] in identities and identities[receipt["review_id"]] != digest:
            raise ValueError("one review identity has conflicting receipts")
        identities[receipt["review_id"]] = digest
        by_sha[digest] = receipt
    grouped = defaultdict(list)
    for digest, receipt in by_sha.items():
        prior = receipt["supersedes_sha256"]
        if prior:
            if prior not in by_sha:
                raise ValueError("review correction is missing its retained predecessor")
            predecessor = by_sha[prior]
            if (receipt["subject"], receipt["field"]) != (predecessor["subject"], predecessor["field"]):
                raise ValueError("review correction changes its original finding")
            if _time(receipt["reviewed_at"]) <= _time(predecessor["reviewed_at"]):
                raise ValueError("review corrections must advance in time")
            if prior in successors and successors[prior] != digest:
                raise ValueError("conflicting review corrections require explicit reconciliation")
            successors[prior] = digest
        grouped[receipt["subject"], receipt["field"]].append(digest)
    rows = []
    for finding in comparison["findings"]:
        members = grouped[finding["subject"], finding["field"]]
        heads = [key for key in members if key not in successors]
        if len(heads) > 1:
            raise ValueError("independent reviews of one finding require an explicit predecessor chain")
        latest = by_sha[heads[0]] if heads else None
        classification = latest["classification"] if latest else finding["classification"]
        unresolved = classification == "ambiguous" or finding["human_review_required"] and latest is None
        rows.append({"original_finding": finding, "finding_sha256": artifact_digest(finding),
            "review_receipts": [by_sha[key] for key in sorted(members, key=lambda k: (_time(by_sha[k]["reviewed_at"]), k))],
            "latest_review_sha256": heads[0] if heads else None, "reviewed_classification": classification,
            "unresolved": unresolved})
    strata = {}
    for field in comparison["policy"]["fields"]:
        selected = [row for row in rows if row["original_finding"]["field"] == field]
        predictions = sum(bool(row["original_finding"]["predictions"]) for row in selected)
        reference = sum(row["original_finding"]["reference_changed"] for row in selected)
        unresolved = sum(row["unresolved"] for row in selected)
        matches = sum(row["reviewed_classification"] == "matched" and not row["unresolved"] for row in selected)
        strata[field] = {"matched_numerator": matches, "prediction_question_denominator": predictions,
            "reference_change_denominator": reference, "unresolved_finding_count": unresolved,
            "precision": matches/predictions if predictions and not unresolved else None,
            "recall": matches/reference if reference and not unresolved else None,
            "material": field in comparison["policy"]["material_fields"],
            "basis": "original working-reference matches plus attributable human analytical reconciliation; not semantic gold"}
    return {"schema_version": "shadow-reconciliation-v1", "measurement_type": "Extraction Measurement",
        "comparison_sha256": artifact_digest(comparison), "reference_dataset_id": comparison["reference_dataset_id"],
        "original_comparison": comparison, "working_reference_strata": comparison["strata"],
        "findings": rows, "reviewed_strata": strata, "source_population": comparison["source_population"],
        "unresolved_finding_count": sum(row["unresolved"] for row in rows),
        "limits": [*comparison["limits"], "Human analytical review does not resolve deltas, write the Project Record or authorize a policy.",
                   "Reviewer identity and evidence are imported assertions; this artifact does not authenticate a person."]}
