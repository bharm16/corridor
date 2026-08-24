"""The production PostgreSQL adapter and its session factory.

Domain modules accept a Session supplied by their caller.  Only command and
HTTP adapters reach this module to choose the configured production engine;
tests bind the same interface to rollback-scoped real PostgreSQL connections.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from corridor.config import settings

engine = create_engine(settings.database_url)
Session = sessionmaker(bind=engine)
