"""Receipted deterministic later-UCM capture in an isolated shadow database.

This composes #561, #511 and #606 instead of translating Proposed Delta identity.
The baseline is adopted separately by a named human. Runtime accepts only the
worker login, checks its actual PostgreSQL capabilities, and cannot provision
its own isolation. Frozen payloads stay in the same transaction as native source
capture, so a retry after interruption either resumes the receipt or rolls back.
No model transport or record/release command is imported here.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

from sqlalchemy import select, text

from corridor.compatibility_intake import CompatibilityReceipt
from corridor.later_revision import capture_later_revision
from corridor.models import Project, ProposedDelta
from corridor.source_revision_declaration import RevisionDeclaration
from corridor.native_provider_boundary import CustomerAuthorization
from corridor.source_delivery import require_stored_envelope
from corridor.shadow_capabilities import ShadowRefused, verify_runtime
from corridor.shadow_receipts import verified_shadow_payload

VERSION = "shadow-ucm-v1"




def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value):
    return sha256(canonical_bytes(value)).hexdigest()


def provision_shadow_project(session, *, project_id, customer, environment, operator):
    """Owner-only bootstrap in a separate database, after human Adopt Baseline.

    Refuse a customer-routed database or any other project. This is intentionally
    not callable with runtime credentials and never adopts a baseline itself.
    """
    role = session.execute(text("select current_user, session_user")).one()
    if any(str(item).startswith("corridor_") for item in role):
        raise ShadowRefused("shadow provisioning requires the separate schema owner")
    if not all((customer, environment, operator)):
        raise ShadowRefused("customer, environment and bootstrap operator are required")
    if session.scalar(text("select count(*) from customer_environment_binding")):
        raise ShadowRefused("a customer-routed database cannot become a shadow environment")
    if session.scalar(text("select count(*) from projects where id <> :id"), {"id": project_id}):
        raise ShadowRefused("shadow processing requires a separate single-project database")
    if not session.scalar(text("select exists(select 1 from project_baseline_adoptions where project_id=:id)"), {"id": project_id}):
        raise ShadowRefused("a named human must adopt the shadow baseline first")
    for table in ("project_roster_entries", "release_preparation_requests", "release_candidates", "release_packages"):
        if session.scalar(text(f"select count(*) from {table} where project_id=:id"), {"id": project_id}):
            raise ShadowRefused("project already reaches a customer surface")
    # Inherited authority owners cannot be repaired with a database-local
    # revoke: SET ROLE would regain the owner's implicit privileges. Refuse
    # that deployment instead of changing cluster-wide membership.
    if session.scalar(text("""
        select exists(select 1 from pg_roles r where
          (r.rolsuper or r.rolcreaterole or r.rolcreatedb or r.rolbypassrls
           or r.rolname = 'corridor_fact_decision_writer'
           or r.oid in (select relowner from pg_class where oid in
             ('public.release_candidates'::regclass, 'public.release_packages'::regclass,
              'public.release_preparation_requests'::regclass)))
          and pg_has_role('corridor_worker', r.oid, 'MEMBER'))
    """)):
        raise ShadowRefused("worker inherits an authority owner; provision a separate non-authoritative login")
    # PostgreSQL function ACLs are database-local. Remove every decision-owner
    # command grant reachable directly, through PUBLIC or via SET ROLE, without
    # changing the cluster's role memberships or any other database's grants.
    session.execute(text("""
        do $$ declare command record; grantee record;
        begin
          for command in
            select p.oid::regprocedure as signature from pg_proc p
            join pg_namespace n on n.oid=p.pronamespace
            join pg_roles r on r.oid=p.proowner
            where n.nspname='public' and r.rolname='corridor_fact_decision_writer'
          loop
            execute format('revoke execute on function %s from public', command.signature);
            for grantee in select rolname from pg_roles
              where pg_has_role('corridor_worker', oid, 'MEMBER')
            loop
              execute format('revoke execute on function %s from %I', command.signature, grantee.rolname);
            end loop;
          end loop;
          for grantee in select rolname from pg_roles
            where pg_has_role('corridor_worker', oid, 'MEMBER')
          loop
            execute format('revoke insert, update, delete on public.release_candidates, public.release_packages, public.release_preparation_requests from %I', grantee.rolname);
          end loop;
          revoke insert, update, delete on public.release_candidates, public.release_packages,
            public.release_preparation_requests from public;
        end $$;
    """))
    values = {"id": project_id, "customer": customer, "environment": environment, "operator": operator}
    existing = session.execute(text("select customer, environment, bootstrap_operator from shadow_projects where project_id=:id"), values).first()
    if existing:
        if tuple(existing) != (customer, environment, operator):
            raise ShadowRefused("shadow provisioning identity is immutable")
        return
    session.execute(text("insert into shadow_projects(project_id, customer, environment, database_name, bootstrap_operator) values (:id,:customer,:environment,current_database(),:operator)"), values)




def _compatibility(receipt, *, source_sha256, customer, project, environment):
    raw = Path(receipt.stored_path).read_bytes()
    if sha256(raw).hexdigest() != receipt.receipt_sha256:
        raise ShadowRefused("compatibility receipt digest changed")
    payload = json.loads(raw)
    if (payload.get("kind") != "compatibility-intake-receipt"
        or payload.get("outcome") != "reported"
        or not payload.get("capability", {}).get("resolved")
        or payload.get("source", {}).get("content_sha256") != source_sha256
        or payload.get("run", {}).get("customer") != customer
        or payload.get("run", {}).get("project") != project):
        raise ShadowRefused("exact bytes lack a resolved compatibility receipt")
    # Compatibility and shadow may be separate isolated environments. Both
    # identities are retained; their equality is not asserted.
    if not payload.get("environment") or not environment:
        raise ShadowRefused("both lane environments must be named")
    return payload


def validate_shadow_input(*, compatibility_receipt, authorization, source_sha256,
                          customer, project_slug, environment, deletion_date, now=None):
    """Check authorization and exact compatibility before intake stores bytes."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or deletion_date <= now.date():
        raise ShadowRefused("aware time and future deletion are required")
    if (not isinstance(authorization, CustomerAuthorization)
        or not authorization.record_id or not authorization.signed_by
        or not authorization.signed_on or not authorization.retention_disclosed
        or "ucm" not in authorization.source_classes
        or "shadow-processing" not in authorization.purposes
        or "shadow" not in authorization.stages or authorization.customer != customer
        or project_slug not in authorization.projects or source_sha256 not in authorization.source_sha256s):
        raise ShadowRefused("signed authorization does not cover these bytes for shadow processing")
    compatibility = _compatibility(compatibility_receipt, source_sha256=source_sha256,
        customer=customer, project=project_slug, environment=environment)
    if date.fromisoformat(compatibility["deletion_date"]) < deletion_date:
        raise ShadowRefused("shadow retention exceeds compatibility authorization treatment")
    return compatibility


