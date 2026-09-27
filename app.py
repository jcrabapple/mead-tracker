"""Mead Batch Tracker — lightweight Flask app for tracking homebrew mead batches."""

import os
import io
import hmac
import hashlib
import secrets
import csv
import json
import shutil
import sqlite3
import logging
from datetime import datetime, date
from contextlib import contextmanager

from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, flash, g, send_file, Response
)
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

from notifications import check_and_send_notifications, send_email, send_ntfy
import meadcalc
from meadcalc import EVENT_TYPES, event_label
import requests as http_requests

app = Flask(__name__)
app.secret_key = os.environ.get("MEAD_SECRET_KEY") or os.urandom(24)
app.jinja_env.globals.update(event_label=event_label, EVENT_TYPES=EVENT_TYPES)

DB_PATH = os.environ.get(
    "MEAD_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "mead.db"),
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Authentication ────────────────────────────────────────────────
#
# Browser UI: HTTP Basic auth. MEAD_USER + MEAD_PASSWORD_HASH (werkzeug hash).
# /api/*:     Authorization: Bearer $MEAD_API_TOKEN (Basic also accepted).
# If no credentials are configured the app refuses every request rather than
# silently running open. Set MEAD_AUTH_DISABLED=1 for local dev/tests only.

AUTH_USER = os.environ.get("MEAD_USER", "")
AUTH_PASS_HASH = os.environ.get("MEAD_PASSWORD_HASH", "")
API_TOKEN = os.environ.get("MEAD_API_TOKEN", "")
AUTH_DISABLED = os.environ.get("MEAD_AUTH_DISABLED") == "1"
PUBLIC_PATHS = {"/healthz"}


def _basic_ok(auth):
    if not auth or not AUTH_USER or not AUTH_PASS_HASH:
        return False
    user_ok = hmac.compare_digest(auth.username or "", AUTH_USER)
    pass_ok = check_password_hash(AUTH_PASS_HASH, auth.password or "")
    return user_ok and pass_ok


def _bearer_ok():
    header = request.headers.get("Authorization", "")
    if not API_TOKEN or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:].strip(), API_TOKEN)


def _same_origin_ok():
    """Basic-auth credentials are sent automatically by the browser, so a
    cross-site form POST would be authenticated. Reject state-changing
    browser requests whose Origin/Referer is a different host."""
    src = request.headers.get("Origin") or request.headers.get("Referer")
    if not src:
        return True  # curl, API clients, some privacy-stripped browsers
    from urllib.parse import urlparse
    return urlparse(src).netloc == request.host


@app.before_request
def require_auth():
    if AUTH_DISABLED or request.path in PUBLIC_PATHS:
        return None
    is_api = request.path.startswith("/api/")
    if is_api and _bearer_ok():
        return None
    if _basic_ok(request.authorization):
        if request.method not in ("GET", "HEAD", "OPTIONS") and not is_api and not _same_origin_ok():
            return Response("Cross-origin request blocked.", 403)
        return None
    if is_api:
        return jsonify({"error": "unauthorized"}), 401
    return Response(
        "Authentication required.", 401,
        {"WWW-Authenticate": 'Basic realm="Mead Tracker", charset="UTF-8"'},
    )


@app.route("/healthz")
def healthz():
    return {"ok": True}


# ── Database helpers ──────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they don't exist."""
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            style TEXT DEFAULT '',
            batch_size_gal REAL DEFAULT 1.0,
            honey_type TEXT DEFAULT '',
            yeast_strain TEXT DEFAULT '',
            og REAL,
            fg REAL,
            target_fg REAL,
            target_abv REAL,
            status TEXT DEFAULT 'planning'
                CHECK(status IN ('planning','active','aging','bottled','drinking','archived')),
            pitch_date TEXT,
            bottled_date TEXT,
            notes TEXT DEFAULT '',
            recipe_source TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS gravity_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
            reading_date TEXT NOT NULL,
            day_number INTEGER,
            gravity REAL NOT NULL,
            temperature_f REAL,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS nutrient_additions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
            addition_date TEXT NOT NULL,
            day_number INTEGER,
            nutrient_type TEXT NOT NULL,
            amount TEXT NOT NULL,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS tasting_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
            tasting_date TEXT NOT NULL,
            aroma TEXT DEFAULT '',
            flavor TEXT DEFAULT '',
            body TEXT DEFAULT '',
            sweetness TEXT DEFAULT '',
            overall_rating INTEGER CHECK(overall_rating BETWEEN 1 AND 10),
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS ingredients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
            category TEXT DEFAULT 'other'
                CHECK(category IN ('honey','fruit','spice','nutrient','yeast','other')),
            name TEXT NOT NULL,
            amount TEXT DEFAULT '',
            notes TEXT DEFAULT ''
        );

        -- Notification system tables
        CREATE TABLE IF NOT EXISTS notification_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL CHECK(channel IN ('email', 'ntfy')),
            enabled INTEGER DEFAULT 0,
            config TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS notification_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER REFERENCES batches(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL CHECK(event_type IN ('nutrient_reminder', 'gravity_reminder', 'aging_milestone')),
            enabled INTEGER DEFAULT 1,
            lead_time_days INTEGER DEFAULT 0,
            interval_days INTEGER DEFAULT 0,
            last_notified TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS notification_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER,
            channel TEXT NOT NULL,
            event_type TEXT NOT NULL,
            subject TEXT,
            body TEXT,
            sent_at TEXT DEFAULT (datetime('now')),
            status TEXT DEFAULT 'sent',
            error TEXT
        );
        -- Process events: dilution, backsweetening, racking, stabilizing...
        CREATE TABLE IF NOT EXISTS process_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
            event_date TEXT NOT NULL,
            day_number INTEGER,
            event_type TEXT NOT NULL,
            volume_added_gal REAL,
            sugar_lb REAL,
            volume_after_gal REAL,
            gravity_before REAL,
            gravity_after REAL,
            amount TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_events_batch ON process_events(batch_id, event_date);
        CREATE INDEX IF NOT EXISTS idx_readings_batch ON gravity_readings(batch_id, reading_date);

        -- AI recipe generation settings
        CREATE TABLE IF NOT EXISTS ai_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            value TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        );
    """)
    # Column migrations (SQLite has no ADD COLUMN IF NOT EXISTS)
    cols = {row[1] for row in db.execute("PRAGMA table_info(batches)")}
    if "initial_volume_gal" not in cols:
        db.execute("ALTER TABLE batches ADD COLUMN initial_volume_gal REAL")
    db.commit()
    db.close()


# ── Utility ───────────────────────────────────────────────────────

def _opt_float(v):
    if v is None or str(v).strip() == "":
        return None
    return float(v)


def calc_abv(og, gravity):
    """Estimate ABV from OG and current/final gravity."""
    if og and gravity and og > gravity:
        return round((og - gravity) * 131.25, 1)
    return None


def calc_potential_abv(og):
    """Estimate potential ABV if fermented to dry (FG ~0.996)."""
    if og:
        return round((og - 0.996) * 131.25, 1)
    return None


def day_number_for(db, batch_id, when):
    """Days since pitch for an ISO date, or None."""
    row = db.execute("SELECT pitch_date FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if not row or not row["pitch_date"] or not when:
        return None
    try:
        pitch = datetime.strptime(row["pitch_date"], "%Y-%m-%d").date()
        return (datetime.strptime(when, "%Y-%m-%d").date() - pitch).days
    except ValueError:
        return None


def batch_calc(db, batch):
    """Run the event-aware fermentation math for one batch."""
    readings = db.execute(
        "SELECT * FROM gravity_readings WHERE batch_id = ? ORDER BY reading_date, created_at, id",
        (batch["id"],),
    ).fetchall()
    events = db.execute(
        "SELECT * FROM process_events WHERE batch_id = ? ORDER BY event_date, created_at, id",
        (batch["id"],),
    ).fetchall()
    result = meadcalc.compute(dict(batch), [dict(r) for r in readings], [dict(e) for e in events])
    return readings, events, result


app.jinja_env.globals.update(calc_abv=calc_abv, calc_potential_abv=calc_potential_abv)


@app.template_filter("from_json")
def from_json_filter(value):
    """Jinja filter to parse JSON string into a Python object."""
    if not value:
        return {}
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}


@app.context_processor
def inject_today():
    return {"today": date.today().isoformat()}


# ── Dashboard ─────────────────────────────────────────────────────

@app.route("/")
def index():
    db = get_db()
    batches = db.execute(
        "SELECT * FROM batches ORDER BY "
        "CASE status "
        "  WHEN 'active' THEN 0 WHEN 'aging' THEN 1 WHEN 'planning' THEN 2 "
        "  WHEN 'bottled' THEN 3 WHEN 'drinking' THEN 4 WHEN 'archived' THEN 5 "
        "END, pitch_date DESC"
    ).fetchall()

    # Attach event-aware current state to each batch
    batch_data = []
    for b in batches:
        readings, events, calc = batch_calc(db, b)
        st = calc["state"]
        batch_data.append({
            **dict(b),
            "current_gravity": st["current_gravity"] if (readings or events) else None,
            "current_abv": st["abv_now"] if (readings or events or b["fg"]) else None,
            "volume_now": st["volume_gal"],
            "diluted": st["diluted"],
        })

    return render_template("index.html", batches=batch_data)


# ── Batch CRUD ────────────────────────────────────────────────────

@app.route("/batch/new", methods=["GET", "POST"])
def batch_new():
    if request.method == "POST":
        db = get_db()
        db.execute(
            """INSERT INTO batches
               (name, style, batch_size_gal, initial_volume_gal, honey_type, yeast_strain,
                og, target_fg, target_abv, status, pitch_date, notes, recipe_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request.form["name"],
                request.form.get("style", ""),
                float(request.form.get("batch_size_gal", 1.0) or 1.0),
                _opt_float(request.form.get("initial_volume_gal")),
                request.form.get("honey_type", ""),
                request.form.get("yeast_strain", ""),
                float(request.form["og"]) if request.form.get("og") else None,
                float(request.form["target_fg"]) if request.form.get("target_fg") else None,
                float(request.form["target_abv"]) if request.form.get("target_abv") else None,
                request.form.get("status", "planning"),
                request.form.get("pitch_date") or None,
                request.form.get("notes", ""),
                request.form.get("recipe_source", ""),
            ),
        )
        db.commit()
        flash("Batch created!", "success")
        return redirect(url_for("index"))
    return render_template("batch_form.html", batch=None)


