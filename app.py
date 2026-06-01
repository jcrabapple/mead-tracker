"""Mead Batch Tracker — lightweight Flask app for tracking homebrew mead batches."""

import os
import io
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
from apscheduler.schedulers.background import BackgroundScheduler

from notifications import check_and_send_notifications, send_email, send_ntfy
import requests as http_requests

app = Flask(__name__)
app.secret_key = os.urandom(24)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mead.db")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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
        -- AI recipe generation settings
        CREATE TABLE IF NOT EXISTS ai_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            value TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        );
    """)
    db.close()


# ── Utility ───────────────────────────────────────────────────────

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

    # Attach latest gravity to each batch
    batch_data = []
    for b in batches:
        latest = db.execute(
            "SELECT gravity, reading_date FROM gravity_readings "
            "WHERE batch_id = ? ORDER BY reading_date DESC LIMIT 1",
            (b["id"],)
        ).fetchone()
        abv = calc_abv(b["og"], latest["gravity"]) if latest and b["og"] else None
        batch_data.append({**dict(b), "latest_gravity": latest, "current_abv": abv})

    return render_template("index.html", batches=batch_data)


# ── Batch CRUD ────────────────────────────────────────────────────

@app.route("/batch/new", methods=["GET", "POST"])
def batch_new():
    if request.method == "POST":
        db = get_db()
        db.execute(
            """INSERT INTO batches
               (name, style, batch_size_gal, honey_type, yeast_strain,
                og, target_fg, target_abv, status, pitch_date, notes, recipe_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request.form["name"],
                request.form.get("style", ""),
                float(request.form.get("batch_size_gal", 1.0) or 1.0),
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

    readings = db.execute(
        "SELECT * FROM gravity_readings WHERE batch_id = ? ORDER BY reading_date",
        (batch_id,),
    ).fetchall()

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

    # Build chart data
    chart_labels = [r["reading_date"] for r in readings]
    chart_gravities = [r["gravity"] for r in readings]
    chart_abvs = [
        calc_abv(batch["og"], r["gravity"]) if batch["og"] else 0
        for r in readings
    ]

    return render_template(
        "batch.html",
        batch=batch,
        readings=readings,
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
               name=?, style=?, batch_size_gal=?, honey_type=?, yeast_strain=?,
               og=?, fg=?, target_fg=?, target_abv=?, status=?,
               pitch_date=?, bottled_date=?, notes=?, recipe_source=?,
               updated_at=datetime('now')
               WHERE id=?""",
            (
                request.form["name"],
                request.form.get("style", ""),
                float(request.form.get("batch_size_gal", 1.0) or 1.0),
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

    # Replace the database
    shutil.move(tmp_path, DB_PATH)
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
        current_abv = calc_abv(b["og"], latest["gravity"]) if latest and b["og"] else ""
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

@app.route("/api/batches")
def api_batches():
    db = get_db()
    batches = db.execute("SELECT * FROM batches ORDER BY pitch_date DESC").fetchall()
    return jsonify([dict(b) for b in batches])


@app.route("/api/batch/<int:batch_id>/readings")
def api_readings(batch_id):
    db = get_db()
    readings = db.execute(
        "SELECT * FROM gravity_readings WHERE batch_id = ? ORDER BY reading_date",
        (batch_id,),
    ).fetchall()
    return jsonify([dict(r) for r in readings])


# ── Background Scheduler ──────────────────────────────────────────

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


# ── Entry point ───────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=8789, debug=True)
