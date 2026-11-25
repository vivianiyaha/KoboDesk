from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class TrialSignupRequest(BaseModel):
    shop_name: str = Field(..., min_length=2, max_length=140)
    whatsapp_phone: str = Field(..., min_length=7, max_length=30)


class ShopAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    shop_name: str
    whatsapp_phone: str
    trial_ends_at: datetime
    subscription_status: str
    paid_until: datetime | None
    created_at: datetime


class TransactionCreate(BaseModel):
    tx_type: Literal["sale", "expense"]
    payment_method: Literal["cash", "transfer_pos"] = "cash"
    category: str = Field(..., min_length=1, max_length=80)
    amount_ngn: int = Field(..., gt=0)
    note: str | None = None


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    shop_id: int
    tx_type: str
    payment_method: str
    category: str
    amount_ngn: int
    note: str | None
    closed_day: bool
    created_at: datetime


class DailyCloseSummaryOut(BaseModel):
    shop_id: int
    shop_name: str
    total_sales_ngn: int
    cash_sales_ngn: int
    transfer_pos_sales_ngn: int
    total_expenses_ngn: int
    net_profit_ngn: int
    whatsapp_summary: str


class DebtorCreate(BaseModel):
    customer_name: str = Field(..., min_length=1, max_length=120)
    customer_phone: str = Field(..., min_length=7, max_length=30)
    balance_ngn: int = Field(..., gt=0)
    due_label: str = Field(default="Due in 3 days", max_length=60)


class DebtorPaymentRequest(BaseModel):
    amount_paid_ngn: int = Field(..., gt=0)


class DebtorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    shop_id: int
    customer_name: str
    customer_phone: str
    balance_ngn: int
    due_label: str
    is_settled: bool
    last_reminded_at: datetime | None


class ReminderPayloadOut(BaseModel):
    debtor_id: int
    customer_name: str
    customer_phone: str
    balance_ngn: int
    message: str
    whatsapp_url: str


class StockItemCreate(BaseModel):
    item_name: str = Field(..., min_length=1, max_length=120)
    qty_left: int = Field(..., ge=0)
    reorder_threshold: int = Field(default=5, ge=0)


class StockItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    shop_id: int
    item_name: str
    qty_left: int
    reorder_threshold: int
    is_low_stock: bool
