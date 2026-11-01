"""Base classes and utility types for Biscuit SQLModel models."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.types import Text, TypeDecorator
from sqlmodel import Field, SQLModel

try:
    from pgvector.sqlalchemy import Vector as PgVector
except ImportError:
    PgVector = None


def utcnow() -> datetime:
    """Return timezone-aware current UTC datetime."""
    return datetime.now(UTC)


class VectorColumn(TypeDecorator):
    """Platform-independent vector type.

    Uses pgvector.sqlalchemy.Vector in PostgreSQL when available,
    or falls back to JSON-serialized floats in SQLite.
    """

    impl = Text
    cache_ok = True

    def __init__(self, dim: int = 768, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.dim = dim

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql" and PgVector is not None:
            return dialect.type_descriptor(PgVector(self.dim))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql" and PgVector is not None:
            return value
        if isinstance(value, (list, tuple)):
            return json.dumps(list(value))
        return value

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError, ValueError):
                return value
        return value


class BiscuitBaseModel(SQLModel):
    """Base SQLModel configuration for all Biscuit entities."""

    model_config = {
        "arbitrary_types_allowed": True,
        "validate_assignment": True,
    }


class UUIDModel(BiscuitBaseModel):
    """Base model with standard UUID primary key."""

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
        index=True,
        nullable=False,
    )


class TimestampMixin(SQLModel):
    """Mixin providing created_at timestamp."""

    created_at: datetime = Field(
        default_factory=utcnow,
        nullable=False,
    )
