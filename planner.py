"""Next-action planning: stored tasks plus rules derived from batch state.

Stored tasks (the `tasks` table) are things with a date: nutrient doses,
"strain the fruit by Oct 11". Derived actions are computed on every page
load from readings/events/bottles, so they can never go stale: "no reading
in 9 days", "gravity stable for 4 days", "backsweetened but not stabilized".
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import meadcalc

TASK_KINDS = {
    "nutrient": "Nutrient",
    "reading": "Gravity reading",
    "process": "Process step",
    "check": "Check",
    "other": "Other",
}

SEVERITY_ORDER = {"overdue": 0, "today": 1, "warning": 2, "soon": 3, "info": 4}


def _d(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except (TypeError, ValueError):
        return None


def _severity_for_due(due, today):
    if due is None:
        return "info"
    if due < today:
        return "overdue"
    if due == today:
        return "today"
    if (due - today).days <= 3:
        return "soon"
    return "info"


def _rel(due, today):
    if due is None:
        return ""
    n = (due - today).days
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    if n == -1:
        return "yesterday"
    return f"in {n} days" if n > 0 else f"{-n} days overdue"


def stable_gravity(readings, min_days=3, tolerance=0.001):
    """Return (gravity, days_stable) if the last readings agree over >= min_days."""
    rs = [(r["reading_date"], r["gravity"]) for r in readings if r["gravity"] is not None]
    if len(rs) < 2:
        return None
    last_date, last_g = _d(rs[-1][0]), rs[-1][1]
    first_stable = last_date
    for d, g in reversed(rs[:-1]):
        if abs(g - last_g) <= tolerance:
            first_stable = _d(d)
        else:
            break
    if first_stable and last_date and (last_date - first_stable).days >= min_days:
        return last_g, (last_date - first_stable).days
    return None


def derived_actions(batch, readings, events, calc, ingredients, bottles_remaining,
                    bottles_recorded, last_tasting, nutrients=(), today=None):
    """Rule-based suggestions for one batch. Pure: no DB access."""
    today = today or date.today()
    out = []
    status = batch["status"]
    pitch = _d(batch["pitch_date"])
    day = (today - pitch).days if pitch else None
    ev_types = {e["event_type"] for e in events}
    st = calc["state"]

    def add(title, detail="", severity="info", due=None, key=None):
        out.append({
            "batch_id": batch["id"], "batch_name": batch["name"], "title": title,
            "detail": detail, "severity": severity, "due": due.isoformat() if due else None,
            "due_rel": _rel(due, today), "source": "rule", "key": key or title,
        })

    if status == "active":
        # Gravity reading cadence
        last_r = _d(readings[-1]["reading_date"]) if readings else pitch
        if last_r:
            gap = (today - last_r).days
            if gap >= 7:
                add("Take a gravity reading", f"Last reading {gap} days ago.",
                    "warning" if gap >= 10 else "soon", key="reading-gap")

        # 1/3 sugar break: remind once gravity crosses it, until a nutrient
        # addition is logged on/after the crossing date.
        og = batch["og"]
        if og and st.get("segments", 1) == 1 and day is not None and day <= 14:
            brk = meadcalc.sugar_break(og)
            crossed = next((r for r in readings if r["gravity"] is not None and r["gravity"] <= brk), None)
            if crossed:
                fed = any((n["addition_date"] or "") >= crossed["reading_date"] for n in nutrients)
                if not fed:
                    add("1/3 sugar break reached",
                        f"Gravity hit {crossed['gravity']:.3f} (break is {brk:.3f}) on "
                        f"{crossed['reading_date']}. Last organic nutrient dose goes in now; "
                        "after this yeast can't use it.", "today", key="sugar-break")

        # Stable gravity => fermentation done
        stable = stable_gravity(readings)
        if stable:
            g, days = stable
            add("Fermentation looks finished",
                f"Gravity steady at {g:.3f} for {days} days. Rack off the lees, then stabilize "
                "before any backsweetening.", "soon", key="stable")

        # Fruit contact time
        has_fruit = any(i["category"] == "fruit" for i in ingredients)
        if has_fruit and "remove_fruit" not in ev_types and day is not None and day >= 7:
            due = pitch + timedelta(days=14)
            sev = "overdue" if day > 14 else ("today" if day == 14 else ("warning" if day >= 10 else "soon"))
            add("Remove fruit from primary",
                f"Day {day}. Fruit much past day 14 risks off-flavors; log a Remove fruit event when done.",
                sev, due=due, key="fruit")

    # Backsweetened / fed without stabilizing
    sweet_after_stab = False
    stabilized_seen = False
    for e in events:
        if e["event_type"] == "stabilize":
            stabilized_seen = True
        elif e["event_type"] == "backsweeten" and not stabilized_seen:
            sweet_after_stab = True
    if sweet_after_stab and status in ("active", "aging"):
        add("Backsweetened without stabilizing",
            "Sugar was added before a stabilize event. Unless you intend to referment, add "
            "k-meta + sorbate, or bottle-bomb risk is real.", "warning", key="unstable-sweet")

    if "stabilize" in ev_types and status in ("active", "aging"):
        stab = max((e for e in events if e["event_type"] == "stabilize"),
                   key=lambda e: e["event_date"])
        sd = _d(stab["event_date"])
        if sd and "backsweeten" not in {e["event_type"] for e in events if e["event_date"] >= stab["event_date"]}:
            due = sd + timedelta(days=2)
            if today >= due:
                add("Ready to backsweeten", "Stabilized 48+ hours ago. Use the backsweetening calculator "
                    "to size the honey.", "soon", due=due, key="ready-sweet")

    # Bottling bookkeeping
    if status in ("bottled", "drinking"):
        if not bottles_recorded:
            add("Record bottle count", "No bottles logged for this batch yet, so it isn't in the inventory.",
                "info", key="no-bottles")
        elif bottles_remaining == 0:
            add("All bottles gone", "Inventory is empty. Archive the batch?", "info", key="empty")

    # Periodic tastings while aging/bottled
    if status in ("aging", "bottled", "drinking"):
        lt = _d(last_tasting)
        ref = lt or _d(batch["bottled_date"]) or pitch
        if ref and (today - ref).days >= 90:
            add("Due for a tasting", f"Last tasting note {(today - ref).days} days ago." if lt
                else "No tasting notes yet.", "info", key="tasting")
    return out


def task_actions(tasks, batch_names, today=None):
    today = today or date.today()
    out = []
    for t in tasks:
        due = _d(t["due_date"])
        out.append({
            "batch_id": t["batch_id"], "batch_name": batch_names.get(t["batch_id"], ""),
            "title": t["title"], "detail": t["details"] or "", "kind": t["kind"],
            "severity": _severity_for_due(due, today), "due": t["due_date"],
            "due_rel": _rel(due, today), "source": "task", "task_id": t["id"], "key": f"task-{t['id']}",
        })
    return out


def sort_actions(actions):
    return sorted(actions, key=lambda a: (SEVERITY_ORDER.get(a["severity"], 9), a["due"] or "9999", a["batch_name"]))


def tosna_tasks(batch, n_level=None, g_per_tsp=meadcalc.FERMAID_O_G_PER_TSP):
    """Task rows for a TOSNA 2.0 schedule anchored on the pitch date."""
    pitch = _d(batch["pitch_date"])
    og = batch["og"]
    vol = batch["initial_volume_gal"] or batch["batch_size_gal"]
    if not (pitch and og and vol):
        raise ValueError("batch needs a pitch date, OG, and volume")
    n_level = n_level or meadcalc.yeast_n_level(batch["yeast_strain"])
    t = meadcalc.calc_tosna(og, vol, n_level, g_per_tsp)
    dose = f"{t['dose_g']} g Fermaid O (~{t['dose_tsp']} tsp)"
    rows = []
    for i, step in enumerate(t["schedule"], 1):
        rows.append({
            "due_date": (pitch + timedelta(days=step["day"])).isoformat(),
            "title": f"TOSNA dose {i}/4: {dose}",
            "details": f"{step['when']}. Degas gently first. Skip if gravity is already below "
                       f"{t['sugar_break']:.3f} and this is dose 4's window passing.",
            "kind": "nutrient",
            "auto_key": f"tosna-{i}",
        })
    return rows, t
