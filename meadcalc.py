"""Fermentation math for batches whose volume and sugar change over time.

The plain ``(OG - G) * 131.25`` formula assumes one fermentation at a fixed
volume. Once you dilute, backsweeten, or feed a batch, that stops being true.
This module replays a batch's gravity readings and process events in
chronological order and tracks:

* carry_abv   alcohol already present at the start of the current segment
* seg_start   the gravity the current segment started from
* volume_gal  current liquid volume

ABV at any gravity ``g`` inside a segment is ``carry_abv + (seg_start - g) * 131.25``.

Additions (water, honey, fruit juice) dilute the alcohol already present by
V1/V2 and start a new segment at the post-addition gravity. Removals (racking
losses, samples) change the volume but not the concentration.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

ABV_FACTOR = 131.25
HONEY_PPG = 35.0            # gravity points per lb per gallon
HONEY_GAL_PER_LB = 0.0857   # volume 1 lb of honey adds (~11.7 lb/gal)

# Sweeteners: gravity points per lb per gallon, gallons of volume per lb.
# Maple is ~66% sugar vs honey ~82%; syrup density ~1.32 (11 lb/gal).
SWEETENERS = {
    "honey": {"label": "Honey", "ppg": 35.0, "gal_per_lb": HONEY_GAL_PER_LB},
    "maple": {"label": "Maple syrup", "ppg": 30.0, "gal_per_lb": 1 / 11.0},
    "sucrose": {"label": "Table sugar", "ppg": 46.0, "gal_per_lb": 0.0755},
    "dextrose": {"label": "Corn sugar (dextrose)", "ppg": 42.0, "gal_per_lb": 0.077},
}
FL_OZ_PER_GAL = 128.0
FERMAID_O_G_PER_TSP = 2.48  # level tsp, meadmaking.wiki conversion table

# TOSNA 2.0 nitrogen factors and per-strain demand (Lallemand datasheets).
N_FACTORS = {"low": 0.75, "medium": 0.90, "high": 1.25}
YEAST_N = [
    ("71b", "low"), ("ec-1118", "low"), ("ec1118", "low"), ("d47", "low"),
    ("qa23", "low"), ("k1", "medium"), ("d254", "medium"), ("m05", "medium"),
    ("premier blanc", "low"), ("dv10", "low"), ("bm45", "high"), ("bm 4x4", "high"),
    ("rc212", "medium"), ("cy3079", "high"),
]

EVENT_TYPES = {
    # type: (label, affects_math)
    "dilute": ("Dilute (add water)", True),
    "backsweeten": ("Backsweeten", True),
    "feed": ("Step-feed (add sugar)", True),
    "add_fruit": ("Add fruit / juice", True),
    "rack": ("Rack / transfer", True),
    "volume_check": ("Measure volume", True),
    "stabilize": ("Stabilize (sorbate/k-meta)", False),
    "remove_fruit": ("Remove fruit / strain", False),
    "add_spice": ("Add spice / tincture", False),
    "add_oak": ("Add oak", False),
    "degas": ("Degas", False),
    "cold_crash": ("Cold crash", False),
    "fining": ("Fining / clarifier", False),
    "note": ("Note", False),
}


def yeast_n_level(strain: str | None) -> str:
    s = (strain or "").lower()
    for key, level in YEAST_N:
        if key in s:
            return level
    return "medium"


def brix(sg: float) -> float:
    """Specific gravity to degrees Brix (standard cubic fit)."""
    return ((182.4601 * sg - 775.6821) * sg + 1262.7794) * sg - 669.5622


def sugar_break(og: float, fraction: float = 1 / 3) -> float:
    """Gravity at which `fraction` of the sugar has fermented (TOSNA 1/3 break)."""
    return round(og - (og - 1.0) * fraction, 3)


def calc_tosna(og: float, volume_gal: float, n_level: str = "medium",
               g_per_tsp: float = FERMAID_O_G_PER_TSP) -> dict:
    """TOSNA 2.0: total Fermaid O (g) = Brix * 10 * N / 50 * gallons, in 4 doses
    at 24 h, 48 h, 72 h and the 1/3 sugar break. Go-Ferm at rehydration."""
    if n_level not in N_FACTORS:
        raise ValueError("n_level must be low, medium, or high")
    if not (1.0 < og < 1.250) or volume_gal <= 0:
        raise ValueError("need OG between 1.000 and 1.250 and a positive volume")
    bx = brix(og)
    target_yan = bx * 10 * N_FACTORS[n_level]          # ppm
    total_g = target_yan / 50 * volume_gal
    dose_g = total_g / 4
    yeast_g = max(1.0, 2.0 * volume_gal)               # ~2 g dry yeast per gallon
    return {
        "brix": round(bx, 1),
        "n_level": n_level,
        "target_yan_ppm": round(target_yan),
        "total_g": round(total_g, 2),
        "total_tsp": round(total_g / g_per_tsp, 2),
        "dose_g": round(dose_g, 2),
        "dose_tsp": round(dose_g / g_per_tsp, 2),
        "sugar_break": sugar_break(og),
        "potential_abv": round((og - 0.996) * ABV_FACTOR, 1),
        "yeast_g": round(yeast_g, 1),
        "goferm_g": round(yeast_g * 1.25, 1),
        "schedule": [
            {"when": "24 hours after pitch", "day": 1},
            {"when": "48 hours after pitch", "day": 2},
            {"when": "72 hours after pitch", "day": 3},
            {"when": f"1/3 sugar break (SG {sugar_break(og):.3f}) or day 7, whichever first", "day": 7},
        ],
    }


def calc_dilution(volume_gal: float, abv: float, target_abv: float | None = None,
                  water_gal: float | None = None, gravity: float | None = None) -> dict:
    """Water needed to reach a target ABV, or the ABV after adding water."""
    if volume_gal <= 0 or abv <= 0:
        raise ValueError("need a positive volume and ABV")
    if water_gal is None:
        if target_abv is None or not (0 < target_abv < abv):
            raise ValueError("target ABV must be above 0 and below the current ABV")
        water_gal = volume_gal * (abv / target_abv - 1)
    v2 = volume_gal + water_gal
    out = {
        "water_gal": round(water_gal, 3),
        "water_qt": round(water_gal * 4, 2),
        "water_cups": round(water_gal * 16, 1),
        "final_volume_gal": round(v2, 3),
        "final_abv": round(abv * volume_gal / v2, 2),
        "gravity_after": None,
    }
    if gravity is not None:
        out["gravity_after"] = round(1 + (gravity - 1) * volume_gal / v2, 4)
    return out


def calc_backsweeten(volume_gal: float, current_sg: float, target_sg: float,
                     sweetener: str = "honey", abv: float | None = None) -> dict:
    """Sweetener needed to raise a batch from current_sg to target_sg.

    Accounts for the volume the sweetener itself adds (which dilutes the
    points it contributes and the alcohol already present)."""
    sw = SWEETENERS.get(sweetener)
    if not sw:
        raise ValueError(f"sweetener must be one of {', '.join(SWEETENERS)}")
    if volume_gal <= 0 or target_sg <= current_sg:
        raise ValueError("target gravity must be above the current gravity")
    pn = (current_sg - 1) * 1000
    pt = (target_sg - 1) * 1000
    denom = sw["ppg"] - pt * sw["gal_per_lb"]
    if denom <= 0:
        raise ValueError("target gravity is higher than this sweetener can reach")
    lb = volume_gal * (pt - pn) / denom
    added_gal = lb * sw["gal_per_lb"]
    v2 = volume_gal + added_gal
    return {
        "sweetener": sweetener,
        "label": sw["label"],
        "lb": round(lb, 3),
        "oz_weight": round(lb * 16, 1),
        "grams": round(lb * 453.6),
        "fl_oz": round(added_gal * FL_OZ_PER_GAL, 1),
        "points_per_2oz_per_gal": round(sw["ppg"] / 8, 1),
        "final_volume_gal": round(v2, 3),
        "final_abv": round(abv * volume_gal / v2, 2) if abv else None,
    }


def calc_honey_for_og(volume_gal: float, target_og: float, sweetener: str = "honey") -> dict:
    """Honey (or other sugar) for a target OG at a final must volume."""
    sw = SWEETENERS.get(sweetener)
    if not sw:
        raise ValueError(f"sweetener must be one of {', '.join(SWEETENERS)}")
    if volume_gal <= 0 or not (1.0 < target_og < 1.250):
        raise ValueError("need a positive volume and OG between 1.000 and 1.250")
    lb = (target_og - 1) * 1000 * volume_gal / sw["ppg"]
    water_gal = volume_gal - lb * sw["gal_per_lb"]
    return {
        "lb": round(lb, 2),
        "oz_weight": round(lb * 16, 1),
        "water_gal": round(water_gal, 3),
        "water_qt": round(water_gal * 4, 2),
        "potential_abv": round((target_og - 0.996) * ABV_FACTOR, 1),
        "brix": round(brix(target_og), 1),
    }


def event_label(event_type: str) -> str:
    return EVENT_TYPES.get(event_type, (event_type, False))[0]


def _f(v):
    """Coerce to float or None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def simple_abv(og, g):
    og, g = _f(og), _f(g)
    if og and g and og > g:
        return round((og - g) * ABV_FACTOR, 1)
    return None