def run_shadow_ucm(session, *, project: Project, staged, envelope,
                   compatibility_receipt: CompatibilityReceipt,
                   authorization: CustomerAuthorization, customer: str,
                   environment: str, source_configuration: str, principal,
                   deletion_date: date, now: datetime | None = None,
                   is_complete_enumerative_source: bool = False,
                   row_accounting_sealed: bool = False):
    """Capture and freeze exact native deltas; repeated inputs return one receipt.

    All operations share the caller's transaction. The caller commits once and
    must never commit a caught failure. The lock serializes competing runs of
    this source configuration before native lifecycle state is frozen.
    """
    if type(is_complete_enumerative_source) is not bool or type(row_accounting_sealed) is not bool:
        raise ShadowRefused("source completeness and row accounting must be explicit booleans")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or deletion_date <= now.date() or not source_configuration:
        raise ShadowRefused("aware time, future deletion and source configuration are required")
    verify_runtime(session, project_id=project.id, customer=customer, environment=environment)
    compatibility = validate_shadow_input(compatibility_receipt=compatibility_receipt,
        authorization=authorization, source_sha256=staged.sha256, customer=customer,
        project_slug=project.slug, environment=environment, deletion_date=deletion_date, now=now)
    delivery = require_stored_envelope(session, envelope)
    if delivery.project_id != project.id or envelope.customer != customer:
        raise ShadowRefused("delivery is outside the shadow customer/project")
    identity = digest({"version": VERSION, "project": project.id, "environment": environment,
        "source_configuration": source_configuration, "delivery": delivery.id,
        "compatibility": compatibility_receipt.receipt_sha256,
        "authorization": authorization.record_id, "operator": principal.subject,
        "deletion_date": deletion_date.isoformat(),
        "is_complete_enumerative_source": is_complete_enumerative_source,
        "row_accounting_sealed": row_accounting_sealed})
    session.execute(text("select pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": f"shadow:{project.id}"})
    saved = session.execute(text("select payload, payload::text as payload_text, output_sha256 from shadow_runs where identity=:id"), {"id": identity}).first()
    if saved:
        return verified_shadow_payload(saved.payload, saved.payload_text, saved.output_sha256)
    # A delivery/configuration has one receipt. Distinct external delivery
    # versions are not conflated merely because their byte digests agree.
    if session.scalar(text("select count(*) from shadow_runs where project_id=:id and payload->>'delivery_id'=:delivery and payload->>'source_configuration'=:configuration"),
        {"id": project.id, "delivery": str(delivery.id), "configuration": source_configuration}):
        raise ShadowRefused("delivery already frozen under different run authorization or retention")
    # The shadow lane declares its own run's terms and names its own operator;
    # there is no confirmation screen here and no retained declaration row, so
    # the declaration is assembled from the arguments this run was authorized
    # with rather than read back (#825). The two flags stay independent here
    # because the run's identity above already digests both of them.
    capture = capture_later_revision(session, project=project, staged=staged,
        envelope=envelope,
        declaration=RevisionDeclaration(
            declared_by=principal,
            is_complete_enumerative_source=is_complete_enumerative_source,
            row_accounting_sealed=row_accounting_sealed,
        ))
    deltas = []
    for row in session.scalars(select(ProposedDelta).where(ProposedDelta.id.in_(capture.delta_ids)).order_by(ProposedDelta.id)):
        item = {column.name: getattr(row, column.name) for column in ProposedDelta.__table__.columns}
        item["created_at"] = row.created_at.isoformat()
        deltas.append(item)
    payload = {"version": VERSION, "identity": identity, "project_id": project.id,
        "customer": customer, "environment": environment, "source_configuration": source_configuration,
        "operator": principal.subject, "authorization": authorization.record_id,
        "deletion_date": deletion_date.isoformat(),
        "provider_posture": "deterministic-no-model", "ingress": envelope.channel,
        "source_sha256": staged.sha256, "delivery_id": delivery.id,
        "compatibility_receipt_sha256": compatibility_receipt.receipt_sha256,
        "compatibility_environment": compatibility["environment"],
        "document_id": capture.document_id, "fact_ids": list(capture.fact_ids),
        "accepted_baseline_revision": capture.accepted_baseline_revision,
        "mapping": asdict(capture.field_mapping),
        "accounting": capture.accounting.as_payload(),
        "is_complete_enumerative_source": is_complete_enumerative_source,
        "row_accounting_sealed": row_accounting_sealed,
        "deltas": deltas}
    # JSON normalization also rejects non-serializable native contract changes.
    payload = json.loads(canonical_bytes(payload))
    saved = session.execute(text("insert into shadow_runs(identity,project_id,payload,output_sha256) values (:id,:project,cast(:payload as jsonb),'') returning payload, payload::text as payload_text, output_sha256"),
        {"id": identity,"project": project.id,"payload": canonical_bytes(payload).decode()}).one()
    return verified_shadow_payload(saved.payload, saved.payload_text, saved.output_sha256)
