"""
Mead Tracker — Notification System
Supports email (SMTP) and push (ntfy) notifications with per-batch rules.
"""

import json
import logging
import smtplib
import sqlite3
import ssl
from datetime import datetime, date, timedelta

import requests

logger = logging.getLogger(__name__)


# ── Email ──────────────────────────────────────────────────────────

def send_email(config, subject, body):
    """
    Send an email via SMTP.
    config: dict with keys: host, port, username, password, from_addr, to_addr, use_tls (bool)
    """
    host = config.get("host", "")
    port = int(config.get("port", 587))
    username = config.get("username", "")
    password = config.get("password", "")
    from_addr = config.get("from_addr", "")
    to_addr = config.get("to_addr", "")
    use_tls = config.get("use_tls", True)

    if not all([host, port, username, password, from_addr, to_addr]):
        raise ValueError("Email config is incomplete — all fields are required.")

    message = (
        f"From: {from_addr}\r\n"
        f"To: {to_addr}\r\n"
        f"Subject: {subject}\r\n"
        f"MIME-Version: 1.0\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"{body}"
    )

    if use_tls:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(username, password)
            server.sendmail(from_addr, to_addr, message.encode("utf-8"))
    else:
        with smtplib.SMTP_SSL(host, port, timeout=30) as server:
            server.login(username, password)
            server.sendmail(from_addr, to_addr, message.encode("utf-8"))


# ── ntfy ───────────────────────────────────────────────────────────

