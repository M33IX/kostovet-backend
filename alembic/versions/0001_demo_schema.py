"""Initial demo schema.

Revision ID: 0001_demo_schema
Revises:
"""

from alembic import op
from kosto_vet.models import Base

revision = "0001_demo_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
