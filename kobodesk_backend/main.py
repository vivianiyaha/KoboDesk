"""KoboDesk API — FastAPI + PostgreSQL. Deploy on Render."""
import hashlib, hmac, json, logging, math, os, re, uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import bcrypt, httpx, jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, String, UniqueConstraint, create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from starlette.concurrency import run_in_threadpool

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kobodesk")

ENV = os.getenv("ENV", "development")
JWT_SECRET = os.environ["JWT_SECRET"]
PAYSTACK_KEY = os.getenv("PAYSTACK_SECRET_KEY", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:8888").rstrip("/")
ORIGINS = [o.strip().rstrip("/") for o in os.getenv("ALLOWED_ORIGINS", FRONTEND_URL).split(",") if o.strip()]
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "14"))
PLANS = {"weekly": (35_000, 7), "monthly": (150_000, 30)}  # (kobo, days)
LAGOS = ZoneInfo("Africa/Lagos")

# ---------- database ----------
DB_URL = re.sub(r"^postgres(ql)?://", "postgresql+psycopg://", os.getenv("DATABASE_URL", "sqlite:///./dev.db"))
engine = create_engine(DB_URL, pool_pre_ping=True,
                       **({} if DB_URL.startswith("sqlite") else {"pool_size": 5, "max_overflow": 5}))
SessionLocal = sessionmaker(engine, expire_on_commit=False)

def now(): return datetime.now(timezone.utc)
def aware(d): return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)
def today(): return datetime.now(LAGOS).date()

class Base(DeclarativeBase): pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_name: Mapped[str] = mapped_column(String(80))
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(80))
    trial_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paid_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    @property
    def access_until(self):
        return max(d for d in (aware(self.trial_ends_at), aware(self.paid_until)) if d)

class Entry(Base):
    __tablename__ = "entries"
    __table_args__ = (UniqueConstraint("user_id", "client_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    client_id: Mapped[Optional[str]] = mapped_column(String(40))  # idempotency key for offline sync
    kind: Mapped[str] = mapped_column(String(10))  # sale | expense | debt
    title: Mapped[str] = mapped_column(String(120))
    amount: Mapped[int] = mapped_column(BigInteger)  # naira
    method: Mapped[Optional[str]] = mapped_column(String(10))  # cash | transfer
    party_phone: Mapped[Optional[str]] = mapped_column(String(20))
    due_date: Mapped[Optional[date]] = mapped_column(Date)
    settled: Mapped[bool] = mapped_column(Boolean, default=False)
    entry_date: Mapped[date] = mapped_column(Date, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Payment(Base):
    __tablename__ = "payments"
    reference: Mapped[str] = mapped_column(String(80), primary_key=True)  # PK makes fulfilment idempotent
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    plan: Mapped[str] = mapped_column(String(10))
    amount: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

@asynccontextmanager
async def lifespan(_):
    Base.metadata.create_all(engine)  # swap for Alembic migrations once the schema starts changing
    yield

def client_ip(request: Request):
    return (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "anon"))

limiter = Limiter(key_func=client_ip)
app = FastAPI(title="KoboDesk API", lifespan=lifespan,
              docs_url=None if ENV == "production" else "/docs", redoc_url=None, openapi_url=None if ENV == "production" else "/openapi.json")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["GET", "POST", "PATCH"],
                   allow_headers=["Authorization", "Content-Type"], max_age=600)

# ---------- schemas ----------
def norm_phone(v: str) -> str:
    d = re.sub(r"\D", "", v)
    if len(d) == 11 and d[0] == "0": d = "234" + d[1:]
    elif len(d) == 10 and d[0] in "789": d = "234" + d
    if not re.fullmatch(r"234[789]\d{9}", d): raise ValueError("Enter a valid Nigerian phone number")
    return "+" + d

class Creds(BaseModel):
    model_config = {"str_strip_whitespace": True}
    phone: str
    password: str = Field(min_length=8, max_length=72)
    _p = field_validator("phone")(norm_phone)

class Register(Creds):
    business_name: str = Field(min_length=2, max_length=80)

class EntryIn(BaseModel):
    model_config = {"str_strip_whitespace": True}
    client_id: Optional[str] = Field(None, max_length=40)
    kind: Literal["sale", "expense", "debt"]
    title: str = Field(min_length=1, max_length=120)
    amount: int = Field(gt=0, le=10_000_000_000)
    method: Optional[Literal["cash", "transfer"]] = None
    party_phone: Optional[str] = None
    due_date: Optional[date] = None
    entry_date: Optional[date] = None

    @field_validator("party_phone")
    @classmethod
    def _pp(cls, v): return norm_phone(v) if v else None

class Checkout(BaseModel):
    plan: Literal["weekly", "monthly"]

# ---------- auth ----------
def hash_pw(p: str) -> str: return bcrypt.hashpw(p.encode()[:72], bcrypt.gensalt(12)).decode()
def check_pw(p: str, h: str) -> bool: return bcrypt.checkpw(p.encode()[:72], h.encode())
def token(uid: int) -> str:
    return jwt.encode({"sub": str(uid), "exp": now() + timedelta(days=30)}, JWT_SECRET, algorithm="HS256")

def get_db():
    with SessionLocal() as db: yield db

def current_user(request: Request, db=Depends(get_db)) -> User:
    try:
        uid = int(jwt.decode(request.headers.get("authorization", "")[7:], JWT_SECRET, algorithms=["HS256"])["sub"])
    except Exception:
        raise HTTPException(401, "Please log in again")
    user = db.get(User, uid)
    if not user: raise HTTPException(401, "Please log in again")
    return user

def active_user(u: User = Depends(current_user)) -> User:
    if u.access_until <= now(): raise HTTPException(402, "Your KoboDesk pass has expired. Renew to continue.")
    return u

def me_out(u: User):
    left = (u.access_until - now()).total_seconds()
    paid = u.paid_until and aware(u.paid_until) > now()
    return {"business_name": u.business_name, "phone": u.phone, "days_left": max(0, math.ceil(left / 86400)),
            "status": "expired" if left <= 0 else "active" if paid else "trial", "access_until": u.access_until.isoformat()}

def entry_out(e: Entry):
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in
            {"id": e.id, "kind": e.kind, "title": e.title, "amount": e.amount, "method": e.method,
             "party_phone": e.party_phone, "due_date": e.due_date, "settled": e.settled, "entry_date": e.entry_date}.items()}

