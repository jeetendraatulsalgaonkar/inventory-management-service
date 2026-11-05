from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from inventory_management_service.db import Base


class Product(Base):
    __tablename__ = "products"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(2000))
    category: Mapped[str] = mapped_column(String(100))
    price_amount: Mapped[Decimal] = mapped_column(Numeric(19, 4))
    price_currency: Mapped[str] = mapped_column(String(3))
    unit_of_measure: Mapped[str] = mapped_column(String(32))
    attributes: Mapped[dict[str, str]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(BigInteger)

    __mapper_args__ = {
        "version_id_col": version,
        "version_id_generator": lambda current: 0 if current is None else current + 1,
    }