@app.route("/batch/<int:batch_id>")
def batch_detail(batch_id):
    db = get_db()
    batch = db.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if not batch:
        flash("Batch not found.", "danger")
        return redirect(url_for("index"))

    readings, events, calc = batch_calc(db, batch)

    nutrients = db.execute(
        "SELECT * FROM nutrient_additions WHERE batch_id = ? ORDER BY addition_date",
        (batch_id,),
    ).fetchall()

    tastings = db.execute(
        "SELECT * FROM tasting_notes WHERE batch_id = ? ORDER BY tasting_date DESC",
        (batch_id,),
    ).fetchall()

    ingredients = db.execute(
        "SELECT * FROM ingredients WHERE batch_id = ? ORDER BY category, name",
        (batch_id,),
    ).fetchall()

    # Load notification rules for this batch
    notif_rules = db.execute(
        "SELECT * FROM notification_rules WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()
    notif_rules_dict = {r["event_type"]: dict(r) for r in notif_rules}

    # Build chart data (ABV is event-aware, so dilution shows as a step down)
    chart_labels = [r["reading_date"] for r in readings]
    chart_gravities = [r["gravity"] for r in readings]
    chart_abvs = [calc["readings"].get(r["id"], {}).get("abv") or 0 for r in readings]
    chart_events = [
        {"date": e["event_date"], "label": event_label(e["event_type"])}
        for e in events
    ]

    return render_template(
        "batch.html",
        batch=batch,
        readings=readings,
        events=events,
        calc=calc,
        chart_events=chart_events,
        nutrients=nutrients,
        tastings=tastings,
        ingredients=ingredients,
        chart_labels=chart_labels,
        chart_gravities=chart_gravities,
        chart_abvs=chart_abvs,
        notif_rules=notif_rules_dict,
    )


@app.route("/batch/<int:batch_id>/edit", methods=["GET", "POST"])
def batch_edit(batch_id):
    db = get_db()
    batch = db.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if not batch:
        flash("Batch not found.", "danger")
        return redirect(url_for("index"))

    if request.method == "POST":
        db.execute(
            """UPDATE batches SET
               name=?, style=?, batch_size_gal=?, initial_volume_gal=?, honey_type=?, yeast_strain=?,
               og=?, fg=?, target_fg=?, target_abv=?, status=?,
               pitch_date=?, bottled_date=?, notes=?, recipe_source=?,
               updated_at=datetime('now')
               WHERE id=?""",
            (
                request.form["name"],
                request.form.get("style", ""),
                float(request.form.get("batch_size_gal", 1.0) or 1.0),
                _opt_float(request.form.get("initial_volume_gal")),
                request.form.get("honey_type", ""),
                request.form.get("yeast_strain", ""),
                float(request.form["og"]) if request.form.get("og") else None,
                float(request.form["fg"]) if request.form.get("fg") else None,
                float(request.form["target_fg"]) if request.form.get("target_fg") else None,
                float(request.form["target_abv"]) if request.form.get("target_abv") else None,
                request.form.get("status", "planning"),
                request.form.get("pitch_date") or None,
                request.form.get("bottled_date") or None,
                request.form.get("notes", ""),
                request.form.get("recipe_source", ""),
                batch_id,
            ),
        )
        db.commit()
        flash("Batch updated!", "success")
        return redirect(url_for("batch_detail", batch_id=batch_id))

    return render_template("batch_form.html", batch=batch)


@app.route("/batch/<int:batch_id>/delete", methods=["POST"])
def batch_delete(batch_id):
    db = get_db()
    db.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
    db.commit()
    flash("Batch deleted.", "warning")
    return redirect(url_for("index"))


# ── Gravity readings ──────────────────────────────────────────────

@app.route("/batch/<int:batch_id>/gravity", methods=["POST"])
def add_gravity(batch_id):
    db = get_db()
    reading_date = request.form.get("reading_date") or date.today().isoformat()
    gravity = float(request.form["gravity"])
    batch = db.execute("SELECT og, pitch_date FROM batches WHERE id = ?", (batch_id,)).fetchone()

    # Auto-calculate day number from pitch date
    day_number = None
    if batch and batch["pitch_date"]:
        try:
            pitch = datetime.strptime(batch["pitch_date"], "%Y-%m-%d").date()
            reading = datetime.strptime(reading_date, "%Y-%m-%d").date()
            day_number = (reading - pitch).days
        except ValueError:
            pass

    if request.form.get("day_number"):
        day_number = int(request.form["day_number"])

    db.execute(
        """INSERT INTO gravity_readings (batch_id, reading_date, day_number, gravity, temperature_f, notes)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            batch_id,
            reading_date,
            day_number,
            gravity,
            float(request.form["temperature_f"]) if request.form.get("temperature_f") else None,
            request.form.get("notes", ""),
        ),
    )
    db.commit()
    flash(f"Gravity reading {gravity} logged!", "success")
    return redirect(url_for("batch_detail", batch_id=batch_id))


@app.route("/gravity/<int:reading_id>/delete", methods=["POST"])
def delete_gravity(reading_id):
    db = get_db()
    reading = db.execute("SELECT batch_id FROM gravity_readings WHERE id = ?", (reading_id,)).fetchone()
    if reading:
        db.execute("DELETE FROM gravity_readings WHERE id = ?", (reading_id,))
        db.commit()
        flash("Reading deleted.", "warning")
        return redirect(url_for("batch_detail", batch_id=reading["batch_id"]))
    return redirect(url_for("index"))


# ── Nutrient additions ────────────────────────────────────────────

@app.route("/batch/<int:batch_id>/nutrient", methods=["POST"])
def add_nutrient(batch_id):
    db = get_db()
    addition_date = request.form.get("addition_date") or date.today().isoformat()
    batch = db.execute("SELECT pitch_date FROM batches WHERE id = ?", (batch_id,)).fetchone()

    day_number = None
    if batch and batch["pitch_date"]:
        try:
            pitch = datetime.strptime(batch["pitch_date"], "%Y-%m-%d").date()
            add_date = datetime.strptime(addition_date, "%Y-%m-%d").date()
            day_number = (add_date - pitch).days
        except ValueError:
            pass

    if request.form.get("day_number"):
        day_number = int(request.form["day_number"])

    db.execute(
        """INSERT INTO nutrient_additions (batch_id, addition_date, day_number, nutrient_type, amount, notes)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            batch_id,
            addition_date,
            day_number,
            request.form["nutrient_type"],
            request.form["amount"],
            request.form.get("notes", ""),
        ),
    )
    db.commit()
    flash(f"Nutrient addition logged: {request.form['nutrient_type']}.", "success")
    return redirect(url_for("batch_detail", batch_id=batch_id))


