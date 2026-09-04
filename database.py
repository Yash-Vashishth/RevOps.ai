"""
database.py
SQLAlchemy engine, session factory, and FastAPI dependency for DB sessions.
Works out of the box with SQLite (default) and Postgres (via DATABASE_URL).
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from config import settings

connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(settings.DATABASE_URL, connect_args=connect_args, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def init_db() -> None:
    """Create all tables. Safe to call multiple times (no-op if tables exist)."""
    import models  # noqa: F401  (ensures models are registered on Base.metadata)
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency: yields a DB session and guarantees it is closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
