"""Clinic booking API (Koya assessment, Task 1) - FastAPI + SQL database.

Everything that must be safe under concurrency is enforced BY THE DATABASE,
not by Python variables, so it keeps working with many workers/instances.
"""
import base64
import hashlib
import json
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import (Column, Float, Index, Integer, MetaData, String, Table,
                        Text, create_engine, delete, func, insert, select, text)
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

API_KEY = os.environ.get("API_KEY", "dev-key")
RATE_LIMIT = 5          # attempts ...
RATE_WINDOW = 60        # ... per this many seconds, per email

# ---------------------------------------------------------------- database
db_url = os.environ.get("DATABASE_URL", "sqlite:///./clinic.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
elif db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)

is_pg = db_url.startswith("postgresql")
engine = create_engine(
    db_url,
    pool_pre_ping=True,
    **({} if is_pg else {"connect_args": {"timeout": 30}}),
)

md = MetaData()
slots = Table(
    "slots", md,
    Column("id", String, primary_key=True),
    Column("provider", String, nullable=False),
    Column("start", String, nullable=False),      # original string, returned as-is
    Column("end", String, nullable=False),
    Column("start_ts", Float, nullable=False),    # epoch seconds, for sorting/"has started"
)
bookings = Table(
    "bookings", md,
    Column("seq", Integer, primary_key=True, autoincrement=True),  # gives a stable "oldest first" order
    Column("id", String, nullable=False, unique=True),
    Column("slot_id", String, nullable=False),
    Column("name", String, nullable=False),
    Column("email", String, nullable=False, index=True),
    Column("status", String, nullable=False),
    Column("created_at", String, nullable=False),
)
# LEVEL 3 KEY IDEA: only ONE confirmed booking per slot, enforced by the DB.
Index("one_confirmed_per_slot", bookings.c.slot_id, unique=True,
      sqlite_where=(bookings.c.status == "confirmed"),
      postgresql_where=(bookings.c.status == "confirmed"))
idem = Table(
    "idempotency_keys", md,
    Column("key", String, primary_key=True),
    Column("body_hash", String, nullable=False),
    Column("status_code", Integer, nullable=False),
    Column("response", Text, nullable=False),
)
attempts = Table(
    "booking_attempts", md,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("email", String, nullable=False, index=True),
    Column("ts", Float, nullable=False),
)
md.create_all(engine)

