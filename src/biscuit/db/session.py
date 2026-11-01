"""Database engine, connection pooling, and session management."""

import os
import re
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.ext.asyncio.session import AsyncSession

DEFAULT_SQLITE_URL = "sqlite:///:memory:"
DEFAULT_ASYNC_SQLITE_URL = "sqlite+aiosqlite:///:memory:"

TRIGGERS_SQL_PATH = os.path.join(
    os.path.dirname(__file__), "migrations", "triggers.sql"
)


def _enable_sqlite_fk_pragma(engine) -> None:
    """SQLite defaults to foreign_keys=OFF; enforce FKs on every pooled connection.

    Without this pragma the production fallback accepts orphan messages and
    invalid composite links. Registering on the engine (not a test fixture) means
    every connection opened by get_engine()/get_async_engine() is covered.
    """
    if engine.dialect.name != "sqlite":
        return

    # AsyncEngine does not accept pool events directly; attach to its sync_engine.
    sync_target = getattr(engine, "sync_engine", engine)

    @event.listens_for(sync_target, "connect")
    def _set_sqlite_fk_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_triggers_sql() -> str:
    """Read the canonical PostgreSQL trigger migration SQL script.

    Fails closed if the migration script is missing or empty to prevent running
    unprotected without critical audit and provenance triggers.
    """
    if not os.path.exists(TRIGGERS_SQL_PATH):
        raise FileNotFoundError(
            f"PostgreSQL triggers migration script not found at {TRIGGERS_SQL_PATH}. "
            "Ensure biscuit package data includes db/migrations/triggers.sql."
        )
    with open(TRIGGERS_SQL_PATH, encoding="utf-8") as f:
        content = f.read().strip()
        if not content:
            raise ValueError(
                f"PostgreSQL triggers migration script at {TRIGGERS_SQL_PATH} is empty."
            )
        return content


