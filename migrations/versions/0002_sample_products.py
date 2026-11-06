"""Seed the sample product catalog without overwriting existing SKUs."""

from pathlib import Path

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    script = Path(__file__).resolve().parents[1] / "sql" / "0002_sample_products.sql"
    op.execute(script.read_text(encoding="utf-8"))


def downgrade():
    # Seed rows can be edited or referenced after insertion. Preserve them on
    # downgrade; the baseline downgrade drops the entire products table.
    pass
