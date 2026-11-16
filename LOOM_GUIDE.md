# Loom explainer guide — Clinic Booking API

Use this as your script. Each section = one thing to say + where to point on screen. Rough length: 8–10 min.

---

## 0. The 30-second pitch (say this first)
"I built a clinic booking API in FastAPI with a Postgres database, plus a small web page that uses it. The interesting part isn't the endpoints. It's that the *hard guarantees* — no double booking, no duplicate retries, rate limiting — are enforced by the **database**, not by Python code in memory. That way they still hold if two requests hit at the exact same millisecond, or if the server runs multiple workers."

**Key sentence to repeat:** *"Don't trust memory, trust the database."*

---

## 1. Project layout (show the file tree)
| File | What it is |
|---|---|
| `app/main.py` | The whole API: tables, auth, every endpoint |
| `static/index.html` | The web page (plain HTML + JS, no framework) |
| `tests/test_api.py` | Automated tests for all 5 levels, incl. a 20-request race |
| `requirements.txt` | Dependencies |
| `render.yaml` | Deploy config for Render |

**Stack:** FastAPI (web framework) · SQLAlchemy (talks to the DB) · Postgres on Neon (prod) / SQLite (local) · hosted on Render.

---

## 2. The data model (point at the `Table(...)` definitions)
Five tables:
- **slots** — id, provider, start, end. `start_ts` is the start as a number (epoch seconds) so I can sort and compare to "now" easily.
- **bookings** — one row per booking. `seq` is an auto-incrementing number that gives a reliable "oldest first" order (used for pagination).
- **idempotency_keys** — remembers, per key, what the request body was and what I answered.
- **booking_attempts** — one row per attempt, used for rate limiting.

"Available" is **not stored**. It's calculated: a slot is available if no *confirmed* booking points at it. One source of truth → can't get out of sync.

---

## 3. Level by level

### Level 1 — list, book, look up
Plain endpoints. `POST /reset` wipes everything and loads the slots the checker sends (their checker uses this to set up a clean test). `GET /slots` sorts by start time and takes an optional `?provider=`. `POST /bookings` returns **201** with status `confirmed`. `GET /bookings/{id}` returns it or **404**.

### Level 2 — API key + validation
- **Middleware** = code that runs before every request. Mine checks the `X-API-Key` header against the `API_KEY` environment variable → otherwise **401**. The key is in an env var, never in the code or GitHub.
- Validation (`_book` function): missing/empty field or bad email → **400**; unknown slot → **404**; slot already started → **400**. I `.strip()` the name/email and lowercase the email.
- All errors come back as `{"error": "..."}` as the spec demands (custom exception handlers).

### Level 3 — no double booking, cancel  ⭐ (spend the most time here)
**The trap:** the naive way is "check if the slot is free, then insert". With two simultaneous requests, *both* check, *both* see "free", *both* insert → double booking. This is a **race condition**.

**My fix:** a **partial unique index** in the database:
> "slot_id must be unique, but only among rows where status = 'confirmed'."

Now the database itself refuses the second insert, no matter how fast they arrive — I catch the error and return **409**. Because only *confirmed* rows count, a **cancelled** booking frees the slot automatically; I just flip `status` to `cancelled`.

Cancel is also race-safe: `UPDATE ... WHERE status='confirmed'` — if it changed 0 rows, someone already cancelled → **409**.

**Proof to show:** the test fires 20 simultaneous bookings at one slot → exactly 1 gets 201, 19 get 409.

### Level 4 — idempotency + pagination
**Idempotency** = "doing it twice has the same effect as doing it once." Real-world: a phone loses signal, the app retries, and you don't want two bookings.
- Client sends an `Idempotency-Key` header. First time: I do the booking and **save the response** under that key (plus a hash of the body).
- Same key + same body → I return the saved response (same status, same booking id). No new booking.
- Same key + *different* body → **422** (key reuse is a client bug).
- On Postgres I take an advisory lock on the key so two simultaneous retries line up instead of both running.
- The web page generates one key per booking attempt, so a double-click is safe.

**Cursor pagination** (`GET /bookings?email=&limit=&cursor=`):
- Why not page numbers? If a new booking arrives while you're paging, page numbers shift and you skip/repeat items. A cursor says "continue *after this exact row*".
- My cursor is just the last row's `seq`, base64-encoded (opaque to the client). Query: `WHERE seq > cursor ORDER BY seq LIMIT n+1`. I fetch **one extra row** — if it exists there's another page (so `next_cursor` is set); if not, `next_cursor: null`.
- `limit` must be 1–50 (default 10), else **400**. Email match is case-insensitive.

### Level 5 — rate limiting
Max **5 attempts per email per minute**, *failed attempts count too* (so spamming bad requests doesn't dodge it).
- Each attempt inserts a row in `booking_attempts`. Before inserting I count rows for that email in the last 60 s. If already 5 → **429** with `Retry-After` = seconds until the oldest attempt falls out of the window.
- Stored in the **database, not in a Python dict**, so it works across multiple workers and survives restarts.
- Rejected (429) requests aren't recorded, otherwise a spammer could keep themselves blocked forever.
- Per email, so other people are unaffected.

---

## 4. Order of checks inside POST /bookings (good to explain)
1. Idempotency replay? → return the saved answer.
2. Rate limit → 429.
3. Validation → 400.
4. Slot exists → 404 · slot started → 400.
5. Insert → 409 if the unique index rejects it.
6. Save the response for the idempotency key.

Everything runs in **one transaction**, and the booking insert is in a **savepoint**, so if the insert loses a race (409), the attempt log is still saved.

---

## 5. The web page
Plain HTML/JS served by the same app at `/`. Loads slots → click **Book** → enter name & email → shows the confirmation with a **Cancel** option. It shows friendly messages for 409 ("someone just booked that") and 429 ("try again in Ns"). The API key is entered once and saved in the browser's localStorage (or pre-filled via `WEB_PAGE_KEY` if you set it).

---

## 6. Deployment (one slide worth)
- **Neon** = free Postgres database → gives a connection string.
- **Render** = runs the FastAPI app. Env vars: `API_KEY`, `DATABASE_URL`, `PYTHON_VERSION`.
- Why a real database and not SQLite/memory? Free hosts restart and wipe local files; the task says bookings must survive.
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

---

## 7. Likely questions & answers
- **Why a DB constraint instead of a Python lock?** A Python lock only protects one process. With 2+ workers/servers it fails. The DB is the single shared referee.
- **Why store `email` lowercase?** So `Ada@X.com` and `ada@x.com` are the same person for lookups and rate limits.
- **What if the idempotent request was a 409?** I store that too, so a retry gets the same 409.
- **Why not store `available` in the slots table?** Two sources of truth drift apart; deriving it can't.
- **What would you add next?** Migrations (Alembic), auto-expiring old idempotency keys/attempt rows, per-IP limits, hashing the API key / per-user keys, structured logging.
- **Known limitation to be honest about:** the API key sits in the web page's browser, so anyone using the page can see it — fine for this assessment, not for production (you'd put the page behind real user login).

---

## 8. Demo script (what to click/run on camera)
1. Open the web page → see slots → book one → show confirmation → refresh, slot now "Booked".
2. Try booking it again from the API docs (`/docs`) → **409**.
3. Cancel → slot available again.
4. Run `pytest -q tests` → 5 passed (mention the race test).
5. Show the same `Idempotency-Key` twice → same booking id.
