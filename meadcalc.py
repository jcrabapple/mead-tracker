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
HONEY_GAL_PER_LB = 0.0857   # volume 1 lb of honey adds (~12 lb/gal)

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

        if sugar_lb and not added:
            added = round(sugar_lb * HONEY_GAL_PER_LB, 4)

        if added > 0 and v1:
            # Addition: dilutes existing alcohol, starts a new segment
            v2 = v1 + added
            if g_after_measured is not None:
                g_after = g_after_measured
            elif g_before is not None:
                gu = (g_before - 1.0) * 1000.0 * v1 + sugar_lb * HONEY_PPG
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