def split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements.

    Preserves PL/pgSQL dollar-quoted blocks ($$...$$ or $tag$...$tag$),
    single-quoted string literals, and comment blocks so multi-statement
    scripts can be executed reliably via SQLAlchemy and asyncpg.
    """
    statements = []
    current: list[str] = []
    i = 0
    n = len(sql)
    dollar_tag: str | None = None
    in_string = False
    in_line_comment = False
    in_block_comment = False

    while i < n:
        # 1. Single-line comments
        if in_line_comment:
            if sql[i] == "\n":
                in_line_comment = False
            current.append(sql[i])
            i += 1
            continue

        # 2. Multi-line block comments
        if in_block_comment:
            if sql[i : i + 2] == "*/":
                in_block_comment = False
                current.append("*/")
                i += 2
                continue
            current.append(sql[i])
            i += 1
            continue

        # 3. Inside dollar-quoted body ($$...$$ or $tag$...$tag$)
        if dollar_tag is not None:
            tag_len = len(dollar_tag)
            if sql[i : i + tag_len] == dollar_tag:
                current.append(dollar_tag)
                i += tag_len
                dollar_tag = None
            else:
                current.append(sql[i])
                i += 1
            continue

        # 4. Inside standard single-quoted string literal
        if in_string:
            if sql[i] == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    current.append("''")
                    i += 2
                    continue
                else:
                    in_string = False
                    current.append("'")
                    i += 1
                    continue
            current.append(sql[i])
            i += 1
            continue

        # 5. Outside string / comment / dollar-quote: check transitions
        if sql[i : i + 2] == "--":
            in_line_comment = True
            current.append("--")
            i += 2
            continue

        if sql[i : i + 2] == "/*":
            in_block_comment = True
            current.append("/*")
            i += 2
            continue

        if sql[i] == "'":
            in_string = True
            current.append("'")
            i += 1
            continue

        if sql[i] == "$":
            match = re.match(r"\$[A-Za-z0-9_]*\$", sql[i:])
            if match:
                dollar_tag = match.group(0)
                current.append(dollar_tag)
                i += len(dollar_tag)
                continue

        if sql[i] == ";":
            stmt = "".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
            i += 1
            continue

        current.append(sql[i])
        i += 1

    rem = "".join(current).strip()
    if rem:
        statements.append(rem)

    return statements


def get_database_url() -> str:
    """Retrieve database URL from environment or return default SQLite memory URL.

    Accepts BISCUIT_TEST_PG_DSN (Issue #17) as an alias for DATABASE_URL so local
    integration tests and CI (TEST_POSTGRES_URL) share one resolution path.
    """
    return os.getenv("DATABASE_URL") or os.getenv("BISCUIT_TEST_PG_DSN") or DEFAULT_SQLITE_URL


def get_engine(db_url: str | None = None, echo: bool = False):
    """Create a synchronous SQLAlchemy / SQLModel engine."""
    url = db_url or get_database_url()
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    elif url.startswith(("postgresql://", "postgres://")):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            if url.startswith("postgresql://"):
                url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
            elif url.startswith("postgres://"):
                url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    engine = create_engine(url, echo=echo, connect_args=connect_args)
    _enable_sqlite_fk_pragma(engine)
    return engine


def get_async_database_url(db_url: str | None = None) -> str:
    """Resolve an asynchronous database URL from a sync or raw URL."""
    url = db_url or get_database_url()
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "sqlite+aiosqlite:///")
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://")
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://")
    return url


def get_async_engine(db_url: str | None = None, echo: bool = False) -> AsyncEngine:
    """Create an asynchronous SQLAlchemy engine."""
    async_url = get_async_database_url(db_url)
    connect_args = {}
    if "sqlite" in async_url:
        connect_args["check_same_thread"] = False
    engine = create_async_engine(async_url, echo=echo, connect_args=connect_args)
    _enable_sqlite_fk_pragma(engine)
    return engine


def bootstrap_postgres_extensions(engine) -> None:
    """Pre-schema phase: install required PostgreSQL extensions prior to table creation."""
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
            conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";'))
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto;"))


async def bootstrap_async_postgres_extensions(async_engine: AsyncEngine) -> None:
    """Asynchronously install required PostgreSQL extensions prior to table creation."""
    if async_engine.dialect.name == "postgresql":
        async with async_engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
            await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";'))
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto;"))


def apply_triggers(engine=None) -> None:
    """Execute triggers.sql against a PostgreSQL database engine statement-by-statement."""
    target_engine = engine or get_engine()
    if target_engine.dialect.name == "postgresql":
        sql = get_triggers_sql()
        statements = split_sql_statements(sql)
        with target_engine.begin() as conn:
            for stmt in statements:
                if stmt.strip():
                    conn.execute(text(stmt))


async def apply_async_triggers(async_engine: AsyncEngine | None = None) -> None:
    """Execute triggers.sql asynchronously statement-by-statement for asyncpg compatibility."""
    target_engine = async_engine or get_async_engine()
    if target_engine.dialect.name == "postgresql":
        sql = get_triggers_sql()
        statements = split_sql_statements(sql)
        async with target_engine.begin() as conn:
            for stmt in statements:
                if stmt.strip():
                    await conn.execute(text(stmt))


def _bootstrap_sqlite_schema(conn) -> None:
    """Create SQLite tables with every declared FK inline, including cyclic FKs.

    SQLAlchemy's normal metadata.create_all() defers cyclic foreign keys to
    ALTER TABLE; SQLite does not support ALTER ADD CONSTRAINT, so those FKs
    are otherwise silently missing. SQLite permits forward FK references in
    CREATE TABLE, allowing us to emit the complete table DDL up front.

    A create_all() on another dialect (PostgreSQL) permanently marks deferred
    constraints with ``_create_rule = AddConstraint._create_rule_disable`` to
    suppress their inline emission; include_foreign_key_constraints does NOT
    bypass that compile-time check, so a PostgreSQL bootstrap earlier in the
    process would otherwise leave later SQLite schemas with no FKs at all.
    The marker is reset for the duration of the SQLite emission and restored
    afterwards, keeping PostgreSQL ALTER-based behavior unchanged.
    """
    for table in SQLModel.metadata.tables.values():
        fks = list(table.foreign_key_constraints)
        saved_rules = [(fk, fk._create_rule) for fk in fks if fk._create_rule is not None]
        try:
            for fk, _rule in saved_rules:
                fk._create_rule = None
            conn.execute(
                CreateTable(
                    table,
                    if_not_exists=True,
                    include_foreign_key_constraints=fks,
                )
            )
        finally:
            for fk, rule in saved_rules:
                fk._create_rule = rule
    for table in SQLModel.metadata.tables.values():
        for index in table.indexes:
            conn.execute(CreateIndex(index, if_not_exists=True))


def init_db(engine=None, apply_postgres_triggers: bool = True) -> None:
    """Create all SQLModel tables in database and bootstrap triggers on PostgreSQL.

    Executes a pre-schema extension phase (ensuring pgvector is available before
    tables with vector columns are created), then creates tables, then executes triggers.
    """
    import biscuit.models  # noqa: F401

    target_engine = engine or get_engine()
    if target_engine.dialect.name == "postgresql":
        bootstrap_postgres_extensions(target_engine)

    if target_engine.dialect.name == "sqlite":
        with target_engine.begin() as conn:
            _bootstrap_sqlite_schema(conn)
    else:
        SQLModel.metadata.create_all(target_engine)

    if apply_postgres_triggers and target_engine.dialect.name == "postgresql":
        apply_triggers(target_engine)


async def init_async_db(
    async_engine: AsyncEngine | None = None,
    apply_postgres_triggers: bool = True,
) -> None:
    """Asynchronously create all SQLModel tables in database and bootstrap triggers.

    Ensures pgvector extension is created prior to table metadata creation, and
    executes triggers statement-by-statement to prevent asyncpg multi-command errors.
    """
    import biscuit.models  # noqa: F401

    target_engine = async_engine or get_async_engine()
    if target_engine.dialect.name == "postgresql":
        await bootstrap_async_postgres_extensions(target_engine)

    async with target_engine.begin() as conn:
        if target_engine.dialect.name == "sqlite":
            await conn.run_sync(_bootstrap_sqlite_schema)
        else:
            await conn.run_sync(SQLModel.metadata.create_all)

    if apply_postgres_triggers and target_engine.dialect.name == "postgresql":
        await apply_async_triggers(target_engine)


@contextmanager
def get_session(engine=None) -> Generator[Session, None, None]:
    """Provide a transactional synchronous database session."""
    target_engine = engine or get_engine()
    with Session(target_engine) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


@asynccontextmanager
async def get_async_session(
    async_engine: AsyncEngine | None = None,
) -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional asynchronous database session."""
    target_engine = async_engine or get_async_engine()
    async_session_factory = sessionmaker(
        target_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
