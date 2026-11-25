from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import Base, engine, get_db
from app.models import DailyTransaction, Debtor, ShopAccount, StockItem
from app.schemas import (
    DailyCloseSummaryOut,
    DebtorCreate,
    DebtorOut,
    DebtorPaymentRequest,
    ReminderPayloadOut,
    ShopAccountOut,
    StockItemCreate,
    StockItemOut,
    TransactionCreate,
    TransactionOut,
    TrialSignupRequest,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_phone_for_wa(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if digits.startswith("0") and len(digits) == 11:
        return "234" + digits[1:]
    return digits


def format_ngn(amount: int) -> str:
    return f"₦{amount:,}"


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten to your domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "service": settings.app_name}


# --- 1. TRIAL ONBOARDING & SHOP ACCOUNT ---
@app.post("/api/shops/trial", response_model=ShopAccountOut, status_code=status.HTTP_201_CREATED)
async def start_free_trial(payload: TrialSignupRequest, db: AsyncSession = Depends(get_db)):
    clean_phone = payload.whatsapp_phone.strip()
    stmt = select(ShopAccount).where(ShopAccount.whatsapp_phone == clean_phone)
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        return existing

    now = utcnow()
    shop = ShopAccount(
        shop_name=payload.shop_name.strip(),
        whatsapp_phone=clean_phone,
        trial_ends_at=now + timedelta(days=settings.trial_days),
        subscription_status="trial",
    )
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


@app.get("/api/shops/{shop_id}", response_model=ShopAccountOut)
async def get_shop(shop_id: int, db: AsyncSession = Depends(get_db)):
    shop = await db.get(ShopAccount, shop_id)
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")
    return shop


# --- 2. DAILY SALES, EXPENSES & 1-TAP CLOSE ---
@app.post("/api/shops/{shop_id}/transactions", response_model=TransactionOut, status_code=status.HTTP_201_CREATED)
async def create_transaction(shop_id: int, payload: TransactionCreate, db: AsyncSession = Depends(get_db)):
    shop = await db.get(ShopAccount, shop_id)
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")

    tx = DailyTransaction(
        shop_id=shop_id,
        tx_type=payload.tx_type,
        payment_method=payload.payment_method,
        category=payload.category.strip(),
        amount_ngn=payload.amount_ngn,
        note=payload.note.strip() if payload.note else None,
    )
    db.add(tx)
    await db.commit()
    await db.refresh(tx)
    return tx


@app.get("/api/shops/{shop_id}/summary/today", response_model=DailyCloseSummaryOut)
async def get_today_summary(shop_id: int, db: AsyncSession = Depends(get_db)):
    shop = await db.get(ShopAccount, shop_id)
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")

    start_of_day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = select(DailyTransaction).where(
        DailyTransaction.shop_id == shop_id,
        DailyTransaction.created_at >= start_of_day,
    )
    rows = (await db.execute(stmt)).scalars().all()

    cash_sales = sum(r.amount_ngn for r in rows if r.tx_type == "sale" and r.payment_method == "cash")
    transfer_pos_sales = sum(r.amount_ngn for r in rows if r.tx_type == "sale" and r.payment_method == "transfer_pos")
    total_sales = cash_sales + transfer_pos_sales
    total_expenses = sum(r.amount_ngn for r in rows if r.tx_type == "expense")
    net_profit = total_sales - total_expenses

    wa_msg = (
        f"*{shop.shop_name} — Daily Close*\n"
        f"Sales: {format_ngn(total_sales)} (Cash: {format_ngn(cash_sales)} | POS/Transfer: {format_ngn(transfer_pos_sales)})\n"
        f"Expenses: -{format_ngn(total_expenses)}\n"
        f"*Net Profit: {format_ngn(net_profit)}*\n"
        f"_Closed via KoboDesk_"
    )

    return DailyCloseSummaryOut(
        shop_id=shop.id,
        shop_name=shop.shop_name,
        total_sales_ngn=total_sales,
        cash_sales_ngn=cash_sales,
        transfer_pos_sales_ngn=transfer_pos_sales,
        total_expenses_ngn=total_expenses,
        net_profit_ngn=net_profit,
        whatsapp_summary=wa_msg,
    )


@app.post("/api/shops/{shop_id}/close-day", response_model=DailyCloseSummaryOut)
async def close_day(shop_id: int, db: AsyncSession = Depends(get_db)):
    summary = await get_today_summary(shop_id=shop_id, db=db)
    start_of_day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = select(DailyTransaction).where(
        DailyTransaction.shop_id == shop_id,
        DailyTransaction.created_at >= start_of_day,
    )
    rows = (await db.execute(stmt)).scalars().all()
    for row in rows:
        row.closed_day = True
    await db.commit()
    return summary


# --- 3. DEBTOR LEDGER & WHATSAPP REMINDERS ---
@app.post("/api/shops/{shop_id}/debtors", response_model=DebtorOut, status_code=status.HTTP_201_CREATED)
async def add_debtor(shop_id: int, payload: DebtorCreate, db: AsyncSession = Depends(get_db)):
    shop = await db.get(ShopAccount, shop_id)
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")

    debtor = Debtor(
        shop_id=shop_id,
        customer_name=payload.customer_name.strip(),
        customer_phone=payload.customer_phone.strip(),
        balance_ngn=payload.balance_ngn,
        due_label=payload.due_label.strip(),
    )
    db.add(debtor)
    await db.commit()
    await db.refresh(debtor)
    return debtor


@app.get("/api/shops/{shop_id}/debtors", response_model=list[DebtorOut])
async def list_debtors(shop_id: int, db: AsyncSession = Depends(get_db)):
    stmt = select(Debtor).where(Debtor.shop_id == shop_id, Debtor.is_settled.is_(False)).order_by(Debtor.id.desc())
    return list((await db.execute(stmt)).scalars().all())


@app.post("/api/debtors/{debtor_id}/remind", response_model=ReminderPayloadOut)
async def create_whatsapp_reminder(debtor_id: int, db: AsyncSession = Depends(get_db)):
    debtor = await db.get(Debtor, debtor_id)
    if not debtor:
        raise HTTPException(status_code=404, detail="Debtor record not found")

    shop = await db.get(ShopAccount, debtor.shop_id)
    shop_label = shop.shop_name if shop else "our shop"
    amount_str = format_ngn(debtor.balance_ngn)

    message = (
        f"Good day {debtor.customer_name},\n"
        f"Friendly reminder from {shop_label}: your open balance is *{amount_str}* ({debtor.due_label}).\n"
        f"Kindly let us know when you can settle via transfer or cash. Thank you!"
    )
    wa_phone = normalize_phone_for_wa(debtor.customer_phone)
    wa_url = f"https://wa.me/{wa_phone}?text={quote(message)}"

    debtor.last_reminded_at = utcnow()
    await db.commit()
    await db.refresh(debtor)

    return ReminderPayloadOut(
        debtor_id=debtor.id,
        customer_name=debtor.customer_name,
        customer_phone=debtor.customer_phone,
        balance_ngn=debtor.balance_ngn,
        message=message,
        whatsapp_url=wa_url,
    )


@app.post("/api/debtors/{debtor_id}/pay", response_model=DebtorOut)
async def record_debtor_payment(debtor_id: int, payload: DebtorPaymentRequest, db: AsyncSession = Depends(get_db)):
    debtor = await db.get(Debtor, debtor_id)
    if not debtor:
        raise HTTPException(status_code=404, detail="Debtor record not found")

    new_balance = max(0, debtor.balance_ngn - payload.amount_paid_ngn)
    debtor.balance_ngn = new_balance
    debtor.is_settled = new_balance == 0
    await db.commit()
    await db.refresh(debtor)
    return debtor


# --- 4. STOCK & LOW-STOCK ALERTS ---
@app.post("/api/shops/{shop_id}/stock", response_model=StockItemOut, status_code=status.HTTP_201_CREATED)
async def upsert_stock_item(shop_id: int, payload: StockItemCreate, db: AsyncSession = Depends(get_db)):
    shop = await db.get(ShopAccount, shop_id)
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")

    item = StockItem(
        shop_id=shop_id,
        item_name=payload.item_name.strip(),
        qty_left=payload.qty_left,
        reorder_threshold=payload.reorder_threshold,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return StockItemOut(
        id=item.id,
        shop_id=item.shop_id,
        item_name=item.item_name,
        qty_left=item.qty_left,
        reorder_threshold=item.reorder_threshold,
        is_low_stock=item.qty_left <= item.reorder_threshold,
    )


@app.get("/api/shops/{shop_id}/stock/low", response_model=list[StockItemOut])
async def get_low_stock_items(shop_id: int, db: AsyncSession = Depends(get_db)):
    stmt = select(StockItem).where(StockItem.shop_id == shop_id)
    items = (await db.execute(stmt)).scalars().all()
    return [
        StockItemOut(
            id=i.id,
            shop_id=i.shop_id,
            item_name=i.item_name,
            qty_left=i.qty_left,
            reorder_threshold=i.reorder_threshold,
            is_low_stock=i.qty_left <= i.reorder_threshold,
        )
        for i in items
        if i.qty_left <= i.reorder_threshold
    ]


# --- 5. PAYSTACK WEBHOOK (SUBSCRIPTION ACTIVATION) ---
@app.post("/api/webhooks/paystack")
async def paystack_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_paystack_signature: str | None = Header(default=None),
):
    raw_body = await request.body()
    secret = settings.paystack_webhook_secret or settings.paystack_secret_key
    if not secret or not x_paystack_signature:
        raise HTTPException(status_code=401, detail="Missing webhook signature")

    computed = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(computed, x_paystack_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    event = json.loads(raw_body.decode("utf-8"))
    if event.get("event") == "charge.success":
        data = event.get("data", {})
        amount_ngn = int(data.get("amount", 0)) // 100
        metadata = data.get("metadata") or {}
        shop_id = metadata.get("shop_id")

        if shop_id:
            shop = await db.get(ShopAccount, int(shop_id))
            if shop:
                days_to_add = 30 if amount_ngn >= settings.monthly_price_ngn else 7
                base_time = max(utcnow(), shop.paid_until or utcnow())
                shop.paid_until = base_time + timedelta(days=days_to_add)
                shop.subscription_status = "active"
                await db.commit()

    return {"status": "received"}