# ── Tasting notes ─────────────────────────────────────────────────

@app.route("/batch/<int:batch_id>/tasting", methods=["POST"])
def add_tasting(batch_id):
    db = get_db()
    db.execute(
        """INSERT INTO tasting_notes
           (batch_id, tasting_date, aroma, flavor, body, sweetness, overall_rating, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            batch_id,
            request.form.get("tasting_date") or date.today().isoformat(),
            request.form.get("aroma", ""),
            request.form.get("flavor", ""),
            request.form.get("body", ""),
            request.form.get("sweetness", ""),
            int(request.form["overall_rating"]) if request.form.get("overall_rating") else None,
            request.form.get("notes", ""),
        ),
    )
    db.commit()
    flash("Tasting note added!", "success")
    return redirect(url_for("batch_detail", batch_id=batch_id))


# ── Ingredients ───────────────────────────────────────────────────

@app.route("/batch/<int:batch_id>/ingredient", methods=["POST"])
def add_ingredient(batch_id):
    db = get_db()
    db.execute(
        """INSERT INTO ingredients (batch_id, category, name, amount, notes)
           VALUES (?, ?, ?, ?, ?)""",
        (
            batch_id,
            request.form.get("category", "other"),
            request.form["name"],
            request.form.get("amount", ""),
            request.form.get("notes", ""),
        ),
    )
    db.commit()
    flash(f"Ingredient added: {request.form['name']}.", "success")
    return redirect(url_for("batch_detail", batch_id=batch_id))


# ── Process events ────────────────────────────────────────────────

def insert_event(db, batch_id, data):
    """Validate and insert a process event. Raises ValueError on bad input."""
    et = (data.get("event_type") or "").strip()
    if et not in EVENT_TYPES:
        raise ValueError(f"event_type must be one of: {', '.join(EVENT_TYPES)}")
    event_date = (data.get("event_date") or data.get("date") or date.today().isoformat()).strip()
    datetime.strptime(event_date, "%Y-%m-%d")
    nums = {}
    for k in ("volume_added_gal", "sugar_lb", "volume_after_gal", "gravity_before", "gravity_after"):
        nums[k] = _opt_float(data.get(k))
    for k in ("gravity_before", "gravity_after"):
        if nums[k] is not None and not (0.900 <= nums[k] <= 1.250):
            raise ValueError(f"{k} out of range (0.900-1.250)")
    for k in ("volume_added_gal", "sugar_lb", "volume_after_gal"):
        if nums[k] is not None and nums[k] < 0:
            raise ValueError(f"{k} cannot be negative")
    # Unit convenience: accept quarts/cups/oz and convert
    if nums["volume_added_gal"] is None:
        if _opt_float(data.get("volume_added_qt")) is not None:
            nums["volume_added_gal"] = float(data["volume_added_qt"]) / 4.0
        elif _opt_float(data.get("volume_added_cups")) is not None:
            nums["volume_added_gal"] = float(data["volume_added_cups"]) / 16.0
    if nums["sugar_lb"] is None and _opt_float(data.get("sugar_oz")) is not None:
        nums["sugar_lb"] = float(data["sugar_oz"]) / 16.0
    if et == "dilute" and not nums["volume_added_gal"]:
        raise ValueError("dilute requires volume_added_gal (or volume_added_qt / volume_added_cups)")
    day = data.get("day_number")
    day = int(day) if day not in (None, "") else day_number_for(db, batch_id, event_date)
    cur = db.execute(
        """INSERT INTO process_events
           (batch_id, event_date, day_number, event_type, volume_added_gal, sugar_lb,
            volume_after_gal, gravity_before, gravity_after, amount, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (batch_id, event_date, day, et, nums["volume_added_gal"], nums["sugar_lb"],
         nums["volume_after_gal"], nums["gravity_before"], nums["gravity_after"],
         str(data.get("amount") or ""), str(data.get("notes") or "")),
    )
    return cur.lastrowid


@app.route("/batch/<int:batch_id>/event", methods=["POST"])
def add_event(batch_id):
    db = get_db()
    form = request.form.to_dict()
    # The form offers volume in a unit dropdown
    unit = form.pop("volume_unit", "gal")
    if form.get("volume_added") not in (None, ""):
        form[{"gal": "volume_added_gal", "qt": "volume_added_qt", "cups": "volume_added_cups"}.get(unit, "volume_added_gal")] = form.pop("volume_added")
    if form.get("sugar_oz") in (None, ""):
        form.pop("sugar_oz", None)
    try:
        insert_event(db, batch_id, form)
        db.commit()
        flash(f"{event_label(form['event_type'])} logged.", "success")
    except (ValueError, KeyError) as e:
        flash(f"Could not log event: {e}", "danger")
    return redirect(url_for("batch_detail", batch_id=batch_id) + "#process")


@app.route("/event/<int:event_id>/delete", methods=["POST"])
def delete_event(event_id):
    db = get_db()
    row = db.execute("SELECT batch_id FROM process_events WHERE id = ?", (event_id,)).fetchone()
    if row:
        db.execute("DELETE FROM process_events WHERE id = ?", (event_id,))
        db.commit()
        flash("Event deleted.", "warning")
        return redirect(url_for("batch_detail", batch_id=row["batch_id"]) + "#process")
    return redirect(url_for("index"))


# ── Backup / Restore / Export ───────────────────────────────────

def _export_batch_dict(db, batch_id):
    """Build a full dict for one batch including all related data."""
    batch = db.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if not batch:
        return None
    data = dict(batch)
    data["gravity_readings"] = [
        dict(r) for r in db.execute(
            "SELECT * FROM gravity_readings WHERE batch_id = ? ORDER BY reading_date",
            (batch_id,),
        ).fetchall()
    ]
    data["nutrient_additions"] = [
        dict(n) for n in db.execute(
            "SELECT * FROM nutrient_additions WHERE batch_id = ? ORDER BY addition_date",
            (batch_id,),
        ).fetchall()
    ]
    data["ingredients"] = [
        dict(i) for i in db.execute(
            "SELECT * FROM ingredients WHERE batch_id = ? ORDER BY category, name",
            (batch_id,),
        ).fetchall()
    ]
    data["tasting_notes"] = [
        dict(t) for t in db.execute(
            "SELECT * FROM tasting_notes WHERE batch_id = ? ORDER BY tasting_date DESC",
            (batch_id,),
        ).fetchall()
    ]
    data["process_events"] = [
        dict(e) for e in db.execute(
            "SELECT * FROM process_events WHERE batch_id = ? ORDER BY event_date, created_at, id",
            (batch_id,),
        ).fetchall()
    ]
    return data


@app.route("/settings")
def settings():
    db = get_db()
    batch_count = db.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
    reading_count = db.execute("SELECT COUNT(*) FROM gravity_readings").fetchone()[0]

    # Load notification settings
    notif_settings = db.execute(
        "SELECT * FROM notification_settings"
    ).fetchall()
    notif_settings_dict = {s["channel"]: dict(s) for s in notif_settings}

    # Load recent notification log
    recent_log = db.execute(
        "SELECT * FROM notification_log ORDER BY sent_at DESC LIMIT 20"
    ).fetchall()

    # Load AI settings
    ai_config = {
        "api_base_url": get_ai_setting(db, "api_base_url"),
        "api_key": get_ai_setting(db, "api_key"),
        "model_name": get_ai_setting(db, "model_name"),
        "custom_system_prompt": get_ai_setting(db, "custom_system_prompt"),
    }

    return render_template("settings.html",
                           batch_count=batch_count,
                           reading_count=reading_count,
                           notif_settings=notif_settings_dict,
                           recent_log=recent_log,
                           ai_config=ai_config)


@app.route("/backup/download")
def backup_download():
    """Download a copy of the SQLite database."""
    if not os.path.exists(DB_PATH):
        flash("No database found.", "danger")
        return redirect(url_for("settings"))
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        DB_PATH,
        as_attachment=True,
        download_name=f"mead-tracker-backup_{timestamp}.db",
        mimetype="application/x-sqlite3",
    )