def send_ntfy(config, title, body):
    """
    Send a push notification via ntfy.
    config: dict with keys: server_url, topic, priority (optional)
    """
    server_url = config.get("server_url", "").rstrip("/")
    topic = config.get("topic", "")
    priority = config.get("priority", "default")

    if not server_url or not topic:
        raise ValueError("ntfy config is incomplete — server_url and topic are required.")

    url = f"{server_url}/{topic}"
    headers = {}
    if priority:
        priority_map = {"low": 1, "default": 3, "high": 4, "urgent": 5}
        p = priority_map.get(priority.lower(), 3)
        headers["Priority"] = str(p)

    resp = requests.post(
        url,
        data=body.encode("utf-8"),
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()


# ── Notification checking logic ────────────────────────────────────

def _log_notification(db, batch_id, channel, event_type, subject, body,
                      status="sent", error=None):
    """Insert a row into notification_log."""
    db.execute(
        """INSERT INTO notification_log
           (batch_id, channel, event_type, subject, body, status, error)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (batch_id, channel, event_type, subject, body, status, error),
    )
    db.commit()


def check_and_send_notifications(app, db_path):
    """
    Main scheduled function: loads enabled settings + rules and sends
    any due notifications. Uses a separate DB connection (not g.db).
    """
    with app.app_context():
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")

        try:
            _check_notifications_internal(db)
        except Exception as exc:
            logger.error("Notification check failed: %s", exc)
        finally:
            db.close()


def _check_notifications_internal(db):
    """Inner check — all DB access via the passed connection."""
    today = date.today()

    # Load enabled notification settings
    settings_rows = db.execute(
        "SELECT * FROM notification_settings WHERE enabled = 1"
    ).fetchall()
    if not settings_rows:
        return  # no channels configured

    # Load enabled notification rules
    rules = db.execute(
        "SELECT * FROM notification_rules WHERE enabled = 1"
    ).fetchall()
    if not rules:
        return  # no rules defined

    for rule in rules:
        batch_id = rule["batch_id"]
        event_type = rule["event_type"]

        # Fetch batch info
        batch = db.execute(
            "SELECT * FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if not batch:
            continue

        batch_name = batch["name"]
        status = batch["status"]
        pitch_date_str = batch["pitch_date"]
        bottled_date_str = batch["bottled_date"]

        subject = None
        body = None

        # ── Nutrient Reminder ──────────────────────────────────
        if event_type == "nutrient_reminder":
            # Only for active batches with a pitch_date
            if status not in ("active",):
                continue
            if not pitch_date_str:
                continue

            try:
                pitch = datetime.strptime(pitch_date_str, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue

            nutrient_days = [1, 2, 3, 4]
            for nday in nutrient_days:
                target_date = pitch + timedelta(days=nday)
                if target_date != today:
                    continue

                # Check if a nutrient addition already exists for this day
                existing = db.execute(
                    "SELECT COUNT(*) FROM nutrient_additions "
                    "WHERE batch_id = ? AND day_number = ?",
                    (batch_id, nday),
                ).fetchone()[0]

                if existing > 0:
                    continue  # already logged, no reminder needed

                # Check we haven't already notified today for this rule+event
                if _already_notified_today(db, rule["id"], "nutrient_reminder"):
                    continue

                subject = f"🍯 Nutrient Reminder: {batch_name}"
                body = (
                    f"Time to add nutrients for '{batch_name}'!\n\n"
                    f"Batch: {batch_name}\n"
                    f"Day {nday} ({target_date})\n"
                    f"Status: {status}\n\n"
                    f"Don't forget to degas before adding nutrients!"
                )
                _send_via_all_channels(db, settings_rows, batch_id,
                                       "nutrient_reminder", subject, body)
                _update_last_notified(db, rule["id"])

        # ── Gravity Reminder ────────────────────────────────────
        elif event_type == "gravity_reminder":
            if status not in ("active", "aging"):
                continue

            interval = rule["interval_days"] if rule["interval_days"] else 7

            # Find the most recent gravity reading
            last_reading = db.execute(
                "SELECT reading_date FROM gravity_readings "
                "WHERE batch_id = ? ORDER BY reading_date DESC LIMIT 1",
                (batch_id,),
            ).fetchone()

            if last_reading:
                try:
                    last_date = datetime.strptime(
                        last_reading["reading_date"], "%Y-%m-%d"
                    ).date()
                except (ValueError, TypeError):
                    continue
                days_since = (today - last_date).days
            else:
                # No readings yet — use pitch_date as reference
                if pitch_date_str:
                    try:
                        last_date = datetime.strptime(
                            pitch_date_str, "%Y-%m-%d"
                        ).date()
                        days_since = (today - last_date).days
                    except (ValueError, TypeError):
                        continue
                else:
                    continue

            if days_since >= interval:
                if _already_notified_today(db, rule["id"], "gravity_reminder"):
                    continue

                subject = f"🍯 Gravity Reading Reminder: {batch_name}"
                body = (
                    f"It's time to take a gravity reading for '{batch_name}'!\n\n"
                    f"Days since last reading: {days_since}\n"
                    f"Interval: {interval} days\n"
                    f"Status: {status}\n\n"
                    f"Track fermentation progress!"
                )
                _send_via_all_channels(db, settings_rows, batch_id,
                                       "gravity_reminder", subject, body)
                _update_last_notified(db, rule["id"])

        # ── Aging Milestone ─────────────────────────────────────
        elif event_type == "aging_milestone":
            if status not in ("aging", "bottled"):
                continue

            # Determine the reference date (bottled_date preferred for aging/bottled)
            ref_date_str = bottled_date_str or pitch_date_str
            if not ref_date_str:
                continue

            try:
                ref_date = datetime.strptime(ref_date_str, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue

            days_elapsed = (today - ref_date).days
            milestones = [30, 60, 90, 180, 365, 730]  # standard aging checkpoints
            lead = rule["lead_time_days"] if rule["lead_time_days"] else 0

            for milestone in milestones:
                # Notify if within lead_time_days of a milestone
                if abs(days_elapsed - milestone) <= lead:
                    if _already_notified_today(db, rule["id"],
                                               "aging_milestone"):
                        continue

                    subject = f"🍯 Aging Milestone: {batch_name}"
                    body = (
                        f"'{batch_name}' has been aging for "
                        f"{days_elapsed} days!\n\n"
                        f"Milestone: {milestone} days of aging\n"
                        f"Started: {ref_date_str}\n"
                        f"Status: {status}\n\n"
                        f"Time for a tasting and evaluation!"
                    )
                    _send_via_all_channels(db, settings_rows, batch_id,
                                           "aging_milestone", subject, body)
                    _update_last_notified(db, rule["id"])
                    break  # only send once per aging milestone


def _already_notified_today(db, rule_id, event_type):
    """Check if this rule has already fired today.

    Keyed on the rule (and therefore the batch). The old version matched on
    event_type alone, so one batch's reminder silently suppressed the same
    reminder for every other batch that day. sent_at is stored in UTC by
    SQLite's datetime('now'), so compare against the UTC date.
    """
    rule = db.execute(
        "SELECT batch_id FROM notification_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    batch_id = rule["batch_id"] if rule else None
    today_str = datetime.utcnow().date().isoformat()
    count = db.execute(
        "SELECT COUNT(*) FROM notification_log "
        "WHERE event_type = ? AND batch_id IS ? AND status = 'sent' AND sent_at LIKE ?",
        (event_type, batch_id, f"{today_str}%"),
    ).fetchone()[0]
    return count > 0


def _update_last_notified(db, rule_id):
    """Update the last_notified timestamp for a rule."""
    db.execute(
        "UPDATE notification_rules SET last_notified = datetime('now') WHERE id = ?",
        (rule_id,),
    )
    db.commit()


def _send_via_all_channels(db, settings_rows, batch_id, event_type,
                           subject, body):
    """Send a notification through every enabled channel."""
    for setting in settings_rows:
        channel = setting["channel"]
        config = json.loads(setting["config"]) if setting["config"] else {}

        try:
            if channel == "email":
                send_email(config, subject, body)
            elif channel == "ntfy":
                send_ntfy(config, subject, body)

            _log_notification(db, batch_id, channel, event_type,
                              subject, body, status="sent")
            logger.info("Notification sent via %s for batch %s: %s",
                        channel, batch_id, subject)
        except Exception as exc:
            logger.error("Failed to send %s notification for batch %s: %s",
                         channel, batch_id, exc)
            _log_notification(db, batch_id, channel, event_type,
                              subject, body, status="error", error=str(exc))
