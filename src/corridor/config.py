from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+psycopg://corridor:corridor@localhost:5433/corridor"
    )
    # Content-addressed store written by `make corpus`. The queue resolves a
    # Document back to its PDF from here to compute quote highlights.
    corpus_store: str = "corpus/files"


settings = Settings()
