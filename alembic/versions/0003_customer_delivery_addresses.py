"""Add customer-owned delivery addresses.

Revision ID: 0003_customer_delivery_addresses
Revises: 0002_content_and_media
"""

from alembic import op
from kosto_vet.infrastructure.models import Base

revision = "0003_customer_delivery_addresses"
down_revision = "0002_content_and_media"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["customer_delivery_addresses"].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["customer_delivery_addresses"].drop(bind=op.get_bind(), checkfirst=True)
