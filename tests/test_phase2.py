"""Phase 2: calculators, next actions, bottle inventory."""
from datetime import date, timedelta

import pytest

import meadcalc as mc
import planner
from test_app import API, basic, client  # noqa: F401  (fixture re-export)


# ── Calculator math ──────────────────────────────────────────────

def test_brix_known_points():
    assert mc.brix(1.000) == pytest.approx(0.0, abs=0.1)
    assert mc.brix(1.100) == pytest.approx(23.8, abs=0.2)


def test_tosna_one_gallon_medium_matches_published_example():
    # 1 gal at 1.100 (~23.8 Bx), medium N: 23.8*10*0.9/50 ≈ 4.3 g
    t = mc.calc_tosna(1.100, 1.0, "medium")
    assert t["total_g"] == pytest.approx(4.28, abs=0.05)
    assert t["dose_g"] == pytest.approx(t["total_g"] / 4, abs=0.01)
    assert len(t["schedule"]) == 4


def test_tosna_scales_with_volume_and_n():
    a = mc.calc_tosna(1.110, 1.0, "low")
    b = mc.calc_tosna(1.110, 0.5, "low")
    c = mc.calc_tosna(1.110, 1.0, "high")
    assert b["total_g"] == pytest.approx(a["total_g"] / 2, abs=0.01)
    assert c["total_g"] > a["total_g"]


def test_sugar_break():
    assert mc.sugar_break(1.110) == pytest.approx(1.073, abs=0.0005)


def test_yeast_levels():
    assert mc.yeast_n_level("Lalvin 71B-1122") == "low"
    assert mc.yeast_n_level("EC-1118") == "low"
    assert mc.yeast_n_level("K1-V1116") == "medium"
    assert mc.yeast_n_level("mystery yeast") == "medium"


def test_dilution_pumpkin_plan():
    # 0.625 gal at 14.4% + 1.6 qt water -> ~8.9%
    d = mc.calc_dilution(0.625, 14.44, water_gal=0.4)
    assert d["final_abv"] == pytest.approx(8.8, abs=0.1)
    t = mc.calc_dilution(0.625, 14.44, target_abv=8.9)
    assert t["water_qt"] == pytest.approx(1.56, abs=0.03)


def test_dilution_rejects_target_above_current():
    with pytest.raises(ValueError):
        mc.calc_dilution(1, 10, target_abv=12)


def test_backsweeten_honey_and_round_trip():
    r = mc.calc_backsweeten(1.0, 0.998, 1.013, "honey")
    # ~15 points in a gallon ≈ 7 oz honey
    assert 6.5 < r["oz_weight"] < 7.5
    # Round trip: mixing that honey back in lands on target
    lb, v2 = r["lb"], r["final_volume_gal"]
    pts = ((0.998 - 1) * 1000 * 1.0 + lb * 35) / v2
    assert 1 + pts / 1000 == pytest.approx(1.013, abs=0.0003)


def test_maple_needs_more_than_honey():
    h = mc.calc_backsweeten(1.0, 0.998, 1.013, "honey")
    m = mc.calc_backsweeten(1.0, 0.998, 1.013, "maple")
    assert m["lb"] > h["lb"]


def test_honey_for_og():
    r = mc.calc_honey_for_og(1.0, 1.105)
    assert r["lb"] == pytest.approx(3.0, abs=0.05)


def test_maple_backsweeten_event_uses_maple_ppg():
    b = {"og": 1.100, "fg": None, "batch_size_gal": 1.0, "initial_volume_gal": 1.0, "pitch_date": "2026-01-01"}
    rd = [{"id": 1, "reading_date": "2026-02-01", "gravity": 0.998, "created_at": "2026-02-01"}]
    base = {"id": 1, "event_date": "2026-02-02", "event_type": "backsweeten", "sugar_lb": 0.5,
            "volume_added_gal": None, "volume_after_gal": None, "gravity_before": None,
            "gravity_after": None, "created_at": "2026-02-02"}
    honey = mc.compute(b, rd, [dict(base, sweetener="honey")])["state"]
    maple = mc.compute(b, rd, [dict(base, sweetener="maple")])["state"]
    assert maple["current_gravity"] < honey["current_gravity"]


# ── Planner rules (pure) ─────────────────────────────────────────

def _batch(**kw):
    b = {"id": 1, "name": "T", "status": "active", "pitch_date": "2026-09-01", "og": 1.110,
         "bottled_date": None, "yeast_strain": "71B", "batch_size_gal": 1.0, "initial_volume_gal": 0.625}
    b.update(kw)
    return b


def _calc(b, readings, events=()):
    return mc.compute(b, readings, list(events))


def R(d, g):
    return {"id": d, "reading_date": d, "gravity": g, "created_at": d}


def keys(actions):
    return {a["key"] for a in actions}


