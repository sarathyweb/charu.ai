"""Async SQLAlchemy engine, session factory, and table creation utilities."""

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(settings.DATABASE_URL)

async_session_factory = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def create_db_tables() -> None:
    """Create all SQLModel tables. Called once during application startup."""
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