# --------------------------------------------------------------------- app
app = FastAPI(title="Clinic booking API", docs_url="/docs", redoc_url=None)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def err(status: int, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


# Every error must look like {"error": "..."}
@app.exception_handler(StarletteHTTPException)
async def http_exc(_, exc):
    return err(exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_exc(_, exc):
    return err(400, "invalid request")


# LEVEL 2: API key check on every API route (page + health are public)
PUBLIC = {"/", "/app", "/health", "/docs", "/openapi.json"}


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    if request.url.path not in PUBLIC and request.method != "OPTIONS":
        if request.headers.get("X-API-Key") != API_KEY:
            return err(401, "missing or invalid API key")
    return await call_next(request)


# Let the web page (if hosted elsewhere) call the API from a browser.
@app.middleware("http")
async def cors(request: Request, call_next):
    if request.method == "OPTIONS":
        from starlette.responses import Response
        resp = Response(status_code=204)
    else:
        resp = await call_next(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "*"
    resp.headers["Access-Control-Expose-Headers"] = "Retry-After"
    return resp


def parse_ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def booking_json(row) -> dict:
    return {k: row[k] for k in ("id", "slot_id", "name", "email", "status", "created_at")}


def advisory_lock(conn, name: str):
    """Postgres: make concurrent requests for the same key/email take turns."""
    if is_pg:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": name})


# ------------------------------------------------------------------ routes
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/reset")
async def reset(request: Request):
    try:
        body = await request.json()
        raw = body["slots"]
        assert isinstance(raw, list)
        rows = [{"id": str(s["id"]), "provider": str(s["provider"]),
                 "start": s["start"], "end": s["end"],
                 "start_ts": parse_ts(s["start"])} for s in raw]
    except Exception:
        return err(400, "body must be {\"slots\": [{id, provider, start, end}]}")
    with engine.begin() as conn:
        for t in (bookings, idem, attempts, slots):
            conn.execute(delete(t))
        if rows:
            conn.execute(insert(slots), rows)
    return {"ok": True, "slots": len(rows)}


@app.get("/slots")
def list_slots(provider: str | None = None):
    # "available" = no confirmed booking points at this slot
    taken = select(bookings.c.slot_id).where(bookings.c.status == "confirmed")
    q = select(slots, slots.c.id.not_in(taken).label("available"))
    if provider is not None:
        q = q.where(slots.c.provider == provider)
    q = q.order_by(slots.c.start_ts, slots.c.id)
    with engine.connect() as conn:
        out = [{"id": r.id, "provider": r.provider, "start": r.start,
                "end": r.end, "available": bool(r.available)}
               for r in conn.execute(q)]
    return {"slots": out}


@app.post("/bookings")
async def create_booking(request: Request):
    idem_key = request.headers.get("Idempotency-Key")
    try:
        body = await request.json()
    except Exception:
        body = None
    # Hash of the exact JSON content, to detect "same key, different body"
    body_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    # One transaction for the whole request. Responses are RETURNED (not raised)
    # so the transaction commits - failed attempts must still be counted (L5).
    with engine.begin() as conn:
        # ---- LEVEL 4: idempotency replay
        if idem_key:
            advisory_lock(conn, "idem:" + idem_key)
            saved = conn.execute(select(idem).where(idem.c.key == idem_key)).first()
            if saved:
                if saved.body_hash != body_hash:
                    return err(422, "Idempotency-Key was already used with a different body")
                return JSONResponse(json.loads(saved.response), status_code=saved.status_code)

        # ---- LEVEL 5: rate limit per email (counts failed attempts too)
        email_raw = body.get("email") if isinstance(body, dict) else None
        if isinstance(email_raw, str) and email_raw.strip():
            email_key = email_raw.strip().lower()
            advisory_lock(conn, "rate:" + email_key)
            now = time.time()
            recent = [r.ts for r in conn.execute(
                select(attempts.c.ts).where(attempts.c.email == email_key,
                                            attempts.c.ts > now - RATE_WINDOW)
                .order_by(attempts.c.ts))]
            if len(recent) >= RATE_LIMIT:
                wait = max(1, math.ceil(recent[0] + RATE_WINDOW - now))
                return err(429, "too many booking attempts, slow down",
                           {"Retry-After": str(wait)})
            conn.execute(insert(attempts).values(email=email_key, ts=now))

        status, payload = _book(conn, body)

        # ---- LEVEL 4: remember the response so a retry gets the same one
        if idem_key:
            conn.execute(insert(idem).values(
                key=idem_key, body_hash=body_hash,
                status_code=status, response=json.dumps(payload)))
    return JSONResponse(payload, status_code=status)


def _book(conn, body):
    """Validate + insert. Returns (status_code, payload)."""
    # ---- LEVEL 2: validation
    if not isinstance(body, dict):
        return 400, {"error": "body must be a JSON object"}
    vals = {}
    for field in ("slot_id", "name", "email"):
        v = body.get(field)
        if not isinstance(v, str) or not v.strip():
            return 400, {"error": f"{field} is required"}
        vals[field] = v.strip()
    vals["email"] = vals["email"].lower()
    if not EMAIL_RE.match(vals["email"]):
        return 400, {"error": "email is not valid"}

    slot = conn.execute(select(slots).where(slots.c.id == vals["slot_id"])).first()
    if not slot:
        return 404, {"error": "slot not found"}
    if slot.start_ts <= time.time():
        return 400, {"error": "slot has already started"}

    # ---- LEVEL 3: the unique partial index decides who wins a race
    row = {**vals, "id": "bk_" + uuid.uuid4().hex[:12], "status": "confirmed",
           "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"}
    try:
        with conn.begin_nested():   # savepoint: a lost race must not undo the attempt log
            conn.execute(insert(bookings).values(**row))
    except IntegrityError:
        return 409, {"error": "slot already booked"}
    return 201, booking_json(row)


@app.get("/bookings")
def list_bookings(email: str | None = None, limit: str | None = None,
                  cursor: str | None = None):
    # LEVEL 4: cursor pagination
    if limit is None:
        n = 10
    else:
        try:
            n = int(limit)
        except ValueError:
            return err(400, "limit must be an integer from 1 to 50")
        if not 1 <= n <= 50:
            return err(400, "limit must be an integer from 1 to 50")
    if not email or not email.strip():
        return err(400, "email is required")
    after = 0
    if cursor:
        try:
            after = int(base64.urlsafe_b64decode(cursor.encode()).decode())
        except Exception:
            return err(400, "invalid cursor")
    q = (select(bookings)
         .where(func.lower(bookings.c.email) == email.strip().lower(),
                bookings.c.seq > after)
         .order_by(bookings.c.seq).limit(n + 1))      # fetch one extra to know if more exist
    with engine.connect() as conn:
        rows = conn.execute(q).mappings().all()
    page = rows[:n]
    next_cursor = None
    if len(rows) > n:
        next_cursor = base64.urlsafe_b64encode(str(page[-1]["seq"]).encode()).decode()
    return {"bookings": [booking_json(r) for r in page], "next_cursor": next_cursor}


@app.get("/bookings/{booking_id}")
def get_booking(booking_id: str):
    with engine.connect() as conn:
        row = conn.execute(select(bookings).where(bookings.c.id == booking_id)).mappings().first()
    if not row:
        return err(404, "booking not found")
    return booking_json(row)


@app.delete("/bookings/{booking_id}")
def cancel_booking(booking_id: str):
    with engine.begin() as conn:
        row = conn.execute(select(bookings).where(bookings.c.id == booking_id)).mappings().first()
        if not row:
            return err(404, "booking not found")
        # Conditional UPDATE: only one of two simultaneous cancels can succeed
        res = conn.execute(
            bookings.update().where(bookings.c.id == booking_id,
                                    bookings.c.status == "confirmed")
            .values(status="cancelled"))
        if res.rowcount == 0:
            return err(409, "booking already cancelled")
        row = conn.execute(select(bookings).where(bookings.c.id == booking_id)).mappings().first()
    return booking_json(row)


# ---------------------------------------------------------------- web page
PAGE = Path(__file__).resolve().parent.parent / "static" / "index.html"


@app.get("/", response_class=HTMLResponse)
@app.get("/app", response_class=HTMLResponse)
def web_page():
    html = PAGE.read_text(encoding="utf-8")
    # Optional: pre-fill the key so a reviewer can use the page immediately.
    return html.replace("__PREFILL_KEY__", os.environ.get("WEB_PAGE_KEY", ""))
