"""Deployment-resolved configuration shared by command and HTTP adapters.

Defaults make a local clone bootable while identity and model settings remain
explicit inputs whose values are recorded at the write seams that use them.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+psycopg://corridor:corridor@localhost:5433/corridor"
    )
    # Content-addressed store written by `make corpus`. The queue resolves a
    # Document back to its PDF from here to compute quote highlights.
    corpus_store: str = "corpus/files"

    # Fail closed by default.  This is a deployment-resolved stable subject,
    # not a request header or form value.  Full authentication/SSO is M9;
    # M8 only closes Admission over an attributable principal.
    human_principal: str = Field(
        default="", validation_alias="CORRIDOR_HUMAN_PRINCIPAL"
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