@app.route("/backup/restore", methods=["POST"])
def backup_restore():
    """Replace the current database with an uploaded .db file."""
    file = request.files.get("backup_file")
    if not file or file.filename == "":
        flash("No file selected.", "danger")
        return redirect(url_for("settings"))

    if not file.filename.endswith(".db"):
        flash("Only .db files are accepted.", "danger")
        return redirect(url_for("settings"))

    # Verify it's a valid SQLite database
    try:
        tmp_path = DB_PATH + ".tmp"
        file.save(tmp_path)
        test = sqlite3.connect(tmp_path)
        # Quick sanity: check that the batches table exists
        tables = {row[0] for row in test.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        test.close()
        if "batches" not in tables:
            os.remove(tmp_path)
            flash("Invalid backup: missing 'batches' table.", "danger")
            return redirect(url_for("settings"))
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        flash(f"Invalid SQLite file: {e}", "danger")
        return redirect(url_for("settings"))

    # Replace the database, then bring its schema up to date
    shutil.move(tmp_path, DB_PATH)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(DB_PATH + suffix):
            os.remove(DB_PATH + suffix)
    init_db()
    flash("Database restored successfully! Refresh to see your data.", "success")
    return redirect(url_for("index"))


@app.route("/export/json")
def export_json():
    """Export all batches with full data as JSON."""
    db = get_db()
    batch_ids = [row["id"] for row in db.execute("SELECT id FROM batches").fetchall()]
    data = [_export_batch_dict(db, bid) for bid in batch_ids]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        json.dumps(data, indent=2, default=str),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename=mead-tracker_{timestamp}.json"},
    )


