import pytest

import meadcalc as mc


def B(**kw):
    base = {"og": 1.110, "fg": None, "batch_size_gal": 1.0, "initial_volume_gal": None}
    base.update(kw)
    return base


def R(i, d, g, created=""):
    return {"id": i, "reading_date": d, "gravity": g, "created_at": created}


def E(i, d, et, created="", **kw):
    return {"id": i, "event_date": d, "event_type": et, "created_at": created, **kw}


def test_no_events_matches_simple_formula():
    out = mc.compute(B(og=1.105), [R(1, "2026-07-18", 1.105), R(2, "2026-08-01", 0.998)], [])
    assert out["readings"][2]["abv"] == pytest.approx((1.105 - 0.998) * 131.25, abs=0.01)
    assert out["state"]["abv_now"] == 14.0
    assert out["state"]["segments"] == 1


def test_pumpkin_dilution_scales_abv_by_volume():
    # 0.625 gal at 1.110 ferments to 1.000, then 1.6 qt (0.4 gal) water added
    batch = B(initial_volume_gal=0.625)
    readings = [R(1, "2026-09-27", 1.110), R(2, "2026-10-10", 1.000)]
    events = [E(10, "2026-10-11", "dilute", volume_added_gal=0.4)]
    out = mc.compute(batch, readings, events)
    pre = (1.110 - 1.000) * 131.25          # 14.44%
    expected = pre * 0.625 / 1.025          # 8.80%
    ev = out["events"][10]
    assert ev["abv_before"] == pytest.approx(pre, abs=0.01)
    assert ev["abv_after"] == pytest.approx(expected, abs=0.02)
    assert ev["volume_after"] == pytest.approx(1.025)
    # Estimated gravity after mixing water in: points scale by V1/V2
    assert ev["gravity_after"] == pytest.approx(1.0, abs=0.0005)
    assert out["state"]["diluted"] is True
    assert out["state"]["abv_now"] == pytest.approx(expected, abs=0.05)


def test_reading_after_dilution_uses_new_segment():
    batch = B(initial_volume_gal=0.625)
    readings = [R(1, "2026-09-27", 1.110), R(2, "2026-10-10", 1.010),
                R(3, "2026-10-11", 1.006, created="z"), R(4, "2026-10-20", 0.998)]
    events = [E(10, "2026-10-11", "dilute", created="a", volume_added_gal=0.4, gravity_after=1.006)]
    out = mc.compute(batch, readings, events)
    carry = (1.110 - 1.010) * 131.25 * 0.625 / 1.025
    assert out["readings"][3]["abv"] == pytest.approx(carry, abs=0.02)
    assert out["readings"][4]["abv"] == pytest.approx(carry + (1.006 - 0.998) * 131.25, abs=0.02)


def test_simple_formula_would_be_wrong_after_dilution():
    batch = B(initial_volume_gal=0.625)
    out = mc.compute(batch, [R(1, "d1", 1.110), R(2, "d2", 1.000), R(3, "d4", 1.000, "z")],
                     [E(9, "d3", "dilute", volume_added_gal=0.4)])
    assert out["state"]["abv_now"] < 9.0
    assert mc.simple_abv(1.110, 1.000) > 14.0


def test_backsweeten_after_stabilize_locks_abv():
    readings = [R(1, "d1", 1.110), R(2, "d2", 1.000)]
    events = [
        E(1, "d3", "stabilize"),
        E(2, "d4", "backsweeten", sugar_lb=6 / 16, gravity_after=1.012),
    ]
    out = mc.compute(B(), readings, events)
    pre = (1.110 - 1.000) * 131.25
    v2 = 1.0 + (6 / 16) * mc.HONEY_GAL_PER_LB
    assert out["events"][2]["abv_after"] == pytest.approx(pre / v2, abs=0.02)
    assert out["state"]["stabilized"] is True
    # Potential ABV should not assume the backsweetening sugar ferments
    assert out["state"]["potential_abv"] == out["state"]["abv_now"]


def test_backsweeten_estimates_gravity_when_not_measured():
    out = mc.compute(B(), [R(1, "d1", 1.110), R(2, "d2", 1.000)],
                     [E(3, "d3", "backsweeten", sugar_lb=0.125)])
    ev = out["events"][3]
    assert ev["estimated_gravity"] is True
    # 2 oz honey raises a gallon ~4.4 points
    assert 1.0035 < ev["gravity_after"] < 1.0050


def test_step_feed_without_stabilizing_keeps_fermenting():
    out = mc.compute(B(og=1.100), [R(1, "d1", 1.100), R(2, "d2", 1.000)],
                     [E(3, "d3", "feed", sugar_lb=0.5, gravity_after=1.016)])
    st = out["state"]
    assert st["stabilized"] is False
    assert st["potential_abv"] > st["abv_now"]


def test_rack_changes_volume_not_abv():
    out = mc.compute(B(), [R(1, "d1", 1.110), R(2, "d2", 1.010)],
                     [E(3, "d3", "rack", volume_after_gal=0.85)])
    ev = out["events"][3]
    assert ev["abv_before"] == ev["abv_after"]
    assert out["state"]["volume_gal"] == 0.85


def test_non_math_events_do_not_change_anything():
    out = mc.compute(B(), [R(1, "d1", 1.110), R(2, "d2", 1.020)],
                     [E(3, "d3", "add_spice", amount="2 tsp tincture"), E(4, "d3", "note")])
    assert out["state"]["segments"] == 1
    assert out["state"]["abv_now"] == pytest.approx((1.110 - 1.020) * 131.25, abs=0.05)


def test_fg_on_batch_used_for_final_abv():
    out = mc.compute(B(og=1.105, fg=0.998), [R(1, "d1", 1.105)], [])
    assert out["state"]["abv_now"] == 14.0


def test_no_og_uses_first_reading():
    out = mc.compute(B(og=None), [R(1, "d1", 1.090), R(2, "d2", 1.000)], [])
    assert out["state"]["abv_now"] == pytest.approx(11.8, abs=0.05)


def test_dilute_without_volume_is_noop_for_math():
    # No volume recorded anywhere: can't compute the ratio, so leave ABV alone
    out = mc.compute(B(batch_size_gal=None), [R(1, "d1", 1.110), R(2, "d2", 1.000)],
                     [E(3, "d3", "dilute", volume_added_gal=0.4)])
    assert out["state"]["segments"] == 1
