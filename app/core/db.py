from sqlmodel import SQLModel, create_engine, Session
from app.core.config import settings
from app.core import soft_removal  # noqa: F401  registers the removed-record filter on every ORM session

engine = create_engine(settings.database_url, pool_pre_ping=True)

def get_session():
    with Session(engine) as session:
        yield session