@app.route("/export/csv")
def export_csv():
    """Export all batches as CSV (one row per batch with summary data)."""
    db = get_db()
    batches = db.execute("SELECT * FROM batches ORDER BY pitch_date DESC").fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "name", "style", "batch_size_gal", "honey_type", "yeast_strain",
        "og", "fg", "target_fg", "target_abv", "status", "pitch_date",
        "bottled_date", "notes", "recipe_source", "readings_count",
        "latest_gravity", "current_abv", "ingredients", "created_at",
    ])

    for b in batches:
        latest = db.execute(
            "SELECT gravity FROM gravity_readings WHERE batch_id = ? "
            "ORDER BY reading_date DESC LIMIT 1", (b["id"],)
        ).fetchone()
        reading_count = db.execute(
            "SELECT COUNT(*) FROM gravity_readings WHERE batch_id = ?", (b["id"],)
        ).fetchone()[0]
        ingredients = "; ".join(
            f"{i['name']} ({i['amount']})" if i["amount"] else i["name"]
            for i in db.execute(
                "SELECT name, amount FROM ingredients WHERE batch_id = ? ORDER BY category, name",
                (b["id"],)
            ).fetchall()
        )
        _, _, calc = batch_calc(db, b)
        current_abv = calc["state"]["abv_now"] if latest or b["fg"] else ""
        writer.writerow([
            b["id"], b["name"], b["style"], b["batch_size_gal"],
            b["honey_type"], b["yeast_strain"], b["og"] or "", b["fg"] or "",
            b["target_fg"] or "", b["target_abv"] or "", b["status"],
            b["pitch_date"], b["bottled_date"], b["notes"], b["recipe_source"],
            reading_count, latest["gravity"] if latest else "", current_abv,
            ingredients, b["created_at"],
        ])

    output.seek(0)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=mead-tracker_{timestamp}.csv"},
    )


@app.route("/batch/<int:batch_id>/export/json")
def export_batch_json(batch_id):
    """Export a single batch with full data as JSON."""
    db = get_db()
    data = _export_batch_dict(db, batch_id)
    if not data:
        flash("Batch not found.", "danger")
        return redirect(url_for("index"))
    safe_name = data["name"].replace(" ", "_").lower()
    return Response(
        json.dumps(data, indent=2, default=str),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename=mead_{safe_name}_{batch_id}.json"},
    )


# ── Notification Settings Routes ──────────────────────────────────

@app.route("/settings/notifications/email", methods=["POST"])
def save_email_notification_settings():
    """Save email notification settings."""
    db = get_db()
    config = {
        "host": request.form.get("smtp_host", ""),
        "port": int(request.form.get("smtp_port", 587)),
        "username": request.form.get("smtp_username", ""),
        "password": request.form.get("smtp_password", ""),
        "from_addr": request.form.get("from_addr", ""),
        "to_addr": request.form.get("to_addr", ""),
        "use_tls": request.form.get("use_tls") == "on",
    }
    enabled = 1 if request.form.get("enabled") == "on" else 0
    config_json = json.dumps(config)

    existing = db.execute(
        "SELECT id FROM notification_settings WHERE channel = 'email'"
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE notification_settings SET enabled = ?, config = ?, updated_at = datetime('now') WHERE channel = 'email'",
            (enabled, config_json),
        )
    else:
        db.execute(
            "INSERT INTO notification_settings (channel, enabled, config) VALUES ('email', ?, ?)",
            (enabled, config_json),
        )
    db.commit()
    flash("Email notification settings saved.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/notifications/ntfy", methods=["POST"])
def save_ntfy_notification_settings():
    """Save ntfy notification settings."""
    db = get_db()
    config = {
        "server_url": request.form.get("ntfy_server_url", "https://ntfy.sh"),
        "topic": request.form.get("ntfy_topic", ""),
        "priority": request.form.get("ntfy_priority", "default"),
    }
    enabled = 1 if request.form.get("enabled") == "on" else 0
    config_json = json.dumps(config)

    existing = db.execute(
        "SELECT id FROM notification_settings WHERE channel = 'ntfy'"
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE notification_settings SET enabled = ?, config = ?, updated_at = datetime('now') WHERE channel = 'ntfy'",
            (enabled, config_json),
        )
    else:
        db.execute(
            "INSERT INTO notification_settings (channel, enabled, config) VALUES ('ntfy', ?, ?)",
            (enabled, config_json),
        )
    db.commit()
    flash("ntfy notification settings saved.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/notifications/test/<channel>", methods=["POST"])
def test_notification(channel):
    """Send a test notification via the specified channel."""
    if channel not in ("email", "ntfy"):
        flash("Invalid notification channel.", "danger")
        return redirect(url_for("settings"))

    db = get_db()
    setting = db.execute(
        "SELECT * FROM notification_settings WHERE channel = ?", (channel,)
    ).fetchone()

    if not setting:
        flash(f"No settings configured for {channel}.", "danger")
        return redirect(url_for("settings"))

    config = json.loads(setting["config"]) if setting["config"] else {}
    subject = f"🧪 Test Notification — Mead Tracker"
    body = (
        f"This is a test notification from Mead Tracker.\n\n"
        f"Channel: {channel}\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"If you're reading this, your notification setup is working!"
    )

    try:
        if channel == "email":
            send_email(config, subject, body)
        elif channel == "ntfy":
            send_ntfy(config, subject, body)

        # Log the test notification
        db.execute(
            """INSERT INTO notification_log (batch_id, channel, event_type, subject, body, status)
               VALUES (NULL, ?, 'test', ?, ?, 'sent')""",
            (channel, subject, body),
        )
        db.commit()
        flash(f"Test {channel} notification sent successfully!", "success")
    except Exception as e:
        logger.error("Test notification failed for %s: %s", channel, e)
        db.execute(
            """INSERT INTO notification_log (batch_id, channel, event_type, subject, body, status, error)
               VALUES (NULL, ?, 'test', ?, ?, 'error', ?)""",
            (channel, subject, body, str(e)),
        )
        db.commit()
        flash(f"Test {channel} notification failed: {e}", "danger")

    return redirect(url_for("settings"))


@app.route("/batch/<int:batch_id>/notifications", methods=["POST"])
def save_batch_notification_rules(batch_id):
    """Update notification rules for a batch."""
    db = get_db()
    batch = db.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if not batch:
        flash("Batch not found.", "danger")
        return redirect(url_for("index"))

    event_types = ["nutrient_reminder", "gravity_reminder", "aging_milestone"]
    for event_type in event_types:
        enabled = 1 if request.form.get(f"notif_enabled_{event_type}") == "on" else 0
        lead_time_days = int(request.form.get(f"notif_lead_{event_type}", 0) or 0)
        interval_days = int(request.form.get(f"notif_interval_{event_type}", 0) or 0)

        existing = db.execute(
            "SELECT id FROM notification_rules WHERE batch_id = ? AND event_type = ?",
            (batch_id, event_type),
        ).fetchone()

        if existing:
            db.execute(
                """UPDATE notification_rules SET
                   enabled = ?, lead_time_days = ?, interval_days = ?
                   WHERE id = ?""",
                (enabled, lead_time_days, interval_days, existing["id"]),
            )
        else:
            db.execute(
                """INSERT INTO notification_rules
                   (batch_id, event_type, enabled, lead_time_days, interval_days)
                   VALUES (?, ?, ?, ?, ?)""",
                (batch_id, event_type, enabled, lead_time_days, interval_days),
            )

    db.commit()
    flash("Notification rules updated!", "success")
    return redirect(url_for("batch_detail", batch_id=batch_id))


