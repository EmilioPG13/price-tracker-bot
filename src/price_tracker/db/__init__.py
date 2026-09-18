"""Persistence.

The surface the rest of the app uses is `Repository`, plus `session_scope` to get a
session to hand it. The models are exported for Alembic and for the tests; a handler
that imports `Product` to build a query of its own is a sign something belongs in the
repository instead.
"""

from __future__ import annotations

from price_tracker.db.engine import (
    create_all,
    create_engine,
    create_session_factory,
    session_scope,
)
from price_tracker.db.models import (
    ALERT_COOLDOWN,
    Base,
    PriceHistory,
    Product,
    ProductStatus,
    Tracking,
    User,
    UtcDateTime,
    utc_now,
)
from price_tracker.db.repository import MAX_CONSECUTIVE_FAILURES, Repository

__all__ = [
    "ALERT_COOLDOWN",
    "MAX_CONSECUTIVE_FAILURES",
    "Base",
    "PriceHistory",
    "Product",
    "ProductStatus",
    "Repository",
    "Tracking",
    "User",
    "UtcDateTime",
    "create_all",
    "create_engine",
    "create_session_factory",
    "session_scope",
    "utc_now",
]