def test_reading_gap_rule():
    b = _batch()
    rd = [R("2026-09-01", 1.110)]
    acts = planner.derived_actions(b, rd, [], _calc(b, rd), [], 0, False, None, today=date(2026, 9, 12))
    assert "reading-gap" in keys(acts)


def test_stable_gravity_rule():
    b = _batch()
    rd = [R("2026-09-01", 1.110), R("2026-09-20", 0.998), R("2026-09-22", 0.998), R("2026-09-24", 0.998)]
    acts = planner.derived_actions(b, rd, [], _calc(b, rd), [], 0, False, None, today=date(2026, 9, 25))
    assert "stable" in keys(acts)


def test_sugar_break_fires_then_clears_when_fed():
    b = _batch()
    rd = [R("2026-09-01", 1.110), R("2026-09-05", 1.070)]
    t = date(2026, 9, 5)
    assert "sugar-break" in keys(planner.derived_actions(b, rd, [], _calc(b, rd), [], 0, False, None, today=t))
    fed = [{"addition_date": "2026-09-05", "nutrient_type": "Fermaid O"}]
    assert "sugar-break" not in keys(planner.derived_actions(b, rd, [], _calc(b, rd), [], 0, False, None,
                                                            nutrients=fed, today=t))


def test_fruit_rule_until_removed():
    b = _batch()
    rd = [R("2026-09-01", 1.110)]
    fruit = [{"category": "fruit"}]
    t = date(2026, 9, 13)
    acts = planner.derived_actions(b, rd, [], _calc(b, rd), fruit, 0, False, None, today=t)
    fr = [a for a in acts if a["key"] == "fruit"][0]
    assert fr["due"] == "2026-09-15" and fr["severity"] == "warning"
    ev = [{"id": 1, "event_type": "remove_fruit", "event_date": "2026-09-12"}]
    assert "fruit" not in keys(planner.derived_actions(b, rd, ev, _calc(b, rd), fruit, 0, False, None, today=t))


def test_backsweeten_without_stabilize_warns():
    b = _batch(status="aging")
    ev = [{"id": 1, "event_type": "backsweeten", "event_date": "2026-10-01"}]
    acts = planner.derived_actions(b, [], ev, {"state": {}}, [], 0, False, None, today=date(2026, 10, 2))
    assert "unstable-sweet" in keys(acts)
    ev2 = [{"id": 0, "event_type": "stabilize", "event_date": "2026-09-29"}] + ev
    acts2 = planner.derived_actions(b, [], ev2, {"state": {}}, [], 0, False, None, today=date(2026, 10, 2))
    assert "unstable-sweet" not in keys(acts2)


def test_bottled_without_bottles_nags():
    b = _batch(status="bottled", bottled_date="2026-09-01")
    acts = planner.derived_actions(b, [], [], {"state": {}}, [], 0, False, "2026-09-10", today=date(2026, 9, 20))
    assert "no-bottles" in keys(acts)


def test_tosna_tasks_anchor_on_pitch():
    b = _batch()
    rows, t = planner.tosna_tasks(b)
    assert [r["due_date"] for r in rows] == ["2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08"]
    assert t["n_level"] == "low"
    assert "Fermaid O" in rows[0]["title"]


def test_sort_puts_overdue_first():
    a = [{"severity": "info", "due": None, "batch_name": "a"},
         {"severity": "overdue", "due": "2026-01-01", "batch_name": "b"}]
    assert planner.sort_actions(a)[0]["severity"] == "overdue"


# ── Web + API ────────────────────────────────────────────────────

def _mk(client, **kw):
    body = {"name": "Test", "og": 1.110, "pitch_date": (date.today() - timedelta(days=2)).isoformat(),
            "yeast_strain": "Lalvin 71B", "batch_size_gal": 1.0, "status": "active"}
    body.update(kw)
    r = client.post("/api/batches", json=body, headers=API)
    assert r.status_code == 201, r.json
    return r.json["id"]


def test_pages_render(client):
    bid = _mk(client)
    for p in ["/", "/tools", f"/tools?batch={bid}", f"/tools?batch={bid}&calc=tosna",
              "/tools?calc=dilution&volume_gal=0.625&abv=14.4&target_abv=8.9",
              "/tools?calc=backsweeten&volume_gal=1&current_sg=0.998&target_sg=1.013&sweetener=maple",
              "/tools?calc=honey&volume_gal=1&target_og=1.1", "/tools?calc=dilution&volume_gal=1&abv=5&target_abv=9",
              "/inventory", f"/batch/{bid}"]:
        r = client.get(p, headers=basic())
        assert r.status_code == 200, p


def test_calc_page_shows_error_not_500(client):
    r = client.get("/tools?calc=dilution&volume_gal=1&abv=5&target_abv=9", headers=basic())
    assert b"below the current ABV" in r.data


