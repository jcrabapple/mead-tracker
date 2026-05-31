"""Mead Batch Tracker — lightweight Flask app for tracking homebrew mead batches."""

import os
import io
import csv
import json
import shutil
import sqlite3
from datetime import datetime, date
from contextlib import contextmanager

from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, flash, g, send_file, Response
)
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.urandom(24)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mead.db")


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
    return render_template("settings.html",
                           batch_count=batch_count,
                           reading_count=reading_count)


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


# ── Entry point ───────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=8789, debug=True)