# ---------- routes ----------
@app.get("/health")
def health(db=Depends(get_db)):
    db.execute(select(1)); return {"status": "ok"}

@app.post("/api/auth/register", status_code=201)
@limiter.limit("5/minute")
def register(request: Request, body: Register, db=Depends(get_db)):
    if db.scalar(select(User.id).where(User.phone == body.phone)):
        raise HTTPException(409, "An account with this number already exists. Please log in.")
    u = User(business_name=body.business_name, phone=body.phone, password_hash=hash_pw(body.password),
             trial_ends_at=now() + timedelta(days=TRIAL_DAYS))
    db.add(u)
    try: db.commit()
    except IntegrityError:
        db.rollback(); raise HTTPException(409, "An account with this number already exists. Please log in.")
    return {"token": token(u.id), "user": me_out(u)}

@app.post("/api/auth/login")
@limiter.limit("10/minute")
def login(request: Request, body: Creds, db=Depends(get_db)):
    u = db.scalar(select(User).where(User.phone == body.phone))
    if not u or not check_pw(body.password, u.password_hash):
        raise HTTPException(401, "Incorrect phone number or password")
    return {"token": token(u.id), "user": me_out(u)}

@app.get("/api/me")
def me(u: User = Depends(current_user)): return me_out(u)

@app.post("/api/entries", status_code=201)
@limiter.limit("120/minute")
def add_entry(request: Request, body: EntryIn, u: User = Depends(active_user), db=Depends(get_db)):
    find = lambda: db.scalar(select(Entry).where(Entry.user_id == u.id, Entry.client_id == body.client_id))
    if body.client_id and (ex := find()): return entry_out(ex)
    d = body.entry_date or today()
    if abs((d - today()).days) > 31: raise HTTPException(422, "Entry date is out of range")
    e = Entry(user_id=u.id, client_id=body.client_id, kind=body.kind, title=body.title, amount=body.amount,
              method=(body.method or "cash") if body.kind == "sale" else None,
              party_phone=body.party_phone if body.kind == "debt" else None,
              due_date=body.due_date if body.kind == "debt" else None, entry_date=d)
    db.add(e)
    try: db.commit()
    except IntegrityError:
        db.rollback()
        if body.client_id and (ex := find()): return entry_out(ex)
        raise
    return entry_out(e)

@app.get("/api/entries")
def list_entries(on: Optional[date] = None, u: User = Depends(current_user), db=Depends(get_db)):
    rows = db.scalars(select(Entry).where(Entry.user_id == u.id, Entry.entry_date == (on or today()))
                      .order_by(Entry.id.desc()).limit(300)).all()
    return [entry_out(r) for r in rows]

