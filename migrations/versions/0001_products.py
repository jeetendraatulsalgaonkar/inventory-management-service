"""Create the Inventra product catalog."""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE products (
            id uuid CONSTRAINT pk_products PRIMARY KEY,
            sku varchar(64) NOT NULL CONSTRAINT uk_products_sku UNIQUE,
            name varchar(255) NOT NULL,
            description varchar(2000),
            category varchar(100) NOT NULL,
            price_amount numeric(19,4) NOT NULL,
            price_currency varchar(3) NOT NULL,
            unit_of_measure varchar(32) NOT NULL,
            attributes jsonb NOT NULL DEFAULT '{}',
            status varchar(16) NOT NULL,
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            deleted_at timestamptz,
            version bigint NOT NULL DEFAULT 0,
            CONSTRAINT ck_products_sku_normalized CHECK (sku ~ '^[A-Z0-9][A-Z0-9._-]*$'),
            CONSTRAINT ck_products_name_not_blank CHECK (btrim(name) <> ''),
            CONSTRAINT ck_products_category_not_blank CHECK (btrim(category) <> ''),
            CONSTRAINT ck_products_price_amount_non_negative CHECK (
                price_amount >= 0 AND price_amount <> 'NaN'::numeric
            ),
            CONSTRAINT ck_products_price_currency_format CHECK (price_currency ~ '^[A-Z]{3}$'),
            CONSTRAINT ck_products_unit_of_measure CHECK (unit_of_measure IN (
                'PIECE', 'BOX', 'PACK', 'PALLET', 'GRAM', 'KILOGRAM', 'MILLILITER', 'LITER',
                'CENTIMETER', 'METER', 'SQUARE_METER', 'CUBIC_METER'
            )),
            CONSTRAINT ck_products_status CHECK (
                status IN ('ACTIVE', 'INACTIVE', 'DISCONTINUED', 'DELETED')
            ),
            CONSTRAINT ck_products_attributes_is_object CHECK (jsonb_typeof(attributes) = 'object'),
            CONSTRAINT ck_products_timestamps CHECK (updated_at >= created_at),
            CONSTRAINT ck_products_deleted_state CHECK ((status = 'DELETED') = (deleted_at IS NOT NULL)),
            CONSTRAINT ck_products_deleted_at CHECK (deleted_at IS NULL OR deleted_at >= created_at),
            CONSTRAINT ck_products_version CHECK (version >= 0)
        )
    """)
    op.create_index("ix_products_category", "products", ["category"])


def downgrade():
    op.drop_table("products")
