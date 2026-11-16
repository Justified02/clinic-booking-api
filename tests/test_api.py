import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

os.environ["API_KEY"] = "k"
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

c = TestClient(app)
H = {"X-API-Key": "k"}


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def fresh():
    now = datetime.now(timezone.utc)
    slots = [{"id": f"s{i}", "provider": "Dr Ade" if i % 2 else "Dr Bo",
              "start": iso(now + timedelta(days=1, hours=i)),
              "end": iso(now + timedelta(days=1, hours=i, minutes=30))} for i in range(1, 9)]
    slots.append({"id": "past", "provider": "Dr Ade", "start": iso(now - timedelta(hours=2)),
                  "end": iso(now - timedelta(hours=1))})
    assert c.post("/reset", json={"slots": slots}, headers=H).status_code == 200


def book(slot="s1", email="ada@example.com", name="Ada", **kw):
    return c.post("/bookings", json={"slot_id": slot, "name": name, "email": email}, headers={**H, **kw})


def test_level1_flow():
    fresh()
    s = c.get("/slots", headers=H).json()["slots"]
    assert s[0]["id"] == "past" and all("available" in x for x in s)
    assert [x["id"] for x in c.get("/slots?provider=Dr Bo", headers=H).json()["slots"]] == ["s2", "s4", "s6", "s8"]
    r = book()
    assert r.status_code == 201 and r.json()["status"] == "confirmed"
    assert c.get(f"/bookings/{r.json()['id']}", headers=H).json() == r.json()
    assert c.get("/bookings/nope", headers=H).status_code == 404
    assert {x["id"]: x["available"] for x in c.get("/slots", headers=H).json()["slots"]}["s1"] is False


def test_level2_auth_and_validation():
    fresh()
    assert c.get("/slots").status_code == 401
    assert c.get("/slots", headers={"X-API-Key": "bad"}).json() == {"error": "missing or invalid API key"}
    for body in [{}, {"slot_id": "s1", "name": "  ", "email": "a@b.co"}, {"slot_id": "s1", "name": "A", "email": "nope"},
                 {"slot_id": "", "name": "A", "email": "a@b.co"}, {"slot_id": 5, "name": "A", "email": "a@b.co"}]:
        assert c.post("/bookings", json=body, headers=H).status_code == 400, body
    assert book("zzz", email="x1@b.co").status_code == 404
    assert book("past", email="x2@b.co").status_code == 400
    r = book("s2", email="  MiXed@Example.COM ", name="  Bob  ")
    assert r.json()["email"] == "mixed@example.com" and r.json()["name"] == "Bob"
    assert c.post("/bookings", data="not json", headers=H).status_code == 400


def test_level3_conflict_cancel_and_race():
    fresh()
    a = book("s3", email="a@b.co")
    assert book("s3", email="c@b.co").status_code == 409
    d = c.delete(f"/bookings/{a.json()['id']}", headers=H)
    assert d.status_code == 200 and d.json()["status"] == "cancelled"
    assert c.delete(f"/bookings/{a.json()['id']}", headers=H).status_code == 409
    assert c.delete("/bookings/nope", headers=H).status_code == 404
    assert book("s3", email="c@b.co").status_code == 201      # slot freed
    fresh()
    with ThreadPoolExecutor(20) as ex:
        codes = list(ex.map(lambda i: book("s4", email=f"p{i}@b.co").status_code, range(20)))
    assert codes.count(201) == 1 and codes.count(409) == 19, codes


def test_level4_idempotency_and_pagination():
    fresh()
    r1 = book("s1", email="i@b.co", **{"Idempotency-Key": "K1"})
    r2 = book("s1", email="i@b.co", **{"Idempotency-Key": "K1"})
    assert r1.status_code == r2.status_code == 201 and r1.json() == r2.json()
    assert book("s2", email="i@b.co", **{"Idempotency-Key": "K1"}).status_code == 422
    # replayed 409 stays 409
    book("s5", email="z@b.co")
    e1 = book("s5", email="i@b.co", **{"Idempotency-Key": "K2"})
    assert e1.status_code == 409 and book("s5", email="i@b.co", **{"Idempotency-Key": "K2"}).status_code == 409
    fresh()
    ids = [book(f"s{i}", email="Pg@b.co").json()["id"] for i in range(1, 6)]
    seen, cur = [], None
    while True:
        q = "/bookings?email=PG@b.co&limit=2" + (f"&cursor={cur}" if cur else "")
        j = c.get(q, headers=H).json()
        seen += [b["id"] for b in j["bookings"]]
        cur = j["next_cursor"]
        if not cur:
            break
    assert seen == ids
    for bad in ["limit=0", "limit=51", "limit=abc"]:
        assert c.get(f"/bookings?email=a@b.co&{bad}", headers=H).status_code == 400
    assert c.get("/bookings?email=nobody@b.co", headers=H).json() == {"bookings": [], "next_cursor": None}


def test_level5_rate_limit():
    fresh()
    for i in range(5):
        assert c.post("/bookings", json={"slot_id": "nope", "name": "A", "email": "rl@b.co"}, headers=H).status_code == 404
    r = book("s1", email="RL@b.co")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) in range(1, 61)
    assert book("s1", email="other@b.co").status_code == 201