@app.route("/api/notifications/log")
def api_notifications_log():
    """Return recent notification log entries as JSON."""
    db = get_db()
    log_entries = db.execute(
        "SELECT * FROM notification_log ORDER BY sent_at DESC LIMIT 50"
    ).fetchall()
    return jsonify([dict(e) for e in log_entries])


# ── AI Recipe Generation ──────────────────────────────────────────

DEFAULT_SYSTEM_PROMPT = """You are an expert mead maker and homebrew specialist with 15+ years of experience. You have deep knowledge of fermentation science, microbiology, chemistry, and mead production techniques.

When generating a recipe, always include ALL of the following sections:

1. **Recipe Name** — a creative, descriptive name
2. **Style** — the mead style (traditional, melomel, metheglin, sack mead, session mead, etc.)
3. **Batch Specifications** — batch size, target OG, target FG, estimated ABV
4. **Ingredients** — complete list with exact amounts:
   - Honey: type recommendation and amount in pounds
   - Water: volume and any water chemistry notes
   - Yeast: specific strain recommendation, pitch rate, and why it fits
   - Nutrients: specific TOSNA or SNA schedule with exact amounts (Fermaid O, Fermaid K, DAP, GoFerm)
   - Any additional ingredients (fruit, spices, etc.) with amounts and timing
5. **Process Instructions** — step-by-step:
   - Must preparation (mixing, temperature, aeration)
   - Primary fermentation details (temperature, duration, airlock)
   - Nutrient schedule (which days, what to add, degassing notes)
   - Secondary fermentation / racking timing
   - Aging recommendations (duration, vessel, temperature)
   - Packaging (bottling or kegging, back-sweetening if applicable)
6. **Timeline** — day-by-day or week-by-week summary of key milestones
7. **Notes** — any tips, warnings, or variations

Be specific with measurements. Use real yeast strain names (Lalvin EC-1118, K1-V1116, QA23, D47, 71B, etc.). Recommend a nutrient protocol that matches the yeast and gravity. Always consider the balance between sweetness, acidity, and alcohol. If ingredients are provided that don't pair well together, say so honestly."""