@dataclass
class State:
    og: float | None
    carry_abv: float = 0.0
    seg_start: float | None = None
    volume_gal: float | None = None
    last_gravity: float | None = None
    stabilized: bool = False
    stabilized_date: str | None = None
    diluted: bool = False
    segments: int = 1
    notes: list = field(default_factory=list)

    def abv_at(self, g):
        g = _f(g)
        if g is None or self.seg_start is None:
            return None
        fermented = max(0.0, self.seg_start - g) * ABV_FACTOR
        return round(self.carry_abv + fermented, 2)


def _sort_key(item):
    kind, row = item
    d = row.get("reading_date") if kind == "reading" else row.get("event_date")
    # created_at breaks same-day ties in the order things were logged
    return (d or "", row.get("created_at") or "", 0 if kind == "reading" else 1, row.get("id") or 0)


def compute(batch: dict, readings: list[dict], events: list[dict]) -> dict:
    """Replay a batch and return per-row results plus the current state.

    Returns a dict with:
      readings: list of {id, abv, segment}
      events:   list of {id, abv_before, abv_after, gravity_before, gravity_after,
                         volume_before, volume_after, estimated_gravity}
      state:    final State as dict, plus derived fields
    """
    og = _f(batch.get("og"))
    vol0 = _f(batch.get("initial_volume_gal")) or _f(batch.get("batch_size_gal"))
    st = State(og=og, seg_start=og, volume_gal=vol0, last_gravity=og)

    timeline = [("reading", dict(r)) for r in readings] + [("event", dict(e)) for e in events]
    timeline.sort(key=_sort_key)

    reading_out, event_out = {}, {}

    for kind, row in timeline:
        if kind == "reading":
            g = _f(row.get("gravity"))
            if st.seg_start is None and g is not None:
                # No OG on the batch: first reading acts as OG
                st.seg_start = g
                st.og = st.og or g
            st.last_gravity = g
            reading_out[row["id"]] = {"abv": st.abv_at(g), "segment": st.segments}
            continue

        et = row.get("event_type")
        g_before = _f(row.get("gravity_before")) or st.last_gravity
        g_after_measured = _f(row.get("gravity_after"))
        v1 = st.volume_gal
        added = _f(row.get("volume_added_gal")) or 0.0
        sugar_lb = _f(row.get("sugar_lb")) or 0.0
        vol_after = _f(row.get("volume_after_gal"))
        abv_before = st.abv_at(g_before)
        out = {
            "abv_before": abv_before,
            "gravity_before": g_before,
            "volume_before": v1,
            "estimated_gravity": False,
        }

        if et == "stabilize":
            st.stabilized = True
            st.stabilized_date = row.get("event_date")

        sw = SWEETENERS.get(row.get("sweetener") or "honey", SWEETENERS["honey"])
        if sugar_lb and not added:
            added = round(sugar_lb * sw["gal_per_lb"], 4)

        if added > 0 and v1:
            # Addition: dilutes existing alcohol, starts a new segment
            v2 = v1 + added
            if g_after_measured is not None:
                g_after = g_after_measured
            elif g_before is not None:
                gu = (g_before - 1.0) * 1000.0 * v1 + sugar_lb * sw["ppg"]
                g_after = round(1.0 + gu / (1000.0 * v2), 4)
                out["estimated_gravity"] = True
            else:
                g_after = None
            st.carry_abv = (abv_before or 0.0) * v1 / v2
            st.seg_start = g_after
            st.last_gravity = g_after
            st.volume_gal = v2
            st.segments += 1
            if et == "dilute":
                st.diluted = True
        elif g_after_measured is not None and et in ("backsweeten", "feed", "add_fruit"):
            # Sugar added but volume change not recorded: treat as negligible
            st.carry_abv = abv_before or 0.0
            st.seg_start = g_after_measured
            st.last_gravity = g_after_measured
            st.segments += 1
        elif vol_after is not None:
            # Removal / measurement: concentration unchanged
            st.volume_gal = vol_after
            if g_after_measured is not None:
                st.last_gravity = g_after_measured

        if vol_after is not None and added > 0:
            # Explicit measured volume after an addition wins over the estimate
            st.volume_gal = vol_after

        out.update({
            "abv_after": st.abv_at(st.last_gravity),
            "gravity_after": st.last_gravity,
            "volume_after": st.volume_gal,
        })
        event_out[row["id"]] = out

    # Final gravity: batch.fg if recorded, else latest gravity
    fg = _f(batch.get("fg"))
    current_g = st.last_gravity
    abv_now = st.abv_at(fg if fg is not None else current_g)

    # Equivalent OG at the current volume (what OG would have produced this ABV
    # in a single fermentation to the current gravity). Useful after dilution.
    equiv_og = None
    if abv_now is not None and current_g is not None and st.segments > 1:
        equiv_og = round(current_g + abv_now / ABV_FACTOR, 3)

    # Potential ABV if the current segment ferments dry (0.996), unless stabilized
    potential = None
    if st.seg_start is not None:
        dry = 0.996
        if st.stabilized:
            potential = round(abv_now, 1) if abv_now is not None else None
        else:
            potential = round(st.carry_abv + max(0.0, st.seg_start - dry) * ABV_FACTOR, 1)

    state = asdict(st)
    state.update({
        "current_gravity": current_g,
        "abv_now": round(abv_now, 1) if abv_now is not None else None,
        "potential_abv": potential,
        "equivalent_og": equiv_og,
        "volume_gal": round(st.volume_gal, 3) if st.volume_gal else st.volume_gal,
        "carry_abv": round(st.carry_abv, 2),
    })
    return {"readings": reading_out, "events": event_out, "state": state}
