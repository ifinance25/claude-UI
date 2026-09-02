"""initial schema

Baseline migration. Creates the full ORM schema (users, projects,
sessions, action_logs, messages) from the shared metadata, so a fresh
database can be stamped/upgraded via Alembic instead of relying solely on
SessionManager's runtime ``CREATE TABLE IF NOT EXISTS`` bootstrap.

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-29 00:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from src.claude.session import Base

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
