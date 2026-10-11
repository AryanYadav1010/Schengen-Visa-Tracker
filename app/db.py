"""SQLAlchemy async engine, session factory, and DB initialisation."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Base

# One engine (connection pool) per process. With SQLite + aiosqlite each connection runs
# its queries in a background thread, so awaiting them never blocks the event loop.
# hide_parameters: DB error messages/logs show the SQL but not the bound values (emails,
# ciphertext, ...).
engine = create_async_engine(settings.DATABASE_URL, echo=False, hide_parameters=True)

# `async with async_session() as session:` = one unit of work; leaving the block without
# commit() rolls back. expire_on_commit=False keeps loaded attributes readable after commit
# (routes return objects they just committed).
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Create missing tables (and their indexes) at startup.

    create_all never alters existing tables, so it can only *add* schema — e.g. the
    alert_outbox table arrived this way. Changing an existing column needs a migration tool.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
