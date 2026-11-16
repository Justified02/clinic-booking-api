# Clinic Booking API (FastAPI)

Run locally: `pip install -r requirements.txt && uvicorn app.main:app --reload` (SQLite by default; set `API_KEY`).
Tests: `pip install -r requirements-dev.txt && pytest -q tests`. Web page: `/` . Env vars: `API_KEY`, `DATABASE_URL` (Postgres in prod), `WEB_PAGE_KEY` (optional).