@app.patch("/api/entries/{entry_id}/settle")
def settle(entry_id: int, u: User = Depends(active_user), db=Depends(get_db)):
    e = db.get(Entry, entry_id)
    if not e or e.user_id != u.id or e.kind != "debt": raise HTTPException(404, "Entry not found")
    e.settled = True; db.commit()
    return entry_out(e)

@app.get("/api/summary")
def summary(on: Optional[date] = None, u: User = Depends(current_user), db=Depends(get_db)):
    on = on or today()
    def total(*c): return int(db.scalar(select(func.coalesce(func.sum(Entry.amount), 0)).where(Entry.user_id == u.id, *c)) or 0)
    day = Entry.entry_date == on
    cash = total(day, Entry.kind == "sale", Entry.method == "cash")
    transfer = total(day, Entry.kind == "sale", Entry.method == "transfer")
    expenses = total(day, Entry.kind == "expense")
    owing = db.scalars(select(Entry).where(Entry.user_id == u.id, Entry.kind == "debt", Entry.settled.is_(False))
                       .order_by(Entry.due_date.is_(None), Entry.due_date, Entry.id).limit(100)).all()
    return {"date": on.isoformat(), "sales_cash": cash, "sales_transfer": transfer, "sales": cash + transfer,
            "expenses": expenses, "net": cash + transfer - expenses,
            "debts_total": sum(d.amount for d in owing), "debts": [entry_out(d) for d in owing]}

# ---------- billing (Paystack) ----------
def paystack(method: str, path: str, **kw):
    if not PAYSTACK_KEY: raise HTTPException(503, "Payments are not configured yet")
    try:
        r = httpx.request(method, "https://api.paystack.co" + path, timeout=15, headers={"Authorization": f"Bearer {PAYSTACK_KEY}"}, **kw)
        r.raise_for_status()
        return r.json()["data"]
    except (httpx.HTTPError, KeyError):
        log.exception("Paystack call failed: %s", path)
        raise HTTPException(502, "Payment provider unavailable. Please try again.")

def fulfil(db, tx: dict) -> bool:
    """Idempotently credit a successful Paystack transaction. Amount and currency are re-checked server-side."""
    if tx.get("status") != "success": return False
    meta = tx.get("metadata") or {}
    try:
        user = db.get(User, int(meta["user_id"])); kobo, days = PLANS[meta["plan"]]
    except (KeyError, ValueError, TypeError): return False
    if not user or tx.get("currency") != "NGN" or int(tx.get("amount", 0)) < kobo:
        log.warning("Rejected transaction %s", tx.get("reference")); return False
    user.paid_until = max(now(), aware(user.paid_until) or now()) + timedelta(days=days)
    db.add(Payment(reference=tx["reference"], user_id=user.id, plan=meta["plan"], amount=int(tx["amount"])))
    try: db.commit()
    except IntegrityError:
        db.rollback(); return False  # already processed
    return True

@app.post("/api/billing/checkout")
@limiter.limit("10/minute")
def checkout(request: Request, body: Checkout, u: User = Depends(current_user)):
    ref = f"kd_{u.id}_{uuid.uuid4().hex[:16]}"
    data = paystack("POST", "/transaction/initialize", json={
        "email": f"{u.phone.lstrip('+')}@kobodesk.ng", "amount": PLANS[body.plan][0], "currency": "NGN",
        "reference": ref, "callback_url": f"{FRONTEND_URL}/", "metadata": {"user_id": u.id, "plan": body.plan}})
    return {"authorization_url": data["authorization_url"], "reference": ref}

@app.get("/api/billing/verify")
def verify(reference: str, u: User = Depends(current_user), db=Depends(get_db)):
    if not re.fullmatch(r"kd_\d+_[0-9a-f]{16}", reference) or not reference.startswith(f"kd_{u.id}_"):
        raise HTTPException(404, "Unknown payment reference")
    fulfil(db, paystack("GET", f"/transaction/verify/{reference}"))
    db.refresh(u)
    return me_out(u)

@app.post("/api/billing/webhook")
async def webhook(request: Request, db=Depends(get_db)):
    raw = await request.body()
    expected = hmac.new(PAYSTACK_KEY.encode(), raw, hashlib.sha512).hexdigest()
    if not PAYSTACK_KEY or not hmac.compare_digest(request.headers.get("x-paystack-signature", ""), expected):
        raise HTTPException(401, "Invalid signature")
    event = json.loads(raw)
    if event.get("event") == "charge.success":
        await run_in_threadpool(fulfil, db, event["data"])
    return {"ok": True}
