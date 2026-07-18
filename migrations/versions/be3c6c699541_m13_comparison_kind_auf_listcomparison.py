"""M13 comparison_kind auf ListComparison

Revision ID: be3c6c699541
Revises: 84dc72643881
Create Date: 2026-07-18 20:09:24.120787

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'be3c6c699541'
down_revision = '84dc72643881'
branch_labels = None
depends_on = None


def upgrade():
    # Spalte zunaechst NULLABLE hinzufuegen, damit bereits bestehende list_comparisons-Zeilen
    # (aus M11/M12) beim Anlegen nicht verletzt werden. Danach per SQL auf 'temporal' befuellen
    # (bisheriges Verhalten war immer zeitbasiert) und erst dann auf NOT NULL umstellen - ein
    # direktes Hinzufuegen als NOT NULL ohne server_default wuerde gegen eine bereits befuellte
    # Tabelle fehlschlagen.
    with op.batch_alter_table('list_comparisons', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('comparison_kind', sa.Enum('TEMPORAL', 'OWN_VS_GS', name='comparisonkind'), nullable=True)
        )

    op.execute("UPDATE list_comparisons SET comparison_kind = 'TEMPORAL' WHERE comparison_kind IS NULL")

    with op.batch_alter_table('list_comparisons', schema=None) as batch_op:
        batch_op.alter_column(
            'comparison_kind', existing_type=sa.Enum('TEMPORAL', 'OWN_VS_GS', name='comparisonkind'), nullable=False
        )
        batch_op.create_index(batch_op.f('ix_list_comparisons_comparison_kind'), ['comparison_kind'], unique=False)


def downgrade():
    with op.batch_alter_table('list_comparisons', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_list_comparisons_comparison_kind'))
        batch_op.drop_column('comparison_kind')
