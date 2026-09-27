import base64
import importlib
import os
import sys

import pytest
from werkzeug.security import generate_password_hash

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TOKEN = "test-token-123"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEAD_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("MEAD_USER", "jason")
    monkeypatch.setenv("MEAD_PASSWORD_HASH", generate_password_hash("hunter2"))
    monkeypatch.setenv("MEAD_API_TOKEN", TOKEN)
    monkeypatch.delenv("MEAD_AUTH_DISABLED", raising=False)
    sys.modules.pop("app", None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    assert app_mod.scheduler is None, "scheduler must not start on import"
    return app_mod.app.test_client()


def basic(user="jason", pw="hunter2"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


API = {"Authorization": f"Bearer {TOKEN}"}


# ── Auth ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/", "/settings", "/backup/download", "/export/json", "/batch/new"])
def test_ui_requires_basic_auth(client, path):
    r = client.get(path)
    assert r.status_code == 401
    assert "Basic" in r.headers["WWW-Authenticate"]


def test_wrong_password_rejected(client):
    assert client.get("/", headers=basic(pw="nope")).status_code == 401


def test_basic_auth_works(client):
    assert client.get("/", headers=basic()).status_code == 200


def test_backup_download_requires_auth(client):
    r = client.get("/backup/download")
    assert r.status_code == 401
    assert b"SQLite" not in r.data


def test_restore_requires_auth(client):
    assert client.post("/backup/restore").status_code == 401


def test_healthz_public(client):
    assert client.get("/healthz").status_code == 200


def test_api_requires_token(client):
    assert client.get("/api/batches").status_code == 401
    assert client.get("/api/batches", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/batches", headers=API).status_code == 200


def test_bearer_does_not_unlock_ui(client):
    assert client.get("/settings", headers=API).status_code == 401


def test_cross_origin_form_post_blocked(client):
    h = {**basic(), "Origin": "https://evil.example"}
    r = client.post("/batch/new", data={"name": "x"}, headers=h)
    assert r.status_code == 403
    h = {**basic(), "Origin": "http://localhost"}
    r = client.post("/batch/new", data={"name": "ok"}, headers=h)
    assert r.status_code == 302


def test_refuses_when_no_credentials_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("MEAD_DB_PATH", str(tmp_path / "t.db"))
    for k in ("MEAD_USER", "MEAD_PASSWORD_HASH", "MEAD_API_TOKEN", "MEAD_AUTH_DISABLED"):
        monkeypatch.delenv(k, raising=False)
    sys.modules.pop("app", None)
    c = importlib.import_module("app").app.test_client()
    assert c.get("/").status_code == 401
    assert c.get("/api/batches").status_code == 401
    assert c.get("/api/batches", headers={"Authorization": "Bearer "}).status_code == 401


# ── Write API ─────────────────────────────────────────────────────

def make_batch(client, **kw):
    body = {"name": "Pumpkin", "og": 1.110, "batch_size_gal": 1.0, "initial_volume_gal": 0.625,
            "status": "active", "pitch_date": "2026-09-27", **kw}
    r = client.post("/api/batches", json=body, headers=API)
    assert r.status_code == 201, r.json
    return r.json


def test_create_batch_and_validation(client):
    b = make_batch(client)
    assert b["id"] and b["state"]["volume_gal"] == 0.625
    assert client.post("/api/batches", json={}, headers=API).status_code == 400
    assert client.post("/api/batches", json={"name": "x", "status": "bogus"}, headers=API).status_code == 400
    assert client.post("/api/batches", json={"name": "x", "og": 2.5}, headers=API).status_code == 400
    assert client.post("/api/batches", data="notjson", headers=API).status_code == 400


def test_reading_auto_day_and_abv(client):
    b = make_batch(client)
    r = client.post(f"/api/batch/{b['id']}/readings",
                    json={"gravity": 1.050, "date": "2026-10-02", "notes": "day 5"}, headers=API)
    assert r.status_code == 201
    assert r.json["created"]["day_number"] == 5
    assert r.json["created"]["abv"] == pytest.approx(7.88, abs=0.01)
    assert r.json["batch"]["state"]["current_gravity"] == 1.050
    bad = client.post(f"/api/batch/{b['id']}/readings", json={"gravity": "abc"}, headers=API)
    assert bad.status_code == 400
    assert client.post("/api/batch/999/readings", json={"gravity": 1.0}, headers=API).status_code == 404


def test_dilution_event_via_api_recalculates(client):
    b = make_batch(client)
    bid = b["id"]
    client.post(f"/api/batch/{bid}/readings", json={"gravity": 1.110, "date": "2026-09-27"}, headers=API)
    client.post(f"/api/batch/{bid}/readings", json={"gravity": 1.000, "date": "2026-10-10"}, headers=API)
    r = client.post(f"/api/batch/{bid}/events",
                    json={"event_type": "dilute", "date": "2026-10-11", "volume_added_qt": 1.6}, headers=API)
    assert r.status_code == 201, r.json
    ev = r.json["created"]
    assert ev["volume_added_gal"] == pytest.approx(0.4)
    assert ev["day_number"] == 14
    assert ev["abv_after"] == pytest.approx(14.4375 * 0.625 / 1.025, abs=0.02)
    st = r.json["batch"]["state"]
    assert st["diluted"] is True and st["volume_gal"] == pytest.approx(1.025)
    full = client.get(f"/api/batch/{bid}", headers=API).json
    assert len(full["process_events"]) == 1
    assert full["gravity_readings"][-1]["abv"] == pytest.approx(14.4, abs=0.1)


def test_event_validation(client):
    b = make_batch(client)
    u = f"/api/batch/{b['id']}/events"
    assert client.post(u, json={"event_type": "explode"}, headers=API).status_code == 400
    assert client.post(u, json={"event_type": "dilute"}, headers=API).status_code == 400
    assert client.post(u, json={"event_type": "note", "date": "10/11/2026"}, headers=API).status_code == 400
    assert client.post(u, json={"event_type": "backsweeten", "gravity_after": 3}, headers=API).status_code == 400
    assert client.post(u, json={"event_type": "stabilize", "notes": "k-meta + sorbate"}, headers=API).status_code == 201


def test_nutrient_tasting_ingredient(client):
    b = make_batch(client)
    bid = b["id"]
    assert client.post(f"/api/batch/{bid}/nutrients",
                       json={"nutrient_type": "Fermaid O", "amount": "1/2 tsp", "date": "2026-09-28"},
                       headers=API).json["created"]["day_number"] == 1
    assert client.post(f"/api/batch/{bid}/nutrients", json={"amount": "x"}, headers=API).status_code == 400
    assert client.post(f"/api/batch/{bid}/tastings", json={"overall_rating": 11}, headers=API).status_code == 400
    assert client.post(f"/api/batch/{bid}/tastings", json={"overall_rating": 8, "flavor": "pie"},
                       headers=API).status_code == 201
    assert client.post(f"/api/batch/{bid}/ingredients", json={"name": "Honey", "category": "honey"},
                       headers=API).status_code == 201
    assert client.post(f"/api/batch/{bid}/ingredients", json={"name": "x", "category": "rocks"},
                       headers=API).status_code == 400


def test_patch_and_delete(client):
    b = make_batch(client)
    bid = b["id"]
    r = client.patch(f"/api/batch/{bid}", json={"status": "aging", "fg": 0.998}, headers=API)
    assert r.status_code == 200 and r.json["status"] == "aging"
    assert client.patch(f"/api/batch/{bid}", json={"id": 5}, headers=API).status_code == 400
    rid = client.post(f"/api/batch/{bid}/readings", json={"gravity": 1.02}, headers=API).json["created"]["id"]
    assert client.delete(f"/api/readings/{rid}", headers=API).status_code == 200
    assert client.delete(f"/api/readings/{rid}", headers=API).status_code == 404
    assert client.delete(f"/api/batches_table/{rid}", headers=API).status_code == 404
    assert client.delete(f"/api/batch/{bid}", headers=API).status_code == 400
    assert client.delete(f"/api/batch/{bid}?confirm=yes", headers=API).status_code == 200


def test_ui_event_form_and_pages_render(client):
    b = make_batch(client)
    bid = b["id"]
    client.post(f"/api/batch/{bid}/readings", json={"gravity": 1.110, "date": "2026-09-27"}, headers=API)
    client.post(f"/api/batch/{bid}/readings", json={"gravity": 1.000, "date": "2026-10-10"}, headers=API)
    r = client.post(f"/batch/{bid}/event", headers=basic(), data={
        "event_type": "dilute", "event_date": "2026-10-11", "volume_added": "1.6", "volume_unit": "qt",
        "sugar_oz": "", "gravity_before": "", "gravity_after": "", "volume_after_gal": "", "notes": "",
    })
    assert r.status_code == 302
    page = client.get(f"/batch/{bid}", headers=basic())
    assert page.status_code == 200
    html = page.data.decode()
    assert "Process Events" in html and "Dilute (add water)" in html
    assert "8.8%" in html                      # event-aware ABV shown
    assert client.get("/", headers=basic()).status_code == 200
    assert client.get("/export/csv", headers=basic()).status_code == 200
    assert client.get(f"/batch/{bid}/edit", headers=basic()).status_code == 200


def test_notification_dedupe_is_per_batch(tmp_path):
    import sqlite3
    import notifications
    db = sqlite3.connect(tmp_path / "n.db")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE notification_rules (id INTEGER PRIMARY KEY, batch_id INTEGER, event_type TEXT);
        CREATE TABLE notification_log (id INTEGER PRIMARY KEY, batch_id INTEGER, channel TEXT,
            event_type TEXT, subject TEXT, body TEXT, sent_at TEXT DEFAULT (datetime('now')),
            status TEXT DEFAULT 'sent', error TEXT);
        INSERT INTO notification_rules VALUES (1, 1, 'gravity_reminder'), (2, 2, 'gravity_reminder');
        INSERT INTO notification_log (batch_id, channel, event_type) VALUES (1, 'ntfy', 'gravity_reminder');
    """)
    assert notifications._already_notified_today(db, 1, "gravity_reminder") is True
    assert notifications._already_notified_today(db, 2, "gravity_reminder") is False


@pytest.mark.parametrize("path", ["/favicon.ico", "/favicon.svg", "/apple-touch-icon.png",
                                  "/static/favicon.svg", "/static/icon-192.png", "/manifest.webmanifest"])
def test_icons_public(client, path):
    r = client.get(path)
    assert r.status_code == 200 and len(r.data) > 100


def test_other_static_still_private(client):
    assert client.get("/static/nope.css").status_code == 401
