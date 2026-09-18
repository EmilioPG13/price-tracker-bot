<%text>"""</%text>${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
<%text>"""</%text>

from collections.abc import Sequence

import sqlalchemy as sa

# Autogenerate renders custom column types by their full path, so this import is what
# keeps `price_tracker.db.models.UtcDateTime(...)` in a migration from being a NameError.
# It is unused in migrations that touch no timestamp; ruff is told so on the line itself.
import price_tracker.db.models  # noqa: F401
from alembic import op
${imports if imports else ""}
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
