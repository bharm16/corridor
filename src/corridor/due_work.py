"""One supervised runtime for bounded operational work and crash recovery.

Feature-owned schedulers were rejected because they duplicate leases, retries,
clocks, and failure semantics while hiding production ownership. This module
keeps a small interface over one durable occurrence/claim/receipt lifecycle.
Persisted rows select only handlers in the server-owned registry; they can never
name imports, commands, arbitrary destinations, or model tools.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import re
from types import MappingProxyType
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from corridor.document_notifications import DOCUMENT_NOTIFICATION_HANDLER
from corridor.models import (
    Document,
    DueWorkOccurrence,
    DueWorkReceipt,
    DueWorkSchedule,
    ExtractionRun,
    Project,
    ProjectRosterEntry,
)
from corridor.notifications import (
    ASSIGNMENT_NOTIFICATION_HANDLER,
    DUE_ACTION_NOTIFICATION_HANDLER,
)
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal
from corridor.telemetry import correlation_scope, log_event


_LOG = logging.getLogger("corridor.due_work")

# The nominal interval between one schedule's occurrence slots. An operational
# reader judges worker freshness against it (`corridor.operational_health`);
# where a slot actually falls stays with `_due_slot`.
CADENCE_INTERVAL_SECONDS: Mapping[str, int] = MappingProxyType(
    {"hourly": 3600, "weekly": 604_800}
)

HANDLER_PROCESSING_HEALTH = "processing_health"
HANDLER_PROJECT_PROCESSING = "project_processing"
HANDLER_REVISION_RECONCILIATION = "revision_reconciliation"
HANDLER_LOCATION_DISCOVERY = "location_discovery"
# Deliver the one new-assignment interruption category through this runtime
# (#351). The delivery logic and this key live in ``corridor.notifications``;
# the runtime depends on that module, never the reverse.
HANDLER_ASSIGNMENT_NOTIFICATION = ASSIGNMENT_NOTIFICATION_HANDLER
# Derive and deliver the due-action categories — soon/past-due Next Action
# reminders, urgent-overdue escalation, and daily summaries — through this same
# runtime (#352). Same ownership rule as the assignment handler above.
HANDLER_DUE_ACTION_NOTIFICATION = DUE_ACTION_NOTIFICATION_HANDLER
HANDLER_EVENT_ADMISSION_REPROOF = "event_admission_reproof"
HANDLER_EVIDENCE_OUTCOME_CAPTURE = "evidence_outcome_capture"
# Retain a weekly Coordination Report reading and prepare its external PDF. The
# string matches ``report_publication.HANDLER_KEY``; the execution lives in that
# lower module, which the runtime imports lazily so no import cycle forms.
HANDLER_REPORT_PUBLICATION = "report_publication"
# Discover and deliver the two document-related interruption categories (#353):
# a Documentation Review that lost applicable support, and a source transition
# affecting a current Commitment or a relocation/removal/abandonment Constraint.
# The discovery and delivery logic and this key live in
# ``corridor.document_notifications``; the runtime depends on that module.
HANDLER_DOCUMENT_NOTIFICATION = DOCUMENT_NOTIFICATION_HANDLER
# The four recurring passes a live pilot needs (#488). Each string matches its
# lower module's ``HANDLER_KEY``; the execution lives there and the runtime
# imports it lazily so no import cycle forms.
HANDLER_CONNECTOR_POLLING = "connector_polling"
HANDLER_DELTA_GENERATION = "delta_generation"
HANDLER_REPORT_PREPARATION = "report_preparation"
HANDLER_RETENTION_SWEEP = "retention_sweep"
# The upper ceiling on sends one bounded delivery pass may attempt. A gate-7
# notification schedule must declare a positive request budget within this.
_ASSIGNMENT_NOTIFICATION_BUDGET_CEILING = 10_000
_DUE_ACTION_NOTIFICATION_BUDGET_CEILING = 10_000
_DOCUMENT_NOTIFICATION_BUDGET_CEILING = 10_000
# The upper safety ceiling for a declared model spend. A processing schedule
# must declare a positive budget (never a silent zero); it may not exceed this.
_PROJECT_PROCESSING_TOKEN_CEILING = 100_000_000
# The upper ceiling for the disposable clones one re-proof attempt may consume;
# the ADR-0050 replay provisions two policy clones plus a migration rehearsal.
_REPROOF_CLONE_BUDGET_CEILING = 8
_CONFIGURATION_VERSION = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_EXTRACTOR_IDENTITY = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_POLICY_IDENTITY = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_COHORT_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_LOCATION_IDENTITY = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
# A location host is a bare DNS name; the adapter reaches only these and follows
# no redirect off them (#350). No scheme, port, path, or wildcard is accepted.
_LOCATION_HOST = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,62})(\.[a-z0-9]([a-z0-9-]{0,62}))+$")
_RUNTIME_OWNER = re.compile(r"^runtime:[A-Za-z0-9][A-Za-z0-9._:-]{1,119}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DueWorkRefusal(ValueError):
    """A schedule, claim, result, or runtime identity is unsafe."""


class StaleDueWorkClaim(DueWorkRefusal):
    """A worker tried to finalize an occurrence it no longer owns."""


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class ProcessingHealthDeclaration:
    project_id: int
    configuration_version: str
    starts_at: datetime
    cadence: str
    timezone_name: str
    missed_run_policy: str
    retention_days: int
    max_attempts: int
    backoff_seconds: int
    claim_ttl_seconds: int
    deadline_seconds: int
    concurrency_limit: int
    model_token_budget: int
    notification_budget: int

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        starts_at: datetime,
    ) -> "ProcessingHealthDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=60,
            claim_ttl_seconds=300,
            deadline_seconds=120,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
        )


@dataclass(frozen=True)
class ProjectProcessingDeclaration:
    """One validated gate-7 declaration that enables project processing.

    Processing reads models, so its bounds differ from the read-only health
    handler: the claim lease and deadline are longer, and a positive model
    spending budget must be declared explicitly rather than left at a silent
    zero. Scope names the exact project and the deployed extractor identity;
    authorized destinations stay empty and concurrency stays one.
    """

    project_id: int
    configuration_version: str
    extractor_identity: str
    starts_at: datetime
    cadence: str
    timezone_name: str
    missed_run_policy: str
    retention_days: int
    max_attempts: int
    backoff_seconds: int
    claim_ttl_seconds: int
    deadline_seconds: int
    concurrency_limit: int
    model_token_budget: int
    notification_budget: int

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        extractor_identity: str,
        starts_at: datetime,
        model_token_budget: int = 2_000_000,
    ) -> "ProjectProcessingDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            extractor_identity=extractor_identity,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=120,
            claim_ttl_seconds=1800,
            deadline_seconds=1800,
            concurrency_limit=1,
            model_token_budget=model_token_budget,
            notification_budget=0,
        )


@dataclass(frozen=True)
class RevisionReconciliationDeclaration:
    """One validated gate-7 declaration that enables automatic revision work.

    Revision reconciliation is deterministic — it converges on an already-produced
    exact comparison and applies the released Automatic Support Update Rules — so
    it authorizes no model spending: its ``model_token_budget`` must be a declared
    zero, never a silent one, and its resource bounds match the read-only health
    handler rather than the model-spending processing pass. Scope names the exact
    project and the two policy identities the pass is bound to: the comparison
    ``matcher_identity`` and the support-transfer ``support_rule_identity``.
    Authorized destinations stay empty and concurrency stays one.
    """

    project_id: int
    configuration_version: str
    matcher_identity: str
    support_rule_identity: str
    starts_at: datetime
    cadence: str
    timezone_name: str
    missed_run_policy: str
    retention_days: int
    max_attempts: int
    backoff_seconds: int
    claim_ttl_seconds: int
    deadline_seconds: int
    concurrency_limit: int
    model_token_budget: int
    notification_budget: int

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        matcher_identity: str,
        support_rule_identity: str,
        starts_at: datetime,
    ) -> "RevisionReconciliationDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            matcher_identity=matcher_identity,
            support_rule_identity=support_rule_identity,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=120,
            claim_ttl_seconds=600,
            deadline_seconds=300,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
        )


@dataclass(frozen=True)
class LocationDiscoveryDeclaration:
    """One validated gate-7 declaration that enables one connected location (#350).

    The adapter discovers and fetches; it reads no model, so ``model_token_budget``
    is a declared zero (extraction of a registered document is the separate
    project-processing pass). Scope names the one exact location the adapter may
    reach — its stable ``location_id``, the deployed ``adapter_identity``, the
    registered source/manifest identity, the concrete ``index_url``, and the
    authorized host boundary no redirect may leave — plus the sealed-holdout flag
    and every archive/resource limit. Missing or invalid configuration leaves the
    adapter refused; credentials never widen this scope, and authorized
    destinations stay empty.
    """

    project_id: int
    configuration_version: str
    location_id: str
    adapter_identity: str
    source_manifest_id: str
    index_url: str
    authorized_hosts: tuple[str, ...]
    sealed: bool
    nested_archive_depth: int
    max_archive_compressed_mib: int
    max_member_decompressed_mib: int
    enumeration_limit: int
    request_limit: int
    document_limit: int
    starts_at: datetime
    cadence: str
    timezone_name: str
    missed_run_policy: str
    retention_days: int
    max_attempts: int
    backoff_seconds: int
    claim_ttl_seconds: int
    deadline_seconds: int
    concurrency_limit: int
    model_token_budget: int
    notification_budget: int
    rid_link_text: str | None = None

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        location_id: str,
        adapter_identity: str,
        source_manifest_id: str,
        index_url: str,
        authorized_hosts: tuple[str, ...],
        sealed: bool = False,
        starts_at: datetime,
        rid_link_text: str | None = None,
    ) -> "LocationDiscoveryDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            location_id=location_id,
            adapter_identity=adapter_identity,
            source_manifest_id=source_manifest_id,
            index_url=index_url,
            authorized_hosts=authorized_hosts,
            sealed=sealed,
            nested_archive_depth=2,
            max_archive_compressed_mib=512,
            max_member_decompressed_mib=128,
            enumeration_limit=500,
            request_limit=200,
            document_limit=100,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=120,
            claim_ttl_seconds=600,
            deadline_seconds=300,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
            rid_link_text=rid_link_text,
        )


@dataclass(frozen=True)
class AssignmentNotificationDeclaration:
    """One validated gate-7 declaration that enables assignment-notification delivery.

    Delivery reads no model, so its ``model_token_budget`` must be a declared
    zero; instead it declares a positive ``notification_budget`` — the request
    budget bounding how many sends one bounded pass may attempt. The declaration
    also records the project and channel scope, dispatch cadence and timezone,
    retry budget, missed-run handling, and retention. Missing or invalid
    configuration leaves delivery refused and disabled; credentials or a wired
    provider alone never enable it. The recipient/contact mapping is fixed for
    this slice: a recipient resolves only through the verified-contact record for
    the selected roster identity's principal.
    """

    project_id: int
    configuration_version: str
    channel: str
    starts_at: datetime
    cadence: str
    timezone_name: str
    missed_run_policy: str
    retention_days: int
    max_attempts: int
    backoff_seconds: int
    claim_ttl_seconds: int
    deadline_seconds: int
    concurrency_limit: int
    model_token_budget: int
    notification_budget: int

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        starts_at: datetime,
        channel: str = "email",
        notification_budget: int = 500,
    ) -> "AssignmentNotificationDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            channel=channel,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=60,
            claim_ttl_seconds=300,
            deadline_seconds=120,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=notification_budget,
        )


@dataclass(frozen=True)
class DueActionNotificationDeclaration:
    """One validated gate-7 declaration that enables due-action delivery (#352).

    This extends the assignment-notification declaration to the three derived
    categories.  Beyond project/channel scope, cadence, timezone and date
    boundary, retry, missed-run and retention, it declares the parameters the
    derivation cannot invent: the ``urgent_overdue_days`` criterion for
    escalation, the single ``escalation_roster_entry_id`` contact (``None`` leaves
    escalation disabled and visible — never a substitute contact or an invented
    urgency rule), and the daily-summary window (``summary_hour_utc`` and
    ``summary_window_days``).  Like the assignment handler it reads no model, so
    ``model_token_budget`` must be a declared zero and it declares a positive
    ``notification_budget``.  Missing or invalid configuration leaves delivery
    refused and disabled; a recipient still resolves only through the verified
    contact for the accountable roster identity.
    """

    project_id: int
    configuration_version: str
    channel: str
    urgent_overdue_days: int
    escalation_roster_entry_id: int | None
    summary_hour_utc: int
    summary_window_days: int
    starts_at: datetime
    cadence: str
    timezone_name: str
    missed_run_policy: str
    retention_days: int
    max_attempts: int
    backoff_seconds: int
    claim_ttl_seconds: int
    deadline_seconds: int
    concurrency_limit: int
    model_token_budget: int
    notification_budget: int

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        starts_at: datetime,
        urgent_overdue_days: int = 3,
        escalation_roster_entry_id: int | None = None,
        summary_hour_utc: int = 13,
        summary_window_days: int = 1,
        channel: str = "email",
        notification_budget: int = 500,
    ) -> "DueActionNotificationDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            channel=channel,
            urgent_overdue_days=urgent_overdue_days,
            escalation_roster_entry_id=escalation_roster_entry_id,
            summary_hour_utc=summary_hour_utc,
            summary_window_days=summary_window_days,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=60,
            claim_ttl_seconds=300,
            deadline_seconds=120,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=notification_budget,
        )


@dataclass(frozen=True)
class DocumentNotificationDeclaration:
    """One validated gate-7 declaration that enables document-notification delivery.

    Delivery reads no model, so its ``model_token_budget`` must be a declared
    zero; instead it declares a positive ``notification_budget`` — the request
    budget bounding how many sends one bounded pass may attempt. The declaration
    records the project and channel scope, dispatch cadence and timezone, retry
    budget, missed-run handling, and retention. Missing or invalid configuration
    leaves delivery refused and disabled; a committed transition still registers
    its durable dispatch, but nothing is delivered until an authorized operator
    records this gate-7 scope. Project, source, and subject scope are fixed for
    this slice: the two approved categories over the recorded affected population,
    reaching the typed current assignee and the original reviewer through their
    verified-contact records only.
    """

    project_id: int
    configuration_version: str
    channel: str
    starts_at: datetime
    cadence: str
    timezone_name: str
    missed_run_policy: str
    retention_days: int
    max_attempts: int
    backoff_seconds: int
    claim_ttl_seconds: int
    deadline_seconds: int
    concurrency_limit: int
    model_token_budget: int
    notification_budget: int

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        starts_at: datetime,
        channel: str = "email",
        notification_budget: int = 500,
    ) -> "DocumentNotificationDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            channel=channel,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=60,
            claim_ttl_seconds=600,
            deadline_seconds=300,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=notification_budget,
        )


@dataclass(frozen=True)
class EventAdmissionReproofDeclaration:
    """One validated gate-7 declaration that enables stale-class re-proof recovery.

    Re-proof replays the policy's own recorded history (ADR-0050): it consults no
    model, so ``model_token_budget`` must be a declared zero, never a silent one, and
    it delivers nothing outward, so ``notification_budget`` is zero too. Scope names
    the exact project and the three bound identities the replay is pinned to — the
    unknown-scope ``policy_version``, its ``reason_version``, and the approved replay
    population ``selection_rule`` (#324). ``clone_budget`` bounds the disposable clones
    one attempt may consume. The lease and deadline are generous because a real replay
    copies the database and rehearses migrations; concurrency stays one.
    """

    project_id: int
    configuration_version: str
    policy_version: str
    reason_version: str
    selection_rule: str
    starts_at: datetime
    cadence: str
    timezone_name: str
    missed_run_policy: str
    retention_days: int
    max_attempts: int
    backoff_seconds: int
    claim_ttl_seconds: int
    deadline_seconds: int
    concurrency_limit: int
    model_token_budget: int
    notification_budget: int
    clone_budget: int

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        policy_version: str,
        reason_version: str,
        selection_rule: str,
        starts_at: datetime,
    ) -> "EventAdmissionReproofDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            policy_version=policy_version,
            reason_version=reason_version,
            selection_rule=selection_rule,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=2,
            backoff_seconds=300,
            claim_ttl_seconds=1800,
            deadline_seconds=1800,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
            clone_budget=3,
        )




@dataclass(frozen=True)
class EvidenceOutcomeCaptureDeclaration:
    """One validated gate-7 declaration that enables cutoff-correct capture.

    The domain half of the contract — the exact frozen cohort, the sealed model
    and prompt identities, the observation window, cutoff, protection end, and
    retained-history coverage — is declared and validated in
    ``evidence_investigator_capture`` and content-addressed by
    ``observation_contract_sha256``. This schedule carries only the operational
    envelope plus that pointer, so the occurrence identity binds the project,
    cohort, and observation contract (and therefore its intended cutoff and
    membership). Capture reads no model, so ``model_token_budget`` must be a
    declared zero; it commits only its own association records, so authorized
    destinations stay empty and concurrency stays one.
    """

    project_id: int
    configuration_version: str
    cohort_id: str
    observation_contract_sha256: str
    starts_at: datetime
    cadence: str
    timezone_name: str
    missed_run_policy: str
    retention_days: int
    max_attempts: int
    backoff_seconds: int
    claim_ttl_seconds: int
    deadline_seconds: int
    concurrency_limit: int
    model_token_budget: int
    notification_budget: int

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        cohort_id: str,
        observation_contract_sha256: str,
        starts_at: datetime,
    ) -> "EvidenceOutcomeCaptureDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            cohort_id=cohort_id,
            observation_contract_sha256=observation_contract_sha256,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=120,
            claim_ttl_seconds=600,
            deadline_seconds=300,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
        )



@dataclass(frozen=True)
class ReportPublicationDeclaration:
    """One validated gate-7 declaration that enables scheduled report publication.

    Publication renders a report and, when declared, prepares one external PDF;
    it reads no model, so ``model_token_budget`` must be a declared zero and the
    schedule is weekly rather than hourly.  Scope names the exact project, the
    provenance mode the reading and any PDF are taken under, and whether an
    external PDF is prepared for later human release — the output identity.  The
    comparison predecessor is the last released report (ADR-0053), declared
    explicitly so no replacement policy is chosen implicitly.  Authorized
    destinations stay empty, concurrency stays one, and the notification budget
    stays zero: this slice adds no delivery and no new notification category.
    """

    project_id: int
    configuration_version: str
    provenance_mode: str
    prepare_external_pdf: bool
    starts_at: datetime
    cadence: str
    timezone_name: str
    missed_run_policy: str
    comparison_window_policy: str
    retention_days: int
    max_attempts: int
    backoff_seconds: int
    claim_ttl_seconds: int
    deadline_seconds: int
    concurrency_limit: int
    model_token_budget: int
    notification_budget: int

    @classmethod
    def released_weekly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        starts_at: datetime,
        provenance_mode: str = "all-supported-sources",
        prepare_external_pdf: bool = True,
    ) -> "ReportPublicationDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            provenance_mode=provenance_mode,
            prepare_external_pdf=prepare_external_pdf,
            starts_at=starts_at,
            cadence="weekly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            comparison_window_policy="since_last_released",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=120,
            claim_ttl_seconds=1800,
            deadline_seconds=1800,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
        )


@dataclass(frozen=True)
class ConnectorPollingDeclaration:
    """One validated gate-7 declaration that enables connector checkpoint polling.

    Scope names the exact normalized ingress identity of #496: the customer and
    channel that, with the project and the item's own identity and version,
    make the delivery identity ADR-0083 fixes, plus the server-owned connector
    the schedule may build and the one location it may reach.  Polling reads no
    model, sends nothing, and writes only content-addressed bytes, so the model
    and notification budgets must both be a declared zero and no destination is
    authorized.
    """

    project_id: int
    configuration_version: str
    customer: str
    channel: str
    connector_identity: str
    source_url: str
    starts_at: datetime
    cadence: str
    timezone_name: str
    missed_run_policy: str
    retention_days: int
    max_attempts: int
    backoff_seconds: int
    claim_ttl_seconds: int
    deadline_seconds: int
    concurrency_limit: int
    model_token_budget: int
    notification_budget: int

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        customer: str,
        channel: str,
        connector_identity: str,
        source_url: str,
        starts_at: datetime,
    ) -> "ConnectorPollingDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            customer=customer,
            channel=channel,
            connector_identity=connector_identity,
            source_url=source_url,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=120,
            claim_ttl_seconds=900,
            deadline_seconds=600,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
        )


@dataclass(frozen=True)
class DeltaGenerationDeclaration:
    """One validated gate-7 declaration that enables Proposed Delta generation.

    Scope names the project and the comparison rule the pass appends under, so
    a later change to how values are compared is visible on every delta rather
    than retroactive.  The pass reads no model and appends only through the
    source-append command, so both budgets are a declared zero, concurrency
    stays one, and no destination is authorized: it can propose a difference
    and can never make one effective.
    """

    project_id: int
    configuration_version: str
    comparison_rule_version: str
    starts_at: datetime
    cadence: str
    timezone_name: str
    missed_run_policy: str
    retention_days: int
    max_attempts: int
    backoff_seconds: int
    claim_ttl_seconds: int
    deadline_seconds: int
    concurrency_limit: int
    model_token_budget: int
    notification_budget: int

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        comparison_rule_version: str,
        starts_at: datetime,
    ) -> "DeltaGenerationDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            comparison_rule_version=comparison_rule_version,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=120,
            claim_ttl_seconds=900,
            deadline_seconds=600,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
        )


@dataclass(frozen=True)
class ReportPreparationDeclaration:
    """One validated gate-7 declaration that enables the weekly change reading.

    The reading counts one project's Proposed Delta lifecycle over the week and
    writes nothing, so it is weekly rather than hourly, reads no model, and
    authorizes no destination: what the change summary and weekly report are
    rendered from is a reading, and releasing either stays a separate
    designated-human act (ADR-0040, ADR-0086).
    """

    project_id: int
    configuration_version: str
    starts_at: datetime
    cadence: str
    timezone_name: str
    missed_run_policy: str
    retention_days: int
    max_attempts: int
    backoff_seconds: int
    claim_ttl_seconds: int
    deadline_seconds: int
    concurrency_limit: int
    model_token_budget: int
    notification_budget: int

    @classmethod
    def released_weekly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        starts_at: datetime,
    ) -> "ReportPreparationDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            starts_at=starts_at,
            cadence="weekly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=120,
            claim_ttl_seconds=600,
            deadline_seconds=300,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
        )


@dataclass(frozen=True)
class RetentionSweepDeclaration:
    """One validated gate-7 declaration that enables the Class B TTL sweep.

    ``authorized_by`` is the named person with authority under the customer
    relationship who authorized this standing sweep (ADR-0080).  It is a
    ``HumanPrincipal`` subject, not a role label, and it is the actor recorded
    on every dry-run manifest and deletion receipt the sweep produces, so an
    automatic expiry is still attributable to somebody.  The sweep reads no
    model and sends nothing; it is weekly because the Class B TTL is measured
    in days, not hours.
    """

    project_id: int
    configuration_version: str
    authorized_by: str
    starts_at: datetime
    cadence: str
    timezone_name: str
    missed_run_policy: str
    retention_days: int
    max_attempts: int
    backoff_seconds: int
    claim_ttl_seconds: int
    deadline_seconds: int
    concurrency_limit: int
    model_token_budget: int
    notification_budget: int

    @classmethod
    def released_weekly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        authorized_by: str,
        starts_at: datetime,
    ) -> "RetentionSweepDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            authorized_by=authorized_by,
            starts_at=starts_at,
            cadence="weekly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=300,
            claim_ttl_seconds=1800,
            deadline_seconds=1800,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
        )


@dataclass(frozen=True)
class DueWorkClaim:
    occurrence_id: int
    occurrence_public_id: str
    schedule_id: int
    attempt_number: int
    attempt_id: str
    claim_token: str
    runtime_owner: str
    started_at: datetime
    lease_expires_at: datetime
    deadline_at: datetime


@dataclass(frozen=True)
class DueWorkRunResult:
    occurrence_id: int
    receipt_id: int
    occurrence_public_id: str
    receipt_public_id: str
    job_public_id: str
    attempt_id: str
    project_id: int
    handler_key: str
    configuration_version: str
    input_identity_sha256: str
    execution_outcome: str
    handler_result: dict[str, Any] | None
    error_code: str | None
    safe_next_step: str


@dataclass(frozen=True)
class HandlerContract:
    key: str
    scope_kind: str
    idempotency_contract: str
    max_result_bytes: int
    model_token_budget: int
    notification_budget: int
    declaration_type: type | None = None
    configure: Callable[[Session, object, datetime], DueWorkSchedule] | None = None
    # A read-only handler runs inside a `reading` session and returns a result
    # dict the runtime finalizes. An effectful handler owns its own durable
    # commits and instead takes an `EffectfulContext`; exactly one is set.
    run: Callable[[Session, DueWorkSchedule, datetime], dict[str, Any]] | None = None
    run_effectful: Callable[["EffectfulContext"], dict[str, Any]] | None = None


@dataclass(frozen=True)
class EffectfulContext:
    """What an effectful handler needs to own its durable work and be recovered.

    The handler commits its domain work through ``session_factory`` before the
    runtime finalizes the claim, so a stale-claim or deadline failure at finalize
    only re-runs work that is already durable and idempotent.
    """

    session_factory: Any
    schedule: DueWorkSchedule
    claim: "DueWorkClaim"
    clock: "Clock"
    registry: Mapping[str, "HandlerContract"]


def _processing_health(
    session: Session,
    schedule: DueWorkSchedule,
    observed_at: datetime,
) -> dict[str, Any]:
    project_id = schedule.project_id
    document_count = int(
        session.scalar(
            select(func.count()).select_from(Document).where(
                Document.project_id == project_id
            )
        )
        or 0
    )
    failed_document_count = int(
        session.scalar(
            select(func.count()).select_from(Document).where(
                Document.project_id == project_id,
                Document.parse_status == "failed",
            )
        )
        or 0
    )
    failed_extraction_count = int(
        session.scalar(
            select(func.count())
            .select_from(ExtractionRun)
            .join(Document, Document.id == ExtractionRun.document_id)
            .where(
                Document.project_id == project_id,
                or_(
                    ExtractionRun.outcome != "completed",
                    ExtractionRun.page_errors > 0,
                ),
            )
        )
        or 0
    )
    health = (
        "healthy"
        if failed_document_count == 0 and failed_extraction_count == 0
        else "processing_failures_observed"
    )
    return {
        "schema_version": "processing-health-result-v1",
        "project_id": project_id,
        "configuration_version": schedule.configuration_version,
        "observed_at": _iso(observed_at),
        "health": health,
        "document_count": document_count,
        "failed_document_count": failed_document_count,
        "failed_extraction_count": failed_extraction_count,
    }


def _project_processing_effectful(context: EffectfulContext) -> dict[str, Any]:
    """Run the bounded project-processing pass for a claimed occurrence.

    The pass commits extraction and Record Inclusion durably through the
    session factory; this wrapper only adapts the runtime's claim into a
    ``process_project`` call over the currently deployed extraction routes and
    summarizes the result into a bounded receipt.
    """

    from corridor.llm import OpenAIClient
    from corridor.pipeline import extraction_route
    from corridor.project_processing import process_project, summarize_pass

    with context.session_factory() as reading:
        schedule = reading.get(DueWorkSchedule, context.claim.schedule_id)
        if schedule is None:
            raise DueWorkRefusal("Due Work schedule disappeared")
        project_id = schedule.project_id
        configuration_version = schedule.configuration_version

    client = OpenAIClient()
    try:
        result = process_project(
            context.session_factory,
            project_id=project_id,
            select_route=lambda document: extraction_route(document, client=client),
            clock=context.clock,
        )
    finally:
        client.close()
    return summarize_pass(
        result,
        configuration_version=configuration_version,
        observed_at=_aware_utc(context.clock.now()),
    )


def _revision_reconciliation_effectful(context: EffectfulContext) -> dict[str, Any]:
    """Run the bounded revision reconciliation for a claimed occurrence.

    The pass commits its Revision Comparisons, released support updates, and the
    downstream Record Inclusion handoff durably through the session factory; this
    wrapper only adapts the runtime's claim into a ``reconcile_project_revisions``
    call over the declared matcher identity and summarizes the result into a
    bounded receipt. It reads no model.
    """

    from corridor.revision_reconciliation import (
        reconcile_project_revisions,
        summarize_revision_reconciliation,
    )

    with context.session_factory() as reading:
        schedule = reading.get(DueWorkSchedule, context.claim.schedule_id)
        if schedule is None:
            raise DueWorkRefusal("Due Work schedule disappeared")
        project_id = schedule.project_id
        configuration_version = schedule.configuration_version
        matcher_identity = schedule.scope_json.get("matcher_identity", "")

    result = reconcile_project_revisions(
        context.session_factory,
        project_id=project_id,
        matcher_version=matcher_identity,
        clock=context.clock,
    )
    return summarize_revision_reconciliation(
        result,
        configuration_version=configuration_version,
        observed_at=_aware_utc(context.clock.now()),
    )


def _location_discovery_effectful(context: EffectfulContext) -> dict[str, Any]:
    """Run one bounded discovery-and-fetch pass for a claimed occurrence (#350).

    The pass commits its discovered references, fetch attempts, and any registered
    Documents durably through the session factory; this wrapper only rebuilds the
    server-owned location scope and resource budgets from the persisted schedule,
    runs the pass over an ordinary HTTP client, and summarizes the result. It reads
    no model — a registered document is handed to the standing project-processing
    pass. Its own HTTP client is closed before the runtime finalizes the claim.
    """

    import httpx

    from corridor.location_discovery import (
        DiscoveryBudgets,
        LocationScope,
        discover_and_process,
        summarize_discovery_pass,
    )

    with context.session_factory() as reading:
        schedule = reading.get(DueWorkSchedule, context.claim.schedule_id)
        if schedule is None:
            raise DueWorkRefusal("Due Work schedule disappeared")
        project_id = schedule.project_id
        configuration_version = schedule.configuration_version
        scope_json = dict(schedule.scope_json)
        deadline_seconds = schedule.deadline_seconds

    scope = LocationScope(
        location_id=scope_json["location_id"],
        project_id=project_id,
        index_url=scope_json["index_url"],
        authorized_hosts=frozenset(scope_json["authorized_hosts"]),
        sealed=bool(scope_json.get("sealed")),
        source_manifest_id=scope_json["source_manifest_id"],
        adapter_identity=scope_json["adapter_identity"],
        rid_link_text=scope_json.get("rid_link_text"),
    )
    budgets = DiscoveryBudgets(
        max_references=scope_json["enumeration_limit"],
        max_requests=scope_json["request_limit"],
        max_documents=scope_json["document_limit"],
        nested_archive_depth=scope_json["nested_archive_depth"],
        max_compressed_bytes=scope_json["max_archive_compressed_mib"] * 1024 * 1024,
        max_decompressed_bytes=scope_json["max_member_decompressed_mib"] * 1024 * 1024,
        time_budget_seconds=deadline_seconds,
    )
    client = httpx.Client(timeout=60.0)
    try:
        result = discover_and_process(
            context.session_factory,
            scope=scope,
            budgets=budgets,
            client=client,
            clock=context.clock,
        )
    finally:
        client.close()
    return summarize_discovery_pass(
        result,
        configuration_version=configuration_version,
        observed_at=_aware_utc(context.clock.now()),
    )


def _assignment_notification_effectful(context: EffectfulContext) -> dict[str, Any]:
    """Deliver a project's due assignment notifications for a claimed occurrence.

    The pass commits each dispatch outcome durably through the session factory
    and holds no transaction across the provider call; this wrapper only adapts
    the runtime's claim into a ``deliver_project_assignment_notifications`` call
    over the declared channel and request budget, and summarizes it into a
    bounded receipt. It reads no model. The channel's adapter is resolved from
    the notifications seam, which defaults to a non-sending adapter so completing
    the code enables no real delivery.
    """

    from corridor import notifications

    with context.session_factory() as reading:
        schedule = reading.get(DueWorkSchedule, context.claim.schedule_id)
        if schedule is None:
            raise DueWorkRefusal("Due Work schedule disappeared")
        project_id = schedule.project_id
        configuration_version = schedule.configuration_version
        channel = schedule.scope_json.get("channel", "")
        max_attempts = schedule.max_attempts
        backoff_seconds = schedule.backoff_seconds
        budget = schedule.notification_budget

    adapter = notifications.resolve_delivery_adapter(channel)
    return notifications.deliver_project_assignment_notifications(
        context.session_factory,
        project_id=project_id,
        configuration_version=configuration_version,
        channel=channel,
        adapter=adapter,
        clock=context.clock,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        budget=budget,
        owner=context.claim.runtime_owner,
    )


def _due_action_notification_effectful(context: EffectfulContext) -> dict[str, Any]:
    """Derive and deliver a project's due-action notifications for one tick (#352).

    Each claimed tick re-derives the current soon/past-due conditions for the
    whole subject population and converges them onto durable occurrences, then
    sweeps the queued dispatches through the adapter — committing each dispatch
    outcome durably and holding no transaction across the provider call.  The
    daily summary is registered only on the one declared summary hour, keyed to
    its exposed window, so a late or repeated tick neither backfills a missed day
    nor replays history.  It reads no model, and the adapter defaults to a
    non-sending capture so completing the code enables no real delivery.
    """

    from corridor import notifications

    with context.session_factory() as reading:
        schedule = reading.get(DueWorkSchedule, context.claim.schedule_id)
        if schedule is None:
            raise DueWorkRefusal("Due Work schedule disappeared")
        project_id = schedule.project_id
        configuration_version = schedule.configuration_version
        scope = dict(schedule.scope_json)
        channel = scope.get("channel", "")
        urgent_overdue_days = scope.get("urgent_overdue_days")
        escalation_roster_entry_id = scope.get("escalation_roster_entry_id")
        summary_hour_utc = scope.get("summary_hour_utc")
        summary_window_days = scope.get("summary_window_days")
        max_attempts = schedule.max_attempts
        backoff_seconds = schedule.backoff_seconds
        budget = schedule.notification_budget

    now = _aware_utc(context.clock.now())
    today = now.date()
    register_summary = now.hour == summary_hour_utc
    summary_window_end = today
    summary_window_start = today - timedelta(days=summary_window_days - 1)

    with context.session_factory() as registering:
        with registering.begin():
            notifications.register_due_action_notifications(
                registering,
                project_id=project_id,
                configuration_version=configuration_version,
                today=today,
                urgent_overdue_days=urgent_overdue_days,
                escalation_roster_entry_id=escalation_roster_entry_id,
                channel=channel,
                owner=context.claim.runtime_owner,
                register_summary=register_summary,
                summary_window_start=summary_window_start,
                summary_window_end=summary_window_end,
            )

    adapter = notifications.resolve_delivery_adapter(channel)
    return notifications.deliver_project_due_action_notifications(
        context.session_factory,
        project_id=project_id,
        configuration_version=configuration_version,
        channel=channel,
        adapter=adapter,
        clock=context.clock,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        budget=budget,
        urgent_overdue_days=urgent_overdue_days,
        escalation_roster_entry_id=escalation_roster_entry_id,
        owner=context.claim.runtime_owner,
    )


def _document_notification_effectful(context: EffectfulContext) -> dict[str, Any]:
    """Discover and deliver a project's due document notifications (#353).

    First, in its own committed transaction, it re-discovers the complete
    authoritative affected population from committed state and registers any new
    interruption occurrences idempotently — a rolled-back transition leaves
    nothing, and a persistent condition converges on the existing rows. Then it
    delivers the due dispatches, committing each outcome durably and holding no
    transaction across the provider call. It reads no model, and the channel's
    adapter is resolved from the shared notifications seam, which defaults to a
    non-sending adapter so completing the code enables no real delivery.
    """

    from corridor import document_notifications, notifications

    with context.session_factory() as reading:
        schedule = reading.get(DueWorkSchedule, context.claim.schedule_id)
        if schedule is None:
            raise DueWorkRefusal("Due Work schedule disappeared")
        project_id = schedule.project_id
        configuration_version = schedule.configuration_version
        channel = schedule.scope_json.get("channel", "")
        max_attempts = schedule.max_attempts
        backoff_seconds = schedule.backoff_seconds
        budget = schedule.notification_budget

    with context.session_factory() as registering:
        with registering.begin():
            document_notifications.register_project_document_notifications(
                registering,
                project_id=project_id,
                registered_by=context.claim.runtime_owner,
            )

    adapter = notifications.resolve_delivery_adapter(channel)
    return document_notifications.deliver_project_document_notifications(
        context.session_factory,
        project_id=project_id,
        configuration_version=configuration_version,
        channel=channel,
        adapter=adapter,
        clock=context.clock,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        budget=budget,
        owner=context.claim.runtime_owner,
    )


def _event_admission_reproof_effectful(context: EffectfulContext) -> dict[str, Any]:
    """Run one bounded stale-class re-proof for a claimed occurrence.

    The recovery decision, the ADR-0050 replay, and the suspension-safe reactivation
    all live in :mod:`corridor.event_admission_reproof`; this wrapper only adapts the
    runtime's claim into that call. It holds no runtime transaction and no project
    mutation lock across the replay — the replay owns its own short final mutation.
    """

    from corridor.event_admission_reproof import execute_scheduled_reproof

    return execute_scheduled_reproof(context)


def _evidence_outcome_capture_effectful(context: EffectfulContext) -> dict[str, Any]:
    """Run the bounded cutoff-correct outcome capture for a claimed occurrence.

    The pass commits its per-case association records durably through the session
    factory before the runtime finalizes the claim, so recovering and re-running
    this occurrence only re-derives associations that are already durable and
    idempotent. It adapts the runtime's claim into a ``run_outcome_capture`` call
    over the schedule's declared contract and summarizes the pass into a bounded
    receipt. It reads no model and releases no protection.
    """

    from corridor.evidence_investigator_capture import run_outcome_capture

    with context.session_factory() as reading:
        schedule = reading.get(DueWorkSchedule, context.claim.schedule_id)
        if schedule is None:
            raise DueWorkRefusal("Due Work schedule disappeared")
        project_id = schedule.project_id
        configuration_version = schedule.configuration_version
        contract_sha256 = schedule.scope_json.get("observation_contract_sha256", "")

    return run_outcome_capture(
        context.session_factory,
        project_id=project_id,
        contract_sha256=contract_sha256,
        configuration_version=configuration_version,
        clock=context.clock,
    )


def _report_publication_effectful(context: EffectfulContext) -> dict[str, Any]:
    """Retain one weekly reading and prepare its external PDF for a claimed slot.

    The pass commits its retained reading and any prepared artifact durably
    through the session factory and converges on the occurrence's one retained
    row; this wrapper only adapts the runtime's claim into the publication call
    and returns its bounded receipt. It reads no model.
    """

    from corridor.report_publication import execute_report_publication

    return execute_report_publication(
        context.session_factory,
        occurrence_id=context.claim.occurrence_id,
        schedule_id=context.claim.schedule_id,
        clock=context.clock,
    )


def _connector_polling_effectful(context: EffectfulContext) -> dict[str, Any]:
    """Take delivery of one connected location's changes for a claimed occurrence.

    The pass stores every listed change in the content-addressed store, records
    each delivery in the shared ledger, and only then records the advance its
    cursor reached, so the checkpoint can never advance past an unstored change
    or a transient failure (ADR-0083 as extended by ADR-0089). It reads no model
    and holds no runtime transaction while fetching. The attempt identity is the
    run identity the ledger and the advance are attributed to.
    """

    from corridor.connector_polling import execute_connector_polling

    return execute_connector_polling(
        context.session_factory,
        schedule_id=context.claim.schedule_id,
        clock=context.clock,
        run_identity=context.claim.attempt_id,
    )


def _delta_generation_effectful(context: EffectfulContext) -> dict[str, Any]:
    """Propose one project's new differences from its accepted record (#518).

    The pass commits its Proposed Deltas durably through the session factory
    before the runtime finalizes the claim, and appends only through the
    source-append command, so it can never write an accepted value on any path
    and a recovered re-run appends nothing that is already there.
    """

    from corridor.delta_generation import execute_delta_generation

    return execute_delta_generation(
        context.session_factory,
        schedule_id=context.claim.schedule_id,
        clock=context.clock,
    )


def _report_preparation(
    session: Session,
    schedule: DueWorkSchedule,
    observed_at: datetime,
) -> dict[str, Any]:
    """Read one week of Proposed Delta lifecycle for the change summary (#488)."""

    from corridor.report_preparation import execute_report_preparation

    return execute_report_preparation(session, schedule, observed_at)


def _retention_sweep_effectful(context: EffectfulContext) -> dict[str, Any]:
    """Expire one project's due Class B intermediaries for a claimed occurrence.

    The manifest, the hold and reachability rechecks, and the deletion permit
    all stay in ``corridor.retention``; this wrapper only adapts the runtime's
    claim into that sweep. A refusal comes back as an attention reading rather
    than an exception, so a held or still-reachable candidate is visible in the
    receipt instead of burning the occurrence's retries.
    """

    from corridor.retention_sweep import execute_retention_sweep

    return execute_retention_sweep(
        context.session_factory,
        schedule_id=context.claim.schedule_id,
        clock=context.clock,
    )


HANDLER_REGISTRY: Mapping[str, HandlerContract] = MappingProxyType(
    {
        HANDLER_PROCESSING_HEALTH: HandlerContract(
            key=HANDLER_PROCESSING_HEALTH,
            scope_kind="one_project_stored_processing_facts",
            idempotency_contract="read_only_reconcilable",
            max_result_bytes=4096,
            model_token_budget=0,
            notification_budget=0,
            declaration_type=ProcessingHealthDeclaration,
            configure=lambda session, declaration, now: configure_processing_health(
                session, declaration, now=now
            ),
            run=_processing_health,
        ),
        HANDLER_PROJECT_PROCESSING: HandlerContract(
            key=HANDLER_PROJECT_PROCESSING,
            scope_kind="one_registered_project_extraction",
            idempotency_contract="at_least_once_reconcilable",
            max_result_bytes=8192,
            model_token_budget=_PROJECT_PROCESSING_TOKEN_CEILING,
            notification_budget=0,
            declaration_type=ProjectProcessingDeclaration,
            configure=lambda session, declaration, now: configure_project_processing(
                session, declaration, now=now
            ),
            run_effectful=_project_processing_effectful,
        ),
        HANDLER_REVISION_RECONCILIATION: HandlerContract(
            key=HANDLER_REVISION_RECONCILIATION,
            scope_kind="one_registered_project_revision",
            idempotency_contract="at_least_once_reconcilable",
            max_result_bytes=4096,
            model_token_budget=0,
            notification_budget=0,
            declaration_type=RevisionReconciliationDeclaration,
            configure=lambda session, declaration, now: configure_revision_reconciliation(
                session, declaration, now=now
            ),
            run_effectful=_revision_reconciliation_effectful,
        ),
        HANDLER_LOCATION_DISCOVERY: HandlerContract(
            key=HANDLER_LOCATION_DISCOVERY,
            scope_kind="one_connected_location",
            idempotency_contract="at_least_once_reconcilable",
            max_result_bytes=4096,
            model_token_budget=0,
            notification_budget=0,
            declaration_type=LocationDiscoveryDeclaration,
            configure=lambda session, declaration, now: configure_location_discovery(
                session, declaration, now=now
            ),
            run_effectful=_location_discovery_effectful,
        ),
        HANDLER_ASSIGNMENT_NOTIFICATION: HandlerContract(
            key=HANDLER_ASSIGNMENT_NOTIFICATION,
            scope_kind="one_project_assignment_notifications",
            idempotency_contract="at_least_once_reconcilable",
            max_result_bytes=4096,
            model_token_budget=0,
            notification_budget=_ASSIGNMENT_NOTIFICATION_BUDGET_CEILING,
            declaration_type=AssignmentNotificationDeclaration,
            configure=lambda session, declaration, now: configure_assignment_notification(
                session, declaration, now=now
            ),
            run_effectful=_assignment_notification_effectful,
        ),
        HANDLER_DUE_ACTION_NOTIFICATION: HandlerContract(
            key=HANDLER_DUE_ACTION_NOTIFICATION,
            scope_kind="one_project_due_action_notifications",
            idempotency_contract="at_least_once_reconcilable",
            max_result_bytes=4096,
            model_token_budget=0,
            notification_budget=_DUE_ACTION_NOTIFICATION_BUDGET_CEILING,
            declaration_type=DueActionNotificationDeclaration,
            configure=lambda session, declaration, now: configure_due_action_notification(
                session, declaration, now=now
            ),
            run_effectful=_due_action_notification_effectful,
        ),
        HANDLER_DOCUMENT_NOTIFICATION: HandlerContract(
            key=HANDLER_DOCUMENT_NOTIFICATION,
            scope_kind="one_project_document_notifications",
            idempotency_contract="at_least_once_reconcilable",
            max_result_bytes=4096,
            model_token_budget=0,
            notification_budget=_DOCUMENT_NOTIFICATION_BUDGET_CEILING,
            declaration_type=DocumentNotificationDeclaration,
            configure=lambda session, declaration, now: configure_document_notification(
                session, declaration, now=now
            ),
            run_effectful=_document_notification_effectful,
        ),
        HANDLER_EVENT_ADMISSION_REPROOF: HandlerContract(
            key=HANDLER_EVENT_ADMISSION_REPROOF,
            scope_kind="one_project_event_admission_class",
            idempotency_contract="at_least_once_reconcilable",
            max_result_bytes=4096,
            model_token_budget=0,
            notification_budget=0,
            declaration_type=EventAdmissionReproofDeclaration,
            configure=lambda session, declaration, now: configure_event_admission_reproof(
                session, declaration, now=now
            ),
            run_effectful=_event_admission_reproof_effectful,
        ),
        HANDLER_EVIDENCE_OUTCOME_CAPTURE: HandlerContract(
            key=HANDLER_EVIDENCE_OUTCOME_CAPTURE,
            scope_kind="one_declared_capture_cohort",
            idempotency_contract="at_least_once_reconcilable",
            max_result_bytes=4096,
            model_token_budget=0,
            notification_budget=0,
            declaration_type=EvidenceOutcomeCaptureDeclaration,
            configure=lambda session, declaration, now: configure_evidence_outcome_capture(
                session, declaration, now=now
            ),
            run_effectful=_evidence_outcome_capture_effectful,
        ),
        HANDLER_REPORT_PUBLICATION: HandlerContract(
            key=HANDLER_REPORT_PUBLICATION,
            scope_kind="one_project_scheduled_report_publication",
            idempotency_contract="at_least_once_reconcilable",
            max_result_bytes=4096,
            model_token_budget=0,
            notification_budget=0,
            declaration_type=ReportPublicationDeclaration,
            configure=lambda session, declaration, now: configure_report_publication(
                session, declaration, now=now
            ),
            run_effectful=_report_publication_effectful,
        ),
        HANDLER_CONNECTOR_POLLING: HandlerContract(
            key=HANDLER_CONNECTOR_POLLING,
            scope_kind="one_declared_pull_connector",
            idempotency_contract="at_least_once_reconcilable",
            max_result_bytes=4096,
            model_token_budget=0,
            notification_budget=0,
            declaration_type=ConnectorPollingDeclaration,
            configure=lambda session, declaration, now: configure_connector_polling(
                session, declaration, now=now
            ),
            run_effectful=_connector_polling_effectful,
        ),
        HANDLER_DELTA_GENERATION: HandlerContract(
            key=HANDLER_DELTA_GENERATION,
            scope_kind="one_project_proposed_delta_generation",
            idempotency_contract="at_least_once_reconcilable",
            max_result_bytes=4096,
            model_token_budget=0,
            notification_budget=0,
            declaration_type=DeltaGenerationDeclaration,
            configure=lambda session, declaration, now: configure_delta_generation(
                session, declaration, now=now
            ),
            run_effectful=_delta_generation_effectful,
        ),
        HANDLER_REPORT_PREPARATION: HandlerContract(
            key=HANDLER_REPORT_PREPARATION,
            scope_kind="one_project_change_summary_reading",
            idempotency_contract="read_only_reconcilable",
            max_result_bytes=4096,
            model_token_budget=0,
            notification_budget=0,
            declaration_type=ReportPreparationDeclaration,
            configure=lambda session, declaration, now: configure_report_preparation(
                session, declaration, now=now
            ),
            run=_report_preparation,
        ),
        HANDLER_RETENTION_SWEEP: HandlerContract(
            key=HANDLER_RETENTION_SWEEP,
            scope_kind="one_project_class_b_intermediaries",
            idempotency_contract="at_least_once_reconcilable",
            max_result_bytes=4096,
            model_token_budget=0,
            notification_budget=0,
            declaration_type=RetentionSweepDeclaration,
            configure=lambda session, declaration, now: configure_retention_sweep(
                session, declaration, now=now
            ),
            run_effectful=_retention_sweep_effectful,
        ),
    }
)


def configure_processing_health(
    session: Session,
    declaration: ProcessingHealthDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 processing-health declaration."""

    configuration = _validated_health_declaration(declaration)
    return _retain_due_work_schedule(
        session,
        declaration,
        now=now,
        handler_key=HANDLER_PROCESSING_HEALTH,
        configuration=configuration,
        scope={"project_id": declaration.project_id},
        input_identity={
            "handler": HANDLER_PROCESSING_HEALTH,
            "project_id": declaration.project_id,
            "source": "stored_processing_facts-v1",
        },
    )


def configure_project_processing(
    session: Session,
    declaration: ProjectProcessingDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 project-processing declaration.

    Missing or invalid configuration leaves the handler refused and no schedule
    written; model credentials alone never enable a project sweep. Enabling a
    new configuration disables the project's prior processing schedule while
    retaining it for audit.
    """

    configuration = _validated_processing_declaration(declaration)
    scope = {
        "project_id": declaration.project_id,
        "extractor_identity": declaration.extractor_identity,
    }
    return _retain_due_work_schedule(
        session,
        declaration,
        now=now,
        handler_key=HANDLER_PROJECT_PROCESSING,
        configuration=configuration,
        scope=scope,
        input_identity={
            "handler": HANDLER_PROJECT_PROCESSING,
            "project_id": declaration.project_id,
            "extractor_identity": declaration.extractor_identity,
        },
    )


def configure_revision_reconciliation(
    session: Session,
    declaration: RevisionReconciliationDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 revision-reconciliation declaration.

    Missing or invalid configuration leaves the handler refused and no schedule
    written; a deterministic handler still requires an explicit gate-7 declaration
    before it may run. Enabling a new configuration disables the project's prior
    revision schedule while retaining it for audit.
    """

    configuration = _validated_revision_reconciliation_declaration(declaration)
    scope = {
        "project_id": declaration.project_id,
        "matcher_identity": declaration.matcher_identity,
        "support_rule_identity": declaration.support_rule_identity,
    }
    return _retain_due_work_schedule(
        session,
        declaration,
        now=now,
        handler_key=HANDLER_REVISION_RECONCILIATION,
        configuration=configuration,
        scope=scope,
        input_identity={
            "handler": HANDLER_REVISION_RECONCILIATION,
            "project_id": declaration.project_id,
            "matcher_identity": declaration.matcher_identity,
            "support_rule_identity": declaration.support_rule_identity,
        },
    )


def configure_location_discovery(
    session: Session,
    declaration: LocationDiscoveryDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 connected-location declaration (#350).

    Missing, invalid, or incomplete configuration leaves the adapter refused and no
    schedule written; a location's credentials never authorize a broader scan.
    Enabling a new configuration disables the project's prior location schedule for
    the same location while retaining it for audit.
    """

    configuration = _validated_location_discovery_declaration(declaration)
    scope = configuration["scope"]
    return _retain_due_work_schedule(
        session,
        declaration,
        now=now,
        handler_key=HANDLER_LOCATION_DISCOVERY,
        configuration=configuration,
        scope=scope,
        input_identity={
            "handler": HANDLER_LOCATION_DISCOVERY,
            "project_id": declaration.project_id,
            "location_id": declaration.location_id,
            "adapter_identity": declaration.adapter_identity,
            "source_manifest_id": declaration.source_manifest_id,
        },
        disable_same_input_only=True,
    )


def configure_assignment_notification(
    session: Session,
    declaration: AssignmentNotificationDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 assignment-notification declaration.

    Missing or invalid configuration leaves the handler refused and no schedule
    written, so a committed assignment still registers its durable dispatch but
    nothing is delivered until an authorized operator records this gate-7 scope.
    Enabling a new configuration disables the project's prior notification
    schedule while retaining it for audit.
    """

    configuration = _validated_assignment_notification_declaration(declaration)
    scope = {"project_id": declaration.project_id, "channel": declaration.channel}
    return _retain_due_work_schedule(
        session,
        declaration,
        now=now,
        handler_key=HANDLER_ASSIGNMENT_NOTIFICATION,
        configuration=configuration,
        scope=scope,
        input_identity={
            "handler": HANDLER_ASSIGNMENT_NOTIFICATION,
            "project_id": declaration.project_id,
            "channel": declaration.channel,
        },
    )


def configure_due_action_notification(
    session: Session,
    declaration: DueActionNotificationDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 due-action-notification declaration.

    Missing or invalid configuration leaves the handler refused and no schedule
    written, so nothing is derived or delivered until an authorized operator
    records this gate-7 scope.  A declared escalation contact must be an active
    roster identity of this project; leaving it unset is valid and leaves
    escalation visibly disabled.  Enabling a new configuration disables the
    project's prior due-action schedule while retaining it for audit.
    """

    configuration = _validated_due_action_notification_declaration(declaration)
    if session.get(Project, declaration.project_id) is None:
        raise DueWorkRefusal(f"project {declaration.project_id} does not exist")
    if declaration.escalation_roster_entry_id is not None:
        roster = session.get(
            ProjectRosterEntry, declaration.escalation_roster_entry_id
        )
        if (
            roster is None
            or roster.project_id != declaration.project_id
            or not roster.active
        ):
            raise DueWorkRefusal(
                "the escalation contact must be an active project roster identity"
            )
    return _retain_due_work_schedule(
        session,
        declaration,
        now=now,
        handler_key=HANDLER_DUE_ACTION_NOTIFICATION,
        configuration=configuration,
        scope=configuration["scope"],
        input_identity={
            "handler": HANDLER_DUE_ACTION_NOTIFICATION,
            "project_id": declaration.project_id,
            "channel": declaration.channel,
        },
    )


def configure_document_notification(
    session: Session,
    declaration: DocumentNotificationDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 document-notification declaration.

    Missing or invalid configuration leaves the handler refused and no schedule
    written, so a committed transition still registers its durable occurrences
    but nothing is delivered until an authorized operator records this gate-7
    scope. Enabling a new configuration disables the project's prior document
    notification schedule while retaining it for audit.
    """

    configuration = _validated_document_notification_declaration(declaration)
    scope = {"project_id": declaration.project_id, "channel": declaration.channel}
    return _retain_due_work_schedule(
        session,
        declaration,
        now=now,
        handler_key=HANDLER_DOCUMENT_NOTIFICATION,
        configuration=configuration,
        scope=scope,
        input_identity={
            "handler": HANDLER_DOCUMENT_NOTIFICATION,
            "project_id": declaration.project_id,
            "channel": declaration.channel,
        },
    )


def configure_event_admission_reproof(
    session: Session,
    declaration: EventAdmissionReproofDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 stale-class re-proof declaration.

    Missing or invalid configuration leaves the handler refused and no schedule
    written; neither a prior activation nor deployed credentials enable recovery
    automatically. Enabling a new configuration disables the project's prior re-proof
    schedule while retaining it for audit.
    """

    configuration = _validated_event_admission_reproof_declaration(declaration)
    scope = {
        "project_id": declaration.project_id,
        "policy_version": declaration.policy_version,
        "reason_version": declaration.reason_version,
        "selection_rule": declaration.selection_rule,
    }
    return _retain_due_work_schedule(
        session,
        declaration,
        now=now,
        handler_key=HANDLER_EVENT_ADMISSION_REPROOF,
        configuration=configuration,
        scope=scope,
        input_identity={
            "handler": HANDLER_EVENT_ADMISSION_REPROOF,
            "project_id": declaration.project_id,
            "policy_version": declaration.policy_version,
            "reason_version": declaration.reason_version,
            "selection_rule": declaration.selection_rule,
        },
    )


def configure_evidence_outcome_capture(
    session: Session,
    declaration: EvidenceOutcomeCaptureDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 outcome-capture declaration.

    Missing or invalid configuration leaves the handler refused and no schedule
    written; an elapsed interval or the mere existence of frozen cases never
    enables capture. The referenced observation contract is declared and
    validated separately in ``evidence_investigator_capture``; this seam only
    binds the operational envelope to the project, cohort, and contract identity.
    Enabling a new configuration disables the cohort's prior capture schedule
    while retaining it for audit.
    """

    configuration = _validated_evidence_outcome_capture_declaration(declaration)
    scope = {
        "project_id": declaration.project_id,
        "cohort_id": declaration.cohort_id,
        "observation_contract_sha256": declaration.observation_contract_sha256,
    }
    return _retain_due_work_schedule(
        session,
        declaration,
        now=now,
        handler_key=HANDLER_EVIDENCE_OUTCOME_CAPTURE,
        configuration=configuration,
        scope=scope,
        input_identity={
            "handler": HANDLER_EVIDENCE_OUTCOME_CAPTURE,
            "project_id": declaration.project_id,
            "cohort_id": declaration.cohort_id,
            "observation_contract_sha256": declaration.observation_contract_sha256,
        },
        disable_same_input_only=True,
    )


def configure_report_publication(
    session: Session,
    declaration: ReportPublicationDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 report-publication declaration.

    Missing or invalid configuration leaves the handler refused and no schedule
    written; a schedule is never enabled with silent production defaults.
    Enabling a new configuration disables the project's prior publication
    schedule for this output identity while retaining it for audit.
    """

    configuration = _validated_report_publication_declaration(declaration)
    scope = {
        "project_id": declaration.project_id,
        "provenance_mode": declaration.provenance_mode,
        "prepare_external_pdf": declaration.prepare_external_pdf,
    }
    return _retain_due_work_schedule(
        session,
        declaration,
        now=now,
        handler_key=HANDLER_REPORT_PUBLICATION,
        configuration=configuration,
        scope=scope,
        input_identity={
            "handler": HANDLER_REPORT_PUBLICATION,
            "project_id": declaration.project_id,
            "provenance_mode": declaration.provenance_mode,
            "prepare_external_pdf": declaration.prepare_external_pdf,
        },
        disable_same_input_only=True,
    )


def configure_connector_polling(
    session: Session,
    declaration: ConnectorPollingDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 connector-polling declaration.

    Enabling a new configuration disables the project's prior schedule for this
    exact ingress identity while retaining it for audit; a second connector or
    a second location on the same project is a different identity and keeps its
    own schedule and its own checkpoint.
    """

    configuration = _validated_connector_polling_declaration(declaration)
    return _retain_due_work_schedule(
        session,
        declaration,
        now=now,
        handler_key=HANDLER_CONNECTOR_POLLING,
        configuration=configuration,
        scope=configuration["scope"],
        input_identity={
            "handler": HANDLER_CONNECTOR_POLLING,
            "project_id": declaration.project_id,
            "customer": declaration.customer,
            "channel": declaration.channel,
            "connector_identity": declaration.connector_identity,
            "source_url": declaration.source_url,
        },
        disable_same_input_only=True,
    )


def configure_delta_generation(
    session: Session,
    declaration: DeltaGenerationDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 delta-generation declaration."""

    configuration = _validated_delta_generation_declaration(declaration)
    return _retain_due_work_schedule(
        session,
        declaration,
        now=now,
        handler_key=HANDLER_DELTA_GENERATION,
        configuration=configuration,
        scope=configuration["scope"],
        input_identity={
            "handler": HANDLER_DELTA_GENERATION,
            "project_id": declaration.project_id,
            "comparison_rule_version": declaration.comparison_rule_version,
        },
    )


def configure_report_preparation(
    session: Session,
    declaration: ReportPreparationDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 report-preparation declaration."""

    configuration = _validated_report_preparation_declaration(declaration)
    return _retain_due_work_schedule(
        session,
        declaration,
        now=now,
        handler_key=HANDLER_REPORT_PREPARATION,
        configuration=configuration,
        scope=configuration["scope"],
        input_identity={
            "handler": HANDLER_REPORT_PREPARATION,
            "project_id": declaration.project_id,
            "source": "proposed_delta_lifecycle-v1",
        },
    )


def configure_retention_sweep(
    session: Session,
    declaration: RetentionSweepDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 Class B retention-sweep declaration."""

    configuration = _validated_retention_sweep_declaration(declaration)
    return _retain_due_work_schedule(
        session,
        declaration,
        now=now,
        handler_key=HANDLER_RETENTION_SWEEP,
        configuration=configuration,
        scope=configuration["scope"],
        input_identity={
            "handler": HANDLER_RETENTION_SWEEP,
            "project_id": declaration.project_id,
            "retention_class": "class_b",
        },
    )


def configure_due_work(
    session: Session,
    declaration: object,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain any server-owned Due Work declaration."""
    for contract in HANDLER_REGISTRY.values():
        if (
            contract.declaration_type is not None
            and isinstance(declaration, contract.declaration_type)
            and contract.configure is not None
        ):
            return contract.configure(session, declaration, now)
    raise DueWorkRefusal("Due Work declaration is not server-owned")


def _retain_due_work_schedule(
    session: Session,
    declaration: object,
    *,
    now: datetime,
    handler_key: str,
    configuration: dict[str, Any],
    scope: dict[str, Any],
    input_identity: dict[str, Any],
    disable_same_input_only: bool = False,
) -> DueWorkSchedule:
    """Own idempotency, supersession, and persistence for every declaration."""
    now = _aware_utc(now)
    project_id = declaration.project_id
    if session.get(Project, project_id) is None:
        raise DueWorkRefusal(f"project {project_id} does not exist")
    configuration_sha256 = _sha256(configuration)
    input_identity_sha256 = _sha256(input_identity)
    existing = session.scalar(
        select(DueWorkSchedule).where(
            DueWorkSchedule.project_id == project_id,
            DueWorkSchedule.handler_key == handler_key,
            DueWorkSchedule.configuration_version
            == declaration.configuration_version,
            DueWorkSchedule.input_identity_sha256 == input_identity_sha256,
        )
    )
    if existing is not None:
        if existing.configuration_sha256 != configuration_sha256:
            raise DueWorkRefusal(
                "configuration version already names different Due Work rules"
            )
        return existing

    active_query = select(DueWorkSchedule).where(
        DueWorkSchedule.project_id == project_id,
        DueWorkSchedule.handler_key == handler_key,
        DueWorkSchedule.disabled_at.is_(None),
    )
    if disable_same_input_only:
        active_query = active_query.where(
            DueWorkSchedule.input_identity_sha256 == input_identity_sha256
        )
    for prior in session.scalars(active_query).all():
        prior.disabled_at = now

    schedule = DueWorkSchedule(
        public_id=f"due-job:{configuration_sha256[:24]}",
        project_id=project_id,
        handler_key=handler_key,
        configuration_version=declaration.configuration_version,
        scope_json=scope,
        configuration_json=configuration,
        configuration_sha256=configuration_sha256,
        input_identity_sha256=input_identity_sha256,
        starts_at=declaration.starts_at,
        cadence=declaration.cadence,
        timezone_name=declaration.timezone_name,
        missed_run_policy=declaration.missed_run_policy,
        retention_days=declaration.retention_days,
        max_attempts=declaration.max_attempts,
        backoff_seconds=declaration.backoff_seconds,
        claim_ttl_seconds=declaration.claim_ttl_seconds,
        deadline_seconds=declaration.deadline_seconds,
        concurrency_limit=declaration.concurrency_limit,
        model_token_budget=declaration.model_token_budget,
        notification_budget=declaration.notification_budget,
        enabled_at=now,
    )
    session.add(schedule)
    session.flush([schedule])
    return schedule


def enqueue_due_work(session: Session, *, now: datetime) -> tuple[DueWorkOccurrence, ...]:
    """Coalesce the latest due occurrence for every active validated schedule."""

    now = _aware_utc(now)
    schedules = session.scalars(
        select(DueWorkSchedule)
        .where(
            DueWorkSchedule.disabled_at.is_(None),
            DueWorkSchedule.starts_at <= now,
        )
        .order_by(DueWorkSchedule.id)
    ).all()
    keys: list[str] = []
    for schedule in schedules:
        _validate_stored_schedule(schedule)
        due_at = _due_slot(schedule, now)
        if due_at is None:
            continue
        occurrence_key = _sha256(
            {
                "handler": schedule.handler_key,
                "scope": schedule.scope_json,
                "configuration_version": schedule.configuration_version,
                "configuration_sha256": schedule.configuration_sha256,
                "input_identity_sha256": schedule.input_identity_sha256,
                "due_at": _iso(due_at),
            }
        )
        keys.append(occurrence_key)
        session.execute(
            insert(DueWorkOccurrence)
            .values(
                public_id=f"due-occurrence:{occurrence_key[:24]}",
                scheduled_job_id=schedule.id,
                occurrence_key=occurrence_key,
                due_at=due_at,
                state="pending",
                attempt_count=0,
            )
            .on_conflict_do_nothing(index_elements=["occurrence_key"])
        )
        _fail_missed_latest_only(
            session,
            schedule,
            latest_due_at=due_at,
            now=now,
        )
    if not keys:
        return ()
    return tuple(
        session.scalars(
            select(DueWorkOccurrence)
            .where(DueWorkOccurrence.occurrence_key.in_(keys))
            .order_by(DueWorkOccurrence.due_at, DueWorkOccurrence.id)
        ).all()
    )


def claim_due_work(
    session: Session,
    *,
    now: datetime,
    owner: str,
) -> DueWorkClaim | None:
    """Claim one ready occurrence, recovering or retaining abandoned attempts."""

    now = _aware_utc(now)
    _validate_owner(owner)
    candidates = session.scalars(
        select(DueWorkOccurrence)
        .join(DueWorkSchedule, DueWorkSchedule.id == DueWorkOccurrence.scheduled_job_id)
        .where(
            DueWorkSchedule.disabled_at.is_(None),
            DueWorkOccurrence.due_at <= now,
            or_(
                DueWorkOccurrence.state == "pending",
                (
                    (DueWorkOccurrence.state == "retry_due")
                    & (DueWorkOccurrence.next_attempt_at <= now)
                ),
                (
                    (DueWorkOccurrence.state == "claimed")
                    & (DueWorkOccurrence.lease_expires_at <= now)
                ),
            ),
        )
        .order_by(DueWorkOccurrence.due_at, DueWorkOccurrence.id)
        .with_for_update(skip_locked=True)
    ).all()
    for occurrence in candidates:
        schedule = session.get(DueWorkSchedule, occurrence.scheduled_job_id)
        if schedule is None:
            continue
        _validate_stored_schedule(schedule)
        if occurrence.state == "claimed":
            if occurrence.attempt_count >= schedule.max_attempts:
                _record_abandoned_failure(session, occurrence, schedule, now=now)
                continue
            _record_abandoned_retry(session, occurrence, schedule, now=now)
        active_claims = int(
            session.scalar(
                select(func.count()).select_from(DueWorkOccurrence).where(
                    DueWorkOccurrence.scheduled_job_id == schedule.id,
                    DueWorkOccurrence.state == "claimed",
                    DueWorkOccurrence.lease_expires_at > now,
                )
            )
            or 0
        )
        if active_claims >= schedule.concurrency_limit:
            continue
        token = uuid4().hex
        occurrence.attempt_count += 1
        occurrence.state = "claimed"
        occurrence.owner = owner
        occurrence.claim_token = token
        occurrence.claimed_at = now
        occurrence.lease_expires_at = now + timedelta(seconds=schedule.claim_ttl_seconds)
        occurrence.deadline_at = now + timedelta(seconds=schedule.deadline_seconds)
        occurrence.next_attempt_at = None
        occurrence.last_error_code = None
        session.flush([occurrence])
        return DueWorkClaim(
            occurrence_id=occurrence.id,
            occurrence_public_id=occurrence.public_id,
            schedule_id=schedule.id,
            attempt_number=occurrence.attempt_count,
            attempt_id=f"due-attempt:{token}",
            claim_token=token,
            runtime_owner=owner,
            started_at=now,
            lease_expires_at=occurrence.lease_expires_at,
            deadline_at=occurrence.deadline_at,
        )
    return None


def complete_due_work(
    session: Session,
    claim: DueWorkClaim,
    *,
    handler_result: dict[str, Any],
    now: datetime,
) -> DueWorkRunResult:
    """Finalize a live claim with one validated append-only completed receipt."""

    now = _aware_utc(now)
    occurrence, schedule = _locked_live_claim(session, claim, now=now)
    contract = _handler(schedule.handler_key)
    _validate_handler_result(contract, handler_result)
    safe_next_step = _safe_next_step(contract, handler_result)
    receipt = _append_receipt(
        session,
        occurrence,
        schedule,
        claim,
        execution_outcome="completed",
        handler_result=handler_result,
        error_code=None,
        safe_next_step=safe_next_step,
        finished_at=now,
    )
    _release_occurrence(occurrence, state="completed", error_code=None)
    session.flush([occurrence])
    return _run_result(receipt, occurrence=occurrence, schedule=schedule)


def fail_due_work(
    session: Session,
    claim: DueWorkClaim,
    *,
    error_code: str,
    now: datetime,
    retryable: bool = True,
) -> DueWorkRunResult:
    """Retain a failed attempt and schedule bounded retry or terminal failure."""

    now = _aware_utc(now)
    occurrence, schedule = _locked_live_claim(session, claim, now=now)
    if not error_code or len(error_code) > 64:
        raise DueWorkRefusal("Due Work error code is invalid")
    retry = retryable and occurrence.attempt_count < schedule.max_attempts
    outcome = "retry_due" if retry else "failed"
    safe_next_step = "retry_after_backoff" if retry else "inspect_processing_failure"
    receipt = _append_receipt(
        session,
        occurrence,
        schedule,
        claim,
        execution_outcome=outcome,
        handler_result=None,
        error_code=error_code,
        safe_next_step=safe_next_step,
        finished_at=now,
    )
    _release_occurrence(occurrence, state=outcome, error_code=error_code)
    if retry:
        occurrence.next_attempt_at = now + timedelta(
            seconds=schedule.backoff_seconds * (2 ** (occurrence.attempt_count - 1))
        )
    session.flush([occurrence])
    return _run_result(receipt, occurrence=occurrence, schedule=schedule)


def run_due_work_once(
    session_factory,
    *,
    clock: Clock,
    owner: str,
    registry: Mapping[str, HandlerContract] = HANDLER_REGISTRY,
) -> DueWorkRunResult | None:
    """Claim, execute outside the claim transaction, and reconcile one attempt."""

    started_at = _aware_utc(clock.now())
    with session_factory() as claiming:
        with claiming.begin():
            claim = claim_due_work(claiming, now=started_at, owner=owner)
    if claim is None:
        return None

    # Everything this attempt logs — the handler's own lines included — is
    # bound to the occurrence and attempt the receipt will name, so a log line
    # and the durable receipt can be read together (#491A).
    with correlation_scope(
        job_id=claim.occurrence_public_id,
        attempt_id=claim.attempt_id,
        attempt_number=claim.attempt_number,
        runtime_owner=claim.runtime_owner,
    ):
        try:
            with session_factory() as reading:
                schedule = reading.get(DueWorkSchedule, claim.schedule_id)
                if schedule is None or schedule.handler_key not in registry:
                    raise DueWorkRefusal("claimed handler is not server-owned")
                contract = registry[schedule.handler_key]
                if contract.run_effectful is None:
                    # Read-only handler: run inside the reading transaction and
                    # let the runtime finalize its result.
                    handler_result = contract.run(reading, schedule, started_at)
            if contract.run_effectful is not None:
                # Effectful handler: it owns its durable commits, run outside any
                # runtime-held transaction so no domain lock spans the finalize.
                handler_result = contract.run_effectful(
                    EffectfulContext(
                        session_factory=session_factory,
                        schedule=schedule,
                        claim=claim,
                        clock=clock,
                        registry=registry,
                    )
                )
            finished_at = _aware_utc(clock.now())
            with session_factory() as finalizing:
                with finalizing.begin():
                    if finished_at > claim.deadline_at:
                        # The domain work already committed durably and is
                        # idempotent, so recovering and re-running this
                        # occurrence is safe.
                        return _logged_attempt(
                            fail_due_work(
                                finalizing,
                                claim,
                                error_code="deadline_exceeded",
                                now=finished_at,
                            )
                        )
                    return _logged_attempt(
                        complete_due_work(
                            finalizing,
                            claim,
                            handler_result=handler_result,
                            now=finished_at,
                        )
                    )
        except StaleDueWorkClaim:
            raise
        except Exception:
            failed_at = _aware_utc(clock.now())
            with session_factory() as finalizing:
                with finalizing.begin():
                    return _logged_attempt(
                        fail_due_work(
                            finalizing,
                            claim,
                            error_code="handler_execution_failed",
                            now=failed_at,
                        )
                    )


def _logged_attempt(result: DueWorkRunResult) -> DueWorkRunResult:
    """Emit the one operational line for a finalized attempt, and pass it on."""

    log_event(
        _LOG,
        "due_work_attempt",
        level=(
            logging.INFO
            if result.execution_outcome == "completed"
            else logging.WARNING
        ),
        handler=result.handler_key,
        project_id=result.project_id,
        configuration_version=result.configuration_version,
        execution_outcome=result.execution_outcome,
        error_code=result.error_code,
        safe_next_step=result.safe_next_step,
        receipt_id=result.receipt_public_id,
    )
    return result


def due_work_status(session: Session, *, project_id: int | None = None) -> dict[str, Any]:
    """Return credential-free operational schedule, occurrence, and receipt status."""

    schedule_query = select(DueWorkSchedule)
    if project_id is not None:
        schedule_query = schedule_query.where(DueWorkSchedule.project_id == project_id)
    schedules = session.scalars(schedule_query.order_by(DueWorkSchedule.id)).all()
    schedule_ids = [schedule.id for schedule in schedules]
    occurrences = (
        session.scalars(
            select(DueWorkOccurrence)
            .where(DueWorkOccurrence.scheduled_job_id.in_(schedule_ids))
            .order_by(DueWorkOccurrence.due_at, DueWorkOccurrence.id)
        ).all()
        if schedule_ids
        else []
    )
    occurrence_ids = [occurrence.id for occurrence in occurrences]
    receipts = (
        session.scalars(
            select(DueWorkReceipt)
            .where(DueWorkReceipt.occurrence_id.in_(occurrence_ids))
            .order_by(DueWorkReceipt.id)
        ).all()
        if occurrence_ids
        else []
    )
    occurrence_by_id = {item.id: item for item in occurrences}
    schedule_by_id = {item.id: item for item in schedules}
    for receipt in receipts:
        occurrence = occurrence_by_id.get(receipt.occurrence_id)
        schedule = (
            schedule_by_id.get(occurrence.scheduled_job_id)
            if occurrence is not None
            else None
        )
        if occurrence is None or schedule is None:
            raise DueWorkRefusal("Due Work receipt lineage is incomplete")
        expected = _receipt_content(
            occurrence_public_id=occurrence.public_id,
            schedule=schedule,
            attempt_number=receipt.attempt_number,
            attempt_id=receipt.attempt_id,
            runtime_owner=receipt.runtime_owner,
            execution_outcome=receipt.execution_outcome,
            handler_result=receipt.handler_result_json,
            error_code=receipt.error_code,
            safe_next_step=receipt.safe_next_step,
            started_at=receipt.started_at,
            finished_at=receipt.finished_at,
        )
        if receipt.content_sha256 != _sha256(expected):
            raise DueWorkRefusal("Due Work receipt integrity check failed")
    return {
        "jobs": [
            {
                "job_id": item.public_id,
                "project_id": item.project_id,
                "handler": item.handler_key,
                "configuration_version": item.configuration_version,
                "configuration_sha256": item.configuration_sha256,
                "input_identity": item.configuration_json["input_identity"],
                "input_identity_sha256": item.input_identity_sha256,
                "cadence": item.cadence,
                "timezone": item.timezone_name,
                "missed_run_policy": item.missed_run_policy,
                "retention_policy": f"retain_at_least_{item.retention_days}_days",
                "enabled": item.disabled_at is None,
                "model_token_budget": item.model_token_budget,
                "notification_budget": item.notification_budget,
            }
            for item in schedules
        ],
        "occurrences": [
            {
                "occurrence_id": item.public_id,
                "job_id": schedule_by_id[item.scheduled_job_id].public_id,
                "project_id": schedule_by_id[item.scheduled_job_id].project_id,
                "handler": schedule_by_id[item.scheduled_job_id].handler_key,
                "configuration_version": schedule_by_id[
                    item.scheduled_job_id
                ].configuration_version,
                "input_identity_sha256": schedule_by_id[
                    item.scheduled_job_id
                ].input_identity_sha256,
                "due_at": _iso(item.due_at),
                "state": item.state,
                "attempt_count": item.attempt_count,
                "next_attempt_at": (
                    _iso(item.next_attempt_at) if item.next_attempt_at else None
                ),
                "runtime_owner": item.owner,
                "last_error_code": item.last_error_code,
            }
            for item in occurrences
        ],
        "receipts": [
            {
                "receipt_id": item.public_id,
                "occurrence_id": occurrence_by_id[item.occurrence_id].public_id,
                "job_id": schedule_by_id[
                    occurrence_by_id[item.occurrence_id].scheduled_job_id
                ].public_id,
                "project_id": item.project_id,
                "handler": item.handler_key,
                "configuration_version": schedule_by_id[
                    occurrence_by_id[item.occurrence_id].scheduled_job_id
                ].configuration_version,
                "input_identity_sha256": schedule_by_id[
                    occurrence_by_id[item.occurrence_id].scheduled_job_id
                ].input_identity_sha256,
                "attempt_id": item.attempt_id,
                "attempt_number": item.attempt_number,
                "runtime_owner": item.runtime_owner,
                "execution_outcome": item.execution_outcome,
                "handler_result": item.handler_result_json,
                "error_code": item.error_code,
                "safe_next_step": item.safe_next_step,
                "content_sha256": item.content_sha256,
            }
            for item in receipts
        ],
    }


def supervise_due_work(
    session_factory,
    *,
    clock: Clock,
    owner: str,
    stop_requested: Callable[[], bool],
    wait: Callable[[float], None],
    poll_seconds: float,
) -> int:
    """Run the production supervisor until shutdown stops taking new work."""

    if poll_seconds <= 0 or poll_seconds > 60:
        raise DueWorkRefusal("Due Work supervisor poll interval is invalid")
    completed_cycles = 0
    while not stop_requested():
        tick_at = _aware_utc(clock.now())
        with session_factory() as ticking:
            with ticking.begin():
                enqueue_due_work(ticking, now=tick_at)
        if stop_requested():
            break
        result = run_due_work_once(
            session_factory,
            clock=clock,
            owner=owner,
        )
        if result is not None:
            completed_cycles += 1
            continue
        wait(poll_seconds)
    return completed_cycles


def _validated_health_declaration(
    declaration: ProcessingHealthDeclaration,
) -> dict[str, Any]:
    starts_at = _aware_utc(declaration.starts_at)
    if starts_at.minute or starts_at.second or starts_at.microsecond:
        raise DueWorkRefusal("processing-health starts_at must align to a UTC hour")
    if not _CONFIGURATION_VERSION.fullmatch(declaration.configuration_version):
        raise DueWorkRefusal("Due Work configuration version is invalid")
    if (
        declaration.cadence != "hourly"
        or declaration.timezone_name != "UTC"
        or declaration.missed_run_policy != "latest_only"
    ):
        raise DueWorkRefusal(
            "processing-health supports only hourly UTC latest-only scheduling"
        )
    if not (
        1 <= declaration.max_attempts <= 5
        and 0 <= declaration.backoff_seconds <= 3600
        and 30 <= declaration.claim_ttl_seconds <= 900
        and 1 <= declaration.deadline_seconds <= declaration.claim_ttl_seconds
        and declaration.concurrency_limit == 1
        and declaration.retention_days >= 365
        and declaration.model_token_budget == 0
        and declaration.notification_budget == 0
    ):
        raise DueWorkRefusal("processing-health gate-7 resource declaration is invalid")
    return {
        "schema_version": "due-work-gate-7-v1",
        "handler": HANDLER_PROCESSING_HEALTH,
        "project_id": declaration.project_id,
        "scope": {"project_id": declaration.project_id},
        "configuration_version": declaration.configuration_version,
        "input_identity": {
            "kind": "stored_processing_facts-v1",
            "project_id": declaration.project_id,
        },
        "starts_at": _iso(starts_at),
        "cadence": declaration.cadence,
        "timezone": declaration.timezone_name,
        "missed_run_policy": declaration.missed_run_policy,
        "retention": {
            "policy": "retain_all_terminal_receipts",
            "minimum_days": declaration.retention_days,
        },
        "retry": {
            "max_attempts": declaration.max_attempts,
            "backoff_seconds": declaration.backoff_seconds,
        },
        "resources": {
            "claim_ttl_seconds": declaration.claim_ttl_seconds,
            "deadline_seconds": declaration.deadline_seconds,
            "concurrency_limit": declaration.concurrency_limit,
            "model_token_budget": declaration.model_token_budget,
            "notification_budget": declaration.notification_budget,
        },
        "authorized_destinations": [],
        "idempotency_contract": "read_only_reconcilable",
    }


def _validated_processing_declaration(
    declaration: ProjectProcessingDeclaration,
) -> dict[str, Any]:
    starts_at = _aware_utc(declaration.starts_at)
    if starts_at.minute or starts_at.second or starts_at.microsecond:
        raise DueWorkRefusal("project-processing starts_at must align to a UTC hour")
    if not _CONFIGURATION_VERSION.fullmatch(declaration.configuration_version):
        raise DueWorkRefusal("Due Work configuration version is invalid")
    if not _EXTRACTOR_IDENTITY.fullmatch(declaration.extractor_identity):
        raise DueWorkRefusal("project-processing extractor identity is invalid")
    if (
        declaration.cadence != "hourly"
        or declaration.timezone_name != "UTC"
        or declaration.missed_run_policy != "latest_only"
    ):
        raise DueWorkRefusal(
            "project-processing supports only hourly UTC latest-only scheduling"
        )
    if not (
        1 <= declaration.max_attempts <= 5
        and 0 <= declaration.backoff_seconds <= 3600
        and 30 <= declaration.claim_ttl_seconds <= 3600
        and 1 <= declaration.deadline_seconds <= declaration.claim_ttl_seconds
        and declaration.concurrency_limit == 1
        and declaration.retention_days >= 365
        and 1 <= declaration.model_token_budget <= _PROJECT_PROCESSING_TOKEN_CEILING
        and declaration.notification_budget == 0
    ):
        raise DueWorkRefusal(
            "project-processing gate-7 resource declaration is invalid"
        )
    return {
        "schema_version": "due-work-gate-7-v1",
        "handler": HANDLER_PROJECT_PROCESSING,
        "project_id": declaration.project_id,
        "scope": {
            "project_id": declaration.project_id,
            "extractor_identity": declaration.extractor_identity,
        },
        "configuration_version": declaration.configuration_version,
        "input_identity": {
            "kind": "registered_project_extraction-v1",
            "project_id": declaration.project_id,
            "extractor_identity": declaration.extractor_identity,
        },
        "starts_at": _iso(starts_at),
        "cadence": declaration.cadence,
        "timezone": declaration.timezone_name,
        "missed_run_policy": declaration.missed_run_policy,
        "retention": {
            "policy": "retain_all_terminal_receipts",
            "minimum_days": declaration.retention_days,
        },
        "retry": {
            "max_attempts": declaration.max_attempts,
            "backoff_seconds": declaration.backoff_seconds,
        },
        "resources": {
            "claim_ttl_seconds": declaration.claim_ttl_seconds,
            "deadline_seconds": declaration.deadline_seconds,
            "concurrency_limit": declaration.concurrency_limit,
            "model_token_budget": declaration.model_token_budget,
            "notification_budget": declaration.notification_budget,
        },
        "authorized_destinations": [],
        "idempotency_contract": "at_least_once_reconcilable",
    }


def _validated_revision_reconciliation_declaration(
    declaration: RevisionReconciliationDeclaration,
) -> dict[str, Any]:
    starts_at = _aware_utc(declaration.starts_at)
    if starts_at.minute or starts_at.second or starts_at.microsecond:
        raise DueWorkRefusal(
            "revision-reconciliation starts_at must align to a UTC hour"
        )
    if not _CONFIGURATION_VERSION.fullmatch(declaration.configuration_version):
        raise DueWorkRefusal("Due Work configuration version is invalid")
    if not _EXTRACTOR_IDENTITY.fullmatch(declaration.matcher_identity):
        raise DueWorkRefusal("revision-reconciliation matcher identity is invalid")
    if not _POLICY_IDENTITY.fullmatch(declaration.support_rule_identity):
        raise DueWorkRefusal(
            "revision-reconciliation support rule identity is invalid"
        )
    if (
        declaration.cadence != "hourly"
        or declaration.timezone_name != "UTC"
        or declaration.missed_run_policy != "latest_only"
    ):
        raise DueWorkRefusal(
            "revision-reconciliation supports only hourly UTC latest-only scheduling"
        )
    if not (
        1 <= declaration.max_attempts <= 5
        and 0 <= declaration.backoff_seconds <= 3600
        and 30 <= declaration.claim_ttl_seconds <= 3600
        and 1 <= declaration.deadline_seconds <= declaration.claim_ttl_seconds
        and declaration.concurrency_limit == 1
        and declaration.retention_days >= 365
        and declaration.model_token_budget == 0
        and declaration.notification_budget == 0
    ):
        raise DueWorkRefusal(
            "revision-reconciliation gate-7 resource declaration is invalid"
        )
    return {
        "schema_version": "due-work-gate-7-v1",
        "handler": HANDLER_REVISION_RECONCILIATION,
        "project_id": declaration.project_id,
        "scope": {
            "project_id": declaration.project_id,
            "matcher_identity": declaration.matcher_identity,
            "support_rule_identity": declaration.support_rule_identity,
        },
        "configuration_version": declaration.configuration_version,
        "input_identity": {
            "kind": "registered_project_revision-v1",
            "project_id": declaration.project_id,
            "matcher_identity": declaration.matcher_identity,
            "support_rule_identity": declaration.support_rule_identity,
        },
        "starts_at": _iso(starts_at),
        "cadence": declaration.cadence,
        "timezone": declaration.timezone_name,
        "missed_run_policy": declaration.missed_run_policy,
        "retention": {
            "policy": "retain_all_terminal_receipts",
            "minimum_days": declaration.retention_days,
        },
        "retry": {
            "max_attempts": declaration.max_attempts,
            "backoff_seconds": declaration.backoff_seconds,
        },
        "resources": {
            "claim_ttl_seconds": declaration.claim_ttl_seconds,
            "deadline_seconds": declaration.deadline_seconds,
            "concurrency_limit": declaration.concurrency_limit,
            "model_token_budget": declaration.model_token_budget,
            "notification_budget": declaration.notification_budget,
        },
        "authorized_destinations": [],
        "idempotency_contract": "at_least_once_reconcilable",
    }


def _validated_location_discovery_declaration(
    declaration: "LocationDiscoveryDeclaration",
) -> dict[str, Any]:
    from urllib.parse import urlparse

    starts_at = _aware_utc(declaration.starts_at)
    if starts_at.minute or starts_at.second or starts_at.microsecond:
        raise DueWorkRefusal("location-discovery starts_at must align to a UTC hour")
    if not _CONFIGURATION_VERSION.fullmatch(declaration.configuration_version):
        raise DueWorkRefusal("Due Work configuration version is invalid")
    if not _LOCATION_IDENTITY.fullmatch(declaration.location_id):
        raise DueWorkRefusal("location-discovery location identity is invalid")
    if not _EXTRACTOR_IDENTITY.fullmatch(declaration.adapter_identity):
        raise DueWorkRefusal("location-discovery adapter identity is invalid")
    if declaration.adapter_identity not in {
        "http-index-v1",
        "txdot-rid-box-v1",
    }:
        raise DueWorkRefusal("location-discovery adapter is not installed")
    rid_link_text = (
        declaration.rid_link_text.strip()
        if isinstance(declaration.rid_link_text, str)
        else None
    )
    if declaration.adapter_identity == "txdot-rid-box-v1" and (
        not rid_link_text or len(rid_link_text) > 128
    ):
        raise DueWorkRefusal("location-discovery TxDOT RID link text is invalid")
    if declaration.adapter_identity == "http-index-v1" and rid_link_text is not None:
        raise DueWorkRefusal("http-index-v1 does not accept TxDOT RID link text")
    if not _LOCATION_IDENTITY.fullmatch(declaration.source_manifest_id):
        raise DueWorkRefusal("location-discovery source manifest identity is invalid")
    hosts = tuple(dict.fromkeys(declaration.authorized_hosts))
    if not hosts or any(not _LOCATION_HOST.fullmatch(host) for host in hosts):
        raise DueWorkRefusal("location-discovery authorized hosts are invalid")
    parsed_index = urlparse(declaration.index_url)
    if (
        parsed_index.scheme not in ("http", "https")
        or not parsed_index.hostname
        or parsed_index.hostname not in hosts
    ):
        raise DueWorkRefusal(
            "location-discovery index url must be http(s) within an authorized host"
        )
    if (
        declaration.cadence != "hourly"
        or declaration.timezone_name != "UTC"
        or declaration.missed_run_policy != "latest_only"
    ):
        raise DueWorkRefusal(
            "location-discovery supports only hourly UTC latest-only scheduling"
        )
    if not (
        1 <= declaration.nested_archive_depth <= 8
        and 1 <= declaration.max_archive_compressed_mib <= 4096
        and 1 <= declaration.max_member_decompressed_mib <= 4096
        and 1 <= declaration.enumeration_limit <= 100_000
        and 1 <= declaration.request_limit <= 100_000
        and 1 <= declaration.document_limit <= 100_000
    ):
        raise DueWorkRefusal(
            "location-discovery archive/resource limits are invalid"
        )
    if not (
        1 <= declaration.max_attempts <= 5
        and 0 <= declaration.backoff_seconds <= 3600
        and 30 <= declaration.claim_ttl_seconds <= 3600
        and 1 <= declaration.deadline_seconds <= declaration.claim_ttl_seconds
        and declaration.concurrency_limit == 1
        and declaration.retention_days >= 365
        and declaration.model_token_budget == 0
        and declaration.notification_budget == 0
    ):
        raise DueWorkRefusal(
            "location-discovery gate-7 resource declaration is invalid"
        )
    scope = {
        "project_id": declaration.project_id,
        "location_id": declaration.location_id,
        "adapter_identity": declaration.adapter_identity,
        "source_manifest_id": declaration.source_manifest_id,
        "index_url": declaration.index_url,
        "authorized_hosts": sorted(hosts),
        "sealed": declaration.sealed,
        "nested_archive_depth": declaration.nested_archive_depth,
        "max_archive_compressed_mib": declaration.max_archive_compressed_mib,
        "max_member_decompressed_mib": declaration.max_member_decompressed_mib,
        "enumeration_limit": declaration.enumeration_limit,
        "request_limit": declaration.request_limit,
        "document_limit": declaration.document_limit,
    }
    if rid_link_text is not None:
        scope["rid_link_text"] = rid_link_text
    return {
        "schema_version": "due-work-gate-7-v1",
        "handler": HANDLER_LOCATION_DISCOVERY,
        "project_id": declaration.project_id,
        "scope": scope,
        "configuration_version": declaration.configuration_version,
        "input_identity": {
            "kind": "connected_location-v1",
            "project_id": declaration.project_id,
            "location_id": declaration.location_id,
            "adapter_identity": declaration.adapter_identity,
            "source_manifest_id": declaration.source_manifest_id,
        },
        "starts_at": _iso(starts_at),
        "cadence": declaration.cadence,
        "timezone": declaration.timezone_name,
        "missed_run_policy": declaration.missed_run_policy,
        "retention": {
            "policy": "retain_all_terminal_receipts",
            "minimum_days": declaration.retention_days,
        },
        "retry": {
            "max_attempts": declaration.max_attempts,
            "backoff_seconds": declaration.backoff_seconds,
        },
        "resources": {
            "claim_ttl_seconds": declaration.claim_ttl_seconds,
            "deadline_seconds": declaration.deadline_seconds,
            "concurrency_limit": declaration.concurrency_limit,
            "model_token_budget": declaration.model_token_budget,
            "notification_budget": declaration.notification_budget,
        },
        "authorized_destinations": [],
        "idempotency_contract": "at_least_once_reconcilable",
    }


def _validated_assignment_notification_declaration(
    declaration: AssignmentNotificationDeclaration,
) -> dict[str, Any]:
    starts_at = _aware_utc(declaration.starts_at)
    if starts_at.minute or starts_at.second or starts_at.microsecond:
        raise DueWorkRefusal(
            "assignment-notification starts_at must align to a UTC hour"
        )
    if not _CONFIGURATION_VERSION.fullmatch(declaration.configuration_version):
        raise DueWorkRefusal("Due Work configuration version is invalid")
    if declaration.channel != "email":
        raise DueWorkRefusal(
            "assignment-notification supports only the email channel in this slice"
        )
    if (
        declaration.cadence != "hourly"
        or declaration.timezone_name != "UTC"
        or declaration.missed_run_policy != "latest_only"
    ):
        raise DueWorkRefusal(
            "assignment-notification supports only hourly UTC latest-only scheduling"
        )
    if not (
        1 <= declaration.max_attempts <= 5
        and 1 <= declaration.backoff_seconds <= 3600
        and 30 <= declaration.claim_ttl_seconds <= 3600
        and 1 <= declaration.deadline_seconds <= declaration.claim_ttl_seconds
        and declaration.concurrency_limit == 1
        and declaration.retention_days >= 365
        and declaration.model_token_budget == 0
        and 1 <= declaration.notification_budget <= _ASSIGNMENT_NOTIFICATION_BUDGET_CEILING
    ):
        raise DueWorkRefusal(
            "assignment-notification gate-7 resource declaration is invalid"
        )
    return {
        "schema_version": "due-work-gate-7-v1",
        "handler": HANDLER_ASSIGNMENT_NOTIFICATION,
        "project_id": declaration.project_id,
        "scope": {
            "project_id": declaration.project_id,
            "channel": declaration.channel,
        },
        "configuration_version": declaration.configuration_version,
        "input_identity": {
            "kind": "project_assignment_notifications-v1",
            "project_id": declaration.project_id,
            "channel": declaration.channel,
        },
        # The recipient/contact mapping this delivery is authorized to use. It is
        # fixed for this slice: a recipient resolves only through the verified
        # contact for the selected roster identity's principal.
        "recipient_contact_source": "verified_person_identity-v1",
        "starts_at": _iso(starts_at),
        "cadence": declaration.cadence,
        "timezone": declaration.timezone_name,
        "missed_run_policy": declaration.missed_run_policy,
        "retention": {
            "policy": "retain_all_terminal_receipts",
            "minimum_days": declaration.retention_days,
        },
        "retry": {
            "max_attempts": declaration.max_attempts,
            "backoff_seconds": declaration.backoff_seconds,
        },
        "resources": {
            "claim_ttl_seconds": declaration.claim_ttl_seconds,
            "deadline_seconds": declaration.deadline_seconds,
            "concurrency_limit": declaration.concurrency_limit,
            "model_token_budget": declaration.model_token_budget,
            "notification_budget": declaration.notification_budget,
        },
        "authorized_destinations": [],
        "idempotency_contract": "at_least_once_reconcilable",
    }


def _validated_due_action_notification_declaration(
    declaration: DueActionNotificationDeclaration,
) -> dict[str, Any]:
    starts_at = _aware_utc(declaration.starts_at)
    if starts_at.minute or starts_at.second or starts_at.microsecond:
        raise DueWorkRefusal(
            "due-action-notification starts_at must align to a UTC hour"
        )
    if not _CONFIGURATION_VERSION.fullmatch(declaration.configuration_version):
        raise DueWorkRefusal("Due Work configuration version is invalid")
    if declaration.channel != "email":
        raise DueWorkRefusal(
            "due-action-notification supports only the email channel in this slice"
        )
    if (
        declaration.cadence != "hourly"
        or declaration.timezone_name != "UTC"
        or declaration.missed_run_policy != "latest_only"
    ):
        raise DueWorkRefusal(
            "due-action-notification supports only hourly UTC latest-only scheduling"
        )
    # The urgent-overdue criterion and the daily-summary window must be declared
    # explicitly and in range; a silent or out-of-range value refuses rather than
    # inventing an urgency rule or a window.
    if not 1 <= declaration.urgent_overdue_days <= 3650:
        raise DueWorkRefusal(
            "due-action-notification urgent-overdue threshold is invalid"
        )
    if (
        isinstance(declaration.escalation_roster_entry_id, bool)
        or (
            declaration.escalation_roster_entry_id is not None
            and (
                not isinstance(declaration.escalation_roster_entry_id, int)
                or declaration.escalation_roster_entry_id <= 0
            )
        )
    ):
        raise DueWorkRefusal(
            "due-action-notification escalation contact mapping is invalid"
        )
    if not 0 <= declaration.summary_hour_utc <= 23:
        raise DueWorkRefusal(
            "due-action-notification daily-summary hour is invalid"
        )
    if not 1 <= declaration.summary_window_days <= 365:
        raise DueWorkRefusal(
            "due-action-notification daily-summary window is invalid"
        )
    if not (
        1 <= declaration.max_attempts <= 5
        and 1 <= declaration.backoff_seconds <= 3600
        and 30 <= declaration.claim_ttl_seconds <= 3600
        and 1 <= declaration.deadline_seconds <= declaration.claim_ttl_seconds
        and declaration.concurrency_limit == 1
        and declaration.retention_days >= 365
        and declaration.model_token_budget == 0
        and 1
        <= declaration.notification_budget
        <= _DUE_ACTION_NOTIFICATION_BUDGET_CEILING
    ):
        raise DueWorkRefusal(
            "due-action-notification gate-7 resource declaration is invalid"
        )
    escalation_contact = (
        "one_configured_roster_identity"
        if declaration.escalation_roster_entry_id is not None
        else "disabled"
    )
    return {
        "schema_version": "due-work-gate-7-v1",
        "handler": HANDLER_DUE_ACTION_NOTIFICATION,
        "project_id": declaration.project_id,
        "scope": {
            "project_id": declaration.project_id,
            "channel": declaration.channel,
            "urgent_overdue_days": declaration.urgent_overdue_days,
            "escalation_roster_entry_id": declaration.escalation_roster_entry_id,
            "summary_hour_utc": declaration.summary_hour_utc,
            "summary_window_days": declaration.summary_window_days,
        },
        "configuration_version": declaration.configuration_version,
        "input_identity": {
            "kind": "project_due_action_notifications-v1",
            "project_id": declaration.project_id,
            "channel": declaration.channel,
        },
        # The recipient/contact mapping this delivery is authorized to use. A
        # reminder recipient resolves only through the verified contact for the
        # accountable roster identity; escalation uses the one declared contact.
        "recipient_contact_source": "verified_person_identity-v1",
        "urgent_overdue": {
            "criterion": "action_overdue_days",
            "threshold_days": declaration.urgent_overdue_days,
            "escalation_contact": escalation_contact,
        },
        "daily_summary": {
            "cadence": "daily",
            "hour_utc": declaration.summary_hour_utc,
            "window_days": declaration.summary_window_days,
        },
        "starts_at": _iso(starts_at),
        "cadence": declaration.cadence,
        "timezone": declaration.timezone_name,
        "missed_run_policy": declaration.missed_run_policy,
        "retention": {
            "policy": "retain_all_terminal_receipts",
            "minimum_days": declaration.retention_days,
        },
        "retry": {
            "max_attempts": declaration.max_attempts,
            "backoff_seconds": declaration.backoff_seconds,
        },
        "resources": {
            "claim_ttl_seconds": declaration.claim_ttl_seconds,
            "deadline_seconds": declaration.deadline_seconds,
            "concurrency_limit": declaration.concurrency_limit,
            "model_token_budget": declaration.model_token_budget,
            "notification_budget": declaration.notification_budget,
        },
        "authorized_destinations": [],
        "idempotency_contract": "at_least_once_reconcilable",
    }


def _validated_document_notification_declaration(
    declaration: DocumentNotificationDeclaration,
) -> dict[str, Any]:
    starts_at = _aware_utc(declaration.starts_at)
    if starts_at.minute or starts_at.second or starts_at.microsecond:
        raise DueWorkRefusal(
            "document-notification starts_at must align to a UTC hour"
        )
    if not _CONFIGURATION_VERSION.fullmatch(declaration.configuration_version):
        raise DueWorkRefusal("Due Work configuration version is invalid")
    if declaration.channel != "email":
        raise DueWorkRefusal(
            "document-notification supports only the email channel in this slice"
        )
    if (
        declaration.cadence != "hourly"
        or declaration.timezone_name != "UTC"
        or declaration.missed_run_policy != "latest_only"
    ):
        raise DueWorkRefusal(
            "document-notification supports only hourly UTC latest-only scheduling"
        )
    if not (
        1 <= declaration.max_attempts <= 5
        and 1 <= declaration.backoff_seconds <= 3600
        and 30 <= declaration.claim_ttl_seconds <= 3600
        and 1 <= declaration.deadline_seconds <= declaration.claim_ttl_seconds
        and declaration.concurrency_limit == 1
        and declaration.retention_days >= 365
        and declaration.model_token_budget == 0
        and 1 <= declaration.notification_budget <= _DOCUMENT_NOTIFICATION_BUDGET_CEILING
    ):
        raise DueWorkRefusal(
            "document-notification gate-7 resource declaration is invalid"
        )
    return {
        "schema_version": "due-work-gate-7-v1",
        "handler": HANDLER_DOCUMENT_NOTIFICATION,
        "project_id": declaration.project_id,
        "scope": {
            "project_id": declaration.project_id,
            "channel": declaration.channel,
        },
        "configuration_version": declaration.configuration_version,
        "input_identity": {
            "kind": "project_document_notifications-v1",
            "project_id": declaration.project_id,
            "channel": declaration.channel,
        },
        # The project/source/subject scope this delivery is authorized for: the
        # two approved categories over the recorded affected population, reaching
        # the typed current assignee and the original reviewer only through their
        # verified-contact records.
        "subject_scope": "documentation_loss_and_document_change-v1",
        "recipient_contact_source": "verified_person_identity-v1",
        "starts_at": _iso(starts_at),
        "cadence": declaration.cadence,
        "timezone": declaration.timezone_name,
        "missed_run_policy": declaration.missed_run_policy,
        "retention": {
            "policy": "retain_all_terminal_receipts",
            "minimum_days": declaration.retention_days,
        },
        "retry": {
            "max_attempts": declaration.max_attempts,
            "backoff_seconds": declaration.backoff_seconds,
        },
        "resources": {
            "claim_ttl_seconds": declaration.claim_ttl_seconds,
            "deadline_seconds": declaration.deadline_seconds,
            "concurrency_limit": declaration.concurrency_limit,
            "model_token_budget": declaration.model_token_budget,
            "notification_budget": declaration.notification_budget,
        },
        "authorized_destinations": [],
        "idempotency_contract": "at_least_once_reconcilable",
    }


def _validated_event_admission_reproof_declaration(
    declaration: EventAdmissionReproofDeclaration,
) -> dict[str, Any]:
    starts_at = _aware_utc(declaration.starts_at)
    if starts_at.minute or starts_at.second or starts_at.microsecond:
        raise DueWorkRefusal(
            "event-admission-reproof starts_at must align to a UTC hour"
        )
    if not _CONFIGURATION_VERSION.fullmatch(declaration.configuration_version):
        raise DueWorkRefusal("Due Work configuration version is invalid")
    if not _POLICY_IDENTITY.fullmatch(declaration.policy_version):
        raise DueWorkRefusal("event-admission-reproof policy version is invalid")
    if not _POLICY_IDENTITY.fullmatch(declaration.reason_version):
        raise DueWorkRefusal("event-admission-reproof reason version is invalid")
    if not _POLICY_IDENTITY.fullmatch(declaration.selection_rule):
        raise DueWorkRefusal("event-admission-reproof selection rule is invalid")
    if (
        declaration.cadence != "hourly"
        or declaration.timezone_name != "UTC"
        or declaration.missed_run_policy != "latest_only"
    ):
        raise DueWorkRefusal(
            "event-admission-reproof supports only hourly UTC latest-only scheduling"
        )
    if not (
        1 <= declaration.max_attempts <= 5
        and 0 <= declaration.backoff_seconds <= 3600
        and 30 <= declaration.claim_ttl_seconds <= 3600
        and 1 <= declaration.deadline_seconds <= declaration.claim_ttl_seconds
        and declaration.concurrency_limit == 1
        and declaration.retention_days >= 365
        and declaration.model_token_budget == 0
        and declaration.notification_budget == 0
        and 1 <= declaration.clone_budget <= _REPROOF_CLONE_BUDGET_CEILING
    ):
        raise DueWorkRefusal(
            "event-admission-reproof gate-7 resource declaration is invalid"
        )
    return {
        "schema_version": "due-work-gate-7-v1",
        "handler": HANDLER_EVENT_ADMISSION_REPROOF,
        "project_id": declaration.project_id,
        "scope": {
            "project_id": declaration.project_id,
            "policy_version": declaration.policy_version,
            "reason_version": declaration.reason_version,
            "selection_rule": declaration.selection_rule,
        },
        "configuration_version": declaration.configuration_version,
        "input_identity": {
            "kind": "one_project_event_admission_class-v1",
            "project_id": declaration.project_id,
            "policy_version": declaration.policy_version,
            "reason_version": declaration.reason_version,
            "selection_rule": declaration.selection_rule,
        },
        "starts_at": _iso(starts_at),
        "cadence": declaration.cadence,
        "timezone": declaration.timezone_name,
        "missed_run_policy": declaration.missed_run_policy,
        "retention": {
            "policy": "retain_all_terminal_receipts",
            "minimum_days": declaration.retention_days,
        },
        "retry": {
            "max_attempts": declaration.max_attempts,
            "backoff_seconds": declaration.backoff_seconds,
        },
        "resources": {
            "claim_ttl_seconds": declaration.claim_ttl_seconds,
            "deadline_seconds": declaration.deadline_seconds,
            "concurrency_limit": declaration.concurrency_limit,
            "model_token_budget": declaration.model_token_budget,
            "notification_budget": declaration.notification_budget,
            "clone_budget": declaration.clone_budget,
        },
        "authorized_destinations": [],
        "idempotency_contract": "at_least_once_reconcilable",
    }


def _validated_evidence_outcome_capture_declaration(
    declaration: EvidenceOutcomeCaptureDeclaration,
) -> dict[str, Any]:
    starts_at = _aware_utc(declaration.starts_at)
    if starts_at.minute or starts_at.second or starts_at.microsecond:
        raise DueWorkRefusal("outcome-capture starts_at must align to a UTC hour")
    if not _CONFIGURATION_VERSION.fullmatch(declaration.configuration_version):
        raise DueWorkRefusal("Due Work configuration version is invalid")
    if not _COHORT_IDENTITY.fullmatch(declaration.cohort_id):
        raise DueWorkRefusal("outcome-capture cohort identity is invalid")
    if not _SHA256.fullmatch(declaration.observation_contract_sha256):
        raise DueWorkRefusal("outcome-capture observation contract must be SHA-256")
    if (
        declaration.cadence != "hourly"
        or declaration.timezone_name != "UTC"
        or declaration.missed_run_policy != "latest_only"
    ):
        raise DueWorkRefusal(
            "outcome-capture supports only hourly UTC latest-only scheduling"
        )
    if not (
        1 <= declaration.max_attempts <= 5
        and 0 <= declaration.backoff_seconds <= 3600
        and 30 <= declaration.claim_ttl_seconds <= 3600
        and 1 <= declaration.deadline_seconds <= declaration.claim_ttl_seconds
        and declaration.concurrency_limit == 1
        and declaration.retention_days >= 365
        and declaration.model_token_budget == 0
        and declaration.notification_budget == 0
    ):
        raise DueWorkRefusal("outcome-capture gate-7 resource declaration is invalid")
    return {
        "schema_version": "due-work-gate-7-v1",
        "handler": HANDLER_EVIDENCE_OUTCOME_CAPTURE,
        "project_id": declaration.project_id,
        "scope": {
            "project_id": declaration.project_id,
            "cohort_id": declaration.cohort_id,
            "observation_contract_sha256": declaration.observation_contract_sha256,
        },
        "configuration_version": declaration.configuration_version,
        "input_identity": {
            "kind": "declared_capture_cohort-v1",
            "project_id": declaration.project_id,
            "cohort_id": declaration.cohort_id,
            "observation_contract_sha256": declaration.observation_contract_sha256,
        },
        "starts_at": _iso(starts_at),
        "cadence": declaration.cadence,
        "timezone": declaration.timezone_name,
        "missed_run_policy": declaration.missed_run_policy,
        "retention": {
            "policy": "retain_all_terminal_receipts",
            "minimum_days": declaration.retention_days,
        },
        "retry": {
            "max_attempts": declaration.max_attempts,
            "backoff_seconds": declaration.backoff_seconds,
        },
        "resources": {
            "claim_ttl_seconds": declaration.claim_ttl_seconds,
            "deadline_seconds": declaration.deadline_seconds,
            "concurrency_limit": declaration.concurrency_limit,
            "model_token_budget": declaration.model_token_budget,
            "notification_budget": declaration.notification_budget,
        },
        "authorized_destinations": [],
        "idempotency_contract": "at_least_once_reconcilable",
    }


def _validated_report_publication_declaration(
    declaration: ReportPublicationDeclaration,
) -> dict[str, Any]:
    starts_at = _aware_utc(declaration.starts_at)
    if starts_at.minute or starts_at.second or starts_at.microsecond:
        raise DueWorkRefusal("report-publication starts_at must align to a UTC hour")
    if not _CONFIGURATION_VERSION.fullmatch(declaration.configuration_version):
        raise DueWorkRefusal("Due Work configuration version is invalid")
    if declaration.provenance_mode not in (
        "all-supported-sources",
        "document-only",
    ):
        raise DueWorkRefusal("report-publication provenance mode is invalid")
    if not isinstance(declaration.prepare_external_pdf, bool):
        raise DueWorkRefusal("report-publication external preparation flag is invalid")
    if (
        declaration.cadence != "weekly"
        or declaration.timezone_name != "UTC"
        or declaration.missed_run_policy != "latest_only"
        or declaration.comparison_window_policy != "since_last_released"
    ):
        raise DueWorkRefusal(
            "report-publication supports only weekly UTC latest-only scheduling "
            "against the last released report"
        )
    if not (
        1 <= declaration.max_attempts <= 5
        and 0 <= declaration.backoff_seconds <= 3600
        and 30 <= declaration.claim_ttl_seconds <= 3600
        and 1 <= declaration.deadline_seconds <= declaration.claim_ttl_seconds
        and declaration.concurrency_limit == 1
        and declaration.retention_days >= 365
        and declaration.model_token_budget == 0
        and declaration.notification_budget == 0
    ):
        raise DueWorkRefusal(
            "report-publication gate-7 resource declaration is invalid"
        )
    return {
        "schema_version": "due-work-gate-7-v1",
        "handler": HANDLER_REPORT_PUBLICATION,
        "project_id": declaration.project_id,
        "scope": {
            "project_id": declaration.project_id,
            "provenance_mode": declaration.provenance_mode,
            "prepare_external_pdf": declaration.prepare_external_pdf,
        },
        "configuration_version": declaration.configuration_version,
        "input_identity": {
            "kind": "scheduled_report_publication-v1",
            "project_id": declaration.project_id,
            "provenance_mode": declaration.provenance_mode,
            "prepare_external_pdf": declaration.prepare_external_pdf,
        },
        "output": {
            "internal_snapshot": True,
            "external_preparation": declaration.prepare_external_pdf,
        },
        "comparison_window_policy": declaration.comparison_window_policy,
        "starts_at": _iso(starts_at),
        "cadence": declaration.cadence,
        "timezone": declaration.timezone_name,
        "missed_run_policy": declaration.missed_run_policy,
        "retention": {
            "policy": "retain_all_terminal_receipts",
            "minimum_days": declaration.retention_days,
        },
        "retry": {
            "max_attempts": declaration.max_attempts,
            "backoff_seconds": declaration.backoff_seconds,
        },
        "resources": {
            "claim_ttl_seconds": declaration.claim_ttl_seconds,
            "deadline_seconds": declaration.deadline_seconds,
            "concurrency_limit": declaration.concurrency_limit,
            "model_token_budget": declaration.model_token_budget,
            "notification_budget": declaration.notification_budget,
        },
        "authorized_destinations": [],
        "idempotency_contract": "at_least_once_reconcilable",
    }


def _validated_connector_polling_declaration(
    declaration: ConnectorPollingDeclaration,
) -> dict[str, Any]:
    from urllib.parse import urlparse

    from corridor.connector_polling import CONNECTOR_FACTORIES

    starts_at = _aware_utc(declaration.starts_at)
    if starts_at.minute or starts_at.second or starts_at.microsecond:
        raise DueWorkRefusal("connector-polling starts_at must align to a UTC hour")
    if not _CONFIGURATION_VERSION.fullmatch(declaration.configuration_version):
        raise DueWorkRefusal("Due Work configuration version is invalid")
    if not _COHORT_IDENTITY.fullmatch(declaration.customer):
        raise DueWorkRefusal("connector-polling customer identity is invalid")
    if not _LOCATION_IDENTITY.fullmatch(declaration.channel):
        raise DueWorkRefusal("connector-polling channel identity is invalid")
    if not _LOCATION_IDENTITY.fullmatch(declaration.connector_identity):
        raise DueWorkRefusal("connector-polling connector identity is invalid")
    if declaration.connector_identity not in CONNECTOR_FACTORIES:
        raise DueWorkRefusal("connector-polling connector is not installed")
    parsed = urlparse(declaration.source_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not _LOCATION_HOST.fullmatch(parsed.hostname)
        or len(declaration.source_url) > 2048
    ):
        raise DueWorkRefusal(
            "connector-polling source url must be one https location"
        )
    if (
        declaration.cadence != "hourly"
        or declaration.timezone_name != "UTC"
        or declaration.missed_run_policy != "latest_only"
    ):
        raise DueWorkRefusal(
            "connector-polling supports only hourly UTC latest-only scheduling"
        )
    if not (
        1 <= declaration.max_attempts <= 5
        and 0 <= declaration.backoff_seconds <= 3600
        and 30 <= declaration.claim_ttl_seconds <= 3600
        and 1 <= declaration.deadline_seconds <= declaration.claim_ttl_seconds
        and declaration.concurrency_limit == 1
        and declaration.retention_days >= 365
        and declaration.model_token_budget == 0
        and declaration.notification_budget == 0
    ):
        raise DueWorkRefusal("connector-polling gate-7 resource declaration is invalid")
    return {
        "schema_version": "due-work-gate-7-v1",
        "handler": HANDLER_CONNECTOR_POLLING,
        "project_id": declaration.project_id,
        "scope": {
            "project_id": declaration.project_id,
            "customer": declaration.customer,
            "channel": declaration.channel,
            "connector_identity": declaration.connector_identity,
            "source_url": declaration.source_url,
        },
        "configuration_version": declaration.configuration_version,
        "input_identity": {
            "kind": "declared_pull_connector-v1",
            "project_id": declaration.project_id,
            "customer": declaration.customer,
            "channel": declaration.channel,
            "connector_identity": declaration.connector_identity,
            "source_url": declaration.source_url,
        },
        # ADR-0083: the token advances only after every change up to it is in
        # the content-addressed store under its digest. The retained completed
        # receipt is where this schedule's token stands.
        "checkpoint_policy": "advance_after_durable_storage",
        "starts_at": _iso(starts_at),
        "cadence": declaration.cadence,
        "timezone": declaration.timezone_name,
        "missed_run_policy": declaration.missed_run_policy,
        "retention": {
            "policy": "retain_all_terminal_receipts",
            "minimum_days": declaration.retention_days,
        },
        "retry": {
            "max_attempts": declaration.max_attempts,
            "backoff_seconds": declaration.backoff_seconds,
        },
        "resources": {
            "claim_ttl_seconds": declaration.claim_ttl_seconds,
            "deadline_seconds": declaration.deadline_seconds,
            "concurrency_limit": declaration.concurrency_limit,
            "model_token_budget": declaration.model_token_budget,
            "notification_budget": declaration.notification_budget,
        },
        "authorized_destinations": [],
        "idempotency_contract": "at_least_once_reconcilable",
    }


def _validated_delta_generation_declaration(
    declaration: DeltaGenerationDeclaration,
) -> dict[str, Any]:
    starts_at = _aware_utc(declaration.starts_at)
    if starts_at.minute or starts_at.second or starts_at.microsecond:
        raise DueWorkRefusal("delta-generation starts_at must align to a UTC hour")
    if not _CONFIGURATION_VERSION.fullmatch(declaration.configuration_version):
        raise DueWorkRefusal("Due Work configuration version is invalid")
    if not _POLICY_IDENTITY.fullmatch(declaration.comparison_rule_version):
        raise DueWorkRefusal("delta-generation comparison rule identity is invalid")
    if (
        declaration.cadence != "hourly"
        or declaration.timezone_name != "UTC"
        or declaration.missed_run_policy != "latest_only"
    ):
        raise DueWorkRefusal(
            "delta-generation supports only hourly UTC latest-only scheduling"
        )
    if not (
        1 <= declaration.max_attempts <= 5
        and 0 <= declaration.backoff_seconds <= 3600
        and 30 <= declaration.claim_ttl_seconds <= 3600
        and 1 <= declaration.deadline_seconds <= declaration.claim_ttl_seconds
        and declaration.concurrency_limit == 1
        and declaration.retention_days >= 365
        and declaration.model_token_budget == 0
        and declaration.notification_budget == 0
    ):
        raise DueWorkRefusal("delta-generation gate-7 resource declaration is invalid")
    return {
        "schema_version": "due-work-gate-7-v1",
        "handler": HANDLER_DELTA_GENERATION,
        "project_id": declaration.project_id,
        "scope": {
            "project_id": declaration.project_id,
            "comparison_rule_version": declaration.comparison_rule_version,
        },
        "configuration_version": declaration.configuration_version,
        "input_identity": {
            "kind": "project_proposed_delta_generation-v1",
            "project_id": declaration.project_id,
            "comparison_rule_version": declaration.comparison_rule_version,
        },
        # The pass proposes; it never makes anything effective. Every append
        # goes through the source-append command, so an adopted-baseline
        # project's accepted values are unreachable from here (#520).
        "record_authority": "proposes_only_never_accepts",
        "starts_at": _iso(starts_at),
        "cadence": declaration.cadence,
        "timezone": declaration.timezone_name,
        "missed_run_policy": declaration.missed_run_policy,
        "retention": {
            "policy": "retain_all_terminal_receipts",
            "minimum_days": declaration.retention_days,
        },
        "retry": {
            "max_attempts": declaration.max_attempts,
            "backoff_seconds": declaration.backoff_seconds,
        },
        "resources": {
            "claim_ttl_seconds": declaration.claim_ttl_seconds,
            "deadline_seconds": declaration.deadline_seconds,
            "concurrency_limit": declaration.concurrency_limit,
            "model_token_budget": declaration.model_token_budget,
            "notification_budget": declaration.notification_budget,
        },
        "authorized_destinations": [],
        "idempotency_contract": "at_least_once_reconcilable",
    }


def _validated_report_preparation_declaration(
    declaration: ReportPreparationDeclaration,
) -> dict[str, Any]:
    starts_at = _aware_utc(declaration.starts_at)
    if starts_at.minute or starts_at.second or starts_at.microsecond:
        raise DueWorkRefusal("report-preparation starts_at must align to a UTC hour")
    if not _CONFIGURATION_VERSION.fullmatch(declaration.configuration_version):
        raise DueWorkRefusal("Due Work configuration version is invalid")
    if (
        declaration.cadence != "weekly"
        or declaration.timezone_name != "UTC"
        or declaration.missed_run_policy != "latest_only"
    ):
        raise DueWorkRefusal(
            "report-preparation supports only weekly UTC latest-only scheduling"
        )
    if not (
        1 <= declaration.max_attempts <= 5
        and 0 <= declaration.backoff_seconds <= 3600
        and 30 <= declaration.claim_ttl_seconds <= 3600
        and 1 <= declaration.deadline_seconds <= declaration.claim_ttl_seconds
        and declaration.concurrency_limit == 1
        and declaration.retention_days >= 365
        and declaration.model_token_budget == 0
        and declaration.notification_budget == 0
    ):
        raise DueWorkRefusal("report-preparation gate-7 resource declaration is invalid")
    return {
        "schema_version": "due-work-gate-7-v1",
        "handler": HANDLER_REPORT_PREPARATION,
        "project_id": declaration.project_id,
        "scope": {"project_id": declaration.project_id},
        "configuration_version": declaration.configuration_version,
        "input_identity": {
            "kind": "project_change_summary_reading-v1",
            "project_id": declaration.project_id,
        },
        # The week the reading covers starts where the previous retained
        # reading observed, so consecutive readings tile without a gap and
        # without counting one resolution twice.
        "comparison_window_policy": "since_last_prepared_reading",
        "starts_at": _iso(starts_at),
        "cadence": declaration.cadence,
        "timezone": declaration.timezone_name,
        "missed_run_policy": declaration.missed_run_policy,
        "retention": {
            "policy": "retain_all_terminal_receipts",
            "minimum_days": declaration.retention_days,
        },
        "retry": {
            "max_attempts": declaration.max_attempts,
            "backoff_seconds": declaration.backoff_seconds,
        },
        "resources": {
            "claim_ttl_seconds": declaration.claim_ttl_seconds,
            "deadline_seconds": declaration.deadline_seconds,
            "concurrency_limit": declaration.concurrency_limit,
            "model_token_budget": declaration.model_token_budget,
            "notification_budget": declaration.notification_budget,
        },
        "authorized_destinations": [],
        "idempotency_contract": "read_only_reconcilable",
    }


def _validated_retention_sweep_declaration(
    declaration: RetentionSweepDeclaration,
) -> dict[str, Any]:
    starts_at = _aware_utc(declaration.starts_at)
    if starts_at.minute or starts_at.second or starts_at.microsecond:
        raise DueWorkRefusal("retention-sweep starts_at must align to a UTC hour")
    if not _CONFIGURATION_VERSION.fullmatch(declaration.configuration_version):
        raise DueWorkRefusal("Due Work configuration version is invalid")
    try:
        HumanPrincipal(declaration.authorized_by)
    except InvalidHumanPrincipal as exc:
        raise DueWorkRefusal(
            "retention-sweep authorization must name a human principal"
        ) from exc
    if (
        declaration.cadence != "weekly"
        or declaration.timezone_name != "UTC"
        or declaration.missed_run_policy != "latest_only"
    ):
        raise DueWorkRefusal(
            "retention-sweep supports only weekly UTC latest-only scheduling"
        )
    if not (
        1 <= declaration.max_attempts <= 5
        and 0 <= declaration.backoff_seconds <= 3600
        and 30 <= declaration.claim_ttl_seconds <= 3600
        and 1 <= declaration.deadline_seconds <= declaration.claim_ttl_seconds
        and declaration.concurrency_limit == 1
        and declaration.retention_days >= 365
        and declaration.model_token_budget == 0
        and declaration.notification_budget == 0
    ):
        raise DueWorkRefusal("retention-sweep gate-7 resource declaration is invalid")
    return {
        "schema_version": "due-work-gate-7-v1",
        "handler": HANDLER_RETENTION_SWEEP,
        "project_id": declaration.project_id,
        "scope": {
            "project_id": declaration.project_id,
            "authorized_by": declaration.authorized_by,
        },
        "configuration_version": declaration.configuration_version,
        "input_identity": {
            "kind": "project_class_b_intermediaries-v1",
            "project_id": declaration.project_id,
        },
        # ADR-0080: only the five Class B intermediary families and registered
        # processing artifacts are reachable, and every deletion still passes
        # the hold check, the reachability check, and the dry-run manifest.
        "retention_class": "class_b",
        "starts_at": _iso(starts_at),
        "cadence": declaration.cadence,
        "timezone": declaration.timezone_name,
        "missed_run_policy": declaration.missed_run_policy,
        "retention": {
            "policy": "retain_all_terminal_receipts",
            "minimum_days": declaration.retention_days,
        },
        "retry": {
            "max_attempts": declaration.max_attempts,
            "backoff_seconds": declaration.backoff_seconds,
        },
        "resources": {
            "claim_ttl_seconds": declaration.claim_ttl_seconds,
            "deadline_seconds": declaration.deadline_seconds,
            "concurrency_limit": declaration.concurrency_limit,
            "model_token_budget": declaration.model_token_budget,
            "notification_budget": declaration.notification_budget,
        },
        "authorized_destinations": [],
        "idempotency_contract": "at_least_once_reconcilable",
    }


def _due_slot(schedule: DueWorkSchedule, now: datetime) -> datetime | None:
    """The current occurrence slot for one schedule's cadence, or ``None``.

    Hourly handlers keep their existing UTC-hour slot.  A weekly publication
    aligns to its declared ``starts_at``, so every trigger within one week
    resolves to the same slot and coalesces onto one occurrence rather than
    producing a snapshot each hour; ``latest_only`` recovery then measures a
    missed week from that same weekly boundary.
    """

    now = _aware_utc(now)
    starts_at = _aware_utc(schedule.starts_at)
    if schedule.cadence == "weekly":
        if now < starts_at:
            return None
        weeks = (now - starts_at) // timedelta(days=7)
        return starts_at + weeks * timedelta(days=7)
    slot = now.replace(minute=0, second=0, microsecond=0)
    return slot if slot >= starts_at else None


def _validate_stored_schedule(schedule: DueWorkSchedule) -> None:
    if schedule.handler_key not in HANDLER_REGISTRY:
        raise DueWorkRefusal("persisted Due Work handler is not server-owned")
    if schedule.configuration_sha256 != _sha256(schedule.configuration_json):
        raise DueWorkRefusal("persisted Due Work configuration digest does not match")
    if schedule.configuration_json.get("handler") != schedule.handler_key:
        raise DueWorkRefusal("persisted Due Work configuration handler does not match")
    if schedule.handler_key == HANDLER_PROJECT_PROCESSING:
        expected_config = _validated_processing_declaration(
            ProjectProcessingDeclaration(
                project_id=schedule.project_id,
                configuration_version=schedule.configuration_version,
                extractor_identity=schedule.scope_json.get("extractor_identity", ""),
                starts_at=schedule.starts_at,
                cadence=schedule.cadence,
                timezone_name=schedule.timezone_name,
                missed_run_policy=schedule.missed_run_policy,
                retention_days=schedule.retention_days,
                max_attempts=schedule.max_attempts,
                backoff_seconds=schedule.backoff_seconds,
                claim_ttl_seconds=schedule.claim_ttl_seconds,
                deadline_seconds=schedule.deadline_seconds,
                concurrency_limit=schedule.concurrency_limit,
                model_token_budget=schedule.model_token_budget,
                notification_budget=schedule.notification_budget,
            )
        )
        expected_scope = {
            "project_id": schedule.project_id,
            "extractor_identity": schedule.scope_json.get("extractor_identity", ""),
        }
        expected_input_identity = _sha256(
            {
                "handler": schedule.handler_key,
                "project_id": schedule.project_id,
                "extractor_identity": schedule.scope_json.get("extractor_identity", ""),
            }
        )
    elif schedule.handler_key == HANDLER_REVISION_RECONCILIATION:
        matcher_identity = schedule.scope_json.get("matcher_identity", "")
        support_rule_identity = schedule.scope_json.get("support_rule_identity", "")
        expected_config = _validated_revision_reconciliation_declaration(
            RevisionReconciliationDeclaration(
                project_id=schedule.project_id,
                configuration_version=schedule.configuration_version,
                matcher_identity=matcher_identity,
                support_rule_identity=support_rule_identity,
                starts_at=schedule.starts_at,
                cadence=schedule.cadence,
                timezone_name=schedule.timezone_name,
                missed_run_policy=schedule.missed_run_policy,
                retention_days=schedule.retention_days,
                max_attempts=schedule.max_attempts,
                backoff_seconds=schedule.backoff_seconds,
                claim_ttl_seconds=schedule.claim_ttl_seconds,
                deadline_seconds=schedule.deadline_seconds,
                concurrency_limit=schedule.concurrency_limit,
                model_token_budget=schedule.model_token_budget,
                notification_budget=schedule.notification_budget,
            )
        )
        expected_scope = {
            "project_id": schedule.project_id,
            "matcher_identity": matcher_identity,
            "support_rule_identity": support_rule_identity,
        }
        expected_input_identity = _sha256(
            {
                "handler": schedule.handler_key,
                "project_id": schedule.project_id,
                "matcher_identity": matcher_identity,
                "support_rule_identity": support_rule_identity,
            }
        )
    elif schedule.handler_key == HANDLER_LOCATION_DISCOVERY:
        scope_json = schedule.scope_json
        expected_config = _validated_location_discovery_declaration(
            LocationDiscoveryDeclaration(
                project_id=schedule.project_id,
                configuration_version=schedule.configuration_version,
                location_id=scope_json.get("location_id", ""),
                adapter_identity=scope_json.get("adapter_identity", ""),
                source_manifest_id=scope_json.get("source_manifest_id", ""),
                index_url=scope_json.get("index_url", ""),
                authorized_hosts=tuple(scope_json.get("authorized_hosts", ())),
                sealed=bool(scope_json.get("sealed", False)),
                nested_archive_depth=scope_json.get("nested_archive_depth", 0),
                max_archive_compressed_mib=scope_json.get(
                    "max_archive_compressed_mib", 0
                ),
                max_member_decompressed_mib=scope_json.get(
                    "max_member_decompressed_mib", 0
                ),
                enumeration_limit=scope_json.get("enumeration_limit", 0),
                request_limit=scope_json.get("request_limit", 0),
                document_limit=scope_json.get("document_limit", 0),
                rid_link_text=scope_json.get("rid_link_text"),
                starts_at=schedule.starts_at,
                cadence=schedule.cadence,
                timezone_name=schedule.timezone_name,
                missed_run_policy=schedule.missed_run_policy,
                retention_days=schedule.retention_days,
                max_attempts=schedule.max_attempts,
                backoff_seconds=schedule.backoff_seconds,
                claim_ttl_seconds=schedule.claim_ttl_seconds,
                deadline_seconds=schedule.deadline_seconds,
                concurrency_limit=schedule.concurrency_limit,
                model_token_budget=schedule.model_token_budget,
                notification_budget=schedule.notification_budget,
            )
        )
        expected_scope = expected_config["scope"]
        expected_input_identity = _sha256(
            {
                "handler": schedule.handler_key,
                "project_id": schedule.project_id,
                "location_id": scope_json.get("location_id", ""),
                "adapter_identity": scope_json.get("adapter_identity", ""),
                "source_manifest_id": scope_json.get("source_manifest_id", ""),
            }
        )
    elif schedule.handler_key == HANDLER_ASSIGNMENT_NOTIFICATION:
        channel = schedule.scope_json.get("channel", "")
        expected_config = _validated_assignment_notification_declaration(
            AssignmentNotificationDeclaration(
                project_id=schedule.project_id,
                configuration_version=schedule.configuration_version,
                channel=channel,
                starts_at=schedule.starts_at,
                cadence=schedule.cadence,
                timezone_name=schedule.timezone_name,
                missed_run_policy=schedule.missed_run_policy,
                retention_days=schedule.retention_days,
                max_attempts=schedule.max_attempts,
                backoff_seconds=schedule.backoff_seconds,
                claim_ttl_seconds=schedule.claim_ttl_seconds,
                deadline_seconds=schedule.deadline_seconds,
                concurrency_limit=schedule.concurrency_limit,
                model_token_budget=schedule.model_token_budget,
                notification_budget=schedule.notification_budget,
            )
        )
        expected_scope = {
            "project_id": schedule.project_id,
            "channel": channel,
        }
        expected_input_identity = _sha256(
            {
                "handler": schedule.handler_key,
                "project_id": schedule.project_id,
                "channel": channel,
            }
        )
    elif schedule.handler_key == HANDLER_DUE_ACTION_NOTIFICATION:
        scope_json = schedule.scope_json
        channel = scope_json.get("channel", "")
        expected_config = _validated_due_action_notification_declaration(
            DueActionNotificationDeclaration(
                project_id=schedule.project_id,
                configuration_version=schedule.configuration_version,
                channel=channel,
                urgent_overdue_days=scope_json.get("urgent_overdue_days", 0),
                escalation_roster_entry_id=scope_json.get(
                    "escalation_roster_entry_id"
                ),
                summary_hour_utc=scope_json.get("summary_hour_utc", -1),
                summary_window_days=scope_json.get("summary_window_days", 0),
                starts_at=schedule.starts_at,
                cadence=schedule.cadence,
                timezone_name=schedule.timezone_name,
                missed_run_policy=schedule.missed_run_policy,
                retention_days=schedule.retention_days,
                max_attempts=schedule.max_attempts,
                backoff_seconds=schedule.backoff_seconds,
                claim_ttl_seconds=schedule.claim_ttl_seconds,
                deadline_seconds=schedule.deadline_seconds,
                concurrency_limit=schedule.concurrency_limit,
                model_token_budget=schedule.model_token_budget,
                notification_budget=schedule.notification_budget,
            )
        )
        expected_scope = expected_config["scope"]
        expected_input_identity = _sha256(
            {
                "handler": schedule.handler_key,
                "project_id": schedule.project_id,
                "channel": channel,
            }
        )
    elif schedule.handler_key == HANDLER_DOCUMENT_NOTIFICATION:
        channel = schedule.scope_json.get("channel", "")
        expected_config = _validated_document_notification_declaration(
            DocumentNotificationDeclaration(
                project_id=schedule.project_id,
                configuration_version=schedule.configuration_version,
                channel=channel,
                starts_at=schedule.starts_at,
                cadence=schedule.cadence,
                timezone_name=schedule.timezone_name,
                missed_run_policy=schedule.missed_run_policy,
                retention_days=schedule.retention_days,
                max_attempts=schedule.max_attempts,
                backoff_seconds=schedule.backoff_seconds,
                claim_ttl_seconds=schedule.claim_ttl_seconds,
                deadline_seconds=schedule.deadline_seconds,
                concurrency_limit=schedule.concurrency_limit,
                model_token_budget=schedule.model_token_budget,
                notification_budget=schedule.notification_budget,
            )
        )
        expected_scope = {
            "project_id": schedule.project_id,
            "channel": channel,
        }
        expected_input_identity = _sha256(
            {
                "handler": schedule.handler_key,
                "project_id": schedule.project_id,
                "channel": channel,
            }
        )
    elif schedule.handler_key == HANDLER_EVENT_ADMISSION_REPROOF:
        policy_version = schedule.scope_json.get("policy_version", "")
        reason_version = schedule.scope_json.get("reason_version", "")
        selection_rule = schedule.scope_json.get("selection_rule", "")
        clone_budget = (schedule.configuration_json.get("resources") or {}).get(
            "clone_budget", 0
        )
        expected_config = _validated_event_admission_reproof_declaration(
            EventAdmissionReproofDeclaration(
                project_id=schedule.project_id,
                configuration_version=schedule.configuration_version,
                policy_version=policy_version,
                reason_version=reason_version,
                selection_rule=selection_rule,
                starts_at=schedule.starts_at,
                cadence=schedule.cadence,
                timezone_name=schedule.timezone_name,
                missed_run_policy=schedule.missed_run_policy,
                retention_days=schedule.retention_days,
                max_attempts=schedule.max_attempts,
                backoff_seconds=schedule.backoff_seconds,
                claim_ttl_seconds=schedule.claim_ttl_seconds,
                deadline_seconds=schedule.deadline_seconds,
                concurrency_limit=schedule.concurrency_limit,
                model_token_budget=schedule.model_token_budget,
                notification_budget=schedule.notification_budget,
                clone_budget=clone_budget,
            )
        )
        expected_scope = {
            "project_id": schedule.project_id,
            "policy_version": policy_version,
            "reason_version": reason_version,
            "selection_rule": selection_rule,
        }
        expected_input_identity = _sha256(
            {
                "handler": schedule.handler_key,
                "project_id": schedule.project_id,
                "policy_version": policy_version,
                "reason_version": reason_version,
                "selection_rule": selection_rule,
            }
        )
    elif schedule.handler_key == HANDLER_EVIDENCE_OUTCOME_CAPTURE:
        cohort_id = schedule.scope_json.get("cohort_id", "")
        observation_contract_sha256 = schedule.scope_json.get(
            "observation_contract_sha256", ""
        )
        expected_config = _validated_evidence_outcome_capture_declaration(
            EvidenceOutcomeCaptureDeclaration(
                project_id=schedule.project_id,
                configuration_version=schedule.configuration_version,
                cohort_id=cohort_id,
                observation_contract_sha256=observation_contract_sha256,
                starts_at=schedule.starts_at,
                cadence=schedule.cadence,
                timezone_name=schedule.timezone_name,
                missed_run_policy=schedule.missed_run_policy,
                retention_days=schedule.retention_days,
                max_attempts=schedule.max_attempts,
                backoff_seconds=schedule.backoff_seconds,
                claim_ttl_seconds=schedule.claim_ttl_seconds,
                deadline_seconds=schedule.deadline_seconds,
                concurrency_limit=schedule.concurrency_limit,
                model_token_budget=schedule.model_token_budget,
                notification_budget=schedule.notification_budget,
            )
        )
        expected_scope = {
            "project_id": schedule.project_id,
            "cohort_id": cohort_id,
            "observation_contract_sha256": observation_contract_sha256,
        }
        expected_input_identity = _sha256(
            {
                "handler": schedule.handler_key,
                "project_id": schedule.project_id,
                "cohort_id": cohort_id,
                "observation_contract_sha256": observation_contract_sha256,
            }
        )
    elif schedule.handler_key == HANDLER_REPORT_PUBLICATION:
        provenance_mode = schedule.scope_json.get("provenance_mode", "")
        prepare_external_pdf = schedule.scope_json.get("prepare_external_pdf")
        expected_config = _validated_report_publication_declaration(
            ReportPublicationDeclaration(
                project_id=schedule.project_id,
                configuration_version=schedule.configuration_version,
                provenance_mode=provenance_mode,
                prepare_external_pdf=prepare_external_pdf,
                starts_at=schedule.starts_at,
                cadence=schedule.cadence,
                timezone_name=schedule.timezone_name,
                missed_run_policy=schedule.missed_run_policy,
                comparison_window_policy=schedule.configuration_json.get(
                    "comparison_window_policy", ""
                ),
                retention_days=schedule.retention_days,
                max_attempts=schedule.max_attempts,
                backoff_seconds=schedule.backoff_seconds,
                claim_ttl_seconds=schedule.claim_ttl_seconds,
                deadline_seconds=schedule.deadline_seconds,
                concurrency_limit=schedule.concurrency_limit,
                model_token_budget=schedule.model_token_budget,
                notification_budget=schedule.notification_budget,
            )
        )
        expected_scope = {
            "project_id": schedule.project_id,
            "provenance_mode": provenance_mode,
            "prepare_external_pdf": prepare_external_pdf,
        }
        expected_input_identity = _sha256(
            {
                "handler": schedule.handler_key,
                "project_id": schedule.project_id,
                "provenance_mode": provenance_mode,
                "prepare_external_pdf": prepare_external_pdf,
            }
        )
    elif schedule.handler_key == HANDLER_CONNECTOR_POLLING:
        customer = schedule.scope_json.get("customer", "")
        channel = schedule.scope_json.get("channel", "")
        connector_identity = schedule.scope_json.get("connector_identity", "")
        source_url = schedule.scope_json.get("source_url", "")
        expected_config = _validated_connector_polling_declaration(
            ConnectorPollingDeclaration(
                project_id=schedule.project_id,
                configuration_version=schedule.configuration_version,
                customer=customer,
                channel=channel,
                connector_identity=connector_identity,
                source_url=source_url,
                starts_at=schedule.starts_at,
                cadence=schedule.cadence,
                timezone_name=schedule.timezone_name,
                missed_run_policy=schedule.missed_run_policy,
                retention_days=schedule.retention_days,
                max_attempts=schedule.max_attempts,
                backoff_seconds=schedule.backoff_seconds,
                claim_ttl_seconds=schedule.claim_ttl_seconds,
                deadline_seconds=schedule.deadline_seconds,
                concurrency_limit=schedule.concurrency_limit,
                model_token_budget=schedule.model_token_budget,
                notification_budget=schedule.notification_budget,
            )
        )
        expected_scope = expected_config["scope"]
        expected_input_identity = _sha256(
            {
                "handler": schedule.handler_key,
                "project_id": schedule.project_id,
                "customer": customer,
                "channel": channel,
                "connector_identity": connector_identity,
                "source_url": source_url,
            }
        )
    elif schedule.handler_key == HANDLER_DELTA_GENERATION:
        comparison_rule_version = schedule.scope_json.get(
            "comparison_rule_version", ""
        )
        expected_config = _validated_delta_generation_declaration(
            DeltaGenerationDeclaration(
                project_id=schedule.project_id,
                configuration_version=schedule.configuration_version,
                comparison_rule_version=comparison_rule_version,
                starts_at=schedule.starts_at,
                cadence=schedule.cadence,
                timezone_name=schedule.timezone_name,
                missed_run_policy=schedule.missed_run_policy,
                retention_days=schedule.retention_days,
                max_attempts=schedule.max_attempts,
                backoff_seconds=schedule.backoff_seconds,
                claim_ttl_seconds=schedule.claim_ttl_seconds,
                deadline_seconds=schedule.deadline_seconds,
                concurrency_limit=schedule.concurrency_limit,
                model_token_budget=schedule.model_token_budget,
                notification_budget=schedule.notification_budget,
            )
        )
        expected_scope = expected_config["scope"]
        expected_input_identity = _sha256(
            {
                "handler": schedule.handler_key,
                "project_id": schedule.project_id,
                "comparison_rule_version": comparison_rule_version,
            }
        )
    elif schedule.handler_key == HANDLER_REPORT_PREPARATION:
        expected_config = _validated_report_preparation_declaration(
            ReportPreparationDeclaration(
                project_id=schedule.project_id,
                configuration_version=schedule.configuration_version,
                starts_at=schedule.starts_at,
                cadence=schedule.cadence,
                timezone_name=schedule.timezone_name,
                missed_run_policy=schedule.missed_run_policy,
                retention_days=schedule.retention_days,
                max_attempts=schedule.max_attempts,
                backoff_seconds=schedule.backoff_seconds,
                claim_ttl_seconds=schedule.claim_ttl_seconds,
                deadline_seconds=schedule.deadline_seconds,
                concurrency_limit=schedule.concurrency_limit,
                model_token_budget=schedule.model_token_budget,
                notification_budget=schedule.notification_budget,
            )
        )
        expected_scope = expected_config["scope"]
        expected_input_identity = _sha256(
            {
                "handler": schedule.handler_key,
                "project_id": schedule.project_id,
                "source": "proposed_delta_lifecycle-v1",
            }
        )
    elif schedule.handler_key == HANDLER_RETENTION_SWEEP:
        authorized_by = schedule.scope_json.get("authorized_by", "")
        expected_config = _validated_retention_sweep_declaration(
            RetentionSweepDeclaration(
                project_id=schedule.project_id,
                configuration_version=schedule.configuration_version,
                authorized_by=authorized_by,
                starts_at=schedule.starts_at,
                cadence=schedule.cadence,
                timezone_name=schedule.timezone_name,
                missed_run_policy=schedule.missed_run_policy,
                retention_days=schedule.retention_days,
                max_attempts=schedule.max_attempts,
                backoff_seconds=schedule.backoff_seconds,
                claim_ttl_seconds=schedule.claim_ttl_seconds,
                deadline_seconds=schedule.deadline_seconds,
                concurrency_limit=schedule.concurrency_limit,
                model_token_budget=schedule.model_token_budget,
                notification_budget=schedule.notification_budget,
            )
        )
        expected_scope = expected_config["scope"]
        expected_input_identity = _sha256(
            {
                "handler": schedule.handler_key,
                "project_id": schedule.project_id,
                "retention_class": "class_b",
            }
        )
    else:
        expected_config = _validated_health_declaration(
            ProcessingHealthDeclaration(
                project_id=schedule.project_id,
                configuration_version=schedule.configuration_version,
                starts_at=schedule.starts_at,
                cadence=schedule.cadence,
                timezone_name=schedule.timezone_name,
                missed_run_policy=schedule.missed_run_policy,
                retention_days=schedule.retention_days,
                max_attempts=schedule.max_attempts,
                backoff_seconds=schedule.backoff_seconds,
                claim_ttl_seconds=schedule.claim_ttl_seconds,
                deadline_seconds=schedule.deadline_seconds,
                concurrency_limit=schedule.concurrency_limit,
                model_token_budget=schedule.model_token_budget,
                notification_budget=schedule.notification_budget,
            )
        )
        expected_scope = {"project_id": schedule.project_id}
        expected_input_identity = _sha256(
            {
                "handler": schedule.handler_key,
                "project_id": schedule.project_id,
                "source": "stored_processing_facts-v1",
            }
        )
    if schedule.configuration_json != expected_config:
        raise DueWorkRefusal("persisted Due Work configuration is unsupported")
    if (
        schedule.scope_json != expected_scope
        or schedule.input_identity_sha256 != expected_input_identity
    ):
        raise DueWorkRefusal("persisted Due Work scope or input identity is invalid")
    contract = HANDLER_REGISTRY[schedule.handler_key]
    if (
        schedule.model_token_budget > contract.model_token_budget
        or schedule.notification_budget > contract.notification_budget
    ):
        raise DueWorkRefusal("persisted Due Work budgets exceed the handler contract")


def _handler(key: str) -> HandlerContract:
    contract = HANDLER_REGISTRY.get(key)
    if contract is None:
        raise DueWorkRefusal("Due Work handler is not server-owned")
    return contract


def _safe_next_step(contract: HandlerContract, handler_result: dict[str, Any]) -> str:
    """The recovery pointer a completed receipt records, per handler."""

    if contract.key == HANDLER_REPORT_PUBLICATION:
        return (
            "review_prepared_report"
            if handler_result.get("prepared")
            else "review_retained_snapshot"
        )
    if handler_result.get("health") in ("healthy", "held_sealed"):
        return "none"
    if contract.key == HANDLER_PROJECT_PROCESSING:
        return "inspect_processing_attention"
    if contract.key == HANDLER_REVISION_RECONCILIATION:
        return "inspect_revision_attention"
    if contract.key == HANDLER_LOCATION_DISCOVERY:
        return "inspect_location_discovery_attention"
    if contract.key == HANDLER_ASSIGNMENT_NOTIFICATION:
        return "inspect_notification_delivery"
    if contract.key == HANDLER_EVENT_ADMISSION_REPROOF:
        return "inspect_event_admission_reproof_attention"
    if contract.key == HANDLER_EVIDENCE_OUTCOME_CAPTURE:
        return "inspect_capture_attention"
    if contract.key == HANDLER_CONNECTOR_POLLING:
        return "inspect_connector_checkpoint"
    if contract.key == HANDLER_DELTA_GENERATION:
        return "adopt_project_baseline"
    if contract.key == HANDLER_REPORT_PREPARATION:
        return "adopt_project_baseline"
    if contract.key == HANDLER_RETENTION_SWEEP:
        return "inspect_retention_refusal"
    return "inspect_failed_document_processing"


def _validate_handler_result(contract: HandlerContract, result: dict[str, Any]) -> None:
    if not isinstance(result, dict):
        raise DueWorkRefusal("Due Work handler result must be an object")
    encoded = _canonical_json(result)
    if len(encoded) > contract.max_result_bytes:
        raise DueWorkRefusal("Due Work handler result exceeds its resource contract")
    if contract.key == HANDLER_PROCESSING_HEALTH and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "document_count",
            "failed_document_count",
            "failed_extraction_count",
        }
        or result.get("health") not in {"healthy", "processing_failures_observed"}
    ):
        raise DueWorkRefusal("processing-health handler result is invalid")
    if contract.key == HANDLER_PROJECT_PROCESSING and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "eligible_document_count",
            "extracted",
            "skipped",
            "failed",
            "unreadable",
            "quarantined",
            "held_out",
            "processing_failures",
            "reconciled",
            "admitted",
            "waiting",
            "ambiguous_documents",
        }
        or result.get("health") not in {"healthy", "processing_attention_required"}
    ):
        raise DueWorkRefusal("project-processing handler result is invalid")
    if contract.key == HANDLER_REVISION_RECONCILIATION and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "did_reconcile",
            "pairs_discovered",
            "comparisons_created",
            "comparisons_reused",
            "comparison_failures",
            "carried",
            "abstained",
            "requested_record_inclusion",
        }
        or result.get("health") not in {"healthy", "revision_attention_required"}
    ):
        raise DueWorkRefusal("revision-reconciliation handler result is invalid")
    if contract.key == HANDLER_LOCATION_DISCOVERY and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "location_id",
            "sealed_excluded",
            "references_observed",
            "new_references",
            "repeat_references",
            "new_archive_urls",
            "authorized_selected",
            "registered",
            "unchanged",
            "drifted",
            "fetch_failures",
            "budget_exhausted",
        }
        or result.get("health")
        not in {"healthy", "attention_required", "held_sealed"}
    ):
        raise DueWorkRefusal("location-discovery handler result is invalid")
    if contract.key == HANDLER_ASSIGNMENT_NOTIFICATION and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "delivery_enabled",
            "considered",
            "completed",
            "retry_due",
            "failed",
            "uncertain",
            "skipped",
        }
        or result.get("health") not in {"healthy", "delivery_attention_required"}
    ):
        raise DueWorkRefusal("assignment-notification handler result is invalid")
    if contract.key == HANDLER_EVENT_ADMISSION_REPROOF and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "policy_version",
            "proof_status",
            "was_stale",
            "trigger",
            "did_reprove",
            "proof_outcome",
            "activated",
            "acceptance_receipt_id",
            "effective_status",
            "effective_policy_version",
            "detail",
        }
        or result.get("health") not in {"healthy", "recovery_attention_required"}
        or result.get("proof_outcome") not in {"passed", "failed", "not_run"}
    ):
        raise DueWorkRefusal("event-admission-reproof handler result is invalid")
    if contract.key == HANDLER_EVIDENCE_OUTCOME_CAPTURE and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "contract_sha256",
            "cutoff_at",
            "observed_at",
            "health",
            "cases_in_scope",
            "captured_complete",
            "captured_incomplete",
            "before_cutoff_pending",
            "excluded",
            "already_captured",
        }
        or result.get("health") not in {"healthy", "capture_attention_required"}
    ):
        raise DueWorkRefusal("outcome-capture handler result is invalid")
    if contract.key == HANDLER_REPORT_PUBLICATION and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "provenance_mode",
            "observed_at",
            "evaluated_on",
            "outcome",
            "snapshot_public_id",
            # The accepted Project Record revision the retained reading was
            # produced against (#602), added with the result schema's v2.
            "accepted_revision_id",
            "prepared",
            "prepared_artifact_id",
            "has_prior_release",
            "comparison_window_days",
        }
        or result.get("outcome") != "retained"
        or not isinstance(result.get("prepared"), bool)
    ):
        raise DueWorkRefusal("report-publication handler result is invalid")
    if contract.key == HANDLER_CONNECTOR_POLLING and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "channel",
            "connector_identity",
            "cursor",
            "checkpoint_token",
            "advanced",
            "changes_taken",
            "dispositions",
            "blocked_by",
        }
        or result.get("health") not in {"healthy", "polling_attention_required"}
        or not isinstance(result.get("advanced"), bool)
        # The cursor is the connector configuration's own durable record since
        # ADR-0089, and this is the pass reporting where it stands, so a result
        # that cannot state a token is not a completed pass.
        or not isinstance(result.get("checkpoint_token"), str)
        # What the pass took delivery of, by ADR-0089 disposition, and what
        # held the cursor back.
        or not isinstance(result.get("dispositions"), dict)
        or not isinstance(result.get("blocked_by"), list)
    ):
        raise DueWorkRefusal("connector-polling handler result is invalid")
    if contract.key == HANDLER_DELTA_GENERATION and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "operating_mode",
            "accepted_baseline_revision",
            "facts_considered",
            "facts_agreed",
            "groups_created",
            "deltas_created",
            "deltas_already_present",
            "through_fact_id",
        }
        or result.get("health")
        not in {"healthy", "delta_generation_attention_required"}
        or result.get("operating_mode") not in {"legacy", "adopted_baseline"}
    ):
        raise DueWorkRefusal("delta-generation handler result is invalid")
    if contract.key == HANDLER_REPORT_PREPARATION and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "window_start",
            "through_delta_id",
            "through_disposition_id",
            "accepted_revision_id",
            "resolved_accepted",
            "resolved_edited",
            "resolved_rejected",
            "proposed_new",
            "open_actionable",
            "open_deferred",
            "superseded",
        }
        or result.get("health") not in {"healthy", "preparation_attention_required"}
    ):
        raise DueWorkRefusal("report-preparation handler result is invalid")
    if contract.key == HANDLER_RETENTION_SWEEP and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "authorized_by",
            "manifest_public_id",
            "planned",
            "deleted",
            "refusal",
        }
        or result.get("health") not in {"healthy", "retention_attention_required"}
        # A sweep that deleted more than it planned deleted something the
        # dry-run manifest never named.
        or int(result.get("deleted", 0)) > int(result.get("planned", 0))
    ):
        raise DueWorkRefusal("retention-sweep handler result is invalid")


def _locked_live_claim(
    session: Session,
    claim: DueWorkClaim,
    *,
    now: datetime,
) -> tuple[DueWorkOccurrence, DueWorkSchedule]:
    occurrence = session.scalar(
        select(DueWorkOccurrence)
        .where(DueWorkOccurrence.id == claim.occurrence_id)
        .with_for_update()
    )
    if (
        occurrence is None
        or occurrence.state != "claimed"
        or occurrence.owner != claim.runtime_owner
        or occurrence.claim_token != claim.claim_token
        or occurrence.attempt_count != claim.attempt_number
        or occurrence.lease_expires_at is None
        or occurrence.lease_expires_at <= now
    ):
        raise StaleDueWorkClaim("Due Work claim is stale or no longer owned")
    schedule = session.get(DueWorkSchedule, occurrence.scheduled_job_id)
    if schedule is None:
        raise DueWorkRefusal("Due Work schedule disappeared")
    return occurrence, schedule


def _append_receipt(
    session: Session,
    occurrence: DueWorkOccurrence,
    schedule: DueWorkSchedule,
    claim: DueWorkClaim,
    *,
    execution_outcome: str,
    handler_result: dict[str, Any] | None,
    error_code: str | None,
    safe_next_step: str,
    finished_at: datetime,
) -> DueWorkReceipt:
    content = _receipt_content(
        occurrence_public_id=occurrence.public_id,
        schedule=schedule,
        attempt_number=claim.attempt_number,
        attempt_id=claim.attempt_id,
        runtime_owner=claim.runtime_owner,
        execution_outcome=execution_outcome,
        handler_result=handler_result,
        error_code=error_code,
        safe_next_step=safe_next_step,
        started_at=claim.started_at,
        finished_at=finished_at,
    )
    digest = _sha256(content)
    receipt = DueWorkReceipt(
        public_id=f"due-receipt:{digest[:24]}",
        occurrence_id=occurrence.id,
        project_id=schedule.project_id,
        handler_key=schedule.handler_key,
        attempt_number=claim.attempt_number,
        attempt_id=claim.attempt_id,
        runtime_owner=claim.runtime_owner,
        execution_outcome=execution_outcome,
        handler_result_json=handler_result,
        error_code=error_code,
        safe_next_step=safe_next_step,
        started_at=claim.started_at,
        finished_at=finished_at,
        content_sha256=digest,
    )
    session.add(receipt)
    session.flush([receipt])
    return receipt


def _receipt_content(
    *,
    occurrence_public_id: str,
    schedule: DueWorkSchedule,
    attempt_number: int,
    attempt_id: str,
    runtime_owner: str,
    execution_outcome: str,
    handler_result: dict[str, Any] | None,
    error_code: str | None,
    safe_next_step: str,
    started_at: datetime,
    finished_at: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": "due-work-attempt-receipt-v1",
        "occurrence_id": occurrence_public_id,
        "job_id": schedule.public_id,
        "project_id": schedule.project_id,
        "handler": schedule.handler_key,
        "configuration_sha256": schedule.configuration_sha256,
        "attempt_number": attempt_number,
        "attempt_id": attempt_id,
        "runtime_owner": runtime_owner,
        "execution_outcome": execution_outcome,
        "handler_result": handler_result,
        "error_code": error_code,
        "safe_next_step": safe_next_step,
        "started_at": _iso(started_at),
        "finished_at": _iso(finished_at),
    }


def _record_abandoned_failure(
    session: Session,
    occurrence: DueWorkOccurrence,
    schedule: DueWorkSchedule,
    *,
    now: datetime,
) -> None:
    claim = DueWorkClaim(
        occurrence_id=occurrence.id,
        occurrence_public_id=occurrence.public_id,
        schedule_id=schedule.id,
        attempt_number=occurrence.attempt_count,
        attempt_id=f"due-attempt:{occurrence.claim_token}",
        claim_token=str(occurrence.claim_token),
        runtime_owner=str(occurrence.owner),
        started_at=occurrence.claimed_at or now,
        lease_expires_at=occurrence.lease_expires_at or now,
        deadline_at=occurrence.deadline_at or now,
    )
    _append_receipt(
        session,
        occurrence,
        schedule,
        claim,
        execution_outcome="failed",
        handler_result=None,
        error_code="claim_abandoned_exhausted",
        safe_next_step="inspect_processing_failure",
        finished_at=now,
    )
    _release_occurrence(
        occurrence,
        state="failed",
        error_code="claim_abandoned_exhausted",
    )


def _fail_missed_latest_only(
    session: Session,
    schedule: DueWorkSchedule,
    *,
    latest_due_at: datetime,
    now: datetime,
) -> None:
    older = session.scalars(
        select(DueWorkOccurrence)
        .where(
            DueWorkOccurrence.scheduled_job_id == schedule.id,
            DueWorkOccurrence.due_at < latest_due_at,
            or_(
                DueWorkOccurrence.state.in_(("pending", "retry_due")),
                (
                    (DueWorkOccurrence.state == "claimed")
                    & (DueWorkOccurrence.lease_expires_at <= now)
                ),
            ),
        )
        .order_by(DueWorkOccurrence.due_at, DueWorkOccurrence.id)
        .with_for_update(skip_locked=True)
    ).all()
    for occurrence in older:
        if occurrence.state == "claimed":
            attempt_number = occurrence.attempt_count
            attempt_id = f"due-attempt:{occurrence.claim_token}"
            runtime_owner = str(occurrence.owner)
            started_at = occurrence.claimed_at or occurrence.due_at
        else:
            occurrence.attempt_count += 1
            attempt_number = occurrence.attempt_count
            attempt_id = (
                f"due-attempt:missed:{occurrence.occurrence_key[:20]}:"
                f"{attempt_number}"
            )
            runtime_owner = "runtime:scheduler"
            started_at = occurrence.due_at
        claim = DueWorkClaim(
            occurrence_id=occurrence.id,
            occurrence_public_id=occurrence.public_id,
            schedule_id=schedule.id,
            attempt_number=attempt_number,
            attempt_id=attempt_id,
            claim_token=str(occurrence.claim_token or "missed"),
            runtime_owner=runtime_owner,
            started_at=started_at,
            lease_expires_at=now,
            deadline_at=now,
        )
        _append_receipt(
            session,
            occurrence,
            schedule,
            claim,
            execution_outcome="failed",
            handler_result=None,
            error_code="missed_run_latest_only",
            safe_next_step="none",
            finished_at=now,
        )
        _release_occurrence(
            occurrence,
            state="failed",
            error_code="missed_run_latest_only",
        )


def _record_abandoned_retry(
    session: Session,
    occurrence: DueWorkOccurrence,
    schedule: DueWorkSchedule,
    *,
    now: datetime,
) -> None:
    claim = DueWorkClaim(
        occurrence_id=occurrence.id,
        occurrence_public_id=occurrence.public_id,
        schedule_id=schedule.id,
        attempt_number=occurrence.attempt_count,
        attempt_id=f"due-attempt:{occurrence.claim_token}",
        claim_token=str(occurrence.claim_token),
        runtime_owner=str(occurrence.owner),
        started_at=occurrence.claimed_at or now,
        lease_expires_at=occurrence.lease_expires_at or now,
        deadline_at=occurrence.deadline_at or now,
    )
    _append_receipt(
        session,
        occurrence,
        schedule,
        claim,
        execution_outcome="retry_due",
        handler_result=None,
        error_code="claim_abandoned",
        safe_next_step="recovered_by_runtime",
        finished_at=now,
    )
    _release_occurrence(
        occurrence,
        state="retry_due",
        error_code="claim_abandoned",
    )
    occurrence.next_attempt_at = now


def _release_occurrence(
    occurrence: DueWorkOccurrence,
    *,
    state: str,
    error_code: str | None,
) -> None:
    occurrence.state = state
    occurrence.owner = None
    occurrence.claim_token = None
    occurrence.claimed_at = None
    occurrence.lease_expires_at = None
    occurrence.deadline_at = None
    occurrence.last_error_code = error_code
    if state != "retry_due":
        occurrence.next_attempt_at = None


def _run_result(
    receipt: DueWorkReceipt,
    *,
    occurrence: DueWorkOccurrence,
    schedule: DueWorkSchedule,
) -> DueWorkRunResult:
    return DueWorkRunResult(
        occurrence_id=receipt.occurrence_id,
        receipt_id=receipt.id,
        occurrence_public_id=occurrence.public_id,
        receipt_public_id=receipt.public_id,
        job_public_id=schedule.public_id,
        attempt_id=receipt.attempt_id,
        project_id=schedule.project_id,
        handler_key=schedule.handler_key,
        configuration_version=schedule.configuration_version,
        input_identity_sha256=schedule.input_identity_sha256,
        execution_outcome=receipt.execution_outcome,
        handler_result=receipt.handler_result_json,
        error_code=receipt.error_code,
        safe_next_step=receipt.safe_next_step,
    )


def _validate_owner(owner: str) -> None:
    if not isinstance(owner, str) or _RUNTIME_OWNER.fullmatch(owner) is None:
        raise DueWorkRefusal("Due Work runtime owner must be a namespaced runtime id")


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DueWorkRefusal("Due Work clock must supply an aware datetime")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _aware_utc(value).isoformat()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()
