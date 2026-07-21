"""M14 list_type auf Document

Revision ID: f1b2c3d4e5f6
Revises: be3c6c699541
Create Date: 2026-07-21 22:45:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f1b2c3d4e5f6'
down_revision = 'be3c6c699541'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'list_type',
                sa.Enum('OWN', 'GS', 'COMPARISON', 'OTHER', name='listtype'),
                nullable=True,
            )
        )


def downgrade():
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_column('list_type')
