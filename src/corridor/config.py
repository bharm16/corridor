"""Deployment-resolved configuration shared by command and HTTP adapters.

Defaults make a local clone bootable while identity and model settings remain
explicit inputs whose values are recorded at the write seams that use them.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # The migration and DDL credential: it owns the schema and runs alembic.
    # No application process connects with it (#492); the capability URLs
    # below are what the web application and the worker use.
    database_url: str = (
        "postgresql+psycopg://corridor:corridor@localhost:5433/corridor"
    )
    # Deployment-resolved runtime credentials. Left empty, each is derived
    # from `database_url` by swapping in that capability's login, so a local
    # clone boots without configuring three URLs.
    web_database_url: str = ""
    worker_database_url: str = ""
    web_db_password: str = Field(
        default="corridor_web", validation_alias="CORRIDOR_WEB_DB_PASSWORD"
    )
    worker_db_password: str = Field(
        default="corridor_worker", validation_alias="CORRIDOR_WORKER_DB_PASSWORD"
    )
    # Content-addressed store written by `make corpus`. The queue resolves a
    # Document back to its PDF from here to compute quote highlights, and product
    # intake stages an uploaded file's exact bytes here before confirmation.
    # With the filesystem backend this directory is the store itself; with the
    # s3 backend it is the local staging directory the store fills on demand
    # (`corridor.object_storage`, ADR-0079).
    corpus_store: str = "corpus/files"
    # Which backend holds every content-addressed artifact: "filesystem" or
    # "s3". Object-store credentials come from the standard AWS environment,
    # never from these settings, so a subprocess can be denied them by
    # scrubbing its environment.
    storage_backend: str = Field(
        default="filesystem", validation_alias="CORRIDOR_STORAGE_BACKEND"
    )
    storage_s3_bucket: str = Field(default="", validation_alias="CORRIDOR_S3_BUCKET")
    storage_s3_prefix: str = Field(default="", validation_alias="CORRIDOR_S3_PREFIX")
    storage_s3_endpoint_url: str = Field(
        default="", validation_alias="CORRIDOR_S3_ENDPOINT_URL"
    )
    storage_s3_region: str = Field(default="", validation_alias="CORRIDOR_S3_REGION")
    # Where page renders land when a document is parsed. Explicit deployment-
    # resolved storage behavior, so an intake adapter renders to the same place
    # the rest of the pipeline reads (`corridor.ingest`).
    corpus_images: str = "out/page-images"

    # The one server-owned mailbox address and webhook credential for #372.
    # Empty credential is intentionally non-operational: a deployment must
    # explicitly configure the sender-authentication boundary before mail can
    # enter the record.
    inbound_service_address: str = ""
    inbound_webhook_secret: str = ""

    # Fail closed by default.  This is a deployment-resolved stable subject,
    # not a request header or form value.  Full authentication/SSO is M9;
    # M8 only closes Admission over an attributable principal.
    human_principal: str = Field(
        default="", validation_alias="CORRIDOR_HUMAN_PRINCIPAL"
    )

    # Whether this deployment serves only the live-pilot route surface (#680).
    # The database half of that boundary is unconditional: the migration
    # revokes every unpartitioned relation from `corridor_web`. This is the
    # application half, and it is declared rather than assumed because a
    # legacy development deployment still runs the frozen surfaces (ADR-0081)
    # by pointing `web_database_url` at the opt-in `corridor_legacy_dev`
    # login, which keeps the blanket read. Left off, that is the deployment
    # this serves: every route reachable, because nothing was taken away.
    # Left off on a deployment whose reads run as `corridor_web`, the two
    # halves disagree, and #694 refuses the affected routes rather than
    # letting PostgreSQL answer — see `corridor.web_boundary.boundary_state`.
    live_pilot_web_boundary: bool = Field(
        default=False, validation_alias="CORRIDOR_LIVE_PILOT_WEB_BOUNDARY"
    )

    # How a sign-in link reaches the person who asked for it. "logging" is the
    # fail-safe default that delivers nothing, so a deployment that forgets to
    # configure delivery cannot quietly issue links nobody receives -- the web
    # application refuses to start with it outside development and test
    # (`corridor.web.auth.build_email_sender`).
    email_backend: str = Field(
        default="logging", validation_alias="CORRIDOR_EMAIL_BACKEND"
    )
    # The verified SES identity every sign-in link is sent from. Required by
    # the "ses" backend; SES rejects an unverified sender outright.
    sign_in_sender_address: str = Field(
        default="", validation_alias="CORRIDOR_SIGN_IN_SENDER"
    )

    # The deployment this process runs in. It labels every structured log line
    # (docs/operations/observability-runbook.md) and names nothing else, so a
    # local clone that configures nothing still produces attributable output.
    environment: str = Field(
        default="development", validation_alias="CORRIDOR_ENVIRONMENT"
    )

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    # Recorded on every candidate. Changing this without an eval run makes
    # the numbers incomparable, which is why it lives in config rather than
    # being hardcoded at a call site.
    llm_model: str = "gpt-5.6-luna"
    # Separate lineage: changing this never changes Candidate extraction model
    # identity, and every investigator receipt records it independently.
    evidence_investigator_model: str = "gpt-5.6-luna"


settings = Settings()