def test_api_calc(client):
    r = client.get("/api/calc/tosna?og=1.1&volume_gal=1&n_level=medium", headers=API)
    assert r.status_code == 200 and r.json["total_g"] == pytest.approx(4.28, abs=0.05)
    assert client.get("/api/calc/nope", headers=API).status_code == 404
    assert client.post("/api/calc/backsweeten", json={"volume_gal": 1, "current_sg": 1.0}, headers=API).status_code == 400


def test_api_calc_prefills_from_batch(client):
    bid = _mk(client)
    r = client.get(f"/api/calc/tosna?batch_id={bid}", headers=API)
    assert r.status_code == 200 and r.json["n_level"] == "low"


def test_tosna_tasks_idempotent_and_show_on_dashboard(client):
    bid = _mk(client)
    r1 = client.post(f"/api/batch/{bid}/tosna", json={}, headers=API)
    r2 = client.post(f"/api/batch/{bid}/tosna", json={}, headers=API)
    assert len(r1.json["created_task_ids"]) == 4
    assert r2.json["created_task_ids"] == []
    acts = client.get("/api/actions", headers=API).json
    assert sum(1 for a in acts if a["source"] == "task") >= 2   # days 1 and 2 are due/overdue
    assert b"TOSNA dose 1/4" in client.get("/", headers=basic()).data


def test_task_lifecycle_api_and_form(client):
    bid = _mk(client)
    t = client.post("/api/tasks", json={"batch_id": bid, "title": "Strain pumpkin",
                                        "due_date": date.today().isoformat(), "kind": "process"},
                    headers=API).json
    assert any(a.get("task_id") == t["id"] for a in client.get("/api/actions", headers=API).json)
    r = client.post(f"/task/{t['id']}/snooze", headers=basic())
    assert r.status_code == 302
    snoozed = client.get(f"/api/tasks?batch_id={bid}", headers=API).json[0]
    assert snoozed["due_date"] == (date.today() + timedelta(days=1)).isoformat()
    client.patch(f"/api/task/{t['id']}", json={"done": True}, headers=API)
    assert client.get(f"/api/tasks?batch_id={bid}", headers=API).json == []
    assert client.post("/api/tasks", json={"title": "x", "kind": "bogus"}, headers=API).status_code == 400
    assert client.post("/api/tasks", json={"title": "x", "due_date": "10/11"}, headers=API).status_code == 400


def test_bottling_flow_updates_status_and_inventory(client):
    bid = _mk(client, status="aging")
    r = client.post(f"/api/batch/{bid}/bottlings", json={"count": 5, "size_ml": 750, "location": "rack"},
                    headers=API)
    assert r.status_code == 201
    btid = r.json["created"]["id"]
    assert r.json["batch"]["status"] == "bottled"
    inv = client.get("/api/inventory", headers=API).json
    assert inv["total_bottles"] == 5 and inv["total_liters"] == 3.75
    u = client.post(f"/api/bottling/{btid}/use", json={"qty": 2, "reason": "gifted"}, headers=API)
    assert u.status_code == 201 and u.json["bottling"]["remaining"] == 3
    assert client.get(f"/api/batch/{bid}", headers=API).json["status"] == "drinking"
    # can't overdraw
    assert client.post(f"/api/bottling/{btid}/use", json={"qty": 4}, headers=API).status_code == 400
    assert client.post(f"/api/bottling/{btid}/use", json={"reason": "stolen"}, headers=API).status_code == 400
    # undo via delete puts it back
    client.delete(f"/api/bottlelog/{u.json['log_id']}", headers=API)
    assert client.get("/api/inventory", headers=API).json["total_bottles"] == 5
    # UI form path
    r = client.post(f"/bottling/{btid}/use", data={"reason": "drank", "next": "/inventory"}, headers=basic())
    assert r.status_code == 302 and r.headers["Location"].endswith("/inventory")
    assert b"4</strong> / 5" in client.get("/inventory", headers=basic()).data


def test_open_redirect_blocked(client):
    bid = _mk(client, status="aging")
    btid = client.post(f"/api/batch/{bid}/bottlings", json={"count": 2}, headers=API).json["created"]["id"]
    r = client.post(f"/bottling/{btid}/use", data={"next": "//evil.example/x"}, headers=basic())
    assert "evil" not in r.headers["Location"]


def test_due_tasks_notify_once(client, monkeypatch):
    import sqlite3
    from datetime import datetime
    import notifications
    import app as app_mod
    bid = _mk(client)
    client.post("/api/tasks", json={"batch_id": bid, "title": "Dose", "due_date": date.today().isoformat()},
                headers=API)
    sent = []
    monkeypatch.setattr(notifications, "_send_via_all_channels", lambda *a, **k: sent.append(a[4]))
    db = sqlite3.connect(app_mod.DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.combine(date.today(), datetime.min.time()).replace(hour=9)
    assert notifications._send_due_tasks(db, [], now=now) == 1
    assert notifications._send_due_tasks(db, [], now=now) == 0
    assert "Dose" in sent[0]
    assert notifications._send_due_tasks(db, [], now=now.replace(hour=6)) == 0
