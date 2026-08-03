from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from corridor.config import settings

engine = create_engine(settings.database_url)
Session = sessionmaker(bind=engine)
