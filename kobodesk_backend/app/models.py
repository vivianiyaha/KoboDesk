from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ShopAccount(Base):
    __tablename__ = "shop_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    shop_name: Mapped[str] = mapped_column(String(140), nullable=False)
    whatsapp_phone: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    trial_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    subscription_status: Mapped[str] = mapped_column(String(24), default="trial", nullable=False)  # trial | active | expired
    paid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    transactions: Mapped[list["DailyTransaction"]] = relationship(back_populates="shop", cascade="all, delete-orphan")
    debtors: Mapped[list["Debtor"]] = relationship(back_populates="shop", cascade="all, delete-orphan")
    stock_items: Mapped[list["StockItem"]] = relationship(back_populates="shop", cascade="all, delete-orphan")


class DailyTransaction(Base):
    __tablename__ = "daily_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shop_accounts.id", ondelete="CASCADE"), index=True, nullable=False)
    tx_type: Mapped[str] = mapped_column(String(16), nullable=False)  # sale | expense
    payment_method: Mapped[str] = mapped_column(String(20), default="cash", nullable=False)  # cash | transfer_pos
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    amount_ngn: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_day: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True, nullable=False)

    shop: Mapped["ShopAccount"] = relationship(back_populates="transactions")


class Debtor(Base):
    __tablename__ = "debtors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shop_accounts.id", ondelete="CASCADE"), index=True, nullable=False)
    customer_name: Mapped[str] = mapped_column(String(120), nullable=False)
    customer_phone: Mapped[str] = mapped_column(String(30), nullable=False)
    balance_ngn: Mapped[int] = mapped_column(Integer, nullable=False)
    due_label: Mapped[str] = mapped_column(String(60), default="Due soon", nullable=False)
    is_settled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    shop: Mapped["ShopAccount"] = relationship(back_populates="debtors")


class StockItem(Base):
    __tablename__ = "stock_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shop_accounts.id", ondelete="CASCADE"), index=True, nullable=False)
    item_name: Mapped[str] = mapped_column(String(120), nullable=False)
    qty_left: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reorder_threshold: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    shop: Mapped["ShopAccount"] = relationship(back_populates="stock_items")