def get_ai_setting(db, key, default=""):
    """Read a single AI setting value."""
    row = db.execute("SELECT value FROM ai_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_ai_setting(db, key, value):
    """Write a single AI setting value (upsert)."""
    existing = db.execute("SELECT id FROM ai_settings WHERE key = ?", (key,)).fetchone()
    if existing:
        db.execute("UPDATE ai_settings SET value = ?, updated_at = datetime('now') WHERE key = ?", (value, key))
    else:
        db.execute("INSERT INTO ai_settings (key, value) VALUES (?, ?)", (key, value))


@app.route("/recipe/generate", methods=["GET"])
def recipe_generate_page():
    """Show the AI recipe generation form."""
    db = get_db()
    ai_config = {
        "api_base_url": get_ai_setting(db, "api_base_url"),
        "api_key": get_ai_setting(db, "api_key"),
        "model_name": get_ai_setting(db, "model_name"),
        "custom_system_prompt": get_ai_setting(db, "custom_system_prompt"),
    }
    return render_template("recipe_generator.html", ai_config=ai_config, recipe_result=None)


@app.route("/recipe/generate", methods=["POST"])
def recipe_generate():
    """Generate a recipe via the configured OpenAI-compatible API."""
    db = get_db()

    # Load AI config
    api_base_url = get_ai_setting(db, "api_base_url").strip()
    api_key = get_ai_setting(db, "api_key").strip()
    model_name = get_ai_setting(db, "model_name").strip()
    custom_prompt = get_ai_setting(db, "custom_system_prompt").strip()

    if not api_base_url or not api_key or not model_name:
        flash("AI recipe generation is not configured. Please set your API base URL, API key, and model in Settings.", "danger")
        return redirect(url_for("settings"))

    # Build the user prompt from form inputs
    batch_size = request.form.get("batch_size", "").strip()
    honey_amount = request.form.get("honey_amount", "").strip()
    target_abv = request.form.get("target_abv", "").strip()
    ingredients = request.form.get("ingredients", "").strip()
    extra_notes = request.form.get("extra_notes", "").strip()

    if not batch_size or not honey_amount:
        flash("Batch size and honey amount are required.", "danger")
        return redirect(url_for("recipe_generate_page"))

    user_prompt = f"Generate a mead recipe with these specifications:\n\n"
    user_prompt += f"- Batch size: {batch_size} gallons\n"
    user_prompt += f"- Honey amount: {honey_amount} lbs\n"
    if target_abv:
        user_prompt += f"- Target ABV: {target_abv}%\n"
    if ingredients:
        user_prompt += f"- Additional ingredients: {ingredients}\n"
    if extra_notes:
        user_prompt += f"- Additional notes: {extra_notes}\n"
    user_prompt += "\nPlease provide a complete, detailed recipe following the format in your instructions."

    system_prompt = custom_prompt if custom_prompt else DEFAULT_SYSTEM_PROMPT

    # Call the OpenAI-compatible API
    try:
        # Normalize base URL — strip trailing slash
        base = api_base_url.rstrip("/")
        # If user provided a full endpoint, use it; otherwise append /chat/completions
        if base.endswith("/chat/completions"):
            endpoint = base
        elif base.endswith("/v1"):
            endpoint = f"{base}/chat/completions"
        else:
            endpoint = f"{base}/v1/chat/completions"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 4096,
        }

        resp = http_requests.post(endpoint, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        result = resp.json()

        recipe_text = result["choices"][0]["message"]["content"]

        # Store the config for the template
        ai_config = {
            "api_base_url": api_base_url,
            "api_key": api_key,
            "model_name": model_name,
            "custom_system_prompt": custom_prompt,
        }

        # Pass back the form values so the user doesn't lose them
        form_data = {
            "batch_size": batch_size,
            "honey_amount": honey_amount,
            "target_abv": target_abv,
            "ingredients": ingredients,
            "extra_notes": extra_notes,
        }

        flash("Recipe generated!", "success")
        return render_template("recipe_generator.html",
                               ai_config=ai_config,
                               recipe_result=recipe_text,
                               form_data=form_data)

    except http_requests.exceptions.Timeout:
        flash("Recipe generation timed out. The model may be too slow or the API is unreachable.", "danger")
    except http_requests.exceptions.HTTPError as e:
        error_detail = ""
        try:
            error_detail = e.response.json().get("error", {}).get("message", str(e))
        except Exception:
            error_detail = str(e)
        flash(f"API error: {error_detail}", "danger")
    except Exception as e:
        logger.error("Recipe generation failed: %s", e)
        flash(f"Recipe generation failed: {e}", "danger")

    return redirect(url_for("recipe_generate_page"))


@app.route("/settings/ai", methods=["POST"])
def save_ai_settings():
    """Save AI recipe generation settings."""
    db = get_db()
    set_ai_setting(db, "api_base_url", request.form.get("ai_api_base_url", "").strip())
    set_ai_setting(db, "api_key", request.form.get("ai_api_key", "").strip())
    set_ai_setting(db, "model_name", request.form.get("ai_model_name", "").strip())
    set_ai_setting(db, "custom_system_prompt", request.form.get("ai_custom_system_prompt", "").strip())
    db.commit()
    flash("AI settings saved.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/ai/test", methods=["POST"])
def test_ai_connection():
    """Test the AI API connection by sending a simple request."""
    db = get_db()
    api_base_url = get_ai_setting(db, "api_base_url").strip()
    api_key = get_ai_setting(db, "api_key").strip()
    model_name = get_ai_setting(db, "model_name").strip()

    if not api_base_url or not api_key or not model_name:
        flash("Please fill in all AI settings before testing.", "danger")
        return redirect(url_for("settings"))

    try:
        base = api_base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            endpoint = base
        elif base.endswith("/v1"):
            endpoint = f"{base}/chat/completions"
        else:
            endpoint = f"{base}/v1/chat/completions"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model_name,
            "messages": [
                {"role": "user", "content": "Say 'Mead Tracker AI connection successful!' in exactly those words."},
            ],
            "max_tokens": 50,
        }

        resp = http_requests.post(endpoint, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        reply = result["choices"][0]["message"]["content"]
        flash(f"AI connection successful! Model replied: {reply}", "success")
    except Exception as e:
        flash(f"AI connection test failed: {e}", "danger")

    return redirect(url_for("settings"))


# ── API (for charts / future mobile app) ─────────────────────────
#
# JSON API. Auth: Authorization: Bearer $MEAD_API_TOKEN.
# All write endpoints accept JSON bodies and return the created row plus the
# batch's recomputed state, so a client can report the new ABV immediately.

class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@app.errorhandler(ApiError)
def _api_error(e):
    return jsonify({"error": str(e)}), e.status


def _json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("expected a JSON object body")
    return data


def _api_batch(db, batch_id):
    b = db.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if not b:
        raise ApiError("batch not found", 404)
    return b


def _batch_summary(db, b):
    _, _, calc = batch_calc(db, b)
    st = calc["state"]
    return {**dict(b), "state": {
        k: st[k] for k in ("current_gravity", "abv_now", "potential_abv", "volume_gal",
                           "carry_abv", "equivalent_og", "stabilized", "diluted", "segments")
    }}


def _date_or_today(v):
    v = (v or date.today().isoformat()).strip()
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        raise ApiError("date must be YYYY-MM-DD")
    return v


def _gravity(v, name="gravity", required=True):
    if v in (None, ""):
        if required:
            raise ApiError(f"{name} is required")
        return None
    try:
        g_ = float(v)
    except (TypeError, ValueError):
        raise ApiError(f"{name} must be a number")
    if not 0.900 <= g_ <= 1.250:
        raise ApiError(f"{name} out of range (0.900-1.250)")
    return g_


BATCH_FIELDS = {
    "name": str, "style": str, "batch_size_gal": float, "initial_volume_gal": float,
    "honey_type": str, "yeast_strain": str, "og": float, "fg": float,
    "target_fg": float, "target_abv": float, "status": str, "pitch_date": str,
    "bottled_date": str, "notes": str, "recipe_source": str,
}
STATUSES = ("planning", "active", "aging", "bottled", "drinking", "archived")


def _clean_batch_fields(data):
    out = {}
    for k, typ in BATCH_FIELDS.items():
        if k not in data:
            continue
        v = data[k]
        if v in (None, "") and typ is float:
            out[k] = None
            continue
        try:
            out[k] = typ(v) if v is not None else None
        except (TypeError, ValueError):
            raise ApiError(f"{k} must be {typ.__name__}")
    if "status" in out and out["status"] not in STATUSES:
        raise ApiError(f"status must be one of {', '.join(STATUSES)}")
    for k in ("og", "fg", "target_fg"):
        if out.get(k) is not None:
            _gravity(out[k], k)
    for k in ("pitch_date", "bottled_date"):
        if out.get(k):
            _date_or_today(out[k])
    return out


@app.route("/api/event-types")
def api_event_types():
    return jsonify({k: {"label": v[0], "affects_math": v[1]} for k, v in EVENT_TYPES.items()})


@app.route("/api/batches", methods=["GET"])
def api_batches():
    db = get_db()
    q, params = "SELECT * FROM batches", []
    clauses = []
    if request.args.get("status"):
        clauses.append("status = ?")
        params.append(request.args["status"])
    if request.args.get("q"):
        clauses.append("name LIKE ?")
        params.append(f"%{request.args['q']}%")
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY pitch_date DESC"
    return jsonify([_batch_summary(db, b) for b in db.execute(q, params).fetchall()])


@app.route("/api/batches", methods=["POST"])
def api_batch_create():
    db = get_db()
    data = _clean_batch_fields(_json_body())
    if not data.get("name"):
        raise ApiError("name is required")
    data.setdefault("status", "planning")
    data.setdefault("batch_size_gal", 1.0)
    cols = ", ".join(data)
    cur = db.execute(
        f"INSERT INTO batches ({cols}) VALUES ({', '.join('?' * len(data))})",
        list(data.values()),
    )
    db.commit()
    return jsonify(_batch_summary(db, _api_batch(db, cur.lastrowid))), 201


@app.route("/api/batch/<int:batch_id>", methods=["GET"])
def api_batch_get(batch_id):
    db = get_db()
    b = _api_batch(db, batch_id)
    data = _export_batch_dict(db, batch_id)
    _, _, calc = batch_calc(db, b)
    for r_ in data["gravity_readings"]:
        r_["abv"] = calc["readings"].get(r_["id"], {}).get("abv")
    for e_ in data["process_events"]:
        e_.update(calc["events"].get(e_["id"], {}))
    data["state"] = calc["state"]
    return jsonify(data)


@app.route("/api/batch/<int:batch_id>", methods=["PATCH"])
def api_batch_update(batch_id):
    db = get_db()
    _api_batch(db, batch_id)
    data = _clean_batch_fields(_json_body())
    if not data:
        raise ApiError("no updatable fields supplied")
    sets = ", ".join(f"{k} = ?" for k in data)
    db.execute(
        f"UPDATE batches SET {sets}, updated_at = datetime('now') WHERE id = ?",
        [*data.values(), batch_id],
    )
    db.commit()
    return jsonify(_batch_summary(db, _api_batch(db, batch_id)))


@app.route("/api/batch/<int:batch_id>", methods=["DELETE"])
def api_batch_delete(batch_id):
    db = get_db()
    _api_batch(db, batch_id)
    if request.args.get("confirm") != "yes":
        raise ApiError("deleting a batch removes all its data; repeat with ?confirm=yes")
    db.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
    db.commit()
    return jsonify({"deleted": batch_id})


@app.route("/api/batch/<int:batch_id>/readings", methods=["GET"])
def api_readings(batch_id):
    db = get_db()
    b = _api_batch(db, batch_id)
    readings, _, calc = batch_calc(db, b)
    return jsonify([
        {**dict(r_), "abv": calc["readings"].get(r_["id"], {}).get("abv")} for r_ in readings
    ])


@app.route("/api/batch/<int:batch_id>/readings", methods=["POST"])
def api_reading_create(batch_id):
    db = get_db()
    _api_batch(db, batch_id)
    data = _json_body()
    gravity = _gravity(data.get("gravity"))
    when = _date_or_today(data.get("reading_date") or data.get("date"))
    temp = data.get("temperature_f")
    try:
        temp = float(temp) if temp not in (None, "") else None
    except (TypeError, ValueError):
        raise ApiError("temperature_f must be a number")
    day = data.get("day_number")
    day = int(day) if day not in (None, "") else day_number_for(db, batch_id, when)
    cur = db.execute(
        """INSERT INTO gravity_readings (batch_id, reading_date, day_number, gravity, temperature_f, notes)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (batch_id, when, day, gravity, temp, str(data.get("notes") or "")),
    )
    db.commit()
    return _created(db, batch_id, "gravity_readings", cur.lastrowid)


@app.route("/api/batch/<int:batch_id>/nutrients", methods=["POST"])
def api_nutrient_create(batch_id):
    db = get_db()
    _api_batch(db, batch_id)
    data = _json_body()
    if not data.get("nutrient_type") or not data.get("amount"):
        raise ApiError("nutrient_type and amount are required")
    when = _date_or_today(data.get("addition_date") or data.get("date"))
    day = data.get("day_number")
    day = int(day) if day not in (None, "") else day_number_for(db, batch_id, when)
    cur = db.execute(
        """INSERT INTO nutrient_additions (batch_id, addition_date, day_number, nutrient_type, amount, notes)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (batch_id, when, day, str(data["nutrient_type"]), str(data["amount"]), str(data.get("notes") or "")),
    )
    db.commit()
    return _created(db, batch_id, "nutrient_additions", cur.lastrowid)


@app.route("/api/batch/<int:batch_id>/tastings", methods=["POST"])
def api_tasting_create(batch_id):
    db = get_db()
    _api_batch(db, batch_id)
    data = _json_body()
    rating = data.get("overall_rating")
    if rating not in (None, ""):
        try:
            rating = int(rating)
        except (TypeError, ValueError):
            raise ApiError("overall_rating must be an integer 1-10")
        if not 1 <= rating <= 10:
            raise ApiError("overall_rating must be 1-10")
    else:
        rating = None
    cur = db.execute(
        """INSERT INTO tasting_notes
           (batch_id, tasting_date, aroma, flavor, body, sweetness, overall_rating, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (batch_id, _date_or_today(data.get("tasting_date") or data.get("date")),
         *(str(data.get(k) or "") for k in ("aroma", "flavor", "body", "sweetness")),
         rating, str(data.get("notes") or "")),
    )
    db.commit()
    return _created(db, batch_id, "tasting_notes", cur.lastrowid)


@app.route("/api/batch/<int:batch_id>/ingredients", methods=["POST"])
def api_ingredient_create(batch_id):
    db = get_db()
    _api_batch(db, batch_id)
    data = _json_body()
    if not data.get("name"):
        raise ApiError("name is required")
    cat = data.get("category") or "other"
    if cat not in ("honey", "fruit", "spice", "nutrient", "yeast", "other"):
        raise ApiError("category must be honey, fruit, spice, nutrient, yeast, or other")
    cur = db.execute(
        "INSERT INTO ingredients (batch_id, category, name, amount, notes) VALUES (?, ?, ?, ?, ?)",
        (batch_id, cat, str(data["name"]), str(data.get("amount") or ""), str(data.get("notes") or "")),
    )
    db.commit()
    return _created(db, batch_id, "ingredients", cur.lastrowid)


@app.route("/api/batch/<int:batch_id>/events", methods=["GET"])
def api_events(batch_id):
    db = get_db()
    b = _api_batch(db, batch_id)
    _, events, calc = batch_calc(db, b)
    return jsonify([{**dict(e_), **calc["events"].get(e_["id"], {})} for e_ in events])


@app.route("/api/batch/<int:batch_id>/events", methods=["POST"])
def api_event_create(batch_id):
    db = get_db()
    _api_batch(db, batch_id)
    try:
        new_id = insert_event(db, batch_id, _json_body())
    except ValueError as e:
        raise ApiError(str(e))
    db.commit()
    return _created(db, batch_id, "process_events", new_id)


def _created(db, batch_id, table, row_id):
    row = dict(db.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())
    b = _api_batch(db, batch_id)
    _, _, calc = batch_calc(db, b)
    if table == "gravity_readings":
        row["abv"] = calc["readings"].get(row_id, {}).get("abv")
    elif table == "process_events":
        row.update(calc["events"].get(row_id, {}))
    return jsonify({"created": row, "batch": _batch_summary(db, b)}), 201


DELETABLE = {
    "readings": "gravity_readings",
    "events": "process_events",
    "nutrients": "nutrient_additions",
    "tastings": "tasting_notes",
    "ingredients": "ingredients",
}


@app.route("/api/<kind>/<int:row_id>", methods=["DELETE"])
def api_row_delete(kind, row_id):
    table = DELETABLE.get(kind)
    if not table:
        raise ApiError("not found", 404)
    db = get_db()
    row = db.execute(f"SELECT batch_id FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if not row:
        raise ApiError("not found", 404)
    db.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    db.commit()
    return jsonify({"deleted": row_id, "batch": _batch_summary(db, _api_batch(db, row["batch_id"]))})


# ── Background Scheduler ──────────────────────────────────────────

# Started explicitly by the entry point, never on import, so tests and any
# reloader child don't spin up a second copy that double-sends reminders.
scheduler = None


def start_scheduler():
    global scheduler
    if scheduler is not None:
        return scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        check_and_send_notifications,
        'interval',
        hours=1,
        args=[app, DB_PATH],
        id='check_notifications',
        replace_existing=True,
    )
    scheduler.start()
    return scheduler


# ── Entry point ───────────────────────────────────────────────────

init_db()

if __name__ == "__main__":
    if not AUTH_DISABLED and not (AUTH_USER and AUTH_PASS_HASH):
        logger.warning("MEAD_USER / MEAD_PASSWORD_HASH not set: every UI request will get 401.")
    start_scheduler()
    host = os.environ.get("MEAD_HOST", "127.0.0.1")
    port = int(os.environ.get("MEAD_PORT", "8789"))
    if os.environ.get("MEAD_DEV") == "1":
        app.run(host=host, port=port, debug=True, use_reloader=False)
    else:
        from waitress import serve
        serve(app, host=host, port=port, threads=8)
